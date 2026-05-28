"""
VaaniBank AI — Pipeline Shared Helpers
PSBs Hackathon 2026 | Team Vectora

Internal helpers extracted from ai_pipeline.py to reduce file size and
improve testability. These functions are shared across STT, LLM, and
TTS endpoint handlers.

IMPORTANT: This module is internal to the routers package.
           Do NOT import it from outside routers/.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from websocket.manager import ws_manager

logger = logging.getLogger("vaanibank.pipeline_helpers")


# ══════════════════════════════════════════════════════════════════════════════
# LANGUAGE MAPPINGS
# ══════════════════════════════════════════════════════════════════════════════

# ── Language maps — imported from canonical source (core.language) ─────────────
from core.language import (
    LANG_CODE_TO_NAME as _LANG_NAMES,
    LANG_CODE_TO_ATTR as _LANG_ATTR_MAP,
    lang_code_to_name,
    lang_code_to_attr,
)


# ══════════════════════════════════════════════════════════════════════════════
# PROCESS STEP INITIALIZATION
# ══════════════════════════════════════════════════════════════════════════════

async def initialize_process_steps(
    session_id: int,
    token_number: str,
    intent_type: str,
    db: AsyncSession,
    customer_language_code: str = "hi",
) -> None:
    """
    Fetch process steps for the detected intent and create
    session_process_tracking rows if not already present.
    Then broadcast the first step via WebSocket in the customer's language.
    """
    from models import ProcessStep, SessionProcessTracking

    # Check if tracking already initialized
    existing = await db.execute(
        select(SessionProcessTracking).where(
            SessionProcessTracking.session_id == session_id
        ).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return

    steps_result = await db.execute(
        select(ProcessStep)
        .where(
            ProcessStep.intent_type == intent_type,
            ProcessStep.is_active == True,  # noqa: E712
        )
        .order_by(ProcessStep.step_number)
    )
    steps = steps_result.scalars().all()

    if not steps:
        return

    for step in steps:
        db.add(
            SessionProcessTracking(
                session_id=session_id,
                step_id=step.id,
                status="pending",
            )
        )
    await db.commit()

    total = len(steps)
    first_step = steps[0]

    lang_attr = f"step_text_{lang_code_to_attr(customer_language_code)}"
    step_text_customer = getattr(first_step, lang_attr, None) or first_step.step_text_hindi

    await ws_manager.broadcast_step_update(
        token_number=token_number,
        current_step=0,
        total_steps=total,
        progress_percentage=0.0,
        step_status="pending",
        step_text_hindi=first_step.step_text_hindi,
        step_text_customer=step_text_customer,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SMART INPUT TRIGGER — keyword detection → customer input popup
# ══════════════════════════════════════════════════════════════════════════════

# Maps keywords (lower-case) → (field_type, staff_label, customer_label)
INPUT_KEYWORD_MAP = [
    (
        ["aadhar", "aadhaar", "adhar", "aadhar number", "aadhaar number", "aadhar batao", "aadhaar de"],
        "aadhaar",
        "Aadhaar Number",
        "आधार नंबर",
    ),
    (
        ["pan", "pan card", "pan number", "pan batao", "pan card number"],
        "pan",
        "PAN Number",
        "PAN नंबर",
    ),
    (
        ["account number", "account no", "account batao", "khata number", "khata no"],
        "account_number",
        "Account Number",
        "खाता नंबर",
    ),
    (
        ["dob", "date of birth", "janm tithi", "janam tithi", "date of birth batao", "d.o.b"],
        "dob",
        "Date of Birth",
        "जन्म तिथि (DD/MM/YYYY)",
    ),
    (
        ["mobile", "mobile number", "phone number", "number batao", "phone batao", "mobile no"],
        "phone",
        "Mobile Number",
        "मोबाइल नंबर",
    ),
    (
        ["ifsc", "ifsc code"],
        "ifsc",
        "IFSC Code",
        "IFSC कोड",
    ),
]


async def trigger_input_request_if_needed(
    token_number: str,
    text: str,
) -> None:
    """
    Check transcribed speech for PII-related keywords.
    If found, send input_request WS event to customer panel.
    Best-effort — never raises.
    """
    if not text:
        return

    lower_text = text.lower()

    for keywords, field_type, field_label, field_label_customer in INPUT_KEYWORD_MAP:
        if any(kw in lower_text for kw in keywords):
            try:
                await ws_manager.broadcast_input_request(
                    token_number=token_number,
                    field_type=field_type,
                    field_label=field_label,
                    field_label_customer=field_label_customer,
                    request_id=str(uuid.uuid4())[:8],
                )
                logger.info(
                    "Smart input triggered | token=%s | field=%s",
                    token_number, field_type,
                )
            except Exception as exc:
                logger.warning(
                    "trigger_input_request_if_needed failed | token=%s | %s",
                    token_number, exc,
                )
            # First match only — one popup at a time
            break
