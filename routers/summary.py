"""
VaaniBank AI — Summary, Process, Analytics & QR Router
PSBs Hackathon 2026 | Team Vectora

Endpoints:
  POST /summary/generate
  GET  /summary/session/{session_id}
  GET  /summary/{summary_id}/pdf
  POST /summary/{summary_id}/whatsapp
  GET  /process/steps/{intent_type}
  POST /process/step/complete
  GET  /process/session/{session_id}/progress
  GET  /analytics/branch/{branch_id}/today
  GET  /branches/{branch_code}/qr
"""

from __future__ import annotations

import io
import json
import logging
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from core.exceptions import ResourceNotFoundError, SessionNotFoundError
from core.security import get_current_staff
from core.guards import check_cross_branch_session, assert_own_branch
from database import get_db, get_redis
from models import (
    AnalyticsDaily,
    BilingualSummary,
    Branch,
    Exchange,
    ProcessStep,
    Session,
    SessionProcessTracking,
    StaffMember,
)
from schemas import (
    AnalyticsTodayResponse,
    BranchAnalyticsResponse,
    GenerateSummaryRequest,
    ProcessProgressResponse,
    ProcessStepCompleteRequest,
    ProcessStepCompleteResponse,
    ProcessStepsResponse,
    SummaryResponse,
    WhatsAppSendResponse,
)
from services.ai_service import ai_service
from services.pdf_service import pdf_service
from websocket.manager import ws_manager


# Module logger
logger = logging.getLogger("vaanibank.summary")

router = APIRouter(tags=["summary"])

# P3 N-MED-1: In-memory cache for process steps (static reference data)
# Key: "intent_type:language_code" → Value: (ProcessStepsResponse, timestamp)
_PROCESS_STEPS_CACHE: dict = {}
_PROCESS_STEPS_CACHE_TTL = 3600  # 1 hour in seconds

# POST /summary/generate

@router.post(
    "/summary/generate",
    response_model=SummaryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate bilingual summary + PDF for a completed session",
)
async def generate_summary(
    body: GenerateSummaryRequest,
    background_tasks: BackgroundTasks,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    """
    Build a bilingual summary (Hindi + customer language) from session exchanges.
    """
    try:
        # 1. Validate session
        session_result = await db.execute(
            select(Session).where(Session.id == body.session_id)
        )
        session_obj = session_result.scalar_one_or_none()
        if session_obj is None:
            raise SessionNotFoundError(session_id=body.session_id)

        # Guard: manager can only generate summaries for own-branch sessions
        check_cross_branch_session(current_staff, session_obj)
        
        # 2. Check if summary already exists and not forcing regeneration
        if not body.force_regenerate:
            existing_result = await db.execute(
                select(BilingualSummary).where(BilingualSummary.session_id == body.session_id)
            )
            existing = existing_result.scalar_one_or_none()
            if existing and existing.pdf_generated:
                # Only return early if a summary record already exists AND its PDF is generated.
                # If pdf_generated is False, it means either a generation is in progress
                # or the PDF is outdated (e.g. after a new form signature).
                logger.info("Summary and PDF already exist for session=%d | returning existing", body.session_id)
                return _summary_to_schema(existing)
            elif existing and not existing.pdf_generated:
                # If it exists but PDF is not generated, we should proceed to trigger the PDF task.
                # We'll continue below to regenerate everything to be safe.
                logger.info("Summary exists but PDF is missing/outdated for session=%d | regenerating", body.session_id)

        # 3. Fetch exchanges
        exchanges_result = await db.execute(
            select(Exchange)
            .where(Exchange.session_id == body.session_id)
            .order_by(Exchange.exchange_number)
        )
        exchanges = exchanges_result.scalars().all()

        customer_language = session_obj.customer_language or "Hindi"

        # 4. Call AI service for structured summary
        try:
            summary_data = await ai_service.summarize_session(
                exchanges=exchanges,
                customer_language=customer_language,
                intent_detected=session_obj.intent_detected,
            )
        except Exception as ai_err:
            logger.error("AI summarization failed for session=%d: %s", body.session_id, ai_err)
            # Fallback to empty summary instead of crashing
            summary_data = {
                "summary_hindi": ["सारांश तैयार नहीं किया जा सका।"],
                "summary_customer_lang": ["Summary could not be generated."],
                "key_points_hindi": [],
                "key_points_customer": [],
                "next_steps_hindi": [],
                "next_steps_customer": [],
            }

        # Fetch branch + staff for PDF
        branch_result = await db.execute(
            select(Branch).where(Branch.id == session_obj.branch_id)
        )
        branch = branch_result.scalar_one_or_none()

        staff_result = await db.execute(
            select(StaffMember).where(StaffMember.id == session_obj.staff_id)
        )
        staff_obj = staff_result.scalar_one_or_none()

        # 5. Upsert bilingual_summaries
        existing_result = await db.execute(
            select(BilingualSummary).where(
                BilingualSummary.session_id == body.session_id
            )
        )
        summary_row = existing_result.scalar_one_or_none()
        now = datetime.now(timezone.utc)

        if summary_row is None:
            summary_row = BilingualSummary(
                session_id=body.session_id,
                customer_language=customer_language,
                summary_hindi=summary_data.get("summary_hindi", []),
                summary_customer_lang=summary_data.get("summary_customer_lang", []),
                key_points_hindi=summary_data.get("key_points_hindi", []),
                key_points_customer=summary_data.get("key_points_customer", []),
                next_steps_hindi=summary_data.get("next_steps_hindi", []),
                next_steps_customer=summary_data.get("next_steps_customer", []),
                generated_at=now,
                pdf_generated=False,
            )
            db.add(summary_row)
        else:
            summary_row.summary_hindi = summary_data.get("summary_hindi", [])
            summary_row.summary_customer_lang = summary_data.get("summary_customer_lang", [])
            summary_row.key_points_hindi = summary_data.get("key_points_hindi", [])
            summary_row.key_points_customer = summary_data.get("key_points_customer", [])
            summary_row.next_steps_hindi = summary_data.get("next_steps_hindi", [])
            summary_row.next_steps_customer = summary_data.get("next_steps_customer", [])
            summary_row.generated_at = now
            summary_row.pdf_generated = False

        await db.commit()
        await db.refresh(summary_row)

        # 6. Background Task for PDF generation
        # Merge AI-extracted data with explicit customer columns for the "SaralForm" section
        form_details = (session_obj.collected_data or {}).copy()
        
        # Mapping for core customer details to be shown in the SaralForm table
        core_fields = {
            "customer_name": session_obj.customer_name,
            "account_number": session_obj.customer_account_number,
            "mobile_number": session_obj.customer_mobile_number,
            "date_of_birth": session_obj.customer_dob,
            "pan_number": session_obj.customer_pan,
            "aadhaar_last_4": session_obj.customer_aadhaar_last4,
            "account_type": session_obj.customer_account_type,
            "kyc_status": session_obj.customer_kyc_status,
            "current_balance": session_obj.customer_balance,
        }
        
        # Only add core fields if they are NOT already in collected_data (SaralForm data)
        # This ensures customer corrections in the SaralForm take priority.
        for k, v in core_fields.items():
            if v and k not in form_details:
                form_details[k] = v

        # Ensure we always have some summary data for the PDF
        pdf_summary_data = summary_data or {
            "summary_hindi": ["सारांश तैयार नहीं किया जा सका।"],
            "summary_customer_lang": ["Summary could not be generated."],
            "key_points_hindi": [],
            "key_points_customer": [],
            "next_steps_hindi": [],
            "next_steps_customer": [],
        }

        # This prevents the connection reset by returning the text summary immediately
        background_tasks.add_task(
            _generate_pdf_task,
            session_id=body.session_id,
            token_number=session_obj.token_number,
            branch_name=branch.branch_name if branch else "Union Bank of India",
            staff_name=staff_obj.full_name if staff_obj else "Staff",
            customer_language=customer_language,
            intent_detected=session_obj.intent_detected,
            sentiment_overall=session_obj.sentiment_overall,
            started_at=session_obj.started_at,
            ended_at=session_obj.ended_at,
            duration_seconds=session_obj.duration_seconds,
            summary_data=pdf_summary_data,
            collected_data=form_details,
        )

        logger.info("Summary text generated | session=%d | PDF queued in background", body.session_id)
        return _summary_to_schema(summary_row)

    except Exception as exc:
        logger.error("CRITICAL error in generate_summary: %s", exc, exc_info=True)
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An internal error occurred while generating the summary: {str(exc)}"
        )


async def _generate_pdf_task(
    session_id: int,
    token_number: str,
    branch_name: str,
    staff_name: str,
    customer_language: str,
    intent_detected: Optional[str],
    sentiment_overall: Optional[str],
    started_at: Optional[datetime],
    ended_at: Optional[datetime],
    duration_seconds: Optional[int],
    summary_data: dict,
    collected_data: Optional[dict] = None,
):
    """Background task to generate PDF and update database."""
    from database import get_db_context
    from models import Session as SessionModel

    # Robustness Fix: If collected_data is empty, try to fetch from DB
    if not collected_data:
        try:
            async with get_db_context() as db:
                session_result = await db.execute(
                    select(SessionModel).where(SessionModel.id == session_id)
                )
                session_obj = session_result.scalar_one_or_none()
                if session_obj:
                    collected_data = (session_obj.collected_data or {}).copy()
                    # Add core fields if missing
                    core_fields = {
                        "customer_name": session_obj.customer_name,
                        "account_number": session_obj.customer_account_number,
                        "mobile_number": session_obj.customer_mobile_number,
                        "date_of_birth": session_obj.customer_dob,
                        "pan_number": session_obj.customer_pan,
                        "aadhaar_last_4": session_obj.customer_aadhaar_last4,
                        "account_type": session_obj.customer_account_type,
                        "kyc_status": session_obj.customer_kyc_status,
                        "current_balance": session_obj.customer_balance,
                    }
                    for k, v in core_fields.items():
                        if v and k not in collected_data:
                            collected_data[k] = v
        except Exception as db_exc:
            logger.warning("Failed to fetch collected_data from DB in background task: %s", db_exc)

    try:
        # Call the sync PDF service
        pdf_url = pdf_service.generate_bilingual_summary(
            session_id=session_id,
            token_number=token_number,
            branch_name=branch_name,
            staff_name=staff_name,
            customer_language=customer_language,
            intent_detected=intent_detected,
            sentiment_overall=sentiment_overall,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
            summary_hindi=summary_data.get("summary_hindi", []),
            summary_customer_lang=summary_data.get("summary_customer_lang", []),
            key_points_hindi=summary_data.get("key_points_hindi", []),
            key_points_customer=summary_data.get("key_points_customer", []),
            next_steps_hindi=summary_data.get("next_steps_hindi", []),
            next_steps_customer=summary_data.get("next_steps_customer", []),
            collected_data=collected_data,
        )

        # Update DB using a new session context
        async with get_db_context() as db:
            await db.execute(
                update(BilingualSummary)
                .where(BilingualSummary.session_id == session_id)
                .values(pdf_url=pdf_url, pdf_generated=True)
            )
            await db.commit()
            logger.info("Background PDF generated successfully | session=%d | url=%s", session_id, pdf_url)

            # Notify staff panel via WebSocket
            await ws_manager.send_to_staff(
                token_number=token_number,
                event_type="pdf_ready",
                data={
                    "session_id": session_id,
                    "pdf_url": pdf_url,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.info("Sent pdf_ready WS event | token=%s", token_number)

    except Exception as e:
        logger.error("Background PDF task FAILED | session=%d | %s", session_id, e, exc_info=True)


# GET /summary/session/{session_id}

@router.get(
    "/summary/session/{session_id}",
    response_model=SummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Fetch summary for a session",
)
async def get_summary_by_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    """Return the bilingual summary for a session (no auth — customer can access)."""
    result = await db.execute(
        select(BilingualSummary).where(BilingualSummary.session_id == session_id)
    )
    summary = result.scalar_one_or_none()
    if summary is None:
        raise ResourceNotFoundError(resource="Summary", identifier=session_id)

    return _summary_to_schema(summary)


# GET /summary/{summary_id}/pdf

@router.get(
    "/summary/{summary_id}/pdf",
    summary="Get PDF status and download URL",
)
async def get_summary_pdf(
    summary_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Return the PDF status and URL if requested via JSON (e.g. API poll).
    Otherwise, redirect the browser to the actual PDF URL for direct viewing/downloading.
    """
    result = await db.execute(
        select(BilingualSummary).where(BilingualSummary.id == summary_id)
    )
    summary = result.scalar_one_or_none()
    if summary is None:
        raise ResourceNotFoundError(resource="Summary", identifier=summary_id)

    pdf_url = summary.pdf_url or ""
    if pdf_url.startswith("/storage/summaries/"):
        pdf_url = pdf_url.replace("/storage/summaries/", "/summaries/")

    # Check if this is a direct browser request for the PDF file or an API fetch
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header or "text/javascript" in accept_header:
        return {
            "summary_id": summary_id,
            "pdf_generated": summary.pdf_generated,
            "pdf_url": pdf_url,
        }

    # Otherwise, redirect to the actual PDF URL (R2 or local static route)
    if pdf_url.startswith("/"):
        base_url = str(request.base_url).rstrip("/")
        pdf_url = f"{base_url}{pdf_url}"

    return RedirectResponse(url=pdf_url)


# GET /summary/session/{session_id}/pdf/download  (CORS-safe proxy)

@router.get(
    "/summary/session/{session_id}/pdf/download",
    summary="Download PDF — fully self-sufficient (generates if needed)",
)
async def download_session_pdf(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    One-call PDF download.  Handles ALL edge cases internally:

    1. No BilingualSummary row  → generates AI summary + PDF on the fly.
    2. Summary exists but PDF not yet generated → generates PDF on the fly.
    3. PDF file missing from disk (Render ephemeral) → regenerates on the fly.
    4. PDF exists → streams it back with Content-Disposition: attachment.

    The frontend only needs to call this single endpoint.
    """
    import aiofiles as _aiofiles
    import httpx

    # 1. Validate session exists
    sess_result = await db.execute(
        select(Session).where(Session.id == session_id)
    )
    session_obj = sess_result.scalar_one_or_none()
    if session_obj is None:
        raise SessionNotFoundError(session_id=session_id)

    token_number = session_obj.token_number or str(session_id)
    filename = f"VaaniBank_Summary_{token_number}.pdf"

    # 2. Look up (or create) the summary row
    sum_result = await db.execute(
        select(BilingualSummary).where(BilingualSummary.session_id == session_id)
    )
    summary = sum_result.scalar_one_or_none()

    if summary is None:
        # No summary at all — generate AI summary first
        logger.info("No summary row for session=%d — generating AI summary + PDF on the fly", session_id)

        exchanges_result = await db.execute(
            select(Exchange)
            .where(Exchange.session_id == session_id)
            .order_by(Exchange.exchange_number)
        )
        exchanges = exchanges_result.scalars().all()
        customer_language = session_obj.customer_language or "Hindi"

        try:
            summary_data = await ai_service.summarize_session(
                exchanges=exchanges,
                customer_language=customer_language,
                intent_detected=session_obj.intent_detected,
            )
        except Exception as ai_err:
            logger.error("AI summarization failed for session=%d: %s", session_id, ai_err)
            summary_data = {
                "summary_hindi": ["सारांश तैयार नहीं किया जा सका।"],
                "summary_customer_lang": ["Summary could not be generated."],
                "key_points_hindi": [],
                "key_points_customer": [],
                "next_steps_hindi": [],
                "next_steps_customer": [],
            }

        now = datetime.now(timezone.utc)
        summary = BilingualSummary(
            session_id=session_id,
            customer_language=customer_language,
            summary_hindi=summary_data.get("summary_hindi", []),
            summary_customer_lang=summary_data.get("summary_customer_lang", []),
            key_points_hindi=summary_data.get("key_points_hindi", []),
            key_points_customer=summary_data.get("key_points_customer", []),
            next_steps_hindi=summary_data.get("next_steps_hindi", []),
            next_steps_customer=summary_data.get("next_steps_customer", []),
            generated_at=now,
            pdf_generated=False,
        )
        db.add(summary)
        await db.commit()
        await db.refresh(summary)

    # 3. Helper: collect form details for PDF
    def _build_form_details() -> dict:
        form_details = (session_obj.collected_data or {}).copy()
        core_fields = {
            "customer_name": session_obj.customer_name,
            "account_number": session_obj.customer_account_number,
            "mobile_number": session_obj.customer_mobile_number,
            "date_of_birth": session_obj.customer_dob,
            "pan_number": session_obj.customer_pan,
            "aadhaar_last_4": session_obj.customer_aadhaar_last4,
            "account_type": session_obj.customer_account_type,
            "kyc_status": session_obj.customer_kyc_status,
            "current_balance": session_obj.customer_balance,
        }
        for k, v in core_fields.items():
            if v and k not in form_details:
                form_details[k] = v
        return form_details

    # 4. Helper: generate or regenerate the PDF file
    def _generate_pdf() -> str:
        """Synchronously generate the PDF, return the storage URL/path."""
        branch_result_sync = None  # Will fetch outside
        # We already have session_obj, summary loaded

        return pdf_service.generate_bilingual_summary(
            session_id=session_id,
            token_number=token_number,
            branch_name=_branch_name,
            staff_name=_staff_name,
            customer_language=session_obj.customer_language or "Hindi",
            intent_detected=session_obj.intent_detected,
            sentiment_overall=session_obj.sentiment_overall,
            started_at=session_obj.started_at,
            ended_at=session_obj.ended_at,
            duration_seconds=session_obj.duration_seconds,
            summary_hindi=summary.summary_hindi or [],
            summary_customer_lang=summary.summary_customer_lang or [],
            key_points_hindi=summary.key_points_hindi or [],
            key_points_customer=summary.key_points_customer or [],
            next_steps_hindi=summary.next_steps_hindi or [],
            next_steps_customer=summary.next_steps_customer or [],
            collected_data=_build_form_details(),
        )

    # Fetch branch + staff names (needed for PDF generation)
    branch_result = await db.execute(
        select(Branch).where(Branch.id == session_obj.branch_id)
    )
    branch = branch_result.scalar_one_or_none()
    _branch_name = branch.branch_name if branch else "Union Bank of India"

    staff_result = await db.execute(
        select(StaffMember).where(StaffMember.id == session_obj.staff_id)
    )
    staff_obj = staff_result.scalar_one_or_none()
    _staff_name = staff_obj.full_name if staff_obj else "Staff"

    # 5. Try to load existing PDF bytes
    pdf_bytes: bytes | None = None

    if summary.pdf_generated and summary.pdf_url:
        pdf_url: str = summary.pdf_url
        # Normalise legacy paths
        if pdf_url.startswith("/storage/summaries/"):
            pdf_url = pdf_url.replace("/storage/summaries/", "/summaries/")

        if pdf_url.startswith("http://") or pdf_url.startswith("https://"):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(pdf_url)
                    resp.raise_for_status()
                    pdf_bytes = resp.content
            except Exception as fetch_err:
                logger.warning("Failed to fetch PDF from R2: %s | url=%s", fetch_err, pdf_url)
                local_path = Path(settings.SUMMARY_STORAGE_PATH) / pdf_url.rsplit("/", 1)[-1]
                if local_path.exists():
                    async with _aiofiles.open(str(local_path), "rb") as f:
                        pdf_bytes = await f.read()
        else:
            local_name = pdf_url.lstrip("/").replace("summaries/", "", 1)
            local_path = Path(settings.SUMMARY_STORAGE_PATH) / local_name
            if local_path.exists():
                async with _aiofiles.open(str(local_path), "rb") as f:
                    pdf_bytes = await f.read()

    # 6. Generate / regenerate if we still don't have bytes
    if not pdf_bytes:
        reason = "not yet generated" if not summary.pdf_generated else "file missing from storage"
        logger.info("PDF %s for session=%d — generating now", reason, session_id)
        try:
            new_pdf_url = _generate_pdf()

            # Update DB
            await db.execute(
                update(BilingualSummary)
                .where(BilingualSummary.session_id == session_id)
                .values(pdf_url=new_pdf_url, pdf_generated=True)
            )
            await db.commit()

            # Load the freshly generated PDF
            local_name = new_pdf_url.lstrip("/").replace("summaries/", "", 1)
            local_path = Path(settings.SUMMARY_STORAGE_PATH) / local_name
            if local_path.exists():
                async with _aiofiles.open(str(local_path), "rb") as f:
                    pdf_bytes = await f.read()
            elif new_pdf_url.startswith("http://") or new_pdf_url.startswith("https://"):
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(new_pdf_url)
                    resp.raise_for_status()
                    pdf_bytes = resp.content

        except Exception as gen_err:
            logger.error(
                "Failed to generate PDF for session=%d: %s",
                session_id, gen_err, exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"PDF generation failed: {str(gen_err)}",
            )

    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PDF was generated but could not be read from storage.",
        )

    logger.info("Serving PDF download | session=%d | bytes=%d", session_id, len(pdf_bytes))

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
        },
    )


# POST /summary/{summary_id}/whatsapp

# POST /summary/form-autofill    (P3: Form Auto-fill Export)

class FormAutofillRequest(BaseModel):
    """Request body for form auto-fill PDF generation."""
    session_id: int
    collected_info: dict = {}
    completion_percent: int = 0


@router.post(
    "/summary/form-autofill",
    status_code=status.HTTP_200_OK,
    summary="Generate auto-filled form PDF + JSON from AI-extracted data",
)
async def generate_form_autofill(
    body: FormAutofillRequest,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Generates a pre-filled banking form PDF from the AI-extracted customer
    data (collected_info) gathered during the conversation.

    Returns:
        - pdf_url: URL to download the generated PDF
        - json_export: The collected_info as a structured JSON object
        - form_ref: The applicable bank form reference number
    """
    # Fetch session
    result = await db.execute(
        select(Session).where(Session.id == body.session_id)
    )
    session_obj = result.scalar_one_or_none()
    if session_obj is None:
        raise SessionNotFoundError(session_id=body.session_id)

    # Fetch branch and staff info
    branch = None
    staff_name = "Staff"
    if session_obj.branch_id:
        branch_result = await db.execute(
            select(Branch).where(Branch.id == session_obj.branch_id)
        )
        branch = branch_result.scalar_one_or_none()
    if session_obj.staff_id:
        staff_result = await db.execute(
            select(StaffMember).where(StaffMember.id == session_obj.staff_id)
        )
        staff_obj = staff_result.scalar_one_or_none()
        if staff_obj:
            staff_name = staff_obj.full_name

    customer_language = session_obj.customer_language or "Hindi"
    intent = session_obj.intent_detected or "general"
    branch_name = branch.branch_name if branch else "Union Bank of India"

    # Form reference mapping
    form_refs = {
        "account_opening": "A-101 (Account Opening Form)",
        "loan_enquiry":    "LA-201 (Loan Application)",
        "kyc_update":      "KYC-07 (KYC Update Form)",
        "card_services":   "CS-301 (Card Application)",
        "fixed_deposit":   "FD-501 (FD Account Opening)",
        "general":         "GQ-601 (General Query Log)",
    }

    # Generate PDF
    pdf_url = pdf_service.generate_form_autofill(
        token_number=session_obj.token_number,
        branch_name=branch_name,
        staff_name=staff_name,
        intent_detected=intent,
        customer_language=customer_language,
        collected_info=body.collected_info,
        completion_percent=body.completion_percent,
    )

    logger.info(
        "Form auto-fill generated | session=%d | token=%s | completion=%d%%",
        body.session_id, session_obj.token_number, body.completion_percent,
    )

    return {
        "pdf_url": pdf_url,
        "token_number": session_obj.token_number,
        "form_ref": form_refs.get(intent, "GQ-601"),
        "intent": intent,
        "completion_percent": body.completion_percent,
        "json_export": body.collected_info,
    }


# POST /summary/{summary_id}/whatsapp

@router.post(
    "/summary/{summary_id}/whatsapp",
    response_model=WhatsAppSendResponse,
    status_code=status.HTTP_200_OK,
    summary="Queue WhatsApp summary send",
)
async def send_whatsapp_summary(
    summary_id: int,
    background_tasks: BackgroundTasks,
    phone_number: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppSendResponse:
    """
    Queue a WhatsApp message with the PDF summary link.

    WhatsApp delivery is a background task — returns immediately.
    In a production build this would call the WhatsApp Business API.
    """
    result = await db.execute(
        select(BilingualSummary).where(BilingualSummary.id == summary_id)
    )
    summary = result.scalar_one_or_none()
    if summary is None:
        raise ResourceNotFoundError(resource="Summary", identifier=summary_id)

    if not summary.pdf_generated or not summary.pdf_url:
        return WhatsAppSendResponse(
            summary_id=summary_id,
            queued=False,
            message="PDF not yet generated. Generate summary first.",
        )

    background_tasks.add_task(
        _send_whatsapp_background,
        summary_id=summary_id,
        pdf_url=summary.pdf_url,
        phone_number=phone_number,
    )

    return WhatsAppSendResponse(
        summary_id=summary_id,
        queued=True,
        message="WhatsApp summary queued (demo mode — requires WhatsApp Business API integration for production delivery).",
    )



async def _send_whatsapp_background(
    summary_id: int,
    pdf_url: str,
    phone_number: Optional[str],
) -> None:
    """Background task: send WhatsApp message (demo mode — uses WhatsApp Web URL)."""
    logger.info(
        "WhatsApp send | summary_id=%d | pdf=%s | phone=%s",
        summary_id,
        pdf_url,
        phone_number or "N/A",
    )

    if not phone_number:
        logger.warning("WhatsApp send aborted: No phone number provided for summary=%d", summary_id)
        return

    # 1. Clean and standardise the phone number
    clean_phone = "".join(filter(str.isdigit, phone_number))
    if len(clean_phone) == 10:
        clean_phone = f"+91{clean_phone}"
    elif len(clean_phone) == 12 and clean_phone.startswith("91"):
        clean_phone = f"+{clean_phone}"
    else:
        # If it has a different format (like with country code already), ensure + is prepended
        if not phone_number.startswith("+"):
            clean_phone = f"+{clean_phone}"
        else:
            clean_phone = f"+{clean_phone}"

    to_whatsapp = f"whatsapp:{clean_phone}"

    # 2. Load configurations
    from config import settings
    from database import AsyncSessionLocal
    from models import BilingualSummary

    # 3. Fetch summary details for template presentation
    summary_preview = ""
    customer_lang = "Customer Language"
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BilingualSummary).where(BilingualSummary.id == summary_id)
            )
            summary = result.scalar_one_or_none()
            if summary:
                customer_lang = summary.customer_language or "Customer Language"
                key_points = []
                if summary.key_points_hindi:
                    key_points.extend(summary.key_points_hindi[:2])
                if summary.key_points_customer:
                    key_points.extend(summary.key_points_customer[:2])
                summary_preview = "\n".join(f"• {pt}" for pt in key_points)
    except Exception as db_exc:
        logger.warning("Failed to fetch summary preview details (non-fatal): %s", db_exc)

    if not summary_preview:
        summary_preview = "• Bilingual transaction details ready."

    # 4. Format absolute PDF download link
    absolute_pdf_url = pdf_url
    if pdf_url.startswith("/"):
        base_url = settings.R2_PUBLIC_URL or "https://api.vaanibank.in"
        absolute_pdf_url = f"{base_url}{pdf_url}"

    # 5. Demo mode — log the message that would be sent
    logger.info(
        "WhatsApp delivery (demo mode) | to=%s | pdf=%s | preview=%s",
        to_whatsapp,
        absolute_pdf_url,
        summary_preview[:100],
    )

    # 6. Update database record status
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(BilingualSummary)
                .where(BilingualSummary.id == summary_id)
                .values(
                    whatsapp_sent=True,
                    whatsapp_sent_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
            logger.info("WhatsApp delivery status updated | summary_id=%d", summary_id)
    except Exception as exc:
        logger.error(
            "Failed to update WhatsApp status | summary_id=%d | %s",
            summary_id, exc,
        )


# GET /process/steps/{intent_type}

@router.get(
    "/process/steps/{intent_type}",
    response_model=ProcessStepsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get process steps for an intent type",
)
async def get_process_steps(
    intent_type: str,
    language_code: str = Query(default="hi"),
    db: AsyncSession = Depends(get_db),
) -> ProcessStepsResponse:
    """
    Return all active process steps for a given intent type,
    with text in the requested customer language.

    P3 N-MED-1: Results are cached in-memory for 1 hour since process
    steps are static reference data that rarely changes.
    """
    cache_key = f"{intent_type}:{language_code}"
    now = time.time()

    # Check in-memory cache (TTL = 1 hour)
    if cache_key in _PROCESS_STEPS_CACHE:
        cached_response, cached_at = _PROCESS_STEPS_CACHE[cache_key]
        if now - cached_at < _PROCESS_STEPS_CACHE_TTL:
            return cached_response

    result = await db.execute(
        select(ProcessStep)
        .where(
            ProcessStep.intent_type == intent_type,
            ProcessStep.is_active == True,  # noqa: E712
        )
        .order_by(ProcessStep.step_number)
    )
    steps = result.scalars().all()

    lang_attr = _lang_code_to_attr(language_code)
    step_list = []
    for step in steps:
        customer_text = getattr(step, f"step_text_{lang_attr}", None) or step.step_text_hindi
        step_list.append(
            {
                "id": step.id,
                "step_number": step.step_number,
                "step_text_hindi": step.step_text_hindi,
                "step_text_customer": customer_text,
                "speak_to_customer": step.speak_to_customer,
            }
        )

    response = ProcessStepsResponse(
        intent_type=intent_type,
        language_code=language_code,
        steps=step_list,
        total=len(step_list),
    )

    # Store in cache
    _PROCESS_STEPS_CACHE[cache_key] = (response, now)

    return response


# POST /process/step/complete

@router.post(
    "/process/step/complete",
    response_model=ProcessStepCompleteResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark a process step as completed",
)
async def complete_process_step(
    body: ProcessStepCompleteRequest,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> ProcessStepCompleteResponse:
    """
    Mark a SessionProcessTracking row as completed and broadcast step_updated.

    Resolves token_number from Session table when not supplied by frontend,
    so the WS broadcast always fires.
    """
    now = datetime.now(timezone.utc)

    # Resolve token_number from DB if not in request body
    token_number: Optional[str] = body.token_number
    if not token_number:
        session_result = await db.execute(
            select(Session.token_number).where(Session.id == body.session_id)
        )
        token_number = session_result.scalar_one_or_none()

    # Lazy-insert: ensure tracking rows exist
    existing_count_result = await db.execute(
        select(func.count(SessionProcessTracking.id)).where(
            SessionProcessTracking.session_id == body.session_id
        )
    )
    existing_count = existing_count_result.scalar() or 0

    if existing_count == 0:
        sess_result = await db.execute(
            select(Session).where(Session.id == body.session_id)
        )
        sess_obj = sess_result.scalar_one_or_none()
        intent = sess_obj.intent_detected if sess_obj else None

        if intent:
            steps_result = await db.execute(
                select(ProcessStep)
                .where(
                    ProcessStep.intent_type == str(intent),
                    ProcessStep.is_active == True,  # noqa: E712
                )
                .order_by(ProcessStep.step_number)
            )
            intent_steps = steps_result.scalars().all()
            for ps in intent_steps:
                db.add(SessionProcessTracking(
                    session_id=body.session_id,
                    step_id=ps.id,
                    status="pending",
                ))
            await db.flush()
            logger.info(
                "Lazy-inserted %d tracking rows via REST | session=%s | intent=%s",
                len(intent_steps), body.session_id, intent,
            )

    # Mark step completed
    await db.execute(
        update(SessionProcessTracking)
        .where(
            SessionProcessTracking.session_id == body.session_id,
            SessionProcessTracking.step_id == body.step_id,
        )
        .values(status="completed", completed_at=now)
    )
    await db.commit()

    # Count progress (reads committed data)
    total_result = await db.execute(
        select(func.count(SessionProcessTracking.id)).where(
            SessionProcessTracking.session_id == body.session_id
        )
    )
    total = total_result.scalar() or 0

    completed_result = await db.execute(
        select(func.count(SessionProcessTracking.id)).where(
            SessionProcessTracking.session_id == body.session_id,
            SessionProcessTracking.status == "completed",
        )
    )
    completed = completed_result.scalar() or 0

    progress = (completed / total * 100) if total else 0.0

    # Always broadcast step_updated via WebSocket
    if token_number:
        await ws_manager.broadcast_step_update(
            token_number=token_number,
            current_step=completed,
            total_steps=total,
            progress_percentage=progress,
            step_status="completed",
        )

    return ProcessStepCompleteResponse(
        session_id=body.session_id,
        step_id=body.step_id,
        current_step=completed,
        total_steps=total,
        progress_percentage=round(progress, 1),
    )


# GET /process/session/{session_id}/progress

@router.get(
    "/process/session/{session_id}/progress",
    response_model=ProcessProgressResponse,
    status_code=status.HTTP_200_OK,
    summary="Get process step progress for a session",
)
async def get_process_progress(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ProcessProgressResponse:
    """Return current step progress for a session."""
    total_result = await db.execute(
        select(func.count(SessionProcessTracking.id)).where(
            SessionProcessTracking.session_id == session_id
        )
    )
    total = total_result.scalar() or 0

    completed_result = await db.execute(
        select(func.count(SessionProcessTracking.id)).where(
            SessionProcessTracking.session_id == session_id,
            SessionProcessTracking.status == "completed",
        )
    )
    completed = completed_result.scalar() or 0

    progress = (completed / total * 100) if total else 0.0

    # Next pending step
    next_result = await db.execute(
        select(SessionProcessTracking)
        .where(
            SessionProcessTracking.session_id == session_id,
            SessionProcessTracking.status == "pending",
        )
        .order_by(SessionProcessTracking.id)
        .limit(1)
    )
    next_tracking = next_result.scalar_one_or_none()
    next_step_id: Optional[int] = next_tracking.step_id if next_tracking else None

    return ProcessProgressResponse(
        session_id=session_id,
        current_step=completed,
        total_steps=total,
        progress_percentage=round(progress, 1),
        next_step_id=next_step_id,
        is_complete=(completed == total and total > 0),
    )


# GET /analytics/branch/{branch_id}/today

@router.get(
    "/analytics/branch/{branch_id}/today",
    response_model=BranchAnalyticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Today's analytics for a branch",
)
async def get_branch_analytics_today(
    branch_id: int,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> BranchAnalyticsResponse:
    """Return today's analytics_daily record for the given branch."""
    # Guard: manager can only query own branch
    from models import StaffRole as _SR
    if current_staff.role not in (_SR.admin.value, _SR.manager.value):
        # Teller/supervisor: only their own branch
        assert_own_branch(current_staff, branch_id)
    elif current_staff.role == _SR.manager.value:
        assert_own_branch(current_staff, branch_id)
    # Use IST (UTC+5:30) for Indian banking day boundary — Render servers
    # run in UTC, so date.today() would split the Indian day incorrectly.
    _IST = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(_IST).date()

    result = await db.execute(
        select(AnalyticsDaily).where(
            AnalyticsDaily.branch_id == branch_id,
            AnalyticsDaily.date == today,
        )
    )
    analytics = result.scalar_one_or_none()

    # P3 D-HIGH-1: If no analytics_daily row exists, compute "live" analytics
    # using SQL aggregates instead of loading all sessions into memory.
    if analytics is None:
        start_dt = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=1)

        from sqlalchemy import case, literal_column

        # Single aggregate query — no Python-side iteration
        agg_result = await db.execute(
            select(
                func.count(Session.id).label("total"),
                func.count(case((Session.status == "completed", 1))).label("completed"),
                func.count(case((Session.status == "abandoned", 1))).label("abandoned"),
                func.count(case((Session.offline_mode == True, 1))).label("offline"),  # noqa: E712
                func.count(case((Session.pii_detected == True, 1))).label("pii"),  # noqa: E712
                func.coalesce(
                    func.avg(case(((Session.status == "completed") & (Session.duration_seconds <= 1800), Session.duration_seconds))),
                    0,
                ).label("avg_dur"),
            ).where(
                Session.branch_id == branch_id,
                Session.created_at >= start_dt,
                Session.created_at < end_dt,
            )
        )
        row = agg_result.one()

        total_sessions = row.total or 0
        completed_sessions = row.completed or 0
        abandoned_sessions = row.abandoned or 0
        offline_sessions = row.offline or 0
        pii_detected_count = row.pii or 0
        avg_duration_seconds = int(row.avg_dur or 0)

        # Language / intent / sentiment breakdowns via GROUP BY queries
        # These are lightweight since they return at most ~12 rows each
        lang_result = await db.execute(
            select(
                func.coalesce(Session.customer_language_code, literal_column("'hi'")).label("lc"),
                func.count(Session.id),
            ).where(
                Session.branch_id == branch_id,
                Session.created_at >= start_dt,
                Session.created_at < end_dt,
            ).group_by("lc")
        )
        languages_used = {r[0].strip(): r[1] for r in lang_result.all()}

        intent_result = await db.execute(
            select(
                func.coalesce(Session.intent_detected, literal_column("'general'")).label("it"),
                func.count(Session.id),
            ).where(
                Session.branch_id == branch_id,
                Session.created_at >= start_dt,
                Session.created_at < end_dt,
            ).group_by("it")
        )
        intents_breakdown = {r[0].strip(): r[1] for r in intent_result.all()}

        sentiment_result = await db.execute(
            select(
                func.coalesce(Session.sentiment_overall, literal_column("'calm'")).label("se"),
                func.count(Session.id),
            ).where(
                Session.branch_id == branch_id,
                Session.created_at >= start_dt,
                Session.created_at < end_dt,
            ).group_by("se")
        )
        sentiments_breakdown = {r[0].strip(): r[1] for r in sentiment_result.all()}

        return BranchAnalyticsResponse(
            branch_id=branch_id,
            date=today.isoformat(),
            total_sessions=total_sessions,
            completed_sessions=completed_sessions,
            abandoned_sessions=abandoned_sessions,
            avg_duration_seconds=avg_duration_seconds,
            languages_used=languages_used,
            intents_breakdown=intents_breakdown,
            sentiments_breakdown=sentiments_breakdown,
            offline_sessions=offline_sessions,
            pii_detected_count=pii_detected_count,
            # These three are tracked in analytics_daily only; keep 0 when row doesn't exist.
            ai_suggestion_used=0,
            ai_suggestion_edited=0,
            ai_suggestion_ignored=0,
        )

    return BranchAnalyticsResponse(
        branch_id=branch_id,
        date=today.isoformat(),
        total_sessions=analytics.total_sessions or 0,
        completed_sessions=analytics.completed_sessions or 0,
        abandoned_sessions=analytics.abandoned_sessions or 0,
        avg_duration_seconds=int(analytics.avg_duration_seconds or 0),
        languages_used=analytics.languages_used or {},
        intents_breakdown=analytics.intents_breakdown or {},
        sentiments_breakdown=analytics.sentiments_breakdown or {},
        offline_sessions=analytics.offline_sessions or 0,
        pii_detected_count=analytics.pii_detected_count or 0,
        ai_suggestion_used=analytics.ai_suggestion_used or 0,
        ai_suggestion_edited=analytics.ai_suggestion_edited or 0,
        ai_suggestion_ignored=analytics.ai_suggestion_ignored or 0,
    )


# GET /branches/{branch_code}/qr

@router.get(
    "/branches/{branch_code}/qr",
    summary="Generate static QR code PNG for a branch",
    response_class=Response,
)
async def get_branch_qr(
    branch_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Generate and return a QR code PNG for the branch's customer panel URL.

    Dynamically resolves the customer panel base URL:
      - Production: uses ALLOWED_ORIGINS (Netlify customer panel URL)
      - Development: falls back to http://localhost:5174

    No auth required — physical QR codes are publicly accessible.
    """
    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage
    except ImportError:
        try:
            import qrcode
        except ImportError as exc:
            raise ResourceNotFoundError(
                resource="QR library",
                identifier="qrcode package not installed",
            ) from exc

    result = await db.execute(
        select(Branch).where(Branch.branch_code == branch_code)
    )
    branch = result.scalar_one_or_none()
    if branch is None:
        raise ResourceNotFoundError(resource="Branch", identifier=branch_code)

    # Dynamic URL resolution — same pattern as session creation
    if settings.is_production:
        origins = settings.allowed_origins_list
        customer_base = next(
            (o for o in origins if "customer" in o),
            origins[0] if origins else f"{request.headers.get('x-forwarded-proto', 'https')}://{request.headers.get('host', 'localhost')}"
        )
    else:
        customer_base = "http://localhost:5174"

    qr_data = f"{customer_base}/?branch={branch_code}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#003087", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="qr_{branch_code}.png"',
            "Cache-Control": "public, max-age=86400",
        },
    )


# HELPERS

def _summary_to_schema(summary: BilingualSummary) -> SummaryResponse:
    return SummaryResponse(
        id=summary.id,
        session_id=summary.session_id,
        customer_language=summary.customer_language,
        summary_hindi=summary.summary_hindi or [],
        summary_customer_lang=summary.summary_customer_lang or [],
        key_points_hindi=summary.key_points_hindi or [],
        key_points_customer=summary.key_points_customer or [],
        next_steps_hindi=summary.next_steps_hindi or [],
        next_steps_customer=summary.next_steps_customer or [],
        pdf_url=summary.pdf_url,
        pdf_generated=summary.pdf_generated,
        whatsapp_sent=summary.whatsapp_sent,
        whatsapp_sent_at=summary.whatsapp_sent_at,
        generated_at=summary.generated_at,
    )


def _lang_code_to_attr(lang_code: str) -> str:
    if not lang_code:
        return "hindi"
    clean_code = lang_code.split("-")[0].strip().lower()
    mapping = {
        "hi": "hindi", "mr": "marathi", "ta": "tamil",
        "te": "telugu", "bn": "bengali", "kn": "kannada",
        "or": "odia", "pa": "punjabi",
        "gu": "gujarati", "ml": "malayalam",
    }
    return mapping.get(clean_code, "hindi")