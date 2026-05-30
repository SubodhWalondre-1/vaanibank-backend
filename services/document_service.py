"""
VaaniBank AI — Document Readiness Service
PSBs Hackathon 2026 | Team Vectora

Pure logic layer (no DB, no WebSocket) that:
  1. Builds a localized document checklist for a given intent
  2. Computes readiness score by cross-referencing collected_info
  3. Generates LLM context about missing documents
"""

from __future__ import annotations

import logging
import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("vaanibank.document_service")

# Load DOCUMENT_REGISTRY from config/document_registry.py
# The config/ directory is a data folder (no __init__.py), so we load via
# importlib.util — same strategy as the YAML config loader in ai_service.py.
_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "document_registry.py"

def _load_registry() -> dict:
    """Load DOCUMENT_REGISTRY from config/document_registry.py (cached)."""
    if not hasattr(_load_registry, "_cache"):
        spec = importlib.util.spec_from_file_location("document_registry", _REGISTRY_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _load_registry._cache = getattr(mod, "DOCUMENT_REGISTRY", {})
        logger.info("Document registry loaded: %d intents", len(_load_registry._cache))
    return _load_registry._cache


def _normalize_intent(intent: Optional[str]) -> str:
    """Normalize sub-intents (e.g. home_loan) to master intent registry keys."""
    if not intent:
        return "general"
    intent_lower = intent.strip().lower()
    mapping = {
        "home_loan": "loan_enquiry",
        "personal_loan": "loan_enquiry",
        "education_loan": "loan_enquiry",
        "vehicle_loan": "loan_enquiry",
        "mudra_loan": "loan_enquiry",
        "cibil_info": "general",
    }
    return mapping.get(intent_lower, intent_lower)


def build_checklist(
    intent: str,
    language_code: str,
    collected_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Build a localized document checklist for the customer panel.

    Returns a list of dicts, each with:
        id, label (in customer language), label_en, required, tag, confirmed
    """
    normalized_intent = _normalize_intent(intent)
    docs = _load_registry().get(normalized_intent, [])
    if not docs:
        return []

    lang = (language_code or "hi").split("-")[0]
    info = collected_info or {}

    checklist = []
    for doc in docs:
        # Determine if this doc is confirmed via collected_info
        confirmed = False
        ci_key = doc.get("collected_info_key")
        if ci_key and ci_key in info:
            val = info[ci_key]
            confirmed = _is_truthy(val)

        checklist.append({
            "id": doc["id"],
            "label": doc["labels"].get(lang, doc["label_en"]),
            "label_en": doc["label_en"],
            "required": doc["required"],
            "tag": doc.get("tag", ""),
            "confirmed": confirmed,
        })

    return checklist


def compute_readiness(
    intent: str,
    collected_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Cross-reference collected_info against required documents.

    Returns:
        {
            "intent": "loan_enquiry",
            "docs": [ {id, label_en, required, confirmed}, ... ],
            "total": 7,
            "confirmed": 3,
            "missing": ["salary_slips", "bank_statement", ...],
            "score": 43,
        }
    """
    normalized_intent = _normalize_intent(intent)
    docs = _load_registry().get(normalized_intent, [])
    if not docs:
        return {
            "intent": normalized_intent,
            "docs": [],
            "total": 0,
            "confirmed": 0,
            "missing": [],
            "score": 100,
        }

    info = collected_info or {}
    result_docs = []
    confirmed_count = 0
    missing_ids = []

    for doc in docs:
        ci_key = doc.get("collected_info_key")
        confirmed = False
        if ci_key and ci_key in info:
            confirmed = _is_truthy(info[ci_key])

        result_docs.append({
            "id": doc["id"],
            "label_en": doc["label_en"],
            "required": doc["required"],
            "confirmed": confirmed,
        })

        if confirmed:
            confirmed_count += 1
        elif doc["required"]:
            missing_ids.append(doc["id"])

    total = len(docs)
    score = int((confirmed_count / max(total, 1)) * 100)

    return {
        "intent": normalized_intent,
        "docs": result_docs,
        "total": total,
        "confirmed": confirmed_count,
        "missing": missing_ids,
        "score": score,
    }


def get_missing_doc_prompt_context(
    intent: str,
    collected_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generate a text block for LLM prompt injection listing unconfirmed
    required documents, so the LLM proactively asks about them.

    Returns empty string if no docs are missing or intent has no docs.
    """
    normalized_intent = _normalize_intent(intent)
    readiness = compute_readiness(normalized_intent, collected_info)
    if not readiness["missing"]:
        return ""

    # Build human-readable doc names from the missing IDs
    docs = _load_registry().get(normalized_intent, [])
    doc_map = {d["id"]: d["label_en"] for d in docs}

    missing_names = [
        doc_map.get(mid, mid) for mid in readiness["missing"]
    ]

    return (
        "\n=== DOCUMENT READINESS ALERT ===\n"
        f"Intent: {normalized_intent}\n"
        f"Documents confirmed: {readiness['confirmed']}/{readiness['total']}\n"
        f"MISSING required documents (customer has NOT confirmed these):\n"
        + "\n".join(f"  - {name}" for name in missing_names)
        + "\n\nIMPORTANT: If appropriate, ask the customer about ONE of these "
        "missing documents in your next_question. Phrase it naturally in Hindi: "
        "e.g. 'क्या आपके पास salary slips हैं?' or 'Property ke documents लाए हैं?'\n"
        "Do NOT ask about documents the customer has already confirmed.\n"
    )


# Internal helpers

def _is_truthy(val: Any) -> bool:
    """Check if a collected_info value means 'yes, provided'."""
    if val is None or val is False or val == "":
        return False
    if isinstance(val, str) and val.strip().lower() in ("null", "no", "false", "none", ""):
        return False
    return True
