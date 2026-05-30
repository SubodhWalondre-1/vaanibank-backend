"""
VaaniBank AI — Auth Router
PSBs Hackathon 2026 | Team Vectora

Endpoints:
  POST /auth/login    → JWT + staff info
  POST /auth/logout   → Clear Redis online key
  POST /auth/refresh  → Rotate JWT
  GET  /auth/me       → Current staff from token
"""

from __future__ import annotations

import logging
import random
import string
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AuthenticationError
from core.security import (
    create_access_token,
    get_current_staff,
    verify_access_token,
    verify_password,
    hash_password,
)

from database import get_db, get_redis
from models import StaffMember
from schemas import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    StaffResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
)

logger = logging.getLogger("vaanibank.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


# POST /auth/login

@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Staff login — returns JWT access token",
)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> LoginResponse:
    """
    Authenticate a staff member with username + password.

    - Verifies password hash via bcrypt.
    - Issues a signed JWT (HS256, 8-hour expiry).
    - Sets `staff_online:{staff_id}` in Redis (8-hour TTL).
    - Updates `last_login_at` on the staff record.
    """
    # Fetch by username
    query = select(StaffMember).where(
        StaffMember.username == body.username,
        StaffMember.is_active == True,  # noqa: E712
    )
    # If staff_id is provided, also match against it
    if body.staff_id:
        query = query.where(StaffMember.staff_id == body.staff_id)

    result = await db.execute(query)
    staff: StaffMember | None = result.scalar_one_or_none()

    if staff is None or not verify_password(body.password, staff.password_hash):
        logger.warning("Login failed for username=%s staff_id=%s", body.username, body.staff_id)
        raise AuthenticationError(
            message="Invalid credentials. Check Staff ID, username and password.",
        )

    # Fetch branch for branch_code in token
    from models import Branch
    branch_result = await db.execute(
        select(Branch).where(Branch.id == staff.branch_id)
    )
    branch = branch_result.scalar_one_or_none()
    branch_code = branch.branch_code if branch else ""
    branch_name = branch.branch_name if branch else ""

    # Mint JWT
    token, expires_in = create_access_token(
        staff_id=staff.staff_id,
        staff_db_id=staff.id,
        username=staff.username,
        role=staff.role,
        branch_id=staff.branch_id,
        branch_code=branch_code,
    )

    # Update last_login_at
    await db.execute(
        update(StaffMember)
        .where(StaffMember.id == staff.id)
        .values(last_login_at=datetime.now(timezone.utc))
    )
    await db.commit()

    # Mark staff online in Redis (8-hour TTL)
    try:
        await redis.setex(f"staff_online:{staff.staff_id}", 8 * 3600, "1")
    except Exception as exc:
        logger.warning("Redis staff_online set failed: %s", exc)

    logger.info("Login success | staff_id=%s | role=%s", staff.staff_id, staff.role)

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        staff=StaffResponse(
            id=staff.id,
            staff_id=staff.staff_id,
            username=staff.username,
            full_name=staff.full_name,
            role=staff.role,
            branch_id=staff.branch_id,
            branch_code=branch_code,
            branch_name=branch_name,
            languages_known=staff.languages_known or [],
        ),
    )


# POST /auth/logout

@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Logout staff — clears Redis online marker",
)
async def logout(
    current_staff: StaffMember = Depends(get_current_staff),
    redis=Depends(get_redis),
) -> LogoutResponse:
    """
    Log out the current staff member.

    Removes `staff_online:{staff_id}` from Redis.
    JWT invalidation is handled client-side (token deletion).
    """
    try:
        await redis.delete(f"staff_online:{current_staff.staff_id}")
    except Exception as exc:
        logger.warning("Redis staff_online delete failed: %s", exc)

    logger.info("Logout | staff_id=%s", current_staff.staff_id)
    return LogoutResponse(message="Logged out successfully.")


# POST /auth/refresh

@router.post(
    "/refresh",
    response_model=TokenRefreshResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate JWT — returns a new access token",
)
async def refresh_token(
    body: TokenRefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> TokenRefreshResponse:
    """
    Validate an existing token and issue a fresh one.

    Useful for clients that refresh before expiry rather than re-logging in.
    """
    # Decode old token (raises 401 if invalid/expired)
    payload = verify_access_token(body.token)

    staff_db_id = int(payload["sub"])
    result = await db.execute(
        select(StaffMember).where(
            StaffMember.id == staff_db_id,
            StaffMember.is_active == True,  # noqa: E712
        )
    )
    staff: StaffMember | None = result.scalar_one_or_none()
    if staff is None:
        raise AuthenticationError(message="Staff account not found or deactivated.")

    from models import Branch
    branch_result = await db.execute(
        select(Branch).where(Branch.id == staff.branch_id)
    )
    branch = branch_result.scalar_one_or_none()
    branch_code = branch.branch_code if branch else ""

    new_token, expires_in = create_access_token(
        staff_id=staff.staff_id,
        staff_db_id=staff.id,
        username=staff.username,
        role=staff.role,
        branch_id=staff.branch_id,
        branch_code=branch_code,
    )

    # Refresh Redis TTL
    try:
        await redis.setex(f"staff_online:{staff.staff_id}", 8 * 3600, "1")
    except Exception as exc:
        logger.warning("Redis refresh TTL failed: %s", exc)

    return TokenRefreshResponse(
        access_token=new_token,
        token_type="bearer",
        expires_in=expires_in,
    )


# GET /auth/me

@router.get(
    "/me",
    response_model=StaffResponse,
    status_code=status.HTTP_200_OK,
    summary="Current authenticated staff info",
)
async def me(
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> StaffResponse:
    """Return profile of the currently authenticated staff member."""
    from models import Branch
    branch_result = await db.execute(
        select(Branch).where(Branch.id == current_staff.branch_id)
    )
    branch = branch_result.scalar_one_or_none()

    return StaffResponse(
        id=current_staff.id,
        staff_id=current_staff.staff_id,
        username=current_staff.username,
        full_name=current_staff.full_name,
        role=current_staff.role,
        branch_id=current_staff.branch_id,
        branch_code=branch.branch_code if branch else "",
        branch_name=branch.branch_name if branch else "",
        languages_known=current_staff.languages_known or [],
    )


# POST /auth/demo-teller

@router.post(
    "/demo-teller",
    status_code=status.HTTP_201_CREATED,
    summary="Generate unique demo staff teller credentials for Nagpur Civil Lines branch",
)
async def generate_demo_teller(db: AsyncSession = Depends(get_db)):
    """
    Dynamically generates a unique staff teller for Nagpur branch.
    Returns: UBI-NGP-XXXX, username, plain password
    """
    from models import Branch, StaffRole
    
    # 1. Fetch Nagpur Branch
    branch_result = await db.execute(
        select(Branch).where(Branch.branch_code == "NGP-CVL-01")
    )
    branch = branch_result.scalar_one_or_none()
    if not branch:
        raise AuthenticationError(message="Nagpur Civil Lines branch (NGP-CVL-01) not found in database.")
    
    # 2. Generate unique username/staff_id to prevent collision
    suffix = "".join(random.choices(string.digits, k=3))
    staff_id = f"UBI-NGP-{suffix}"
    username = f"demo_{suffix}"
    
    # Ensure uniqueness in db
    clash_check = await db.execute(
        select(StaffMember).where(
            (StaffMember.username == username) | (StaffMember.staff_id == staff_id)
        )
    )
    if clash_check.scalar_one_or_none() is not None:
        suffix = "".join(random.choices(string.digits, k=3))
        staff_id = f"UBI-NGP-{suffix}"
        username = f"demo_{suffix}"
        
    password_plain = f"demo{suffix}"
    password_hash = hash_password(password_plain)
    
    # 3. Create new staff member
    new_staff = StaffMember(
        staff_id=staff_id,
        username=username,
        password_hash=password_hash,
        full_name=f"Nagpur Teller {suffix}",
        role=StaffRole.teller,
        branch_id=branch.id,
        languages_known=["Hindi", "Marathi"],
        is_active=True,
    )
    
    db.add(new_staff)
    await db.commit()
    await db.refresh(new_staff)
    
    return {
        "success": True,
        "staff_id": staff_id,
        "username": username,
        "password": password_plain,
        "full_name": new_staff.full_name,
        "role": str(new_staff.role),
        "branch_name": branch.branch_name,
    }
