"""
VaaniBank AI — Role & Branch Guards (Phase 3)
PSBs Hackathon 2026 | Team Vectora

This module is the single source of truth for all access-control logic.
Every router imports from here — never duplicates guard logic inline.

Guard hierarchy
───────────────
  StaffRole enum values (ascending privilege):
    teller < supervisor < manager < admin

  Rules that never change:
    • teller      → own session only, no management
    • supervisor  → own session + limited oversight
    • manager     → own branch (staff + analytics + sessions)
    • admin       → all branches, system-wide

Exported guards (FastAPI Depends-compatible):
  require_manager_or_admin   → manager or admin
  require_admin              → admin only
  require_branch_access      → manager/admin + branch_id scope check
  require_staff_access       → manager/admin + target staff branch check
  require_session_access     → any staff but manager/admin can cross-lookup
  BranchGuard                → callable class, resolves branch from path param

Exported helpers (used inside endpoint bodies):
  assert_own_branch(current, branch_id)         → raises 403 if manager sees foreign branch
  assert_role_can_create(current, new_role)     → raises 403 if manager tries to create manager/admin
  assert_role_can_set(current, new_role)        → same for update operations
  assert_not_self_deactivate(current, target)   → raises 400
  check_cross_branch_session(current, session)  → 403 if manager accesses other branch session
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Branch, Session, StaffMember, StaffRole

logger = logging.getLogger("vaanibank.guards")


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

_MANAGER_CREATABLE_ROLES = {StaffRole.teller.value, StaffRole.supervisor.value}
_ALL_ROLES = {r.value for r in StaffRole}

# Roles that have management capability (branch-scoped or global)
_MANAGEMENT_ROLES = {StaffRole.manager.value, StaffRole.admin.value}


# ══════════════════════════════════════════════════════════════════════════════
# LOW-LEVEL ASSERTION HELPERS
# (synchronous — called inside endpoint body, not as Depends)
# ══════════════════════════════════════════════════════════════════════════════

def assert_own_branch(current: StaffMember, branch_id: int) -> None:
    """
    Raise HTTP 403 if a manager tries to access a different branch.
    Admin always passes through.
    """
    if current.role == StaffRole.admin.value:
        return
    if current.branch_id != branch_id:
        logger.warning(
            "GUARD | cross-branch denied | staff=%s role=%s own=%s target=%s",
            current.staff_id, current.role, current.branch_id, branch_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Cross-branch access denied. "
                f"Your branch: {current.branch_id}. Requested: {branch_id}."
            ),
        )


def assert_role_can_create(current: StaffMember, new_role: str) -> None:
    """
    Manager can only create teller / supervisor.
    Admin can create any role.
    Raises HTTP 403 otherwise.
    """
    if current.role == StaffRole.admin.value:
        if new_role not in _ALL_ROLES:
            raise HTTPException(status_code=422, detail=f"Unknown role: {new_role}")
        return

    if current.role == StaffRole.manager.value:
        if new_role not in _MANAGER_CREATABLE_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Manager can only create teller or supervisor. "
                    f"Got '{new_role}'. To create manager/admin, contact system administrator."
                ),
            )
        return

    # Any other role (teller, supervisor) cannot create staff at all
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Role '{current.role}' is not permitted to create staff.",
    )


def assert_role_can_set(current: StaffMember, new_role: str) -> None:
    """
    Same rules as assert_role_can_create but for PATCH updates.
    Prevents privilege escalation via role change.
    """
    assert_role_can_create(current, new_role)


def assert_not_self_deactivate(current: StaffMember, target: StaffMember) -> None:
    """
    Prevent staff from deactivating themselves
    (would immediately lock them out).
    """
    if current.id == target.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account.",
        )


def assert_not_self_role_change(current: StaffMember, target: StaffMember) -> None:
    """
    Prevent staff from changing their own role
    (could be used for privilege escalation).
    """
    if current.id == target.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot change your own role.",
        )


def check_cross_branch_session(current: StaffMember, session: Session) -> None:
    """
    Manager cannot view sessions from other branches.
    Admin can view all.
    Teller/supervisor can only view their own sessions.
    """
    if current.role == StaffRole.admin.value:
        return

    if current.role == StaffRole.manager.value:
        if session.branch_id != current.branch_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Session belongs to a different branch. "
                    f"Manager access is limited to branch {current.branch_id}."
                ),
            )
        return

    # Teller / supervisor — must own the session
    if session.staff_id != current.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only access sessions assigned to you.",
        )


def check_admin_only(current: StaffMember, feature: str = "This feature") -> None:
    """Raise 403 if current user is not admin."""
    if current.role != StaffRole.admin.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{feature} is restricted to admin only.",
        )


def check_management_role(current: StaffMember) -> None:
    """Raise 403 if current user is not manager or admin."""
    if current.role not in _MANAGEMENT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Role '{current.role}' does not have management access. "
                f"Required: manager or admin."
            ),
        )


# ══════════════════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCY GUARDS
# (async — used as Depends(...) in route signatures)
# ══════════════════════════════════════════════════════════════════════════════

from core.security import get_current_staff  # noqa: E402  (after models import)


async def require_manager_or_admin(
    current_staff: StaffMember = Depends(get_current_staff),
) -> StaffMember:
    """
    FastAPI dependency: gate endpoint to manager or admin.

    Usage:
        @router.get("/staff/list")
        async def list_staff(staff = Depends(require_manager_or_admin)):
    """
    check_management_role(current_staff)
    return current_staff


async def require_admin(
    current_staff: StaffMember = Depends(get_current_staff),
) -> StaffMember:
    """
    FastAPI dependency: gate endpoint to admin only.

    Usage:
        @router.get("/admin/branches")
        async def list_branches(staff = Depends(require_admin)):
    """
    check_admin_only(current_staff)
    return current_staff


class BranchGuard:
    """
    Callable dependency that:
      1. Resolves branch from DB by ID (path param `branch_id`)
      2. Enforces that manager can only access own branch
      3. Returns the Branch ORM object

    Usage:
        @router.get("/analytics/branch/{branch_id}/range")
        async def analytics(
            branch: Branch = Depends(BranchGuard()),
            current_staff = Depends(require_manager_or_admin),
        ):
    """

    def __init__(self, path_param: str = "branch_id"):
        self.path_param = path_param

    async def __call__(
        self,
        branch_id: int = Path(...),
        current_staff: StaffMember = Depends(require_manager_or_admin),
        db: AsyncSession = Depends(get_db),
    ) -> Branch:
        # Scope check
        assert_own_branch(current_staff, branch_id)

        result = await db.execute(select(Branch).where(Branch.id == branch_id))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Branch {branch_id} not found.",
            )
        return branch


async def require_branch_access(
    branch_id: int = Path(...),
    current_staff: StaffMember = Depends(require_manager_or_admin),
    db: AsyncSession = Depends(get_db),
) -> tuple[Branch, StaffMember]:
    """
    Dependency that returns (branch, current_staff) after validating scope.
    Useful when you need both the branch object and the staff object.

    Usage:
        @router.get("/analytics/branch/{branch_id}/range")
        async def analytics(ctx = Depends(require_branch_access)):
            branch, staff = ctx
    """
    assert_own_branch(current_staff, branch_id)
    result = await db.execute(select(Branch).where(Branch.id == branch_id))
    branch = result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )
    return branch, current_staff


async def require_staff_target_access(
    staff_db_id: int = Path(...),
    current_staff: StaffMember = Depends(require_manager_or_admin),
    db: AsyncSession = Depends(get_db),
) -> tuple[StaffMember, Branch, StaffMember]:
    """
    Dependency that:
      - Loads target staff by DB id
      - Verifies current_staff may access target's branch
      - Returns (target_staff, target_branch, current_staff)

    Usage:
        @router.patch("/staff/{staff_db_id}/deactivate")
        async def deactivate(ctx = Depends(require_staff_target_access)):
            target, branch, actor = ctx
    """
    result = await db.execute(
        select(StaffMember).where(StaffMember.id == staff_db_id)
    )
    target: StaffMember | None = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Staff member {staff_db_id} not found.",
        )

    # Scope guard: manager cannot access staff in other branches
    assert_own_branch(current_staff, target.branch_id)

    branch_result = await db.execute(
        select(Branch).where(Branch.id == target.branch_id)
    )
    branch = branch_result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {target.branch_id} not found.",
        )

    return target, branch, current_staff


# ══════════════════════════════════════════════════════════════════════════════
# SESSION GUARD
# ══════════════════════════════════════════════════════════════════════════════

async def require_session_scope(
    session_id: int = Path(...),
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> tuple[Session, StaffMember]:
    """
    Load session and enforce branch/ownership scope:
      - admin: any session
      - manager: any session in own branch
      - teller/supervisor: only own sessions

    Returns (session, current_staff).
    """
    result = await db.execute(
        select(Session).where(Session.id == session_id)
    )
    session: Session | None = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    check_cross_branch_session(current_staff, session)
    return session, current_staff


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT GUARD
# ══════════════════════════════════════════════════════════════════════════════

async def require_audit_access(
    current_staff: StaffMember = Depends(get_current_staff),
) -> StaffMember:
    """
    Audit logs are admin-only.
    Raises 403 for manager/teller/supervisor.
    """
    check_admin_only(current_staff, "Audit logs")
    return current_staff


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM SETTINGS GUARD
# ══════════════════════════════════════════════════════════════════════════════

async def require_system_settings_access(
    current_staff: StaffMember = Depends(get_current_staff),
) -> StaffMember:
    """
    System settings (AI model, session timeout, demo mode) are admin-only.
    Manager cannot access these.
    """
    check_admin_only(current_staff, "System settings")
    return current_staff


# ══════════════════════════════════════════════════════════════════════════════
# PII LOG GUARD
# ══════════════════════════════════════════════════════════════════════════════

async def require_pii_log_access(
    current_staff: StaffMember = Depends(get_current_staff),
) -> StaffMember:
    """
    Raw PII logs (unmasked view) are admin-only.
    Manager sees only masked PII in session logs.
    """
    check_admin_only(current_staff, "PII logs")
    return current_staff


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE RE-EXPORTS for clean import in routers
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Dependency guards (use as Depends(...))
    "require_manager_or_admin",
    "require_admin",
    "require_branch_access",
    "require_staff_target_access",
    "require_session_scope",
    "require_audit_access",
    "require_system_settings_access",
    "require_pii_log_access",
    "BranchGuard",

    # Assertion helpers (call inside endpoint body)
    "assert_own_branch",
    "assert_role_can_create",
    "assert_role_can_set",
    "assert_not_self_deactivate",
    "assert_not_self_role_change",
    "check_cross_branch_session",
    "check_admin_only",
    "check_management_role",
]
