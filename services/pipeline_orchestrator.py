"""
VaaniBank AI — Pipeline Orchestrator
PSBs Hackathon 2026 | Team Vectora

Extracted from ai_pipeline.py (P2 #9 + #10).
Encapsulates the shared transcription → LLM → persist → broadcast pipeline
used by both /stt/transcribe (staff-auth) and /stt/customer-transcribe (public).

The router endpoints now validate auth / session, then delegate to this service.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
import yaml

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from core.language import lang_code_to_name
from models import (
    Exchange,
    PIILog,
    ProcessStep,
    Session,
    SessionProcessTracking,
)
from services.ai_service import ai_service
from services.pii_service import pii_service
from services.storage_service import storage_service
from websocket.manager import ws_manager

logger = logging.getLogger("vaanibank.pipeline")


_SOURCE_SCRIPT_RANGES: Dict[str, tuple[str, str]] = {
    "ta": ("\u0B80", "\u0BFF"),
    "te": ("\u0C00", "\u0C7F"),
    "bn": ("\u0980", "\u09FF"),
    "kn": ("\u0C80", "\u0CFF"),
    "or": ("\u0B00", "\u0B7F"),
    "pa": ("\u0A00", "\u0A7F"),
    "gu": ("\u0A80", "\u0AFF"),
    "ml": ("\u0D00", "\u0D7F"),
}

_MARATHI_MARKERS = frozenset({
    "आहे", "आहेत", "मला", "माझा", "माझी", "माझे", "माझ्या",
    "पाहिजे", "हवे", "हवी", "हवं", "तुम्हाला", "कुठे", "कसे",
    "कसा", "कशी", "किती", "करायचे", "करायचा", "करायची",
    "घ्यायचे", "घ्यायचा", "नको", "होय", "थांबा", "सांगा",
    "शकतो", "शकता", "शकते", "भरू",
})

_MARATHI_TO_HINDI_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"(?<![\u0900-\u097F])मला(?![\u0900-\u097F])", "मुझे"),
    (r"(?<![\u0900-\u097F])माझा(?![\u0900-\u097F])", "मेरा"),
    (r"(?<![\u0900-\u097F])माझी(?![\u0900-\u097F])", "मेरी"),
    (r"(?<![\u0900-\u097F])माझे(?![\u0900-\u097F])", "मेरे"),
    (r"(?<![\u0900-\u097F])होमे(?![\u0900-\u097F])", "होम"),
    (r"होम\s+लोन", "होम लोन"),
    (r"(?<![\u0900-\u097F])कर्ज(?![\u0900-\u097F])", "लोन"),
    (r"(?<![\u0900-\u097F])पाहिजे(?![\u0900-\u097F])", "चाहिए"),
    (r"(?<![\u0900-\u097F])हवे(?![\u0900-\u097F])", "चाहिए"),
    (r"(?<![\u0900-\u097F])हवी(?![\u0900-\u097F])", "चाहिए"),
    (r"(?<![\u0900-\u097F])हवं(?![\u0900-\u097F])", "चाहिए"),
    (r"(?<![\u0900-\u097F])खाते(?![\u0900-\u097F])", "खाता"),
    (r"(?<![\u0900-\u097F])उघडायचे(?![\u0900-\u097F])", "खोलना है"),
    (r"(?<![\u0900-\u097F])उघडायचा(?![\u0900-\u097F])", "खोलना है"),
    (r"(?<![\u0900-\u097F])उघडायची(?![\u0900-\u097F])", "खोलना है"),
    (r"(?<![\u0900-\u097F])माहिती(?![\u0900-\u097F])", "जानकारी"),
    (r"(?<![\u0900-\u097F])सांगा(?![\u0900-\u097F])", "बताइए"),
    (r"(?<![\u0900-\u097F])हो(?![\u0900-\u097F])", "हाँ"),
    (r"(?<![\u0900-\u097F])भरू\s+शकतो(?![\u0900-\u097F])", "चुका सकता हूँ"),
    (r"(?<![\u0900-\u097F])भरू\s+शकते(?![\u0900-\u097F])", "चुका सकती हूँ"),
    (r"(?<![\u0900-\u097F])भरू\s+शकता(?![\u0900-\u097F])", "चुका सकते हैं"),
    (r"(?<![\u0900-\u097F])शकतो(?![\u0900-\u097F])", "सकता हूँ"),
    (r"(?<![\u0900-\u097F])शकते(?![\u0900-\u097F])", "सकती हूँ"),
    (r"(?<![\u0900-\u097F])शकता(?![\u0900-\u097F])", "सकते हैं"),
)


# ── Intent keyword detection (shared between both endpoints) ──────────────────

from services.llm_utils import (
    pre_detect_intent,
    fetch_history_rows,
    build_conversation_history,
    build_session_state_context,
)


# ── RAG Knowledge Base Loader ──────────────────────────────────────────────────

async def _load_ubi_knowledge_base(intent: str = "general") -> str:
    """Load and format relevant sections of the official UBI knowledge base."""
    kb_path = Path(__file__).parent.parent / "ubi_knowledge_base.yaml"
    if not kb_path.exists():
        return ""

    try:
        async with aiofiles.open(kb_path, mode="r", encoding="utf-8") as f:
            content = await f.read()
            kb_data = yaml.safe_load(content)
            
            lines = ["[OFFICIAL UNION BANK OF INDIA KNOWLEDGE BASE]"]
            
            # 1. KYC Standards (Relevant for almost everything)
            if "kyc_standards" in kb_data:
                lines.append("\n=== KYC STANDARDS (RBI/UBI) ===")
                ks = kb_data["kyc_standards"]
                lines.append(f"- Mandatory Baseline: {', '.join(ks.get('mandatory_baseline', []))}")
                lines.append(f"- Accepted POI: {', '.join(ks.get('proof_of_identity_poi', []))}")

            # 2. Intent-Specific Content
            if intent == "loan_enquiry":
                if "interest_rates" in kb_data:
                    lines.append("\n=== LOAN INTEREST RATES ===")
                    loans = kb_data["interest_rates"].get("loan_products", {})
                    for loan, info in loans.items():
                        lines.append(f"- {loan.replace('_', ' ').title()}: {info.get('range')}")
                        if info.get("lowest_rate_logic"):
                            lines.append(f"  * {info['lowest_rate_logic']}")
                
                if "eligibility" in kb_data:
                    lines.append("\n=== LOAN ELIGIBILITY ===")
                    for prod, info in kb_data["eligibility"].items():
                        if "loan" in prod:
                            lines.append(f"- {prod.replace('_', ' ').title()}: {info}")

                if "document_checklists" in kb_data:
                    lines.append("\n=== LOAN DOCUMENTS ===")
                    dc = kb_data["document_checklists"]
                    for prod, info in dc.items():
                        if "loan" in prod:
                            lines.append(f"- {prod.replace('_', ' ').title()}: {info}")

                if "service_charges" in kb_data:
                    lines.append("\n=== LOAN CHARGES & FEES ===")
                    fees = kb_data["service_charges"].get("processing_fees", {})
                    for fee, val in fees.items():
                        if "loan" in fee:
                            lines.append(f"- {fee.replace('_', ' ').title()}: {val}")

                if "penalties" in kb_data:
                    lines.append("\n=== LOAN PENALTIES ===")
                    for k, v in kb_data["penalties"].items():
                        if "payment" in k:
                            lines.append(f"- {k.replace('_', ' ').title()}: {v}")

            elif intent == "account_opening":
                if "savings_account_rules" in kb_data:
                    lines.append("\n=== SAVINGS ACCOUNT RULES ===")
                    sar = kb_data["savings_account_rules"]
                    lines.append(f"- Minimum Balance: {sar.get('qab_tiering', {}).get('with_cheque_book')}")
                    lines.append(f"- Penalties: {sar.get('shortfall_penalties', {}).get('structure')}")

            elif intent == "fixed_deposit":
                if "interest_rates" in kb_data:
                    lines.append("\n=== FD/RD INTEREST RATES ===")
                    deposits = kb_data["interest_rates"].get("deposit_products", {})
                    for dep, info in deposits.items():
                        lines.append(f"- {dep.replace('_', ' ').title()}: {info}")
                if "fixed_recurring_deposits" in kb_data:
                    lines.append("\n=== FD/RD RULES ===")
                    frd = kb_data["fixed_recurring_deposits"]
                    lines.append(f"- Peak Rate: {frd.get('interest_tiers', {}).get('peak_rate')}")
                    lines.append(f"- Senior Citizen: {frd.get('interest_tiers', {}).get('senior_citizen_premium')}")

            return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to load UBI knowledge base: %s", e)
        return ""


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    """Unified result returned by the orchestrator to the router."""
    exchange_id: int
    text_original: str
    text_translated: str
    masked_text: str
    confidence: float
    model_used: str
    language_detected: str
    pii_detected: bool
    pii_types: List[str]
    sentiment: str
    intent: str
    suggested_response_hindi: str
    suggested_response_customer_lang: str
    response_time_ms: int


# ══════════════════════════════════════════════════════════════════════════════
# CORE PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def run_transcription_pipeline(
    *,
    audio_bytes: bytes,
    session_id: int,
    token_number: str,
    language_code: str,
    exchange_number: Optional[int],
    db: AsyncSession,
    source_label: str = "staff",
) -> PipelineResult:
    """
    Execute the full transcription → LLM → persist → broadcast pipeline.

    This is the single source of truth for both /stt/transcribe and
    /stt/customer-transcribe.  The only differences (auth, STT error
    handling) are handled by the caller.

    Args:
        audio_bytes: Raw audio from the upload.
        session_id: Active session PK.
        token_number: Session token for WS routing.
        language_code: BCP-47 short code (e.g. 'hi', 'mr').
        exchange_number: Explicit exchange number or None for auto-detect.
        db: Active SQLAlchemy async session.
        source_label: 'staff' or 'customer' — used only for logging.
    """
    start_time = time.time()

    # ── 1. STT ────────────────────────────────────────────────────────────────
    stt_result = await ai_service.transcribe(
        audio_bytes=audio_bytes,
        language_code=language_code,
        session_id=session_id,
    )

    elapsed_ms = int((time.time() - start_time) * 1000)

    # ── 2. Determine exchange number ──────────────────────────────────────────
    if exchange_number is None:
        count_result = await db.execute(
            select(func.count(Exchange.id)).where(Exchange.session_id == session_id)
        )
        exchange_number = (count_result.scalar() or 0) + 1

    # ── 3. Save audio file ────────────────────────────────────────────────────
    audio_filename = f"cust_{session_id}_{exchange_number}_{int(time.time())}.wav"
    audio_url = await storage_service.upload_audio_bytes(audio_bytes, audio_filename)

    # ── 4. Save Exchange to DB ────────────────────────────────────────────────
    exchange = Exchange(
        session_id=session_id,
        exchange_number=exchange_number,
        direction="customer_to_staff",
        customer_audio_url=audio_url,
        customer_text_original=stt_result.text,
        pii_detected=stt_result.pii_detected,
        pii_masked_text=stt_result.masked_text if stt_result.pii_detected else None,
        stt_confidence=stt_result.confidence,
        stt_model_used=stt_result.model_used,
        response_time_ms=elapsed_ms,
        created_at=datetime.now(timezone.utc),
    )
    db.add(exchange)
    await db.flush()

    # ── 5. Log PII ────────────────────────────────────────────────────────────
    if stt_result.pii_detected:
        for pii_type in stt_result.pii_types:
            db.add(
                PIILog(
                    session_id=session_id,
                    exchange_id=exchange.id,
                    pii_type=pii_type,
                    masked_value="[masked]",
                    detected_at=datetime.now(timezone.utc),
                )
            )
        existing_pii_result = await db.execute(
            select(Session.pii_types_found).where(Session.id == session_id)
        )
        existing_pii = existing_pii_result.scalar() or []
        merged_pii = list(set(existing_pii + stt_result.pii_types))
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(pii_detected=True, pii_types_found=merged_pii)
        )

    # ── 6. Update session metadata ────────────────────────────────────────────
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(stt_model_used=stt_result.model_used, total_exchanges=exchange_number)
    )
    # Note: deferred commit — all DB writes (exchange, PII, session metadata,
    # LLM results, collected_info) are committed in a single round-trip below.

    # ── 7. Broadcast customer_speaking indicator ──────────────────────────────
    await ws_manager.broadcast_customer_speaking(token_number)

    # ── 8. LLM processing ────────────────────────────────────────────────────
    translation = stt_result.masked_text or stt_result.text
    sentiment = "calm"
    intent = "general"
    suggested_hindi = ""
    suggested_customer = ""

    try:
        # Fetch previously accumulated collected_info
        prev_collected = {}
        try:
            cd_result = await db.execute(
                select(Session.collected_data).where(Session.id == session_id)
            )
            prev_collected = cd_result.scalar() or {}
        except Exception:
            prev_collected = {}

        # Build conversation history
        conversation_history = build_conversation_history(
            await fetch_history_rows(db, session_id, exchange.id),
            prev_collected,
        )

        customer_text_for_ai = stt_result.masked_text or stt_result.text

        # Pre-detect intent from keywords
        pre_intent = pre_detect_intent(customer_text_for_ai)

        # ── RAG: Retrieve grounded banking knowledge for this query ──────────
        # Runs between pre_detect_intent() and process_with_llm() so the LLM
        # receives bank-policy context before generating any suggestion.
        # Uses masked_text to avoid sending PII to the embedding model.
        from services.rag_service import rag_service
        _rag_query = stt_result.masked_text or stt_result.text

        # Rewrite short follow-up queries using conversation history
        # e.g. "documents phir?" → "home loan 40 lakh ke liye kaunse documents chahiye"
        _rag_query = await rag_service.rewrite_query(
            current_query=_rag_query,
            conversation_history=conversation_history,
            intent=pre_intent,
        )

        _rag_result = await rag_service.retrieve(
            query=_rag_query,
            intent=pre_intent,
            product=prev_collected.get("loan_type") or prev_collected.get("account_type"),
        )
        rag_context_block = rag_service.format_context_for_llm(_rag_result)

        # ── Inject Official UBI Knowledge Base ───────────────────────────────
        _ubi_kb_ctx = await _load_ubi_knowledge_base(intent=pre_intent)
        if _ubi_kb_ctx:
            if not rag_context_block:
                rag_context_block = _ubi_kb_ctx
            else:
                rag_context_block = f"{_ubi_kb_ctx}\n\n{rag_context_block}"
        # ── End RAG ──────────────────────────────────────────────────────────

        # ── DRV Trigger A: Inject missing-document context into LLM ──────
        from services.document_service import get_missing_doc_prompt_context, compute_readiness
        doc_gap_ctx = get_missing_doc_prompt_context(pre_intent, prev_collected)
        if doc_gap_ctx:
            # FIX: conversation_history is List[Dict], not str — inject as system message
            conversation_history.append({"role": "user", "content": doc_gap_ctx})

        # ── Full Session State Injection: Give LLM awareness of InfoBoard,
        #    Process Panel, and Navigator phase so it doesn't repeat stale questions ──
        _session_state_ctx = build_session_state_context(
            intent=pre_intent,
            collected_info=prev_collected,
            exchange_count=exchange_number,
        )
        if _session_state_ctx:
            conversation_history.append({"role": "user", "content": _session_state_ctx})

        llm_result = await ai_service.process_with_llm(
            text=customer_text_for_ai,
            source_language=lang_code_to_name(language_code),
            detected_intent=pre_intent,
            conversation_history=conversation_history,
            rag_context=rag_context_block,   # grounded KB context injected here
        )

        # ═══ DEBUG: Trace InfoBoard pipeline ═══
        print(f"\n{'='*60}")
        print(f"[DEBUG] LLM completed successfully")
        print(f"[DEBUG] collected_info type: {type(llm_result.collected_info)}")
        print(f"[DEBUG] collected_info value: {llm_result.collected_info}")
        print(f"[DEBUG] collected_info is not None: {llm_result.collected_info is not None}")
        print(f"[DEBUG] next_question_hindi: '{llm_result.next_question_hindi}'")
        print(f"[DEBUG] intent: {llm_result.intent}")
        print(f"[DEBUG] conversation_stage: {llm_result.conversation_stage}")
        print(f"[DEBUG] prev_collected: {prev_collected}")
        print(f"{'='*60}\n")

        # Update exchange with translation + sentiment + intent
        await db.execute(
            update(Exchange)
            .where(Exchange.id == exchange.id)
            .values(
                customer_text_translated=llm_result.translation,
                staff_response_suggested=llm_result.suggested_response_hindi,
                staff_response_translated=llm_result.suggested_response_customer_lang,
                sentiment=llm_result.sentiment,
                intent=llm_result.intent,
            )
        )
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(
                sentiment_overall=llm_result.sentiment,
                intent_detected=llm_result.intent,
            )
        )

        # ── Conversation Intelligence board ───────────────────────────────────
        merged_info = None
        if llm_result.collected_info is not None or llm_result.next_question_hindi:
            merged_info = _merge_collected_info(prev_collected, llm_result.collected_info or {})
        elif prev_collected:
            # LLM returned no new fields — use prev_collected so InfoBoard stays populated
            merged_info = prev_collected
        if merged_info is not None:
            total_fields = len(merged_info)
            filled = sum(
                1 for v in merged_info.values()
                if v is not None and v is not False and v != ""
            )
            completion_pct = int((filled / max(total_fields, 1)) * 100)

            # Persist collected_info to DB (part of single commit below)
            await db.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(collected_data=merged_info)
            )

        # ── SINGLE COMMIT — all DB mutations in one round-trip (P2 B-HIGH-1) ──
        await db.commit()
        await db.refresh(exchange)

        # ── Verify LLM translation. If the model echoes the customer's source
        # language, force a dedicated source-language → Hindi translation before
        # the staff panel sees the red "Hindi" block.
        _hindi_translation = llm_result.translation
        source_lang_code = (language_code or "hi").split("-")[0].lower()
        if _needs_hindi_translation_retry(
            original_text=customer_text_for_ai,
            candidate_text=_hindi_translation,
            source_language_code=source_lang_code,
        ):
            logger.warning(
                "LLM translation still looks like source language (lang=%s) — forcing Hindi translate",
                source_lang_code,
            )
            try:
                _forced_hi = await ai_service.translate_text(
                    text=customer_text_for_ai,
                    target_language_code="hi",
                    source_language_code=source_lang_code,
                )
                if _forced_hi and not _needs_hindi_translation_retry(
                    original_text=customer_text_for_ai,
                    candidate_text=_forced_hi,
                    source_language_code=source_lang_code,
                ):
                    _hindi_translation = _forced_hi
            except Exception as _tr_exc:
                logger.warning("Forced Hindi translation failed (non-fatal): %s", _tr_exc)

        if _needs_hindi_translation_retry(
            original_text=customer_text_for_ai,
            candidate_text=_hindi_translation,
            source_language_code=source_lang_code,
        ):
            _fallback_hi = _fallback_source_to_hindi(
                customer_text_for_ai,
                source_lang_code,
            )
            if _fallback_hi:
                _hindi_translation = _fallback_hi
                logger.info(
                    "Hindi translation corrected with local fallback | token=%s | text=%s",
                    token_number, _hindi_translation[:60],
                )

        if _hindi_translation != llm_result.translation:
            await db.execute(
                update(Exchange)
                .where(Exchange.id == exchange.id)
                .values(customer_text_translated=_hindi_translation)
            )
            await db.commit()
            logger.info(
                "Hindi translation corrected | token=%s | text=%s",
                token_number, _hindi_translation[:60],
            )

        translation = _hindi_translation

        # ── Broadcast to staff panel ──────────────────────────────────────────
        await ws_manager.broadcast_transcription(
            token_number=token_number,
            text_original=stt_result.text,
            text_translated=_hindi_translation,
            confidence=stt_result.confidence,
            sentiment=llm_result.sentiment,
            intent=llm_result.intent,
            pii_detected=stt_result.pii_detected,
            exchange_id=exchange.id,
        )
        # ── Context-Aware Suggestion: Override LLM suggestion if navigator
        #    detects all fields collected + all docs confirmed ──────────────
        _final_hindi = llm_result.suggested_response_hindi
        _final_customer = llm_result.suggested_response_customer_lang
        _final_intent = llm_result.intent or pre_intent

        if _final_intent and _final_intent.upper() not in ("GENERAL", ""):
            try:
                _rd = compute_readiness(_final_intent, prev_collected)
                _doc_score = _rd.get("score", 0)
                _missing_docs = _rd.get("missing", [])

                from services.session_navigator import compute_next_actions as _cna
                _nav = _cna(
                    intent=_final_intent,
                    collected_info=prev_collected,
                    doc_readiness_score=_doc_score,
                    conversation_stage=llm_result.conversation_stage or "exploring",
                    exchange_count=exchange_number,
                )

                # If all fields filled AND all required docs confirmed,
                # override generic document-asking suggestions
                if _nav.get("all_complete") and _doc_score >= 100 and not _missing_docs:
                    _final_hindi = _override_suggestion_for_complete_state(
                        llm_hindi=llm_result.suggested_response_hindi,
                        intent=_final_intent,
                        nav_phase=_nav.get("phase", ""),
                        stt_text=stt_result.text,
                    )
                    logger.info(
                        "Suggestion overridden (all complete) | token=%s | phase=%s",
                        token_number, _nav.get("phase"),
                    )
            except Exception as ovr_exc:
                logger.debug("Suggestion override check failed (non-fatal): %s", ovr_exc)

        await ws_manager.broadcast_suggestion(
            token_number=token_number,
            suggested_hindi=_final_hindi,
            suggested_customer_lang=_final_customer,
            intent=llm_result.intent,
            process_triggered=llm_result.process_triggered,
            exchange_id=exchange.id,
        )

        # ── Broadcast info board update (after commit, so data is persisted) ──
        if merged_info is not None:
            # ── Deterministic Navigator: compute phase + next question ──
            from services.session_navigator import compute_next_actions
            from services.document_service import compute_readiness
            _nav_intent = llm_result.intent or pre_intent
            _doc_score = 0
            try:
                _rd = compute_readiness(_nav_intent, merged_info)
                _doc_score = _rd.get("score", 0)
            except Exception:
                pass

            nav_state = compute_next_actions(
                intent=_nav_intent,
                collected_info=merged_info,
                doc_readiness_score=_doc_score,
                conversation_stage=llm_result.conversation_stage or "exploring",
                exchange_count=exchange_number,
            )

            # Use deterministic next_question instead of LLM's (never repeats)
            det_next_hi = nav_state["next_question"]["question_hi"] if nav_state["next_question"] else None
            det_next_label = nav_state["next_question"]["label"] if nav_state["next_question"] else None

            await ws_manager.send_to_staff(
                token_number,
                "info_board_update",
                {
                    "collected_info": merged_info,
                    "completion_percent": completion_pct,
                    "next_question_hindi": det_next_hi or llm_result.next_question_hindi,
                    "next_question_customer_lang": llm_result.next_question_customer_lang,
                    "auto_step_completed": llm_result.auto_step_completed,
                    "exchange_number": exchange_number,
                    "conversation_stage": llm_result.conversation_stage,
                    "intent": _nav_intent,  # ← FIX: was missing; InfoTab needs this to pick correct INTENT_SCHEMA
                },
            )

            # ── Navigator state → staff panel ──
            await ws_manager.send_to_staff(
                token_number,
                "navigator_update",
                nav_state,
            )

            # ── AUTO TRIGGER: All info + docs collected → send customer notification ──
            # Fires once per session (Redis guard inside broadcast_all_info_collected)
            _all_complete = nav_state.get("all_complete", False)
            _doc_ready = _doc_score >= 80  # 80% docs = enough to start verification
            if _all_complete and _doc_ready:
                _lang = language_code or "hi"
                _nav_intent = llm_result.intent or pre_intent
                try:
                    await ws_manager.broadcast_all_info_collected(
                        token_number=token_number,
                        lang_code=_lang,
                        intent=_nav_intent,
                        session_id=session_id,
                    )
                    logger.info(
                        "Auto all_info_collected triggered | token=%s | intent=%s | doc_score=%d%%",
                        token_number, _nav_intent, _doc_score,
                    )
                except Exception as _aic_exc:
                    logger.warning(
                        "broadcast_all_info_collected failed (non-fatal) | token=%s | %s",
                        token_number, _aic_exc,
                    )

            # ── DRV Trigger B: Update doc readiness after each info_board_update ──
            _drv_intent = llm_result.intent or pre_intent
            if _drv_intent and _drv_intent.upper() not in ("GENERAL", ""):
                from services.document_service import build_checklist, compute_readiness
                try:
                    _readiness = compute_readiness(_drv_intent, merged_info)
                    await ws_manager.broadcast_doc_readiness(token_number, _readiness)
                    _checklist = build_checklist(_drv_intent, language_code, merged_info)
                    if _checklist:
                        await ws_manager.broadcast_document_checklist(
                            token_number, _drv_intent, _checklist, language_code,
                        )
                except Exception as drv_exc:
                    logger.debug("DRV trigger B failed (non-fatal): %s", drv_exc)

        # ── Auto-step completion ──────────────────────────────────────────────
        if llm_result.auto_step_completed:
            await _handle_auto_step_completion(
                db=db,
                session_id=session_id,
                token_number=token_number,
                intent=llm_result.intent or "general",
                auto_label=llm_result.auto_step_completed,
                source_label=source_label,
            )

        # ── Intent Engine PROCESS_UPDATE ──────────────────────────────────────
        if llm_result.intent and llm_result.intent.upper() not in ("GENERAL", ""):
            await _broadcast_intent_engine_update(
                stt_text=stt_result.text,
                language_code=language_code,
                token_number=token_number,
                source_label=source_label,
            )

        # ── PII alert ─────────────────────────────────────────────────────────
        if stt_result.pii_detected:
            await ws_manager.broadcast_pii_alert(
                token_number=token_number,
                pii_types=stt_result.pii_types,
                masked_text=stt_result.masked_text,
                exchange_id=exchange.id,
            )

        # ── Process step initialization ───────────────────────────────────────
        if llm_result.process_triggered:
            from routers._pipeline_helpers import initialize_process_steps
            await initialize_process_steps(
                session_id=session_id,
                token_number=token_number,
                intent_type=llm_result.intent,
                customer_language_code=language_code,
                db=db,
            )

            # ── DRV Trigger C: Send initial checklist when process starts ─────
            from services.document_service import build_checklist, compute_readiness
            try:
                _drv_i = llm_result.intent or "general"
                _cl = build_checklist(_drv_i, language_code, merged_info or prev_collected)
                if _cl:
                    await ws_manager.broadcast_document_checklist(
                        token_number, _drv_i, _cl, language_code,
                    )
                    _rd = compute_readiness(_drv_i, merged_info or prev_collected)
                    await ws_manager.broadcast_doc_readiness(token_number, _rd)
                    logger.info(
                        "DRV initial checklist sent | token=%s | intent=%s | docs=%d",
                        token_number, _drv_i, len(_cl),
                    )
            except Exception as drv_exc:
                logger.debug("DRV trigger C failed (non-fatal): %s", drv_exc)

        sentiment = llm_result.sentiment
        intent = llm_result.intent
        suggested_hindi = llm_result.suggested_response_hindi
        suggested_customer = llm_result.suggested_response_customer_lang

    except Exception as exc:
        import traceback
        print(f"\n{'='*60}")
        print(f"[DEBUG] ❌ PIPELINE EXCEPTION CAUGHT!")
        print(f"[DEBUG] Error type: {type(exc).__name__}")
        print(f"[DEBUG] Error message: {exc}")
        print(f"[DEBUG] Full traceback:")
        traceback.print_exc()
        print(f"{'='*60}\n")
        logger.warning("LLM processing failed during %s transcribe: %s", source_label, exc)
        await ws_manager.broadcast_transcription(
            token_number=token_number,
            text_original=stt_result.text,
            text_translated=translation,
            confidence=stt_result.confidence,
            sentiment="calm",
            intent="general",
            pii_detected=stt_result.pii_detected,
            exchange_id=exchange.id,
        )

    return PipelineResult(
        exchange_id=exchange.id,
        text_original=stt_result.text,
        text_translated=translation,
        masked_text=stt_result.masked_text,
        confidence=stt_result.confidence,
        model_used=stt_result.model_used,
        language_detected=stt_result.language_detected,
        pii_detected=stt_result.pii_detected,
        pii_types=stt_result.pii_types,
        sentiment=sentiment,
        intent=intent,
        suggested_response_hindi=suggested_hindi,
        suggested_response_customer_lang=suggested_customer,
        response_time_ms=elapsed_ms,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalise_for_retry_compare(text: str) -> str:
    """Keep only letters/digits so tiny punctuation or spacing shifts still match."""
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def _contains_script(text: str, language_code: str) -> bool:
    script_range = _SOURCE_SCRIPT_RANGES.get(language_code)
    if not script_range:
        return False
    start, end = script_range
    return any(start <= ch <= end for ch in text or "")


def _contains_marathi_markers(text: str) -> bool:
    tokens = set(re.findall(r"[\u0900-\u097F]+", text or ""))
    return bool(tokens & _MARATHI_MARKERS)


def _needs_hindi_translation_retry(
    *,
    original_text: str,
    candidate_text: str,
    source_language_code: str,
) -> bool:
    """Return True when the staff-facing Hindi translation is likely not Hindi."""
    source_language_code = (source_language_code or "hi").split("-")[0].lower()
    if source_language_code == "hi" or not (original_text or "").strip():
        return False
    if not (candidate_text or "").strip():
        return True

    original_norm = _normalise_for_retry_compare(original_text)
    candidate_norm = _normalise_for_retry_compare(candidate_text)
    if original_norm and candidate_norm:
        similarity = SequenceMatcher(None, original_norm, candidate_norm).ratio()
        if similarity >= 0.86:
            return True

    if source_language_code == "mr" and _contains_marathi_markers(candidate_text):
        return True

    return _contains_script(candidate_text, source_language_code)


def _fallback_source_to_hindi(text: str, source_language_code: str) -> Optional[str]:
    """Small offline safeguard for common demo banking phrases."""
    if (source_language_code or "").split("-")[0].lower() != "mr":
        return None
    if not _contains_marathi_markers(text):
        return None

    translated = text.strip()
    for pattern, replacement in _MARATHI_TO_HINDI_REPLACEMENTS:
        translated = re.sub(pattern, replacement, translated)
    translated = re.sub(r"\s+", " ", translated).strip()
    translated = translated.rstrip("।.!?") + "।"

    if translated == text.strip():
        return None
    return translated


# ── Stale-suggestion detection patterns ──────────────────────────────────────

_STALE_DOC_PATTERNS = [
    "aadhaar card", "aadhar card", "आधार कार्ड", "आधार",
    "pan card", "पैन कार्ड", "पॅन कार्ड",
    "documents", "दस्तावेज", "दस्तावेज़",
    "kya aapke paas", "क्या आपके पास",
    "laye hain kya", "लाए हैं क्या",
    "hai kya", "है क्या", "है ना",
]

_INTENT_COMPLETION_MESSAGES = {
    "loan_enquiry": (
        "जी बहुत अच्छा, आपकी सारी जानकारी और दस्तावेज़ मिल गए हैं। "
        "अब मैं आपका loan application form भर देता हूँ। बस 5-10 मिनट लगेंगे।"
    ),
    "account_opening": (
        "जी, आपकी सारी जानकारी ले ली है और सभी दस्तावेज़ मिल गए हैं। "
        "अब मैं आपका account opening form भर रहा हूँ।"
    ),
    "kyc_update": (
        "जी, आपके KYC update के लिए सब कुछ तैयार है। "
        "मैं अभी system में update कर देता हूँ।"
    ),
    "fixed_deposit": (
        "जी, FD के लिए सारी details मिल गई हैं। "
        "अब मैं FD opening form process कर देता हूँ।"
    ),
    "card_services": (
        "जी, card service request के लिए सब जानकारी ले ली है। "
        "अब मैं request process कर देता हूँ।"
    ),
    "balance_enquiry": (
        "जी, verification हो गया है। अब मैं आपका balance check करता हूँ।"
    ),
}


def _override_suggestion_for_complete_state(
    llm_hindi: str,
    intent: str,
    nav_phase: str,
    stt_text: str,
) -> str:
    """
    When all fields and documents are confirmed, check if the LLM suggestion
    is stale (asking about already-confirmed docs). If so, replace it with
    a contextually appropriate completion message.

    Only overrides if the LLM response contains stale document-asking patterns.
    If the LLM response is already contextually appropriate, it passes through.
    """
    hindi_lower = llm_hindi.lower()

    # Check if the LLM response contains stale document-asking patterns
    is_stale = any(pat in hindi_lower for pat in _STALE_DOC_PATTERNS)

    if not is_stale:
        # LLM response doesn't mention docs — probably already contextual
        return llm_hindi

    # Replace with intent-specific completion message
    intent_key = intent.lower()
    return _INTENT_COMPLETION_MESSAGES.get(
        intent_key,
        "जी, आपकी सारी जानकारी और दस्तावेज़ मिल गए हैं। "
        "अब मैं आगे की process शुरू करता हूँ।"
    )


def _merge_collected_info(
    prev: Dict, new: Dict
) -> Dict:
    """Merge new LLM-extracted info into previously collected info.

    Strategy:
    - Start with ALL keys from new (preserves schema including null fields)
    - Overlay with non-null values from prev (don't lose previously collected data)
    - Overlay with non-null values from new (accept new extractions)
    - Map fields to canonical SaralForm keys (e.g. net_salary → monthly_income)
    - ALWAYS carry forward customer_name across intent switches
    """
    # Start with the full schema from LLM (includes null placeholders)
    merged = {**new}
    # Overlay previously collected non-null values
    for k, v in prev.items():
        if v is not None and v != "" and v is not False:
            merged[k] = v
    # Overlay new non-null values (they take priority)
    for k, v in new.items():
        if v is not None and v != "" and v is not False:
            merged[k] = v

    # ── MAPPING: Sync LLM keys to SaralForm canonical keys ──
    # This ensures that fields extracted as 'monthly_income' also populate 'net_salary'
    # which is the key expected by LoanForm.jsx / FIELD_LABELS.
    MAPPINGS = {
        "monthly_income": "net_salary",
        "net_salary": "monthly_income",
        "age": "dob",  # LLM extracts age, Form expects dob (approx)
        "cibil_score": "credit_score",
        "existing_emis": "monthly_obligations",
        "monthly_obligations": "existing_emis",
        "applicant_age": "age",
    }
    for src, dst in MAPPINGS.items():
        if merged.get(src) and not merged.get(dst):
            merged[dst] = merged[src]
        elif merged.get(dst) and not merged.get(src):
            merged[src] = merged[dst]

    # CRITICAL: Always carry forward customer_name — it's intent-agnostic
    if prev.get("customer_name") and not merged.get("customer_name"):
        merged["customer_name"] = prev["customer_name"]
    return merged


async def _handle_auto_step_completion(
    *,
    db: AsyncSession,
    session_id: int,
    token_number: str,
    intent: str,
    auto_label: str,
    source_label: str,
) -> None:
    """Fuzzy-match auto_label against process steps and mark as completed."""
    try:
        label_lower = auto_label.lower().strip()

        steps_q = await db.execute(
            select(ProcessStep)
            .where(
                ProcessStep.intent_type == intent,
                ProcessStep.is_active == True,  # noqa: E712
            )
            .order_by(ProcessStep.step_number)
        )
        all_steps = steps_q.scalars().all()

        matched_step = None
        for ps in all_steps:
            step_text = (ps.step_text_hindi or "").lower()
            if label_lower in step_text or step_text in label_lower:
                matched_step = ps
                break
            auto_words = set(label_lower.split())
            step_words = set(step_text.split())
            if len(auto_words & step_words) >= 2:
                matched_step = ps
                break

        if not matched_step:
            return

        # Ensure tracking rows exist
        existing = await db.execute(
            select(func.count(SessionProcessTracking.id)).where(
                SessionProcessTracking.session_id == session_id
            )
        )
        if (existing.scalar() or 0) == 0:
            for ps in all_steps:
                db.add(SessionProcessTracking(
                    session_id=session_id,
                    step_id=ps.id,
                    status="pending",
                ))
            await db.flush()

        # Mark matched step completed
        await db.execute(
            update(SessionProcessTracking)
            .where(
                SessionProcessTracking.session_id == session_id,
                SessionProcessTracking.step_id == matched_step.id,
            )
            .values(status="completed", completed_at=datetime.now(timezone.utc))
        )

        # Count progress
        total_r = await db.execute(
            select(func.count(SessionProcessTracking.id)).where(
                SessionProcessTracking.session_id == session_id
            )
        )
        total = total_r.scalar() or 0
        done_r = await db.execute(
            select(func.count(SessionProcessTracking.id)).where(
                SessionProcessTracking.session_id == session_id,
                SessionProcessTracking.status == "completed",
            )
        )
        done = done_r.scalar() or 0
        progress = (done / total * 100) if total else 0.0

        await db.commit()

        await ws_manager.broadcast_step_update(
            token_number=token_number,
            current_step=done,
            total_steps=total,
            progress_percentage=progress,
            step_status="completed",
        )
        logger.info(
            "Auto-step completed (%s) | token=%s | step=%s | %d/%d",
            source_label, token_number,
            matched_step.step_text_hindi[:40], done, total,
        )

        # ── All steps completed → send verification time message ──────────
        if done == total and total > 0:
            try:
                from models import Session as SessionModel
                from database import AsyncSessionLocal
                async with AsyncSessionLocal() as db2:
                    sess_r = await db2.execute(
                        select(
                            SessionModel.intent_detected,
                            SessionModel.customer_language_code,
                        ).where(SessionModel.id == session_id)
                    )
                    sess_row = sess_r.one_or_none()
                if sess_row:
                    intent = (sess_row.intent_detected or "general").lower()
                    lang = sess_row.customer_language_code or "hi"
                    await ws_manager.send_verification_time_message(
                        token_number=token_number,
                        intent=intent,
                        lang_code=lang,
                        session_id=session_id,
                    )
            except Exception as vt_err:
                logger.warning(
                    "Verification time message after auto-step failed: %s", vt_err
                )
    except Exception as err:
        logger.warning("Auto-step completion failed (non-fatal): %s", err)


async def _broadcast_intent_engine_update(
    *,
    stt_text: str,
    language_code: str,
    token_number: str,
    source_label: str,
) -> None:
    """Call the intent_engine and broadcast process_update to staff."""
    try:
        from intent_engine import detect_intent

        ir = await detect_intent(
            text=stt_text,
            language_code=language_code,
            groq_api_key=settings.GROQ_API_KEY,
            groq_model=settings.GROQ_MODEL,
        )

        # Build key_entities dict (handle optional cibil_score attribute)
        key_entities = {
            "amount": ir.key_entities.amount,
            "tenure": ir.key_entities.tenure,
            "applicant_age": ir.key_entities.applicant_age,
            "income": ir.key_entities.income,
            "purpose": ir.key_entities.purpose,
        }
        if hasattr(ir.key_entities, "cibil_score"):
            key_entities["cibil_score"] = ir.key_entities.cibil_score

        await ws_manager.broadcast_process_update(
            token_number=token_number,
            intent=ir.intent,
            process_data=ir.process_data,
            staff_message=ir.staff_message,
            detected_language=ir.detected_language,
            key_entities=key_entities,
            key_info=ir.key_info,
            product_name=ir.product_name,
            tts_voice=ir.tts_voice,
            confidence=ir.confidence,
        )
    except Exception as err:
        logger.warning("PROCESS_UPDATE (%s) failed (non-fatal): %s", source_label, err)
