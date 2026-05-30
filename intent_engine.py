"""
VaaniBank AI — Intent Engine
PSBs Hackathon 2026 | Team Vectora

Takes customer speech text + language code, calls Groq LLM,
returns structured IntentResult with:
  - intent (normalised)
  - confidence
  - detected_language
  - staff_message (Hindi) — guide for the bank employee
  - customer_response (in customer language) — TTS text for customer panel
  - key_entities (amount, tenure, age, income, purpose, cibil_score)
  - sub_intent (new_enquiry / eligibility_check / document_submission / etc.)

Integrates with process_loader to attach full process JSON.

Usage:
    from intent_engine import detect_intent
    result = await detect_intent(text="50 lakh ka home loan chahiye", language_code="hi")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from language_config import (
    SUPPORTED_INTENTS,
    get_language_name,
    normalise_intent,
)
from process_loader import get_key_info, get_process_steps, load_process

logger = logging.getLogger("vaanibank.intent_engine")


# DATA CLASSES

@dataclass
class KeyEntities:
    amount:        Optional[str] = None
    tenure:        Optional[str] = None
    applicant_age: Optional[str] = None
    income:        Optional[str] = None
    purpose:       Optional[str] = None
    cibil_score:   Optional[str] = None   # NEW — customer's CIBIL score mention


@dataclass
class IntentResult:
    # Core intent
    intent:            str = "GENERAL"
    sub_intent:        str = "general"    # NEW — more specific sub-intent
    confidence:        float = 0.0
    detected_language: str = "hi"

    # Messages
    staff_message:      str = ""  # Hindi — shown on staff panel
    customer_response:  str = ""  # customer language — goes to TTS

    # Entities
    key_entities: KeyEntities = field(default_factory=KeyEntities)

    # Process data (from process_loader)
    process_data:  dict = field(default_factory=dict)   # full JSON
    process_steps: list = field(default_factory=list)   # process_steps list
    key_info:      dict = field(default_factory=dict)   # summary KPIs
    product_name:  str  = ""

    # TTS config
    tts_voice: str = "hi-IN"


# GROQ SYSTEM PROMPT  (v2 — entity-aware, actionable staff guidance)

_SYSTEM_PROMPT = """You are VaaniBank AI — an intelligent assistant for Union Bank of India frontline staff.
Customers may speak in ANY Indian language (Hindi, Tamil, Telugu, Gujarati, Marathi, Bengali, Malayalam, Kannada, Odia, Punjabi, English).

Your task is to analyse the customer's message and return a single valid JSON object.

=== RULES ===

1. INTENT — detect from this EXACT list only:
   HOME_LOAN | PERSONAL_LOAN | EDUCATION_LOAN | VEHICLE_LOAN |
   FIXED_DEPOSIT | ACCOUNT_OPENING | CIBIL_INFO | GENERAL

2. SUB_INTENT — narrow down what the customer wants to do:
   For loans:   "new_enquiry" | "eligibility_check" | "document_submission" | "status_check" | "emi_query"
   For account: "new_opening" | "type_selection" | "kyc_verification" | "zero_balance"
   For FD:      "new_fd" | "renewal" | "premature_withdrawal" | "rate_inquiry"
   For CIBIL:   "score_check" | "improvement_tips" | "report_query"
   Default:     "general"

3. STAFF_MESSAGE — Hindi mein SHORT + SPECIFIC + ACTIONABLE guidance.
   CRITICAL RULES for staff_message:
   a) If entities are detected (amount/tenure/income/age), make sure to MENTION them.
   b) Tell the staff member WHAT TO DO NEXT — give step-by-step instructions.
   c) If income is mentioned — estimate loan eligibility (approx 40-50x monthly).
   d) If CIBIL is mentioned — state whether they are eligible or not (700+ = eligible).
   e) If a specific purpose is mentioned — highlight the relevant product.

   GOOD examples:
   - Customer said: "50 lakh home loan 20 saal":
     "Customer 50 lakh ka 20-saal Home Loan chahte hain. EMI approx Rs.38,500/month. Pehle CIBIL check karo (min 700), phir income proof maango. Eligibility form fill karo."
   - Customer said: "80,000 salary hai, ghar lena hai":
     "Income Rs.80,000/month — Home Loan eligibility approx Rs.32-40 lakh. CIBIL confirm karo, phir suitable property value range batao customer ko."
   - Customer said: "khata kholna hai, zero balance":
     "Customer zero-balance account chahte hain. BSBD ya Jan Dhan recommend karo. Aadhaar + PAN maango. eKYC se 15 min mein ho sakta hai."
   - No entities: "Customer home loan enquiry kar rahe hain. Profile samjho — income kitni, property decide ki kya, CIBIL score pata hai?"

   BAD (too generic — AVOID):
   - "Customer home loan ke liye enquiry kar rahe hain. Pehle eligibility check karein."

4. CUSTOMER_RESPONSE — customer ki APNI LANGUAGE mein friendly 1-2 sentences.
   Acknowledge what they said and reassure them.

5. KEY_ENTITIES — extract ALL mentioned values. Use null if not present.
   - amount: loan/deposit amount (e.g. "50 lakh", "500000", "2 crore")
   - tenure: duration (e.g. "20 saal", "60 months", "5 years")
   - applicant_age: age (e.g. "35", "35 saal")
   - income: monthly/annual income (e.g. "80000", "1 lakh/month", "12 LPA")
   - purpose: specific use (e.g. "ghar kharidna", "car", "beti ki padhai", "second hand car")
   - cibil_score: if mentioned (e.g. "750", "score 780 hai")

6. Return ONLY valid JSON — no markdown, no backticks, no explanations.

=== JSON FORMAT ===
{
  "intent": "HOME_LOAN",
  "sub_intent": "new_enquiry",
  "confidence": 0.95,
  "detected_language": "hi",
  "staff_message": "Customer 50 lakh ka 20-saal Home Loan chahte hain. EMI approx Rs.38,500. CIBIL check karo pehle (min 700), phir income documents maango.",
  "customer_response": "Aapka home loan request samajh aaya. Hum aapki poori madad karenge!",
  "key_entities": {
    "amount": "50 lakh",
    "tenure": "20 saal",
    "applicant_age": null,
    "income": null,
    "purpose": "ghar kharidna",
    "cibil_score": null
  }
}"""


# GROQ CALL

async def _call_groq(text: str, language_code: str, api_key: str, model: str) -> dict:
    """
    Call Groq LLM via the shared ai_service AsyncGroq client for connection
    pooling, or fall back to raw httpx if ai_service is unavailable.
    Raises RuntimeError on parse failure.
    """
    lang_name = get_language_name(language_code)
    user_prompt = (
        f"Customer language: {lang_name} ({language_code})\n"
        f"Customer message: {text}"
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]

    raw_content: str = ""
    try:
        # Prefer shared AsyncGroq client from ai_service (connection-pooled)
        from services.ai_service import ai_service as _ai
        import httpx as _httpx
        completion = await _ai._call_groq_with_fallback(
            model=model,
            max_tokens=700,
            temperature=0.15,
            messages=messages,
            timeout=_httpx.Timeout(15.0),
        )
        raw_content = completion.choices[0].message.content or ""
    except Exception as _svc_err:
        logger.warning("ai_service Groq client failed, falling back to httpx: %s", _svc_err)
        # Fallback: raw httpx call (original behavior)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       model,
                    "max_tokens":  700,
                    "temperature": 0.15,
                    "messages":    messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"]

    # Strip markdown code fences if present
    clean = raw_content.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError as exc:
        logger.warning("Groq JSON parse failed: %s | raw: %s", exc, raw_content[:300])
        raise RuntimeError(f"JSON parse failed: {exc}")


# PUBLIC API

async def detect_intent(
    text:          str,
    language_code: str,
    groq_api_key:  str,
    groq_model:    str = "llama-3.3-70b-versatile",
) -> IntentResult:
    """
    Main function: detect intent from customer text.

    1. Call Groq LLM v2 — entity-aware, sub-intent, actionable staff guidance
    2. Load process JSON from process_loader
    3. Attach process_steps + key_info to result
    4. Return complete IntentResult

    On any error, returns a safe GENERAL fallback so the pipeline never crashes.
    """
    from language_config import get_tts_voice, get_language_display

    # Normalise language code (strip -IN suffix if frontend sends 'hi-IN')
    short_lang = language_code.split("-")[0].lower() if language_code else "hi"

    # Groq call
    llm_data: dict[str, Any] = {}
    try:
        llm_data = await _call_groq(
            text=text,
            language_code=short_lang,
            api_key=groq_api_key,
            model=groq_model,
        )
    except Exception as exc:
        logger.error("Intent detection LLM call failed: %s", exc)
        llm_data = {
            "intent":             "GENERAL",
            "sub_intent":         "general",
            "confidence":         0.5,
            "detected_language":  short_lang,
            "staff_message":      "Groq unavailable. Listen to the customer and guide manually.",
            "customer_response":  text,
            "key_entities":       {},
        }

    # Parse LLM output
    raw_intent        = llm_data.get("intent", "GENERAL")
    sub_intent        = llm_data.get("sub_intent", "general")
    confidence        = float(llm_data.get("confidence", 0.5))
    detected_lang     = llm_data.get("detected_language") or short_lang
    staff_message     = llm_data.get("staff_message", "")
    customer_response = llm_data.get("customer_response", "")
    raw_entities      = llm_data.get("key_entities") or {}

    normalised_intent = normalise_intent(raw_intent)

    entities = KeyEntities(
        amount        = raw_entities.get("amount"),
        tenure        = raw_entities.get("tenure"),
        applicant_age = raw_entities.get("applicant_age"),
        income        = raw_entities.get("income"),
        purpose       = raw_entities.get("purpose"),
        cibil_score   = raw_entities.get("cibil_score"),
    )

    # Load process data
    process_data  = load_process(normalised_intent)
    process_steps = get_process_steps(normalised_intent)
    key_info      = get_key_info(normalised_intent)
    product_name  = process_data.get("product_name", "Union Bank of India")
    tts_voice     = get_tts_voice(detected_lang)

    logger.info(
        "Intent detected | intent=%s | sub=%s | confidence=%.2f | lang=%s | entities=%s",
        normalised_intent, sub_intent, confidence, detected_lang, entities,
    )

    return IntentResult(
        intent             = normalised_intent,
        sub_intent         = sub_intent,
        confidence         = confidence,
        detected_language  = detected_lang,
        staff_message      = staff_message,
        customer_response  = customer_response,
        key_entities       = entities,
        process_data       = process_data,
        process_steps      = process_steps,
        key_info           = key_info,
        product_name       = product_name,
        tts_voice          = tts_voice,
    )
