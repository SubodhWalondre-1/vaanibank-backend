"""
VaaniBank AI — Staff Management Router (Phase 1)
PSBs Hackathon 2026 | Team Vectora

Manager Endpoints (own branch only):
  POST   /staff/create                → Add new staff to own branch
  GET    /staff/list                  → List own branch staff
  PATCH  /staff/{id}                  → Edit staff details / role
  PATCH  /staff/{id}/deactivate       → Deactivate staff (is_active = False)
  PATCH  /staff/{id}/activate         → Re-activate staff
  POST   /staff/{id}/reset-password   → Generate new password (returned once only)
  GET    /staff/{id}                  → Get single staff detail
  GET    /staff/{id}/sessions         → Login history + sessions handled

Admin Endpoints (all branches):
  GET    /admin/branches              → List all branches
  POST   /admin/branches/create       → Create new branch
  GET    /admin/analytics             → Network-wide analytics
  GET    /admin/audit-logs            → System-wide audit log

Shared:
  GET    /analytics/branch/{id}/range → Date-range analytics (manager = own branch only)
"""

from __future__ import annotations

import logging
import random
import re
import string
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import get_current_staff, require_roles, hash_password
from core.guards import (
    assert_own_branch,
    assert_role_can_create,
    assert_role_can_set,
    assert_not_self_deactivate,
    require_manager_or_admin,
    require_admin,
    require_branch_access,
    require_staff_target_access,
    BranchGuard,
)
from database import get_db
from models import (
    AuditLog,
    Branch,
    StaffMember,
    StaffRole,
    Session as SessionModel,
    AnalyticsDaily,
)

logger = logging.getLogger("vaanibank.staff")

router = APIRouter(tags=["staff-management"])


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMAS (inline — no circular import risk)
# ══════════════════════════════════════════════════════════════════════════════

class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, str_strip_whitespace=True)


class StaffCreateRequest(_Base):
    full_name: str = Field(..., min_length=2, max_length=200)
    role: str = Field(..., description="teller | supervisor")
    languages_known: Optional[List[str]] = Field(default_factory=list)
    username: Optional[str] = Field(None, min_length=3, max_length=100, description="Leave blank to auto-generate")

    def validate_role(self) -> str:
        allowed = {StaffRole.teller.value, StaffRole.supervisor.value}
        if self.role not in allowed:
            raise ValueError(f"Manager can only create teller or supervisor. Got: {self.role}")
        return self.role


class StaffUpdateRequest(_Base):
    full_name: Optional[str] = Field(None, min_length=2, max_length=200)
    role: Optional[str] = Field(None, description="teller | supervisor (manager cannot promote to manager/admin)")
    languages_known: Optional[List[str]] = None


class StaffDetailResponse(_Base):
    id: int
    staff_id: str
    username: str
    full_name: str
    role: str
    branch_id: int
    branch_code: str
    branch_name: str
    languages_known: List[str]
    is_active: bool
    last_login_at: Optional[datetime]
    created_at: datetime


class StaffCreateResponse(_Base):
    staff: StaffDetailResponse
    username: str
    plain_password: str  # shown ONCE — never stored in plain
    message: str = "Staff created. Share credentials once — password cannot be recovered."


class StaffListResponse(_Base):
    staff: List[StaffDetailResponse]
    total: int
    branch_code: str
    branch_name: str


class PasswordResetResponse(_Base):
    staff_id: str
    username: str
    new_plain_password: str  # shown ONCE
    message: str = "Password reset. Share the new password — it will not be shown again."


class BranchCreateRequest(_Base):
    branch_code: str = Field(..., min_length=3, max_length=50, description="e.g. NGP-CVL-02")
    branch_name: str = Field(..., min_length=3, max_length=200)
    bank_name: str = Field(default="Union Bank of India", max_length=200)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    region: Optional[str] = None
    address: Optional[str] = None
    pincode: Optional[str] = Field(None, max_length=10)


class BranchDetailResponse(_Base):
    id: int
    branch_code: str
    branch_name: str
    bank_name: str
    city: str
    state: str
    region: Optional[str]
    address: Optional[str]
    pincode: Optional[str]
    is_active: bool
    created_at: datetime
    # enriched at query time
    staff_count: int = 0
    sessions_today: int = 0


class BranchListResponse(_Base):
    branches: List[BranchDetailResponse]
    total: int


class AnalyticsRangeResponse(_Base):
    branch_id: int
    branch_code: str
    branch_name: str
    from_date: str
    to_date: str
    total_sessions: int
    completed_sessions: int
    abandoned_sessions: int
    avg_duration_seconds: float
    completion_rate: float
    languages_used: dict
    intents_breakdown: dict
    sentiments_breakdown: dict
    offline_sessions: int
    pii_detected_count: int
    ai_suggestion_used: int
    ai_suggestion_edited: int
    ai_suggestion_ignored: int
    ai_suggestion_acceptance_rate: float
    daily_breakdown: List[dict]  # [{date, sessions, completed, abandoned}]


class NetworkAnalyticsResponse(_Base):
    from_date: str
    to_date: str
    total_branches: int
    total_sessions: int
    completed_sessions: int
    abandoned_sessions: int
    completion_rate: float
    avg_duration_seconds: float
    total_pii_detected: int
    ai_suggestion_used: int
    per_branch: List[dict]  # per-branch summary rows


class AuditLogEntry(_Base):
    id: int
    actor_staff_id: str
    actor_name: str
    actor_role: str
    action: str
    target_staff_id: Optional[str]
    target_name: Optional[str]
    branch_code: str
    timestamp: datetime
    detail: Optional[str]


class AuditLogResponse(_Base):
    logs: List[AuditLogEntry]
    total: int
    page: int
    page_size: int


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _generate_password(length: int = 10) -> str:
    """Random password: letters + digits + a symbol."""
    chars = string.ascii_letters + string.digits + "!@#$"
    pwd = (
        random.choice(string.ascii_uppercase)
        + random.choice(string.digits)
        + random.choice("!@#$")
        + "".join(random.choices(chars, k=length - 3))
    )
    return "".join(random.sample(pwd, len(pwd)))  # shuffle


def _generate_username(full_name: str, branch_code: str, existing: set[str]) -> str:
    """e.g. rajesh.kumar.ngp or rajesh.kumar.ngp2"""
    parts = full_name.lower().split()
    city = branch_code.split("-")[0].lower() if "-" in branch_code else "ubi"
    base = re.sub(r"[^a-z0-9.]", "", ".".join(parts[:2]) + "." + city)
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


def _generate_staff_id(branch_code: str, existing_ids: set[str]) -> str:
    """e.g. UBI-NGP-023"""
    prefix = "UBI-" + branch_code.split("-")[0].upper() + "-"
    n = 1
    while True:
        candidate = f"{prefix}{n:03d}"
        if candidate not in existing_ids:
            return candidate
        n += 1


async def _get_branch_or_403(db: AsyncSession, branch_id: int, current_staff: StaffMember) -> Branch:
    """Return branch if current_staff may access it, else 403."""
    if current_staff.role == StaffRole.admin.value:
        result = await db.execute(select(Branch).where(Branch.id == branch_id))
    else:
        # Manager can only access own branch
        if current_staff.branch_id != branch_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-branch access denied.")
        result = await db.execute(select(Branch).where(Branch.id == branch_id))
    branch = result.scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found.")
    return branch


def _build_staff_detail(staff: StaffMember, branch: Branch) -> StaffDetailResponse:
    return StaffDetailResponse(
        id=staff.id,
        staff_id=staff.staff_id,
        username=staff.username,
        full_name=staff.full_name,
        role=staff.role,
        branch_id=staff.branch_id,
        branch_code=branch.branch_code,
        branch_name=branch.branch_name,
        languages_known=staff.languages_known or [],
        is_active=staff.is_active,
        last_login_at=staff.last_login_at,
        created_at=staff.created_at,
    )


# ── DB-backed audit writer ────────────────────────────────────────────────────
# Called inside every mutating endpoint with the open db session.
# Writes to audit_logs table (migration 002). Falls back to log-only on error.

async def _audit(
    db: AsyncSession,
    actor: StaffMember,
    branch: Branch,
    action: str,
    target: Optional[StaffMember] = None,
    detail: Optional[str] = None,
) -> None:
    try:
        log = AuditLog(
            actor_id=actor.id,
            actor_staff_id=actor.staff_id,
            actor_name=actor.full_name,
            actor_role=actor.role,
            action=action,
            detail=detail,
            target_id=target.id if target else None,
            target_staff_id=target.staff_id if target else None,
            target_name=target.full_name if target else None,
            branch_id=branch.id,
            branch_code=branch.branch_code,
        )
        db.add(log)
        # Note: caller must commit — we only add to the session here
    except Exception as exc:
        logger.warning("Audit log write failed (non-fatal): %s", exc)

    logger.info(
        "AUDIT | %s | %s | actor=%s | target=%s | %s",
        action, branch.branch_code, actor.staff_id,
        target.staff_id if target else "-", detail or "",
    )


# ══════════════════════════════════════════════════════════════════════════════
# STAFF ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/staff/create",
    response_model=StaffCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add new staff to own branch (manager) or any branch (admin)",
)
async def create_staff(
    body: StaffCreateRequest,
    current_staff: StaffMember = Depends(require_manager_or_admin),
    db: AsyncSession = Depends(get_db),
) -> StaffCreateResponse:
    """
    Manager can create teller or supervisor only (not manager/admin).
    Admin can create any role.
    Staff ID and username are auto-generated if not provided.
    Password is generated randomly and returned once — never recoverable.
    """
    # Centralised role guard (raises 403 if manager tries to create manager/admin)
    assert_role_can_create(current_staff, body.role)

    # Fetch branch
    branch = await _get_branch_or_403(db, current_staff.branch_id, current_staff)

    # Collect existing usernames and staff IDs to avoid collision
    existing_usernames_result = await db.execute(select(StaffMember.username))
    existing_usernames: set[str] = {row[0] for row in existing_usernames_result.fetchall()}
    existing_ids_result = await db.execute(select(StaffMember.staff_id))
    existing_ids: set[str] = {row[0] for row in existing_ids_result.fetchall()}

    # Resolve username
    if body.username:
        if body.username in existing_usernames:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Username '{body.username}' is already taken.",
            )
        username = body.username
    else:
        username = _generate_username(body.full_name, branch.branch_code, existing_usernames)

    staff_id = _generate_staff_id(branch.branch_code, existing_ids)
    plain_password = _generate_password()

    new_staff = StaffMember(
        staff_id=staff_id,
        username=username,
        password_hash=hash_password(plain_password),
        full_name=body.full_name,
        role=body.role,
        branch_id=current_staff.branch_id,
        languages_known=body.languages_known or [],
        is_active=True,
    )
    db.add(new_staff)
    await db.commit()
    await db.refresh(new_staff)

    await _audit(db, current_staff, branch, "staff_created", new_staff,
                 f"role={body.role}, username={username}")

    logger.info("Staff created | staff_id=%s | by=%s", staff_id, current_staff.staff_id)

    return StaffCreateResponse(
        staff=_build_staff_detail(new_staff, branch),
        username=username,
        plain_password=plain_password,
        message="Staff created. Share credentials once — password cannot be recovered.",
    )


@router.get(
    "/staff/list",
    response_model=StaffListResponse,
    status_code=status.HTTP_200_OK,
    summary="List staff in own branch (manager) or any branch (admin)",
)
async def list_staff(
    branch_id: Optional[int] = Query(None, description="Admin only: filter by branch. Manager always sees own branch."),
    include_inactive: bool = Query(False, description="Include deactivated staff"),
    current_staff: StaffMember = Depends(require_manager_or_admin),
    db: AsyncSession = Depends(get_db),
) -> StaffListResponse:
    """
    Manager always sees own branch only.
    Admin can pass ?branch_id= to filter; without it, defaults to all branches (but returns paginated).
    """
    # Determine target branch — manager is always locked to own branch
    if current_staff.role == StaffRole.manager.value:
        target_branch_id = current_staff.branch_id
    else:
        target_branch_id = branch_id if branch_id else current_staff.branch_id

    # Scope guard (no-op for admin; raises 403 for manager cross-branch)
    assert_own_branch(current_staff, target_branch_id)
    result = await db.execute(select(Branch).where(Branch.id == target_branch_id))
    branch = result.scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found.")

    query = select(StaffMember).where(StaffMember.branch_id == target_branch_id)
    if not include_inactive:
        query = query.where(StaffMember.is_active == True)  # noqa: E712
    query = query.order_by(StaffMember.created_at.desc())

    result = await db.execute(query)
    staff_list = result.scalars().all()

    return StaffListResponse(
        staff=[_build_staff_detail(s, branch) for s in staff_list],
        total=len(staff_list),
        branch_code=branch.branch_code,
        branch_name=branch.branch_name,
    )


@router.get(
    "/staff/{staff_db_id}",
    response_model=StaffDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get single staff member detail",
)
async def get_staff(
    ctx: tuple = Depends(require_staff_target_access),
) -> StaffDetailResponse:
    target, branch, _actor = ctx
    return _build_staff_detail(target, branch)


@router.patch(
    "/staff/{staff_db_id}",
    response_model=StaffDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Update staff name, role, or languages",
)
async def update_staff(
    body: StaffUpdateRequest,
    ctx: tuple = Depends(require_staff_target_access),
    db: AsyncSession = Depends(get_db),
) -> StaffDetailResponse:
    target, branch, current_staff = ctx

    # Centralised role guard — prevents privilege escalation
    if body.role is not None:
        assert_role_can_set(current_staff, body.role)
        target.role = body.role

    if body.full_name is not None:
        target.full_name = body.full_name
    if body.languages_known is not None:
        target.languages_known = body.languages_known

    await db.commit()
    await db.refresh(target)

    await _audit(db, current_staff, branch, "staff_updated", target,
                 f"name={target.full_name}, role={target.role}")

    return _build_staff_detail(target, branch)


@router.patch(
    "/staff/{staff_db_id}/deactivate",
    response_model=StaffDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Deactivate staff (is_active = False) — they cannot log in",
)
async def deactivate_staff(
    ctx: tuple = Depends(require_staff_target_access),
    db: AsyncSession = Depends(get_db),
) -> StaffDetailResponse:
    target, branch, current_staff = ctx
    assert_not_self_deactivate(current_staff, target)

    target.is_active = False
    await db.commit()
    await db.refresh(target)

    await _audit(db, current_staff, branch, "staff_deactivated", target)
    logger.info("Staff deactivated | %s | by=%s", target.staff_id, current_staff.staff_id)

    return _build_staff_detail(target, branch)


@router.patch(
    "/staff/{staff_db_id}/activate",
    response_model=StaffDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Re-activate a deactivated staff member",
)
async def activate_staff(
    ctx: tuple = Depends(require_staff_target_access),
    db: AsyncSession = Depends(get_db),
) -> StaffDetailResponse:
    target, branch, current_staff = ctx

    target.is_active = True
    await db.commit()
    await db.refresh(target)

    await _audit(db, current_staff, branch, "staff_activated", target)
    return _build_staff_detail(target, branch)


@router.post(
    "/staff/{staff_db_id}/reset-password",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_200_OK,
    summary="Reset staff password — new password returned ONCE only",
)
async def reset_staff_password(
    ctx: tuple = Depends(require_staff_target_access),
    db: AsyncSession = Depends(get_db),
) -> PasswordResetResponse:
    target, branch, current_staff = ctx

    new_plain = _generate_password()
    target.password_hash = hash_password(new_plain)
    await db.commit()

    await _audit(db, current_staff, branch, "password_reset", target)
    logger.info("Password reset | %s | by=%s", target.staff_id, current_staff.staff_id)

    return PasswordResetResponse(
        staff_id=target.staff_id,
        username=target.username,
        new_plain_password=new_plain,
        message="Password reset. Share the new password — it will not be shown again.",
    )


@router.get(
    "/staff/{staff_db_id}/sessions",
    status_code=status.HTTP_200_OK,
    summary="Get sessions handled by this staff member (login history + activity)",
)
async def staff_sessions(
    days: int = Query(7, ge=1, le=90, description="How many days back to look"),
    ctx: tuple = Depends(require_staff_target_access),
    db: AsyncSession = Depends(get_db),
):
    target, _branch, current_staff = ctx

    since = datetime.now(timezone.utc) - timedelta(days=days)
    sessions_result = await db.execute(
        select(SessionModel)
        .where(
            SessionModel.staff_id == target.id,
            SessionModel.created_at >= since,
        )
        .order_by(desc(SessionModel.created_at))
        .limit(100)
    )
    sessions = sessions_result.scalars().all()

    return {
        "staff_id": target.staff_id,
        "full_name": target.full_name,
        "last_login_at": target.last_login_at,
        "is_active": target.is_active,
        "total_sessions_in_range": len(sessions),
        "days": days,
        "sessions": [
            {
                "id": s.id,
                "token_number": s.token_number,
                "status": s.status,
                "intent_detected": s.intent_detected,
                "sentiment_overall": s.sentiment_overall,
                "customer_language": s.customer_language,
                "duration_seconds": s.duration_seconds,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
            }
            for s in sessions
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# ANALYTICS ENDPOINT (Manager + Admin)
# ══════════════════════════════════════════════════════════════════════════════

async def _analytics_from_sessions(
    db: AsyncSession,
    branch: Branch,
    from_dt,
    to_dt,
    from_date: str,
    to_date: str,
) -> AnalyticsRangeResponse:
    """
    Fallback: compute analytics directly from the sessions table
    when analytics_daily has no rows for this date range.

    P3 D-HIGH-1 / D-HIGH-3: Uses SQL aggregates instead of loading all
    session rows into Python memory (previously O(n) memory risk).
    """
    from datetime import date as _date, timedelta as _td
    from sqlalchemy import case, cast, literal_column
    from sqlalchemy.types import Date as SADate

    base_where = [
        SessionModel.branch_id == branch.id,
        cast(SessionModel.created_at, SADate) >= from_dt,
        cast(SessionModel.created_at, SADate) <= to_dt,
        SessionModel.status.in_(["completed", "abandoned"]),
    ]

    # ── Single aggregate query for scalar stats ──────────────────────────────
    from sqlalchemy import func as sqf
    agg_result = await db.execute(
        select(
            sqf.count(SessionModel.id).label("total"),
            sqf.count(case((SessionModel.status == "completed", 1))).label("completed"),
            sqf.count(case((SessionModel.status == "abandoned", 1))).label("abandoned"),
            sqf.count(case((SessionModel.offline_mode == True, 1))).label("offline"),  # noqa: E712
            sqf.count(case((SessionModel.pii_detected == True, 1))).label("pii"),  # noqa: E712
            sqf.coalesce(
                sqf.avg(case((SessionModel.status == "completed", SessionModel.duration_seconds))),
                0,
            ).label("avg_dur"),
        ).where(*base_where)
    )
    row = agg_result.one()

    total_sessions = row.total or 0
    completed = row.completed or 0
    abandoned = row.abandoned or 0
    offline = row.offline or 0
    pii_count = row.pii or 0
    avg_duration = float(row.avg_dur or 0.0)
    completion_rate = round((completed / total_sessions * 100), 1) if total_sessions else 0.0

    # ── GROUP BY queries for breakdowns (~12 rows each max) ──────────────────
    lang_result = await db.execute(
        select(
            sqf.coalesce(SessionModel.customer_language, literal_column("'Unknown'")).label("val"),
            sqf.count(SessionModel.id),
        ).where(*base_where).group_by("val")
    )
    languages = {r[0]: r[1] for r in lang_result.all()}

    intent_result = await db.execute(
        select(
            sqf.coalesce(SessionModel.intent_detected, literal_column("'general'")).label("val"),
            sqf.count(SessionModel.id),
        ).where(*base_where).group_by("val")
    )
    intents = {r[0]: r[1] for r in intent_result.all()}

    sentiment_result = await db.execute(
        select(
            sqf.coalesce(SessionModel.sentiment_overall, literal_column("'calm'")).label("val"),
            sqf.count(SessionModel.id),
        ).where(*base_where).group_by("val")
    )
    sentiments = {r[0]: r[1] for r in sentiment_result.all()}

    # ── Daily breakdown via GROUP BY on date ──────────────────────────────────
    daily_result = await db.execute(
        select(
            cast(SessionModel.created_at, SADate).label("day"),
            sqf.count(SessionModel.id).label("sessions"),
            sqf.count(case((SessionModel.status == "completed", 1))).label("completed"),
            sqf.count(case((SessionModel.status == "abandoned", 1))).label("abandoned"),
        ).where(*base_where).group_by("day").order_by("day")
    )
    daily_rows = {str(r.day): {"date": str(r.day), "sessions": r.sessions, "completed": r.completed, "abandoned": r.abandoned} for r in daily_result.all()}

    # Fill in dates with zero sessions
    daily_map: dict = {}
    cur = from_dt
    while cur <= to_dt:
        day_str = str(cur)
        daily_map[day_str] = daily_rows.get(day_str, {"date": day_str, "sessions": 0, "completed": 0, "abandoned": 0})
        cur += _td(days=1)

    return AnalyticsRangeResponse(
        branch_id=branch.id,
        branch_code=branch.branch_code,
        branch_name=branch.branch_name,
        from_date=from_date,
        to_date=to_date,
        total_sessions=total_sessions,
        completed_sessions=completed,
        abandoned_sessions=abandoned,
        avg_duration_seconds=round(avg_duration, 1),
        completion_rate=completion_rate,
        languages_used=languages,
        intents_breakdown=intents,
        sentiments_breakdown=sentiments,
        offline_sessions=offline,
        pii_detected_count=pii_count,
        ai_suggestion_used=0,
        ai_suggestion_edited=0,
        ai_suggestion_ignored=0,
        ai_suggestion_acceptance_rate=0.0,
        daily_breakdown=list(daily_map.values()),
    )


@router.get(
    "/analytics/branch/{branch_id}/range",
    response_model=AnalyticsRangeResponse,
    status_code=status.HTTP_200_OK,
    summary="Date-range analytics for a branch (manager sees own branch only)",
)
async def branch_analytics_range(
    from_date: str = Query(..., description="YYYY-MM-DD"),
    to_date: str = Query(..., description="YYYY-MM-DD"),
    ctx: tuple = Depends(require_branch_access),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsRangeResponse:
    branch, current_staff = ctx
    branch_id = branch.id

    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Date format must be YYYY-MM-DD")

    if from_dt > to_dt:
        raise HTTPException(status_code=422, detail="from_date must be before to_date")

    rows_result = await db.execute(
        select(AnalyticsDaily)
        .where(
            AnalyticsDaily.branch_id == branch_id,
            AnalyticsDaily.date >= from_dt,
            AnalyticsDaily.date <= to_dt,
        )
        .order_by(AnalyticsDaily.date)
    )
    rows = rows_result.scalars().all()

    # ── If no analytics_daily rows exist, compute live from sessions table ──────────
    if not rows:
        return await _analytics_from_sessions(db, branch, from_dt, to_dt, from_date, to_date)

    # Aggregate
    total_sessions = sum(r.total_sessions for r in rows)
    completed = sum(r.completed_sessions for r in rows)
    abandoned = sum(r.abandoned_sessions for r in rows)
    offline = sum(r.offline_sessions for r in rows)
    pii_count = sum(r.pii_detected_count for r in rows)
    ai_used = sum(r.ai_suggestion_used for r in rows)
    ai_edited = sum(r.ai_suggestion_edited for r in rows)
    ai_ignored = sum(r.ai_suggestion_ignored for r in rows)

    durations = [r.avg_duration_seconds for r in rows if r.avg_duration_seconds]
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    # Merge JSONB dicts
    languages: dict = {}
    intents: dict = {}
    sentiments: dict = {}
    for r in rows:
        for k, v in (r.languages_used or {}).items():
            languages[k] = languages.get(k, 0) + v
        for k, v in (r.intents_breakdown or {}).items():
            intents[k] = intents.get(k, 0) + v
        for k, v in (r.sentiments_breakdown or {}).items():
            sentiments[k] = sentiments.get(k, 0) + v

    completion_rate = round((completed / total_sessions * 100), 1) if total_sessions else 0.0
    ai_acceptance = round((ai_used / (ai_used + ai_ignored) * 100), 1) if (ai_used + ai_ignored) else 0.0

    daily_breakdown = [
        {
            "date": str(r.date),
            "sessions": r.total_sessions,
            "completed": r.completed_sessions,
            "abandoned": r.abandoned_sessions,
            "avg_duration_seconds": r.avg_duration_seconds,
        }
        for r in rows
    ]

    return AnalyticsRangeResponse(
        branch_id=branch_id,
        branch_code=branch.branch_code,
        branch_name=branch.branch_name,
        from_date=from_date,
        to_date=to_date,
        total_sessions=total_sessions,
        completed_sessions=completed,
        abandoned_sessions=abandoned,
        avg_duration_seconds=round(avg_duration, 1),
        completion_rate=completion_rate,
        languages_used=languages,
        intents_breakdown=intents,
        sentiments_breakdown=sentiments,
        offline_sessions=offline,
        pii_detected_count=pii_count,
        ai_suggestion_used=ai_used,
        ai_suggestion_edited=ai_edited,
        ai_suggestion_ignored=ai_ignored,
        ai_suggestion_acceptance_rate=ai_acceptance,
        daily_breakdown=daily_breakdown,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN-ONLY ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/admin/branches",
    response_model=BranchListResponse,
    status_code=status.HTTP_200_OK,
    summary="[Admin only] List all branches with live stats",
)
async def admin_list_branches(
    include_inactive: bool = Query(False),
    current_staff: StaffMember = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> BranchListResponse:
    query = select(Branch)
    if not include_inactive:
        query = query.where(Branch.is_active == True)  # noqa: E712
    result = await db.execute(query.order_by(Branch.branch_code))
    branches = result.scalars().all()

    today = datetime.now(timezone.utc).date()

    branch_details = []
    for b in branches:
        # Count active staff
        sc_result = await db.execute(
            select(func.count(StaffMember.id)).where(
                StaffMember.branch_id == b.id,
                StaffMember.is_active == True,  # noqa: E712
            )
        )
        staff_count = sc_result.scalar_one() or 0

        # Count sessions today
        st_result = await db.execute(
            select(func.count(SessionModel.id)).where(
                SessionModel.branch_id == b.id,
                func.date(SessionModel.created_at) == today,
            )
        )
        sessions_today = st_result.scalar_one() or 0

        branch_details.append(BranchDetailResponse(
            id=b.id,
            branch_code=b.branch_code,
            branch_name=b.branch_name,
            bank_name=b.bank_name,
            city=b.city,
            state=b.state,
            region=b.region,
            address=b.address,
            pincode=b.pincode,
            is_active=b.is_active,
            created_at=b.created_at,
            staff_count=staff_count,
            sessions_today=sessions_today,
        ))

    return BranchListResponse(branches=branch_details, total=len(branch_details))


@router.post(
    "/admin/branches/create",
    response_model=BranchDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Admin only] Create a new branch",
)
async def admin_create_branch(
    body: BranchCreateRequest,
    current_staff: StaffMember = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> BranchDetailResponse:
    # Check duplicate branch_code
    existing = await db.execute(
        select(Branch).where(Branch.branch_code == body.branch_code)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Branch code '{body.branch_code}' already exists.",
        )

    new_branch = Branch(
        branch_code=body.branch_code,
        branch_name=body.branch_name,
        bank_name=body.bank_name,
        city=body.city,
        state=body.state,
        region=body.region,
        address=body.address,
        pincode=body.pincode,
        is_active=True,
    )
    db.add(new_branch)
    await db.commit()
    await db.refresh(new_branch)

    await _audit(db, current_staff, new_branch, "branch_created", detail=f"city={body.city}, state={body.state}")
    logger.info("Branch created | %s | by=%s", body.branch_code, current_staff.staff_id)

    return BranchDetailResponse(
        id=new_branch.id,
        branch_code=new_branch.branch_code,
        branch_name=new_branch.branch_name,
        bank_name=new_branch.bank_name,
        city=new_branch.city,
        state=new_branch.state,
        region=new_branch.region,
        address=new_branch.address,
        pincode=new_branch.pincode,
        is_active=new_branch.is_active,
        created_at=new_branch.created_at,
        staff_count=0,
        sessions_today=0,
    )


@router.get(
    "/admin/analytics",
    response_model=NetworkAnalyticsResponse,
    status_code=status.HTTP_200_OK,
    summary="[Admin only] Network-wide analytics across all branches",
)
async def admin_network_analytics(
    from_date: str = Query(..., description="YYYY-MM-DD"),
    to_date: str = Query(..., description="YYYY-MM-DD"),
    current_staff: StaffMember = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> NetworkAnalyticsResponse:
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Date format must be YYYY-MM-DD")

    # All branches
    branches_result = await db.execute(select(Branch).where(Branch.is_active == True))  # noqa: E712
    branches = branches_result.scalars().all()

    total_sessions = 0
    completed_total = 0
    abandoned_total = 0
    pii_total = 0
    ai_used_total = 0
    durations_all: list[float] = []
    per_branch = []

    from sqlalchemy import cast
    from sqlalchemy.types import Date as SADate

    for b in branches:
        rows_result = await db.execute(
            select(AnalyticsDaily).where(
                AnalyticsDaily.branch_id == b.id,
                AnalyticsDaily.date >= from_dt,
                AnalyticsDaily.date <= to_dt,
            )
        )
        rows = rows_result.scalars().all()

        if rows:
            # Use pre-aggregated analytics_daily rows
            b_sessions  = sum(r.total_sessions for r in rows)
            b_completed = sum(r.completed_sessions for r in rows)
            b_abandoned = sum(r.abandoned_sessions for r in rows)
            b_pii       = sum(r.pii_detected_count for r in rows)
            b_ai        = sum(r.ai_suggestion_used for r in rows)
            b_durations = [r.avg_duration_seconds for r in rows if r.avg_duration_seconds]
        else:
            # Fallback: compute live from sessions table (fresh DB / no analytics_daily yet)
            sess_result = await db.execute(
                select(SessionModel).where(
                    SessionModel.branch_id == b.id,
                    cast(SessionModel.created_at, SADate) >= from_dt,
                    cast(SessionModel.created_at, SADate) <= to_dt,
                )
            )
            sess_rows = sess_result.scalars().all()
            b_sessions  = len(sess_rows)
            b_completed = sum(1 for s in sess_rows if s.status == "completed")
            b_abandoned = sum(1 for s in sess_rows if s.status == "abandoned")
            b_pii       = sum(1 for s in sess_rows if getattr(s, "pii_detected", False))
            b_ai        = 0
            b_durations = [s.duration_seconds for s in sess_rows if s.duration_seconds]

        b_avg_dur   = sum(b_durations) / len(b_durations) if b_durations else 0.0
        b_completion = round(b_completed / b_sessions * 100, 1) if b_sessions else 0.0

        total_sessions  += b_sessions
        completed_total += b_completed
        abandoned_total += b_abandoned
        pii_total       += b_pii
        ai_used_total   += b_ai
        durations_all.extend(b_durations)

        per_branch.append({
            "branch_id":          b.id,
            "branch_code":        b.branch_code,
            "branch_name":        b.branch_name,
            "city":               b.city,
            "total_sessions":     b_sessions,
            "completed_sessions": b_completed,
            "abandoned_sessions": b_abandoned,
            "avg_duration_seconds": round(b_avg_dur, 1),
            "completion_rate":    b_completion,
            "pii_detected_count": b_pii,
            "ai_suggestion_used": b_ai,
        })

    avg_duration_all = sum(durations_all) / len(durations_all) if durations_all else 0.0
    completion_rate = round(completed_total / total_sessions * 100, 1) if total_sessions else 0.0

    return NetworkAnalyticsResponse(
        from_date=from_date,
        to_date=to_date,
        total_branches=len(branches),
        total_sessions=total_sessions,
        completed_sessions=completed_total,
        abandoned_sessions=abandoned_total,
        completion_rate=completion_rate,
        avg_duration_seconds=round(avg_duration_all, 1),
        total_pii_detected=pii_total,
        ai_suggestion_used=ai_used_total,
        per_branch=per_branch,
    )


@router.get(
    "/admin/audit-logs",
    response_model=AuditLogResponse,
    status_code=status.HTTP_200_OK,
    summary="[Admin only] System-wide audit logs (staff changes, password resets, etc.)",
)
async def admin_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action_filter: Optional[str] = Query(None, description="Filter by action type e.g. staff_created"),
    current_staff: StaffMember = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AuditLogResponse:
    """Reads audit logs from DB (audit_logs table — migration 002)."""
    query = select(AuditLog).order_by(desc(AuditLog.created_at))

    if action_filter:
        query = query.where(AuditLog.action == action_filter)

    # Total count
    count_query = select(func.count(AuditLog.id))
    if action_filter:
        count_query = count_query.where(AuditLog.action == action_filter)
    total_result = await db.execute(count_query)
    total = total_result.scalar_one() or 0

    # Paginated rows
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)
    rows_result = await db.execute(query)
    rows = rows_result.scalars().all()

    return AuditLogResponse(
        logs=[
            AuditLogEntry(
                id=r.id,
                actor_staff_id=r.actor_staff_id,
                actor_name=r.actor_name,
                actor_role=r.actor_role,
                action=r.action,
                target_staff_id=r.target_staff_id,
                target_name=r.target_name,
                branch_code=r.branch_code,
                timestamp=r.created_at,
                detail=r.detail,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
