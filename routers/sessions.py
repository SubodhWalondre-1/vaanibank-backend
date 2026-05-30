"""
VaaniBank AI — Sessions Router
PSBs Hackathon 2026 | Team Vectora

Endpoints:
  POST  /sessions/create
  GET   /sessions/active
  GET   /sessions/history
  GET   /sessions/{token_number}
  PATCH /sessions/{session_id}/end
  WS    /ws/{token_number}
  GET   /sessions/{session_id}/customer-profile
"""

from __future__ import annotations

import json
import logging
import random
import string
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import SessionAlreadyEndedError, SessionNotFoundError
from core.security import get_current_staff, verify_access_token
from core.guards import check_cross_branch_session
from database import get_db, get_redis
from models import Branch, Session, SessionStatus, StaffMember, AnalyticsDaily
from schemas import (
    CustomerSessionCreateRequest,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionEndResponse,
    SessionListResponse,
    SessionResponse,
)
from websocket.manager import ws_manager

logger = logging.getLogger("vaanibank.sessions")

router = APIRouter(tags=["sessions"])

_ACTIVE_SESSION_TTL = 2 * 3600  # 2 hours

_DEMO_BRANCH_SEED = {
    "NGP-CVL-01": {
        "branch_name": "Nagpur Civil Lines Branch",
        "bank_name": "Union Bank of India",
        "city": "Nagpur",
        "state": "Maharashtra",
        "region": "West",
        "address": "Civil Lines, Nagpur, Maharashtra 440001",
        "pincode": "440001",
        "is_active": True,
    },
    "MUM-AND-01": {
        "branch_name": "Mumbai Andheri Branch",
        "bank_name": "Union Bank of India",
        "city": "Mumbai",
        "state": "Maharashtra",
        "region": "West",
        "address": "Andheri West, Mumbai, Maharashtra 400058",
        "pincode": "400058",
        "is_active": True,
    },
    "CHN-TNG-01": {
        "branch_name": "Chennai T Nagar Branch",
        "bank_name": "Union Bank of India",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "region": "South",
        "address": "T Nagar, Chennai, Tamil Nadu 600017",
        "pincode": "600017",
        "is_active": True,
    },
}


# HELPERS

def _generate_token_number() -> str:
    """Generate a human-friendly token like MRT-2847."""
    letters = "".join(random.choices(string.ascii_uppercase, k=3))
    digits = "".join(random.choices(string.digits, k=4))
    return f"{letters}-{digits}"


def _session_to_schema(session: Session, branch: Optional[Branch] = None) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        token_number=session.token_number,
        branch_id=session.branch_id,
        branch_name=branch.branch_name if branch else None,
        staff_id=session.staff_id,
        customer_language=session.customer_language,
        customer_language_code=session.customer_language_code,
        staff_language=session.staff_language,
        entry_method=session.entry_method,
        status=session.status,
        sentiment_overall=session.sentiment_overall,
        intent_detected=session.intent_detected,
        total_exchanges=session.total_exchanges,
        duration_seconds=session.duration_seconds,
        offline_mode=session.offline_mode,
        pii_detected=session.pii_detected,
        pii_types_found=session.pii_types_found or [],
        started_at=session.started_at,
        ended_at=session.ended_at,
        created_at=session.created_at,
    )


async def _upsert_analytics_daily(db: AsyncSession, session: Session, ended_at: datetime) -> None:
    """
    Called after every session end. Upserts (INSERT or UPDATE) the analytics_daily
    row for (branch_id, date). This is the sole writer — no cron job needed.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    import json as _json

    session_date = ended_at.date()
    branch_id = session.branch_id

    # Try to load existing row
    existing_result = await db.execute(
        select(AnalyticsDaily).where(
            AnalyticsDaily.branch_id == branch_id,
            AnalyticsDaily.date == session_date,
        )
    )
    row = existing_result.scalar_one_or_none()

    is_completed = session.status == SessionStatus.completed
    is_abandoned = session.status == SessionStatus.abandoned
    is_offline = getattr(session, "offline_mode", False) or False
    has_pii = getattr(session, "pii_detected", False) or False

    # Language counter — use code (hi, mr) to match frontend expectation
    lang = session.customer_language_code or "hi"
    if "-" in lang:
        lang = lang.split("-")[0]
    
    # Intent counter
    intent = session.intent_detected or "general"
    # Sentiment counter
    sentiment = session.sentiment_overall or "calm"
    # Calculate exact average duration from sessions table directly to avoid rolling average corruption
    # only completed sessions that ended on the same session_date (UTC) with duration <= 1800
    from datetime import timedelta, time as _time
    start_dt = datetime.combine(session_date, _time.min).replace(tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)

    avg_dur_result = await db.execute(
        select(func.coalesce(
            func.avg(Session.duration_seconds),
            0.0
        )).where(
            Session.branch_id == branch_id,
            Session.status == "completed",
            Session.duration_seconds <= 1800,
            Session.ended_at >= start_dt,
            Session.ended_at < end_dt
        )
    )
    db_avg_dur = float(avg_dur_result.scalar() or 0.0)

    if row is None:
        # INSERT new row
        row = AnalyticsDaily(
            branch_id=branch_id,
            date=session_date,
            total_sessions=1,
            completed_sessions=1 if is_completed else 0,
            abandoned_sessions=1 if is_abandoned else 0,
            avg_duration_seconds=db_avg_dur if db_avg_dur > 0.0 else None,
            languages_used={lang: 1},
            intents_breakdown={intent: 1},
            sentiments_breakdown={sentiment: 1},
            offline_sessions=1 if is_offline else 0,
            pii_detected_count=1 if has_pii else 0,
            ai_suggestion_used=0,
            ai_suggestion_edited=0,
            ai_suggestion_ignored=0,
        )
        db.add(row)
    else:
        # UPDATE existing row
        row.total_sessions += 1
        if is_completed:
            row.completed_sessions += 1
        if is_abandoned:
            row.abandoned_sessions += 1
        if is_offline:
            row.offline_sessions += 1
        if has_pii:
            row.pii_detected_count += 1

        row.avg_duration_seconds = db_avg_dur if db_avg_dur > 0.0 else None

        # Merge JSONB counters
        langs = dict(row.languages_used or {})
        langs[lang] = langs.get(lang, 0) + 1
        row.languages_used = langs

        intents = dict(row.intents_breakdown or {})
        intents[intent] = intents.get(intent, 0) + 1
        row.intents_breakdown = intents

        sentiments = dict(row.sentiments_breakdown or {})
        sentiments[sentiment] = sentiments.get(sentiment, 0) + 1
        row.sentiments_breakdown = sentiments

    await db.commit()
    logger.info(
        "Analytics upserted | branch=%s | date=%s | total=%s",
        branch_id, session_date, row.total_sessions
    )


async def _generate_summary_background(session_id: int, token_number: str) -> None:
    """Background task: generate bilingual PDF after session ends."""
    try:
        from database import AsyncSessionLocal
        from models import BilingualSummary, Exchange
        from services.ai_service import ai_service
        from services.pdf_service import pdf_service

        async with AsyncSessionLocal() as db:
            session_result = await db.execute(
                select(Session).where(Session.id == session_id)
            )
            session_obj = session_result.scalar_one_or_none()
            if not session_obj:
                return

            branch_result = await db.execute(
                select(Branch).where(Branch.id == session_obj.branch_id)
            )
            branch = branch_result.scalar_one_or_none()

            staff_result = await db.execute(
                select(StaffMember).where(StaffMember.id == session_obj.staff_id)
            )
            staff = staff_result.scalar_one_or_none()

            exchange_result = await db.execute(
                select(Exchange)
                .where(Exchange.session_id == session_id)
                .order_by(Exchange.exchange_number)
            )
            exchanges = exchange_result.scalars().all()

            # Build conversation text for LLM summary
            conversation_text = "\n".join(
                f"Customer: {ex.customer_text_translated or ex.customer_text_original or ''}\n"
                f"Staff: {ex.staff_response_final or ex.staff_response_suggested or ''}"
                for ex in exchanges
                if ex.customer_text_original
            )

            if not conversation_text.strip():
                return

            # Call Groq to generate structured summary
            summary_prompt = (
                f"Summarize this banking conversation in both Hindi and "
                f"{session_obj.customer_language or 'the customer language'}.\n\n"
                f"Conversation:\n{conversation_text}\n\n"
                f"Return JSON only:\n"
                f'{{"summary_hindi": ["..."], "summary_customer_lang": ["..."], '
                f'"key_points_hindi": ["..."], "key_points_customer": ["..."], '
                f'"next_steps_hindi": ["..."], "next_steps_customer": ["..."]}}'
            )

            llm_result = await ai_service.process_with_llm(
                text=summary_prompt,
                source_language=session_obj.customer_language or "Hindi",
            )

            # Parse summary from raw response
            import json as _json
            import re as _re
            try:
                # Strip markdown fences if Groq wraps JSON in ```json...```
                raw = llm_result.raw_response.strip()
                raw = _re.sub(r"^```(?:json)?\s*", "", raw)
                raw = _re.sub(r"\s*```$", "", raw)
                summary_data = _json.loads(raw)
            except Exception:
                summary_data = {}

            # Upsert bilingual_summaries
            existing = await db.execute(
                select(BilingualSummary).where(
                    BilingualSummary.session_id == session_id
                )
            )
            summary_obj = existing.scalar_one_or_none()

            if summary_obj is None:
                summary_obj = BilingualSummary(
                    session_id=session_id,
                    customer_language=session_obj.customer_language or "Hindi",
                    summary_hindi=summary_data.get("summary_hindi", []),
                    summary_customer_lang=summary_data.get("summary_customer_lang", []),
                    key_points_hindi=summary_data.get("key_points_hindi", []),
                    key_points_customer=summary_data.get("key_points_customer", []),
                    next_steps_hindi=summary_data.get("next_steps_hindi", []),
                    next_steps_customer=summary_data.get("next_steps_customer", []),
                    generated_at=datetime.now(timezone.utc),
                )
                db.add(summary_obj)
                await db.flush()

            # Generate PDF
            pdf_url = pdf_service.generate_bilingual_summary(
                session_id=session_id,
                token_number=token_number,
                branch_name=branch.branch_name if branch else "Union Bank of India",
                staff_name=staff.full_name if staff else "Staff",
                customer_language=session_obj.customer_language or "Hindi",
                intent_detected=session_obj.intent_detected,
                sentiment_overall=session_obj.sentiment_overall,
                started_at=session_obj.started_at,
                ended_at=session_obj.ended_at,
                duration_seconds=session_obj.duration_seconds,
                summary_hindi=summary_obj.summary_hindi,
                summary_customer_lang=summary_obj.summary_customer_lang,
                key_points_hindi=summary_obj.key_points_hindi,
                key_points_customer=summary_obj.key_points_customer,
                next_steps_hindi=summary_obj.next_steps_hindi,
                next_steps_customer=summary_obj.next_steps_customer,
                collected_data=session_obj.collected_data,
            )

            await db.execute(
                update(BilingualSummary)
                .where(BilingualSummary.session_id == session_id)
                .values(pdf_url=pdf_url, pdf_generated=True)
            )
            await db.commit()
            logger.info("Background summary generated | session=%d | pdf=%s", session_id, pdf_url)

    except Exception as exc:
        logger.error("Background summary generation failed | session=%d | %s", session_id, exc)


# POST /sessions/create

@router.post(
    "/sessions/create",
    response_model=SessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new customer session",
)
async def create_session(
    body: SessionCreateRequest,
    request: Request,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> SessionCreateResponse:
    """
    Create a new session for an incoming customer.

    - Generates a unique token_number (e.g. MRT-2847).
    - Stores session in PostgreSQL.
    - Caches active session data in Redis (2-hour TTL).
    - Returns the WebSocket URL for both staff and customer panels.
    """
    # Unique token (retry on collision)
    token_number = ""
    for _ in range(10):
        token_number = _generate_token_number()
        existing = await db.execute(
            select(Session).where(Session.token_number == token_number)
        )
        if existing.scalar_one_or_none() is None:
            break
    else:
        # All 10 retries collided — should be astronomically rare
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to generate unique token. Please try again.",
        )

    session = Session(
        token_number=token_number,
        branch_id=current_staff.branch_id,
        staff_id=current_staff.id,
        customer_language=body.customer_language,
        customer_language_code=body.customer_language_code,
        staff_language="Hindi",
        entry_method=body.entry_method or "manual",
        status=SessionStatus.waiting,
        offline_mode=body.offline_mode or False,
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    # Cache active session in Redis
    try:
        session_cache = {
            "session_id": session.id,
            "token_number": token_number,
            "staff_id": current_staff.id,
            "branch_id": current_staff.branch_id,
            "customer_language_code": body.customer_language_code,
            "status": SessionStatus.waiting,
        }
        await redis.setex(
            f"active_session:{token_number}",
            _ACTIVE_SESSION_TTL,
            json.dumps(session_cache),
        )
    except Exception as exc:
        logger.warning("Redis session cache failed: %s", exc)

    from config import settings
    # Dynamic URL generation — works for both localhost and production
    host = request.headers.get("host", f"localhost:{settings.APP_PORT}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    ws_scheme = "wss" if scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{host}/ws/{token_number}"
    # Customer panel URL (use ALLOWED_ORIGINS first entry for production)
    if settings.is_production:
        origins = settings.allowed_origins_list
        customer_base = next((o for o in origins if "customer" in o), origins[0] if origins else f"{scheme}://{host}")
    else:
        customer_base = "http://localhost:5174"
    customer_panel_url = f"{customer_base}/?token={token_number}"

    logger.info(
        "Session created | token=%s | staff=%s | lang=%s",
        token_number,
        current_staff.staff_id,
        body.customer_language_code,
    )

    return SessionCreateResponse(
        session_id=session.id,
        token_number=token_number,
        websocket_url=ws_url,
        customer_panel_url=customer_panel_url,
        customer_language=body.customer_language,
        customer_language_code=body.customer_language_code,
        status=SessionStatus.waiting,
        created_at=session.created_at,
    )


# Public Settings Endpoint

@router.get(
    "/sessions/settings/public",
    status_code=200,
    summary="Get public system settings (demo_mode) without authentication",
)
async def get_public_settings() -> dict:
    from services.settings_service import settings_service
    settings = await settings_service.get_all_settings()
    return {
        "demo_mode": settings.get("demo_mode", False),
    }


# POST /sessions/customer-create  (PUBLIC — no auth, for customer QR-scan)

@router.post(
    "/sessions/customer-create",
    response_model=SessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create session from customer panel (no auth, QR-scan flow)",
)
async def customer_create_session(
    body: CustomerSessionCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> SessionCreateResponse:
    """
    Public endpoint for customers who scanned the branch QR code.
    No authentication required.

    - Looks up branch by branch_code.
    - Assigns to any active staff member at that branch.
    - Generates a token_number and stores the session.
    """

    normalized_branch_code = body.branch_code.strip().upper()

    # Validate branch exists
    branch_result = await db.execute(
        select(Branch).where(
            Branch.branch_code == normalized_branch_code,
            Branch.is_active == True,
        )
    )
    branch = branch_result.scalar_one_or_none()

    # Local/demo resiliency: auto-create missing demo branch rows.
    if branch is None and normalized_branch_code in _DEMO_BRANCH_SEED:
        branch_payload = _DEMO_BRANCH_SEED[normalized_branch_code]
        branch = Branch(branch_code=normalized_branch_code, **branch_payload)
        db.add(branch)
        await db.commit()
        await db.refresh(branch)
        logger.warning(
            "Auto-created missing demo branch | branch=%s",
            normalized_branch_code,
        )

    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch '{normalized_branch_code}' not found or inactive.",
        )

    # QR-scan sessions: keep staff_id NULL so any active teller in the branch
    # active teller can see it in /sessions/active and accept it.
    # (Previously we auto-assigned active staff — new staff joining would be missed)
    staff_id = None

    # Unique token (retry on collision)
    for _ in range(10):
        token_number = _generate_token_number()
        existing = await db.execute(
            select(Session).where(Session.token_number == token_number)
        )
        if existing.scalar_one_or_none() is None:
            break

    session = Session(
        token_number=token_number,
        branch_id=branch.id,
        staff_id=staff_id,
        customer_language=body.customer_language,
        customer_language_code=body.customer_language_code,
        staff_language="Hindi",
        entry_method=body.entry_method or "qr_scan",
        status=SessionStatus.waiting,
        offline_mode=False,
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    # Cache active session in Redis
    try:
        session_cache = {
            "session_id": session.id,
            "token_number": token_number,
            "staff_id": staff_id,
            "branch_id": branch.id,
            "customer_language_code": body.customer_language_code,
            "status": SessionStatus.waiting,
        }
        await redis.setex(
            f"active_session:{token_number}",
            _ACTIVE_SESSION_TTL,
            json.dumps(session_cache),
        )
    except Exception as exc:
        logger.warning("Redis session cache failed: %s", exc)

    from config import settings
    # Dynamic URL generation — works for both localhost and production
    host = request.headers.get("host", f"localhost:{settings.APP_PORT}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    ws_scheme = "wss" if scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{host}/ws/{token_number}"
    # Customer panel URL
    if settings.is_production:
        origins = settings.allowed_origins_list
        customer_base = next((o for o in origins if "customer" in o), origins[0] if origins else f"{scheme}://{host}")
    else:
        customer_base = "http://localhost:5174"
    customer_panel_url = f"{customer_base}/?token={token_number}"

    logger.info(
        "Customer session created | token=%s | branch=%s | lang=%s",
        token_number,
        normalized_branch_code,
        body.customer_language_code,
    )

    return SessionCreateResponse(
        session_id=session.id,
        token_number=token_number,
        websocket_url=ws_url,
        customer_panel_url=customer_panel_url,
        customer_language=body.customer_language,
        customer_language_code=body.customer_language_code,
        status=SessionStatus.waiting,
        created_at=session.created_at,
    )


# GET /sessions/active

@router.get(
    "/sessions/active",
    response_model=List[SessionResponse],
    status_code=status.HTTP_200_OK,
    summary="List active sessions for current staff",
)
async def get_active_sessions(
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> List[SessionResponse]:
    """Return all waiting/active sessions visible to the authenticated staff member.

    Scope rules:
      - teller / supervisor  : own sessions (staff_id == me) +
                               branch sessions assigned to me OR unassigned
      - manager / admin      : all waiting/active sessions in own branch
    """
    from sqlalchemy import or_
    from models import StaffRole as SR

    role = current_staff.role

    if role in (SR.manager.value, SR.admin.value, "manager", "admin"):
        # Manager/admin see all branch sessions
        result = await db.execute(
            select(Session)
            .where(
                Session.branch_id == current_staff.branch_id,
                Session.status.in_([SessionStatus.waiting, SessionStatus.active]),
            )
            .order_by(desc(Session.created_at))
        )
    else:
        # Teller/supervisor: own sessions + branch sessions assigned to them
        # Also include sessions assigned to this branch but where staff_id
        # matches current user (covers QR-scan sessions assigned during creation)
        result = await db.execute(
            select(Session)
            .where(
                Session.branch_id == current_staff.branch_id,
                Session.status.in_([SessionStatus.waiting, SessionStatus.active]),
                or_(
                    Session.staff_id == current_staff.id,
                    Session.staff_id.is_(None),
                ),
            )
            .order_by(desc(Session.created_at))
        )

    sessions = result.scalars().all()

    branch_result = await db.execute(
        select(Branch).where(Branch.id == current_staff.branch_id)
    )
    branch = branch_result.scalar_one_or_none()

    return [_session_to_schema(s, branch) for s in sessions]


# GET /sessions/history

@router.get(
    "/sessions/history",
    response_model=SessionListResponse,
    status_code=status.HTTP_200_OK,
    summary="Paginated session history for current staff",
)
async def get_session_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> SessionListResponse:
    """
    Paginated list of all sessions (completed + abandoned) for current staff.
    Managers and Admins see all sessions in their branch.
    """

    offset = (page - 1) * page_size

    # Define visibility: Tellers only see their own; Managers/Admins see branch-wide
    is_management = current_staff.role in ["manager", "supervisor", "admin", "super_admin"]
    
    base_query = select(Session).where(
        Session.status.in_([SessionStatus.completed, SessionStatus.abandoned])
    )
    
    if is_management:
        # Managers/Admins see everything in their branch
        base_query = base_query.where(Session.branch_id == current_staff.branch_id)
    else:
        # Tellers only see their own
        base_query = base_query.where(Session.staff_id == current_staff.id)

    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        base_query.order_by(desc(Session.created_at))
        .offset(offset)
        .limit(page_size)
    )
    sessions = result.scalars().all()

    branch_result = await db.execute(
        select(Branch).where(Branch.id == current_staff.branch_id)
    )
    branch = branch_result.scalar_one_or_none()

    return SessionListResponse(
        sessions=[_session_to_schema(s, branch) for s in sessions],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


# GET /sessions/{token_number}

@router.get(
    "/sessions/{token_number}",
    response_model=SessionResponse,
    status_code=status.HTTP_200_OK,
    summary="Fetch session by token (no auth — customer access)",
)
async def get_session(
    token_number: str,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """
    Fetch session details by token number.

    No authentication required — allows the customer panel to load
    session info by scanning the QR code token.
    """
    result = await db.execute(
        select(Session).where(Session.token_number == token_number)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise SessionNotFoundError(token_number=token_number)

    branch_result = await db.execute(
        select(Branch).where(Branch.id == session.branch_id)
    )
    branch = branch_result.scalar_one_or_none()

    return _session_to_schema(session, branch)


# PATCH /sessions/{session_id}/end

@router.patch(
    "/sessions/{session_id}/end",
    response_model=SessionEndResponse,
    status_code=status.HTTP_200_OK,
    summary="End a session and trigger summary generation",
)
async def end_session(
    session_id: int,
    background_tasks: BackgroundTasks,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> SessionEndResponse:
    """
    Mark session as completed, calculate duration, queue PDF generation.
    """
    result = await db.execute(
        select(Session).where(Session.id == session_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise SessionNotFoundError(session_id=session_id)

    if session.status in (SessionStatus.completed, SessionStatus.abandoned):
        raise SessionAlreadyEndedError(
            token_number=session.token_number,
            current_status=session.status,
        )

    # Guard: teller/supervisor can only end own sessions;
    # manager can end any session in own branch; admin = unrestricted.
    check_cross_branch_session(current_staff, session)

    now = datetime.now(timezone.utc)
    started = session.started_at
    if started and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    duration = int((now - started).total_seconds()) if started else None

    session.status = SessionStatus.completed
    session.ended_at = now
    session.duration_seconds = duration

    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(status=SessionStatus.completed, ended_at=now, duration_seconds=duration)
    )
    await db.commit()

    # Remove Redis active session key
    try:
        await redis.delete(f"active_session:{session.token_number}")
    except Exception as exc:
        logger.warning("Redis delete on end failed: %s", exc)

    # Upsert analytics_daily row
    try:
        await _upsert_analytics_daily(db, session, now)
    except Exception as exc:
        logger.warning("Analytics upsert failed (non-fatal): %s", exc)

    # Broadcast session_ended via WebSocket to notify both staff and customer panels
    try:
        # Auto-farewell: send thank-you message before ending (non-blocking)
        await ws_manager._send_auto_farewell(session.token_number, session.id)

        await ws_manager.broadcast_session_ended(
            token_number=session.token_number,
            summary_url=None,
            duration_seconds=duration,
            total_exchanges=session.total_exchanges or 0,
            session_id=session.id,
            collected_data=session.collected_data or {},
            intent=str(session.intent_detected or "general"),
            language_code=session.customer_language_code or "hi",
        )
        logger.info("Broadcasted session_ended via WebSocket | token=%s", session.token_number)
    except Exception as ws_err:
        logger.warning("Failed to broadcast session_ended: %s", ws_err)

    # Queue PDF generation in background
    background_tasks.add_task(
        _generate_summary_background, session_id, session.token_number
    )

    logger.info(
        "Session ended | id=%d | token=%s | duration=%ss",
        session_id,
        session.token_number,
        duration,
    )

    return SessionEndResponse(
        session_id=session_id,
        token_number=session.token_number,
        status=SessionStatus.completed.value,
        duration_seconds=duration,
        message="Session ended. Summary is being generated.",
    )


# GET /sessions/{session_id}/collected-info

@router.get(
    "/sessions/{session_id}/collected-info",
    status_code=status.HTTP_200_OK,
    summary="Fetch accumulated AI-collected customer info for session restore",
)
async def get_session_collected_info(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the accumulated collected_info (AI-extracted entity data)
    stored in Session.collected_data.

    Used by the frontend to restore InfoBoard state after page refresh
    or WebSocket reconnect.
    """
    result = await db.execute(
        select(Session.collected_data, Session.intent_detected)
        .where(Session.id == session_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    collected = row.collected_data or {}
    intent = row.intent_detected or "general"

    # Calculate completion
    filled = sum(
        1 for v in collected.values()
        if v is not None and v != "" and v is not False
    )
    total = len(collected)
    completion_pct = int((filled / max(total, 1)) * 100)

    # DRV: Compute document readiness for restore
    doc_readiness = None
    if intent and intent.upper() not in ("GENERAL", ""):
        from services.document_service import compute_readiness
        try:
            doc_readiness = compute_readiness(intent, collected)
        except Exception as drv_exc:
            logger.warning("DRV restore computation failed | session=%d | %s", session_id, drv_exc)

    return {
        "session_id": session_id,
        "collected_info": collected,
        "completion_percent": completion_pct,
        "intent_detected": intent,
        "doc_readiness": doc_readiness,
    }


# WS /ws/{token_number}

@router.websocket("/ws/{token_number}")
async def websocket_endpoint(
    websocket: WebSocket,
    token_number: str,
    role: str = Query(..., description="staff or customer"),
    token: Optional[str] = Query(default=None, description="JWT — required for staff"),
) -> None:
    """
    WebSocket endpoint for real-time session communication.

    Query params:
      role=staff|customer
      token=<JWT>  (required for staff, optional for customer)

    Staff connections require a valid JWT.
    Customer connections only need a valid token_number with an active/waiting session.
    """
    # MUST accept() first — closing before accept causes 1006 on the client
    await websocket.accept()

    # Validate session exists
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Session).where(Session.token_number == token_number)
        )
        session = result.scalar_one_or_none()

    if session is None:
        await websocket.close(code=4004, reason="Session not found")
        return

    # Customer: session must be active or waiting
    if role == "customer" and session.status not in (SessionStatus.waiting, SessionStatus.active):
        await websocket.close(code=4003, reason="Session is no longer active")
        return

    # Staff: must present a valid JWT
    if role == "staff":
        if not token:
            await websocket.close(code=4001, reason="JWT required for staff")
            return
        try:
            verify_access_token(token)
        except Exception:
            await websocket.close(code=4001, reason="Invalid or expired JWT")
            return

    # Register connection with manager
    await ws_manager.connect(websocket, token_number, role)

    # Mark session active when staff connects + assign staff_id if unset
    if role == "staff" and session.status == SessionStatus.waiting:
        async with AsyncSessionLocal() as db:
            # Fetch JWT payload to get staff db id
            try:
                payload = verify_access_token(token)
                staff_db_id = payload.get("sub") if payload else None
                # sub is staff.id (integer stored as string in JWT)
                staff_db_id_int = int(staff_db_id) if staff_db_id else None
            except Exception:
                staff_db_id_int = None

            update_vals = {"status": SessionStatus.active}
            if staff_db_id_int and session.staff_id is None:
                update_vals["staff_id"] = staff_db_id_int

            await db.execute(
                update(Session)
                .where(Session.token_number == token_number)
                .values(**update_vals)
            )
            await db.commit()

    try:
        while True:
            raw = await websocket.receive()
            # Route binary frames (audio chunks) vs text frames (JSON events)
            if "bytes" in raw and raw["bytes"]:
                if role == "customer":
                    await ws_manager.handle_customer_audio_chunk(token_number, raw["bytes"])
            elif "text" in raw and raw["text"]:
                try:
                    data = json.loads(raw["text"])
                except Exception:
                    logger.warning("WS non-JSON text | token=%s | %s", token_number, raw["text"][:120])
                    continue
                if role == "staff":
                    await ws_manager.handle_staff_message(token_number, data)
                elif role == "customer":
                    await ws_manager.handle_customer_message(token_number, data)
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket, token_number, role)
    except Exception as exc:
        logger.error("WS error | token=%s | role=%s | %s", token_number, role, exc)
        await ws_manager.disconnect(websocket, token_number, role)


# GET /sessions/{session_id}/customer-profile

# GET /sessions/{session_id}/exchanges

@router.get(
    "/sessions/{session_id}/exchanges",
    status_code=status.HTTP_200_OK,
    summary="Fetch all exchanges for a session (browser-reload restore)",
)
async def get_session_exchanges(
    session_id: int,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Returns all exchanges for the session ordered by exchange_number.
    Called by the staff panel on mount / browser reload to restore the
    full conversation history from the database.
    """
    from models import Exchange as ExchangeModel

    session_result = await db.execute(
        select(Session).where(Session.id == session_id)
    )
    session_obj = session_result.scalar_one_or_none()
    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    exchange_result = await db.execute(
        select(ExchangeModel)
        .where(ExchangeModel.session_id == session_id)
        .order_by(ExchangeModel.exchange_number)
    )
    exchanges = exchange_result.scalars().all()

    def _serialize(ex: ExchangeModel) -> dict:
        return {
            "id":                        f"ex-db-{ex.id}",
            "exchange_id":               ex.id,
            "exchange_number":           ex.exchange_number,
            "direction":                 ex.direction,
            # Customer fields
            "text_original":             ex.customer_text_original,
            "text_translated":           ex.customer_text_translated,
            "customer_text_original":    ex.customer_text_original,
            "customer_text_translated":  ex.customer_text_translated,
            # Staff fields
            "staff_original_text":       ex.staff_response_final or ex.staff_response_suggested,
            "staff_response_final":      ex.staff_response_final,
            "staff_response_suggested":  ex.staff_response_suggested,
            "staff_response_translated": ex.staff_response_translated,
            "staff_translated_text":     ex.staff_response_translated,
            "staff_lang_name":           "Hindi",
            "customer_lang_name":        session_obj.customer_language or "Customer Language",
            "customer_lang_code":        session_obj.customer_language_code or "hi",
            # Metadata
            "sentiment":                 ex.sentiment,
            "intent":                    ex.intent,
            "confidence":                ex.stt_confidence,
            "pii_detected":              ex.pii_detected,
            "pii_masked_text":           ex.pii_masked_text,
            "staff_used_suggestion":     ex.staff_used_suggestion,
            "is_replay":                 True,
            "timestamp":                 ex.created_at.isoformat() if ex.created_at else None,
            "created_at":                ex.created_at.isoformat() if ex.created_at else None,
        }

    logger.info(
        "Exchanges fetched for restore | session=%d | count=%d",
        session_id, len(exchanges),
    )

    return {
        "session_id": session_id,
        "exchanges":  [_serialize(ex) for ex in exchanges],
        "total":      len(exchanges),
    }


@router.get(
    "/sessions/{session_id}/customer-profile",
    status_code=status.HTTP_200_OK,
    summary="Fetch customer profile for balance enquiry popup",
)
async def get_customer_profile(
    session_id: int,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Balance enquiry ke liye staff panel popup mein customer profile dikhata hai.

    PIILog table se is session ka account_number dhundh ke return karta hai.
    Agar account_number nahi mila toh session ki basic info return karta hai
    taaki popup hamesha kuch toh show kare.
    """
    from models import PIILog

    # Fetch session
    session_result = await db.execute(
        select(Session).where(Session.id == session_id)
    )
    session_obj = session_result.scalar_one_or_none()
    if session_obj is None:
        raise SessionNotFoundError(session_id=session_id)

    # Branch info
    branch_result = await db.execute(
        select(Branch).where(Branch.id == session_obj.branch_id)
    )
    branch = branch_result.scalar_one_or_none()

    # Fallback: look up account_number from PIILog (if not in Session)
    pii_result = await db.execute(
        select(PIILog).where(
            PIILog.session_id == session_id,
            PIILog.pii_type == "account_number",
        ).order_by(PIILog.detected_at.desc()).limit(1)
    )
    pii_log = pii_result.scalar_one_or_none()

    # Prioritize Session fields, fall back to PIILog
    account_number = (
        session_obj.customer_account_number
        or (pii_log.masked_value if pii_log else None)
    )

    # CBS lookup — fetch full profile via account_number or aadhaar
    from services.cbs_service import lookup_customer

    cbs_profile = lookup_customer(
        account_number=account_number,
        aadhaar_last4=session_obj.customer_aadhaar_last4,
        mobile=session_obj.customer_mobile_number,
    )

    # Merge CBS data with Session DB fields
    # CBS data overrides Session-captured fields (more accurate)
    if cbs_profile:
        # Save CBS fields to Session DB as well (for future refreshes)
        try:
            update_vals = {}
            if cbs_profile.get("full_name") and not session_obj.customer_name:
                update_vals["customer_name"] = cbs_profile["full_name"]
            if cbs_profile.get("mobile_number") and not session_obj.customer_mobile_number:
                update_vals["customer_mobile_number"] = cbs_profile["mobile_number"]
            if cbs_profile.get("account_type") and not session_obj.customer_account_type:
                update_vals["customer_account_type"] = cbs_profile["account_type"]
            if cbs_profile.get("kyc_status") and not session_obj.customer_kyc_status:
                update_vals["customer_kyc_status"] = cbs_profile["kyc_status"]
            if cbs_profile.get("balance") and not session_obj.customer_balance:
                update_vals["customer_balance"] = cbs_profile["balance"]
            if update_vals:
                await db.execute(
                    update(Session)
                    .where(Session.id == session_id)
                    .values(**update_vals)
                )
                await db.commit()
        except Exception as exc:
            logger.warning("CBS→DB sync failed | session=%d | %s", session_id, exc)

    logger.info(
        "Customer profile fetched | session=%d | cbs_hit=%s | account=%s",
        session_id,
        bool(cbs_profile),
        bool(account_number),
    )

    # No CBS data — return what we have from session
    if not cbs_profile:
        return {
            "session_id":        session_id,
            "token_number":      session_obj.token_number,
            "customer_language": session_obj.customer_language,
            "branch_name":       branch.branch_name if branch else "Union Bank of India",
            "branch_code":       branch.branch_code if branch else None,
            "intent_detected":   session_obj.intent_detected,
            "sentiment":         session_obj.sentiment_overall,
            "started_at":        session_obj.started_at.isoformat() if session_obj.started_at else None,
            "cbs_linked":        False,
            "account_number":    account_number,
            "mobile_number":     session_obj.customer_mobile_number,
            "customer_name":     session_obj.customer_name,
            "customer_dob":      session_obj.customer_dob,
            "customer_pan":      session_obj.customer_pan,
            "aadhaar_last4":     session_obj.customer_aadhaar_last4,
            "account_type":      session_obj.customer_account_type or "—",
            "kyc_status":        session_obj.customer_kyc_status or "Unknown",
            "balance":           session_obj.customer_balance,
            "linked_accounts":   None,
            "active_loans":      None,
            "active_fds":        None,
        }

    # Full CBS profile return
    return {
        # Session context
        "session_id":        session_id,
        "token_number":      session_obj.token_number,
        "customer_language": session_obj.customer_language,
        "branch_name":       cbs_profile.get("branch_name") or (branch.branch_name if branch else "Union Bank of India"),
        "branch_code":       branch.branch_code if branch else None,
        "intent_detected":   session_obj.intent_detected,
        "sentiment":         session_obj.sentiment_overall,
        "started_at":        session_obj.started_at.isoformat() if session_obj.started_at else None,
        "cbs_linked":        True,
        "_lookup_method":    cbs_profile.get("_lookup_method", "account_number"),

        # Identity (from CBS)
        "customer_id":       cbs_profile.get("customer_id"),
        "customer_name":     cbs_profile.get("full_name") or session_obj.customer_name,
        "full_name":         cbs_profile.get("full_name"),
        "dob":               cbs_profile.get("dob") or session_obj.customer_dob,
        "age":               cbs_profile.get("age"),
        "gender":            cbs_profile.get("gender"),
        "occupation":        cbs_profile.get("occupation"),
        "pan":               cbs_profile.get("pan") or session_obj.customer_pan,
        "aadhaar_masked":    cbs_profile.get("aadhaar_masked"),
        "mobile_number":     cbs_profile.get("mobile_number") or session_obj.customer_mobile_number,
        "email":             cbs_profile.get("email"),
        "address":           cbs_profile.get("address"),
        "city":              cbs_profile.get("city"),
        "state":             cbs_profile.get("state"),
        "pincode":           cbs_profile.get("pincode"),

        # Account details
        "account_number":    cbs_profile.get("account_number") or account_number,
        "account_type":      cbs_profile.get("account_type") or session_obj.customer_account_type,
        "ifsc_code":         cbs_profile.get("ifsc_code"),
        "account_opened":    cbs_profile.get("account_opened"),
        "last_txn_date":     cbs_profile.get("last_txn_date"),
        "balance":           cbs_profile.get("balance") or session_obj.customer_balance,
        "available_balance": cbs_profile.get("available_balance"),

        # KYC
        "kyc_status":        cbs_profile.get("kyc_status") or session_obj.customer_kyc_status or "Unknown",
        "kyc_expiry_date":   cbs_profile.get("kyc_expiry_date"),
        "kyc_mode":          cbs_profile.get("kyc_mode"),

        # Linked products
        "linked_accounts":   cbs_profile.get("linked_accounts"),
        "active_loans":      cbs_profile.get("active_loans"),
        "active_fds":        cbs_profile.get("active_fds"),
        "fd_maturing_soon":  cbs_profile.get("fd_maturing_soon", False),
        "debit_card":        cbs_profile.get("debit_card"),
        "net_banking":       cbs_profile.get("net_banking"),

        # Nominee
        "nominee_name":      cbs_profile.get("nominee_name"),
        "nominee_relation":  cbs_profile.get("nominee_relation"),

        # Risk & Compliance
        "risk_category":     cbs_profile.get("risk_category"),
        "cibil_score":       cbs_profile.get("cibil_score"),
        "pmjdy_account":     cbs_profile.get("pmjdy_account", False),
    }
