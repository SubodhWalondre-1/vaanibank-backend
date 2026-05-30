"""
VaaniBank AI — SaralForm Submission Router
PSBs Hackathon 2026 | Team Vectora

Handles the SaralForm flow that sits BETWEEN session_ended and SummaryPage.
The customer reviews AI-collected fields, edits any errors, draws a
signature, then POSTs here.  On success:
  • collected_data in the Session row is updated with confirmed fields
  • form_signed_at timestamp is recorded
  • signature PNG is saved to ./storage/signatures/
  • WebSocket "form_signed" event is pushed to the staff panel

Endpoints:
  POST /forms/submit              — Customer submits reviewed + signed form
  GET  /forms/signature/{token}   — Staff downloads the signature PNG
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Session as SessionModel, Branch, StaffMember
from websocket.manager import ws_manager
from config import settings

# Module logger
logger = logging.getLogger("vaanibank.forms")

# Router
router = APIRouter(prefix="/forms", tags=["SaralForm"])

# Signature storage directory
# Configurable via environment variable; defaults to ./storage/signatures
# The directory is created on module load so the first write never fails.
_SIGNATURE_DIR: str = settings.SIGNATURE_STORAGE_PATH
os.makedirs(_SIGNATURE_DIR, exist_ok=True)

# Human-readable form name lookup
# Maps the short form reference codes (used in URLs and DB) to the full
# display names shown on the staff panel notification.
_FORM_NAMES: Dict[str, str] = {
    "A-101":  "Account Opening Form",
    "LA-201": "Loan Application Form",
    "KYC-07": "KYC Update Form",
    "CS-301": "Card Application Form",
    "FD-501": "Fixed Deposit Opening Form",
    "GQ-601": "General Query Log",
}

# Intent → form reference mapping
# When the frontend sends an intent string, we derive the form_ref from this
# table so the staff notification shows the correct document name.
_INTENT_TO_FORM_REF: Dict[str, str] = {
    "account_opening": "A-101",
    "loan_enquiry":    "LA-201",
    "kyc_update":      "KYC-07",
    "card_services":   "CS-301",
    "fixed_deposit":   "FD-501",
    "balance_enquiry": "GQ-601",
    "general":         "GQ-601",
}


# REQUEST / RESPONSE SCHEMAS

class FormSubmitRequest(BaseModel):
    """
    Payload sent by the customer panel when the SaralForm is submitted.

    Fields:
      token_number       — Session token (e.g. "NJT-1267"); used for WS routing
      session_id         — Database PK of the Session row
      form_ref           — Short form code (A-101 / LA-201 / KYC-07 …)
                           If omitted, derived from intent_detected.
      intent_detected    — LLM-detected intent; used to derive form_ref when
                           form_ref is not explicitly provided.
      confirmed_fields   — Key/value map of fields the customer confirmed or
                           corrected.  These are merged on top of the existing
                           session.collected_data (customer corrections win).
      signature_data_url — Base64-encoded PNG data URL produced by the HTML5
                           canvas ("data:image/png;base64,…").  Empty string
                           means the customer skipped signing (graceful).
      language_code      — BCP-47 language code of the customer (e.g. "hi",
                           "mr", "ta").  Forwarded in the WS event so the
                           staff panel can display language-aware labels.
    """

    token_number: str = Field(..., description="Session token number")
    session_id: int = Field(..., description="Database Session PK")
    form_ref: str = Field(
        default="",
        description="Short form reference code.  Auto-derived from intent_detected when empty.",
    )
    intent_detected: str = Field(
        default="general",
        description="LLM-detected intent; used when form_ref is empty.",
    )
    confirmed_fields: Dict[str, str] = Field(
        default_factory=dict,
        description="Customer-confirmed or -corrected field values.",
    )
    signature_data_url: str = Field(
        default="",
        description="Base64 PNG data URL of the drawn signature.  Empty = unsigned.",
    )
    language_code: str = Field(
        default="hi",
        description="BCP-47 customer language code.",
    )


class FormSubmitResponse(BaseModel):
    """Response returned to the customer panel after a successful form submit."""

    success: bool
    message: str
    form_ref: str
    token_number: str
    session_id: int
    signature_saved: bool = False
    redirect_to_summary: bool = True  # Tells the frontend to navigate to /summary/:id


# HELPERS

def _resolve_form_ref(form_ref: str, intent_detected: str) -> str:
    """
    Return the canonical form reference code.

    Priority:
      1. Use form_ref if the caller already provided a known code.
      2. Derive from intent_detected via _INTENT_TO_FORM_REF lookup.
      3. Fall back to "GQ-601" (General Query Log) if nothing matches.
    """
    if form_ref and form_ref in _FORM_NAMES:
        return form_ref

    # Normalise intent string (strip whitespace, lower-case) before lookup
    clean_intent = intent_detected.strip().lower()
    return _INTENT_TO_FORM_REF.get(clean_intent, "GQ-601")


def _save_signature(
    token_number: str, session_id: int, data_url: str
) -> Optional[str]:
    """
    Decode the base64 PNG data URL and write it to _SIGNATURE_DIR.

    Returns the *public URL path* (/forms/signature/{token}) on success,
    or None when data_url is empty / malformed.  Errors are logged as
    warnings (non-fatal) so the form submit can still succeed.
    """
    if not data_url or not data_url.startswith("data:image"):
        return None

    try:
        # Strict validation of token_number to prevent path traversal
        import re
        if not re.match(r"^[A-Za-z0-9_\-]+$", token_number):
            logger.warning("Potential path injection attempt blocked: %s", token_number)
            return None

        # data URL format: "data:<mime>;base64,<encoded>"
        _header, encoded = data_url.split(",", 1)
        signature_bytes: bytes = base64.b64decode(encoded)

        # File name: signature_<TOKEN>_<SESSION_ID>.png
        # Hyphens in the token are replaced with underscores so the filename
        # stays safe on all filesystems (Windows FAT32 allows hyphens but
        # some cloud storage providers encode them).
        safe_token = token_number.replace("-", "_")
        filename = f"signature_{safe_token}_{session_id}.png"
        
        # Ensure the filename has no path components
        filename = os.path.basename(filename)
        filepath = os.path.join(_SIGNATURE_DIR, filename)

        # Canonical path boundary check to guarantee it writes inside the designated folder
        real_dir = os.path.abspath(_SIGNATURE_DIR)
        real_filepath = os.path.abspath(filepath)
        if not real_filepath.startswith(real_dir + os.path.sep):
            logger.warning("Path boundary violation blocked: %s", filepath)
            return None

        with open(filepath, "wb") as fh:
            fh.write(signature_bytes)

        logger.info(
            "Signature saved | token=%s | session=%d | file=%s",
            token_number, session_id, filename,
        )
        # Public URL that the GET endpoint below can serve
        return f"/forms/signature/{token_number}"

    except Exception as exc:
        logger.warning(
            "Signature save failed (non-fatal) | token=%s | session=%d | error=%s",
            token_number, session_id, exc,
        )
        return None


# POST /forms/submit

@router.post(
    "/submit",
    response_model=FormSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Customer submits the reviewed and signed SaralForm",
    description=(
        "Merges confirmed_fields into session.collected_data, saves the signature PNG, "
        "records form_signed_at, and fires a 'form_signed' WebSocket event to the staff panel."
    ),
)
async def submit_form(
    body: FormSubmitRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> FormSubmitResponse:
    """
    SaralForm submission endpoint — the single HTTP call from the customer panel.

    Steps:
      1. Validate that the Session exists in the database.
      2. Resolve the canonical form_ref (from body or intent).
      3. Merge confirmed_fields on top of existing collected_data.
      4. Persist merged data + form_signed_at timestamp in one UPDATE.
      5. Save the signature PNG to disk (best-effort, non-fatal).
      6. Commit the DB transaction.
      7. Push "form_signed" WebSocket event to the staff panel.
      8. Return success response to the customer panel.
    """

    # Step 1: Validate Session
    result = await db.execute(
        select(SessionModel).where(SessionModel.id == body.session_id)
    )
    session_obj: Optional[SessionModel] = result.scalar_one_or_none()

    if session_obj is None:
        logger.warning(
            "Form submit rejected — session not found | session_id=%d | token=%s",
            body.session_id, body.token_number,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {body.session_id} not found.  "
                   f"The session may have already been purged.",
        )

    # Step 2: Resolve form reference
    form_ref: str = _resolve_form_ref(body.form_ref, body.intent_detected)
    form_name: str = _FORM_NAMES.get(form_ref, "Banking Form")

    logger.info(
        "Form submit received | token=%s | session=%d | form=%s | fields=%d",
        body.token_number, body.session_id, form_ref, len(body.confirmed_fields),
    )

    # Step 3: Merge confirmed_fields → collected_data
    # Customer corrections take priority over whatever the LLM extracted.
    # We do a shallow merge (top-level keys only); nested JSONB is preserved.
    existing_data: dict = session_obj.collected_data or {}
    merged_data: dict = {**existing_data, **body.confirmed_fields}

    # Step 4: Persist merged data + timestamp
    signed_at: datetime = datetime.now(timezone.utc)

    await db.execute(
        update(SessionModel)
        .where(SessionModel.id == body.session_id)
        .values(
            collected_data=merged_data,
            form_signed_at=signed_at,
        )
    )

    # Step 4b: Force PDF regeneration if summary exists
    # We reset the pdf_generated flag so the next Export PDF call generates
    # a fresh PDF with the latest collected_data and signature.
    from models import BilingualSummary
    await db.execute(
        update(BilingualSummary)
        .where(BilingualSummary.session_id == body.session_id)
        .values(pdf_generated=False)
    )

    # Step 5: Save signature PNG (best-effort)
    # We do this before commit so if it fails, we still have the data.
    # But it's independent of the DB transaction.
    signature_url: Optional[str] = _save_signature(
        token_number=session_obj.token_number,
        session_id=session_obj.id,
        data_url=body.signature_data_url,
    )
    signature_saved: bool = signature_url is not None

    # Step 6: Commit DB transaction
    await db.commit()

    # Step 6b: Proactively trigger PDF generation task
    # We do this AFTER commit so the background task can see the latest DB state
    # if it needs to fetch fallback data.
    try:
        # Fetch branch + staff for PDF (fresh after commit)
        async with get_db_context() as task_db:
            session_result = await task_db.execute(select(SessionModel).where(SessionModel.id == body.session_id))
            task_session_obj = session_result.scalar_one_or_none()
            
            if task_session_obj:
                branch_result = await task_db.execute(select(Branch).where(Branch.id == task_session_obj.branch_id))
                branch = branch_result.scalar_one_or_none()
                staff_result = await task_db.execute(select(StaffMember).where(StaffMember.id == task_session_obj.staff_id))
                staff_obj = staff_result.scalar_one_or_none()

                # Fetch existing summary text
                summary_result = await task_db.execute(select(BilingualSummary).where(BilingualSummary.session_id == body.session_id))
                summary_row = summary_result.scalar_one_or_none()
                
                if summary_row:
                    from routers.summary import _generate_pdf_task
                    
                    # Prepare form_details (same logic as summary.py)
                    form_details = (merged_data or {}).copy()
                    core_fields = {
                        "customer_name": task_session_obj.customer_name,
                        "account_number": task_session_obj.customer_account_number,
                        "mobile_number": task_session_obj.customer_mobile_number,
                        "date_of_birth": task_session_obj.customer_dob,
                        "pan_number": task_session_obj.customer_pan,
                        "aadhaar_last_4": task_session_obj.customer_aadhaar_last4,
                        "account_type": task_session_obj.customer_account_type,
                        "kyc_status": task_session_obj.customer_kyc_status,
                        "current_balance": task_session_obj.customer_balance,
                    }
                    for k, v in core_fields.items():
                        if v and k not in form_details:
                            form_details[k] = v

                    background_tasks.add_task(
                        _generate_pdf_task,
                        session_id=body.session_id,
                        token_number=body.token_number,
                        branch_name=branch.branch_name if branch else "Union Bank of India",
                        staff_name=staff_obj.full_name if staff_obj else "Staff",
                        customer_language=task_session_obj.customer_language or "Hindi",
                        intent_detected=task_session_obj.intent_detected,
                        sentiment_overall=task_session_obj.sentiment_overall,
                        started_at=task_session_obj.started_at,
                        ended_at=task_session_obj.ended_at,
                        duration_seconds=task_session_obj.duration_seconds,
                        summary_data={
                            "summary_hindi": summary_row.summary_hindi,
                            "summary_customer_lang": summary_row.summary_customer_lang,
                            "key_points_hindi": summary_row.key_points_hindi,
                            "key_points_customer": summary_row.key_points_customer,
                            "next_steps_hindi": summary_row.next_steps_hindi,
                            "next_steps_customer": summary_row.next_steps_customer,
                        },
                        collected_data=form_details,
                    )
                    logger.info("PDF regeneration task queued proactively | session=%d", body.session_id)
    except Exception as pdf_exc:
        logger.warning("Proactive PDF regeneration failed (non-fatal): %s", pdf_exc)

    logger.info(
        "Form data persisted | token=%s | session=%d | form=%s | "
        "fields_merged=%d | signature_saved=%s | signed_at=%s",
        body.token_number, body.session_id, form_ref,
        len(body.confirmed_fields), signature_saved, signed_at.isoformat(),
    )

    # Step 7: Notify staff panel via WebSocket
    # This is a fire-and-forget call.  If the staff WS is not connected the
    # event is silently dropped — the form data is already persisted in the DB
    # and the staff can still download the signature via GET /forms/signature/{token}.
    try:
        ws_delivered: bool = await ws_manager.send_to_staff(
            token_number=body.token_number,
            event_type="form_signed",
            data={
                "token_number":     body.token_number,
                "session_id":       body.session_id,
                "form_ref":         form_ref,
                "form_name":        form_name,
                "signature_url":    signature_url or "",
                "confirmed_fields": body.confirmed_fields,
                "language_code":    body.language_code,
                "submitted_at":     signed_at.isoformat(),
                # Human-readable toast message for the staff panel
                "message": (
                    f"Customer has reviewed and signed {form_name}. "
                    f"Token: {body.token_number}"
                ),
            },
        )

        if ws_delivered:
            logger.info(
                "form_signed WS event delivered | token=%s | form=%s",
                body.token_number, form_ref,
            )
        else:
            # Staff WS not connected — event dropped.  Not an error; the staff
            # can poll or the event will be visible in the DB.
            logger.warning(
                "form_signed WS event NOT delivered (staff WS absent) "
                "| token=%s | form=%s",
                body.token_number, form_ref,
            )

    except Exception as ws_exc:
        # WS failure must never roll back the DB commit.
        logger.error(
            "form_signed WS send failed (non-fatal) | token=%s | %s",
            body.token_number, ws_exc,
        )

    # Step 8: Return success to customer panel
    return FormSubmitResponse(
        success=True,
        message="Form submitted successfully. Returning to your session.",
        form_ref=form_ref,
        token_number=body.token_number,
        session_id=body.session_id,
        signature_saved=signature_saved,
        redirect_to_summary=False,
    )


# GET /forms/signature/{token_number}

@router.get(
    "/signature/{token_number}",
    summary="Staff downloads the customer's signature PNG",
    response_class=Response,
    responses={
        200: {
            "content": {"image/png": {}},
            "description": "PNG image of the drawn signature.",
        },
        404: {"description": "Signature not found for the given token."},
    },
)
async def get_signature(
    token_number: str,
    session_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Serve the signature PNG file for a given session token.

    Lookup strategy:
      1. If session_id is provided, look for the exact file
         signature_<TOKEN>_<SESSION_ID>.png.
      2. If session_id is omitted, scan the signatures directory and return
         the first file whose name starts with signature_<TOKEN>_.
         (Handles the case where the staff panel only has the token number.)

    The response is served with:
      • Content-Type: image/png
      • Content-Disposition: attachment (triggers browser download)
      • Cache-Control: no-store (signatures are PII — never cache)
    """
    # Strict validation of token_number to prevent path traversal
    import re
    if not re.match(r"^[A-Za-z0-9_\-]+$", token_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token number format.",
        )

    # Verify session exists in DB to break the taint trace from request parameters
    # Rather than using request parameters directly, we fetch the verified token/id from PostgreSQL.
    query = select(SessionModel).where(SessionModel.token_number == token_number)
    if session_id is not None:
        query = query.where(SessionModel.id == session_id)
    
    result = await db.execute(query)
    session_obj: Optional[SessionModel] = result.scalar_one_or_none()

    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Signature for token '{token_number}' not found. "
                "The customer may not have completed the SaralForm yet."
            ),
        )

    db_token = session_obj.token_number
    db_session_id = session_obj.id
    safe_token: str = db_token.replace("-", "_")

    # Attempt 1: exact file by session_id
    if session_id is not None:
        filepath = os.path.join(
            _SIGNATURE_DIR, f"signature_{safe_token}_{db_session_id}.png"
        )
        if not os.path.exists(filepath):
            filepath = None  # Fall through to directory scan
    else:
        filepath = None

    # Attempt 2: scan directory for any matching file
    if filepath is None:
        try:
            prefix = f"signature_{safe_token}_"
            for fname in os.listdir(_SIGNATURE_DIR):
                if fname.startswith(prefix) and fname.endswith(".png"):
                    filepath = os.path.join(_SIGNATURE_DIR, fname)
                    break
        except OSError as exc:
            logger.error("Signature directory scan failed: %s", exc)

    # 404 if still not found
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Signature for token '{token_number}' not found. "
                "The customer may not have completed the SaralForm yet."
            ),
        )

    # Verify canonical path boundary check to prevent directory traversal
    real_dir = os.path.abspath(_SIGNATURE_DIR)
    real_filepath = os.path.abspath(filepath)
    if not real_filepath.startswith(real_dir + os.path.sep):
        logger.warning("Path traversal read attempt blocked: %s", filepath)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    # Read and return the PNG
    with open(filepath, "rb") as fh:
        image_bytes: bytes = fh.read()

    logger.info(
        "Signature served | token=%s | file=%s | size=%d bytes",
        token_number, os.path.basename(filepath), len(image_bytes),
    )

    return Response(
        content=image_bytes,
        media_type="image/png",
        headers={
            # Filename in download dialog
            "Content-Disposition": (
                f'attachment; filename="signature_{token_number}.png"'
            ),
            # PII data — must not be cached anywhere in the chain
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
