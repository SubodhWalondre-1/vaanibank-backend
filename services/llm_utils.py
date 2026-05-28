"""
VaaniBank AI — LLM Utilities
PSBs Hackathon 2026 | Team Vectora

Shared helpers for building conversation history and session state context
used by both PipelineOrchestrator and WebSocket Handlers.
"""

import logging
from typing import Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import Exchange

logger = logging.getLogger("vaanibank.llm_utils")

_INTENT_KEYWORDS: Dict[str, List[str]] = {
    "loan_enquiry": [
        "loan", "qarz", "karz", "udhar", "home loan",
        "personal loan", "mudra", "ghar lena",
    ],
    "account_opening": [
        "khata", "account", "kholna", "jan dhan", "pmjdy", "bachat",
    ],
    "balance_enquiry": [
        "balance", "paisa kitna", "statement", "kitna hai",
        "jama", "mini statement",
    ],
    "fixed_deposit": [
        "fd", "fixed deposit", "recurring", "invest", "miaadi",
    ],
    "kyc_update": [
        "kyc", "aadhaar update", "address change",
        "mobile number badal", "naam change",
    ],
    "card_services": [
        "card", "atm", "debit", "credit", "pin", "card band",
    ],
}

def pre_detect_intent(text: str) -> str:
    """Fast keyword-based intent pre-detection for LLM context seeding."""
    lower = text.lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return intent
    return "general"

async def fetch_history_rows(
    db: AsyncSession,
    session_id: int,
    current_exchange_id: Optional[int] = None,
):
    """Fetch last 10 exchanges for conversation context."""
    try:
        query = select(
            Exchange.customer_text_original,
            Exchange.customer_text_translated,
            Exchange.pii_masked_text,
            Exchange.staff_response_suggested,
            Exchange.staff_response_final,
        ).where(Exchange.session_id == session_id)
        
        if current_exchange_id:
            query = query.where(Exchange.id != current_exchange_id)
            
        result = await db.execute(
            query.order_by(Exchange.exchange_number.desc()).limit(10)
        )
        return list(reversed(result.fetchall()))
    except Exception as err:
        logger.warning("Failed to fetch conversation history: %s", err)
        return []

def build_conversation_history(
    rows,
    prev_collected: Dict,
) -> List[Dict[str, str]]:
    """Convert DB rows + collected context into LLM message history."""
    history: List[Dict[str, str]] = []

    for row in rows:
        customer_msg = (
            row.pii_masked_text
            or row.customer_text_original
            or row.customer_text_translated
        )
        if customer_msg:
            history.append({"role": "user", "content": customer_msg})

        if row.staff_response_final:
            history.append({"role": "assistant", "content": row.staff_response_final})
        elif row.staff_response_suggested:
            history.append({
                "role": "assistant",
                "content": f"[AI SUGGESTED — not yet confirmed by staff]: {row.staff_response_suggested}",
            })

    if prev_collected:
        ctx_lines = []
        for k, v in prev_collected.items():
            if v is not None and v != "" and v is not False:
                ctx_lines.append(f"  - {k}: {v}")
        if ctx_lines:
            ctx_block = (
                "[SYSTEM CONTEXT] PREVIOUSLY COLLECTED INFO "
                "(do NOT ask these again, carry forward in collected_info):\n"
                + "\n".join(ctx_lines)
            )
            history.insert(0, {"role": "user", "content": ctx_block})

    return history

def build_session_state_context(
    intent: str,
    collected_info: Dict,
    exchange_count: int,
) -> str:
    """Build session state context for LLM injection."""
    if not intent or intent.lower() in ("general", ""):
        return ""

    from services.document_service import compute_readiness
    from services.session_navigator import compute_next_actions

    readiness = compute_readiness(intent, collected_info)
    doc_score = readiness.get("score", 0)
    confirmed_docs = readiness.get("confirmed", 0)
    total_docs = readiness.get("total", 0)
    missing_doc_ids = readiness.get("missing", [])

    nav_state = compute_next_actions(
        intent=intent,
        collected_info=collected_info,
        doc_readiness_score=doc_score,
        conversation_stage="ready_to_apply" if exchange_count > 2 else "exploring",
        exchange_count=exchange_count,
    )
    phase = nav_state.get("phase", "collect")
    fill_pct = nav_state.get("fill_percent", 0)
    all_complete = nav_state.get("all_complete", False)
    next_q = nav_state.get("next_question")

    lines = [
        "[SYSTEM STATE — STAFF DASHBOARD CURRENT VIEW]",
        f"Intent: {intent}",
        f"Session Phase: {phase.upper()} ({nav_state.get('phase_label', '')})",
        f"Exchange Count: {exchange_count}",
        "",
        f"=== DOCUMENT READINESS: {doc_score}% ({confirmed_docs}/{total_docs} confirmed) ===",
    ]

    if missing_doc_ids:
        lines.append(f"MISSING required documents: {', '.join(missing_doc_ids)}")
    else:
        lines.append("ALL required documents are CONFIRMED ✓ — Do NOT ask about documents.")

    lines.append("")
    lines.append(f"=== INFORMATION COLLECTION: {fill_pct}% complete ===")

    if all_complete:
        lines.append("ALL required fields are FILLED ✓ — Do NOT ask for more info.")
    elif next_q:
        lines.append(f"NEXT field to ask: {next_q.get('label', '')} — {next_q.get('question_hi', '')}")

    return "\n".join(lines)
