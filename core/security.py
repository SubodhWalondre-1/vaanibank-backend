"""
VaaniBank AI — Security Utilities
PSBs Hackathon 2026 | Team Vectora

Provides:
  - create_access_token()     → sign a JWT with staff payload
  - verify_access_token()     → decode + validate a JWT
  - hash_password()           → bcrypt hash
  - verify_password()         → bcrypt verify
  - get_current_staff()       → FastAPI dependency — validates Bearer JWT,
                                fetches StaffMember from DB
  - require_roles()           → role-based access factory dependency

bcrypt compatibility note:
  passlib 1.7.4 + bcrypt ≥ 4.x prints a harmless "(trapped) error reading
  bcrypt version" warning.  We silence it by patching __about__ at import
  time so passlib can read the version string it expects.
"""

from __future__ import annotations

import logging
import types
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import bcrypt as _bcrypt_lib
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db

logger = logging.getLogger("vaanibank.security")

# Silence passlib / bcrypt version mismatch warning
# passlib 1.7.4 reads bcrypt.__about__.__version__ — bcrypt 4.x removed __about__
if not hasattr(_bcrypt_lib, "__about__"):
    _about = types.ModuleType("__about__")
    _about.__version__ = getattr(_bcrypt_lib, "__version__", "4.0.0")
    _bcrypt_lib.__about__ = _about  # type: ignore[attr-defined]


# Password hashing
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    return _pwd_context.verify(plain, hashed)


# JWT
_ALGORITHM = settings.JWT_ALGORITHM
_SECRET = settings.JWT_SECRET_KEY
_EXPIRE_HOURS = settings.JWT_EXPIRE_HOURS

# Fields embedded in every token payload
_TOKEN_TYPE_KEY = "type"
_TOKEN_TYPE_VALUE = "access"


def create_access_token(
    *,
    staff_id: str,
    staff_db_id: int,
    username: str,
    role: str,
    branch_id: int,
    branch_code: str,
) -> tuple[str, int]:
    """
    Create a signed JWT for a staff member.

    Returns:
        (encoded_token, expires_in_seconds)
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=_EXPIRE_HOURS)
    expires_in = int((expire - now).total_seconds())

    payload = {
        # Standard claims
        "sub": str(staff_db_id),       # DB primary key — used for DB lookup
        "iat": now,
        "exp": expire,
        # Custom claims
        _TOKEN_TYPE_KEY: _TOKEN_TYPE_VALUE,
        "staff_id": staff_id,           # e.g. "UBI-MUM-042"
        "username": username,
        "role": role,
        "branch_id": branch_id,
        "branch_code": branch_code,
    }

    token = jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)
    return token, expires_in


def verify_access_token(token: str) -> dict:
    """
    Decode and validate a JWT string.

    Returns the decoded payload dict on success.
    Raises HTTPException 401 on any failure.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
    except JWTError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise credentials_exception

    if payload.get(_TOKEN_TYPE_KEY) != _TOKEN_TYPE_VALUE:
        raise credentials_exception

    sub: Optional[str] = payload.get("sub")
    if sub is None:
        raise credentials_exception

    return payload


# FastAPI Bearer scheme
_bearer_scheme = HTTPBearer(auto_error=True)


async def get_current_staff(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    """
    FastAPI dependency — validates JWT from Authorization header,
    then fetches and returns the live StaffMember ORM object.

    Usage:
        @router.get("/me")
        async def me(staff = Depends(get_current_staff)):
            ...
    """
    # Import here to avoid circular import at module level
    from models import StaffMember

    payload = verify_access_token(credentials.credentials)

    staff_db_id: Optional[int] = None
    try:
        staff_db_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(
        select(StaffMember).where(
            StaffMember.id == staff_db_id,
            StaffMember.is_active == True,  # noqa: E712
        )
    )
    staff = result.scalar_one_or_none()

    if staff is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Staff account not found or deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return staff


def require_roles(*allowed_roles: str):
    """
    Role-based access control factory.

    Usage:
        @router.delete("/branch/{id}")
        async def delete_branch(
            staff = Depends(require_roles("admin", "supervisor"))
        ):
    """
    def _role_checker(
        current_staff=Depends(get_current_staff),
    ):
        if current_staff.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Role '{current_staff.role}' is not permitted. "
                    f"Required: {list(allowed_roles)}"
                ),
            )
        return current_staff

    return _role_checker