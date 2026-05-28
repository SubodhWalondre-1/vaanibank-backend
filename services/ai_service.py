"""
VaaniBank AI — Core AI Service
PSBs Hackathon 2026 | Team Vectora

Orchestrates the full AI pipeline:
  STT  → Sarvam Saarika 2.5 → Groq Whisper Large-v3-Turbo → Reverie RevUp BFSI
  LLM  → Groq llama-3.3-70b (banking prompt, JSON response)
  TTS  → Sarvam Bulbul v3 → Redis 7-day cache

Usage:
    from services.ai_service import ai_service

    result = await ai_service.transcribe(audio_bytes, "mr", session_id=42)
    llm    = await ai_service.process_with_llm(result.text, "Marathi", history=[])
    tts    = await ai_service.generate_tts(llm.suggested_response_hindi, "hi")
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from groq import AsyncGroq

# Gemini backup LLM
try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

from config import settings
from core.exceptions import LLMError, STTError, TTSError
from services.pii_service import pii_service
from services.storage_service import storage_service

logger = logging.getLogger("vaanibank.ai")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# ── Language maps — imported from canonical source (core.language) ─────────────
from core.language import (
    LANG_CODE_TO_NAME as _LANG_DISPLAY,
    SARVAM_LANG_MAP as _SARVAM_LANG_MAP,
    SARVAM_LANGUAGES as _SARVAM_LANGUAGES,
    REVERIE_LANG_MAP as _REVERIE_LANG_MAP,
)

_STT_CONFIDENCE_THRESHOLD = 0.6
_TTS_CACHE_TTL = 7 * 24 * 3600   # 7 days in seconds
_HTTP_TIMEOUT = 30.0              # seconds


# ══════════════════════════════════════════════════════════════════════════════
# RESULT DATACLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TranscriptionResult:
    text: str
    confidence: float
    model_used: str
    language_detected: str
    pii_detected: bool = False
    pii_types: List[str] = field(default_factory=list)
    masked_text: str = ""


@dataclass
class LLMProcessResult:
    translation: str
    intent: str
    intent_confidence: float
    sentiment: str
    sentiment_score: float
    suggested_response_hindi: str
    suggested_response_customer_lang: str
    banking_terms_detected: List[str]
    process_triggered: Optional[str]
    raw_response: str = ""
    # ── Conversation Intelligence (P0 feature) ──
    collected_info: Dict[str, Any] = field(default_factory=dict)
    next_question_hindi: str = ""
    next_question_customer_lang: str = ""
    auto_step_completed: Optional[str] = None
    # ── Conversation Stage (P2 — exploring vs ready_to_apply) ──
    conversation_stage: str = "exploring"


@dataclass
class TTSResult:
    audio_url: str
    duration_seconds: float
    model_used: str
    from_cache: bool


@dataclass
class LanguageDetectionResult:
    language_code: str
    language_name: str
    confidence: float


# ══════════════════════════════════════════════════════════════════════════════
# GROQ SYSTEM PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

# Language code → fallback phrase in that language
_WAIT_PHRASES: Dict[str, str] = {
    "hi": "कृपया एक क्षण रुकें, मैं आपकी सहायता करता हूँ।",
    "mr": "कृपया एक क्षण थांबा, मी तुमची मदत करतो।",
    "ta": "ஒரு க்ஷணம் காதிருங்கள், நான் உங்களுக்கு உதவுகிறேன்.",
    "te": "ఒక క్షణం వేచ్చండి, నేను మీకు సహాయం చేస్తాను.",
    "bn": "একটু অপেক্ষা করুন, আমি আপনাকে সহায্য করব।",
    "kn": "ಒಂದು ಕ್ಷಣ ಕಾಯಿರಿ, ನಾನು ನಿಮಗೆ ಸಹಾಯ ಮಾಡುತ್ತೇನೆ.",
    "or": "ଏକ କ୍ଷଣ ଅପେକ୍ଷା କରନ୍ତୁ, ମୁଁ ଆପଣଙ୍କୁ ସାହାଯ୍ୟ କରିବି।",
    "pa": "ਇੱਕ ਮਿੰਟ ਰੁਕੋ, ਮੈਂ ਤੁਹਾਡੀ ਸਹਾਇਤਾ ਕਰਦਾ ਹਾਂ।",
    "gu": "એક ક્ષણ રાહો, હું તમને મદદ કરીશ.",
    "ml": "ഒരു നിമിഷം കാത്തിരിക്കൂ, ഞാൻ നിങ്ങളെ സഹായിക്കുന്നു.",
}

# Reverse map: display name (lowercase) → language code (for fallback phrases)
_LANG_NAME_TO_CODE: Dict[str, str] = {
    "hindi": "hi", "marathi": "mr", "tamil": "ta", "telugu": "te",
    "bengali": "bn", "kannada": "kn", "odia": "or", "punjabi": "pa",
    "gujarati": "gu", "malayalam": "ml",
}

@lru_cache(maxsize=1)
def _load_intent_guidance_map() -> Dict[str, str]:
    """Load intent guidance blocks from YAML config (cached after first call)."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "intent_guidance.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                logger.info("Loaded %d intent guidance blocks from %s", len(data), config_path.name)
                return data
        except Exception as exc:
            logger.warning("Failed to load intent_guidance.yaml: %s — using empty guidance", exc)
    return {}


def _load_intent_guidance(detected_intent: str) -> str:
    """Return the guidance block for the given intent, or empty string."""
    guidance_map = _load_intent_guidance_map()
    return guidance_map.get(detected_intent.lower(), "")


def _build_system_prompt(source_language: str, detected_intent: str = "") -> str:
    # Load intent guidance from external YAML config (loaded once, cached)
    intent_block = _load_intent_guidance(detected_intent)


    return (
        f"""You are VaaniBank AI — an intelligent real-time assistant for Union Bank of India frontline staff.

You are embedded in the bank's staff dashboard. A customer is speaking {source_language} at the branch counter. The staff member needs your help RIGHT NOW to serve them correctly, quickly, and politely.

=== YOUR ROLE ===
You are the staff's silent expert co-pilot. You:
1. Translate what the customer said into Hindi (for staff to read)
2. Detect what the customer ACTUALLY wants (intent detection)
3. Sense how the customer is feeling (sentiment)
4. Give the staff a READY-TO-SPEAK response in Hindi — natural, polite, professional
5. Translate that same response into {source_language} for TTS playback to the customer
6. EXTRACT any customer information revealed in this message (name, amounts, documents, etc.)
7. Generate the NEXT BEST QUESTION the staff should ask based on what's still missing

=== CONVERSATION CONTEXT ===
You may receive previous conversation turns as context. USE THEM to:
- Understand the ongoing topic (don't re-detect intent if already clear)
- Track what information has ALREADY been collected (don't ask again)
- Build on previous exchanges — your suggestions should be PROGRESSIVE
- If customer mentions "5 lakh" after discussing loans, understand it's the LOAN AMOUNT

=== CONVERSATION STAGE DETECTION ===
Detect the customer's CURRENT STAGE in this conversation:

STAGE = "exploring" (customer is asking for information, comparing options, learning):
  Trigger phrases: "जानकारी चाहिए", "बताइए", "kya hota hai", "details do",
    "kitna milega", "kaise milta hai", "interest rate kya hai", "options kya hain",
    "kya kya chahiye", "loan ke baare mein", "batao", "samjhao", "konsa achha hai"
  YOUR BEHAVIOR in exploring stage:
    - Provide COMPREHENSIVE, EDUCATIONAL answers about the product/service
    - Include key facts: rates, limits, tenure, eligibility, required documents
    - Compare options if relevant (e.g., different loan types, account types)
    - End with a guiding question: "Kaunsa option pasand hai?" or "Apply karna chahenge?"
    - Do NOT aggressively collect form data — let customer learn first
    - Set collected_info fields to null (don't force collection in exploring mode)
    - Set next_question to an educational follow-up, NOT a form-filling question

STAGE = "ready_to_apply" (customer has decided and wants to proceed):
  Trigger phrases: "apply karna hai", "form bharo", "karna chahta hoon",
    "shuru karo", "account kholna hai", "loan lena hai", "haan chahiye",
    "kar do", "le lunga", "ban jayega kya", "kab tak ho jayega"
  YOUR BEHAVIOR in ready_to_apply stage:
    - Switch to data collection mode — fill collected_info systematically
    - Generate next_question for missing required fields one by one
    - Guide through the process step by step
    - Be efficient — collect one piece of info per question

STAGE TRANSITION RULES:
  - Default to "exploring" if customer's first message is a question/enquiry
  - Switch to "ready_to_apply" ONLY when customer explicitly says they want to proceed
  - If customer already gave data (name, amount, etc.) assume "ready_to_apply"
  - Once in "ready_to_apply", stay there unless customer asks a new general question
  - If [SYSTEM CONTEXT] shows several fields already collected, assume "ready_to_apply"

=== UNION BANK OF INDIA CONTEXT ===
Products & Services the staff handles:
- Savings / Current Account opening (KYC required: Aadhaar + PAN + photo + address proof)
- Home Loan, Personal Loan, Education Loan, Vehicle Loan, Gold Loan, Mudra Loan
- Fixed Deposit (FD), Recurring Deposit (RD), PPF
- KYC Update (Aadhaar seeding, address change, mobile number update)
- Debit Card / Credit Card (issue, block, PIN reset, upgrade)
- Balance enquiry, Mini statement, Passbook update
- NEFT / RTGS / IMPS fund transfer
- Internet Banking / Mobile Banking (UNI Mobile) activation
- Locker facility, Nomination update
- Grievance / Complaint registration

Key RBI / Bank Rules staff must follow:
- PAN mandatory for transactions above ₹50,000
- Aadhaar seeding required for government scheme benefits (PMJDY, DBT)
- CIBIL score >= 700 for most retail loans
- KYC must be re-verified every 2 years for high-risk customers
- Minimum balance: Savings Urban ₹1000, Rural ₹500, No-frill (BSBD) ₹0
- FD minimum: ₹1000, Max tenure: 10 years, TDS on FD interest if > ₹40,000/year
- Home Loan: max 80% of property value, tenure up to 30 years
- Personal Loan: max ₹15 lakh, 12-60 months, no collateral

=== STAFF RESPONSE GUIDELINES ===
The suggested_response_hindi must be:
- Spoken naturally (as if staff is talking to customer face-to-face)
- 1-3 sentences maximum — concise, actionable, respectful
- Start with an acknowledgement: 'जी बिल्कुल', 'हाँ जी', 'समझ गया', etc.
- Tell the customer what's happening or what they need to do NEXT
- Use simple Hindi — NO jargon, no English banking terms unless unavoidable
- If documents are needed, name them clearly
- If waiting is needed, say so politely with a timeframe
- If the request is not possible, explain why gently and offer an alternative

Examples of GOOD responses (EXPLORING stage):
✅ 'जी बिल्कुल, होम लोन पर अभी ब्याज दर 7.15% से 9.60% तक है, जो आपके CIBIL स्कोर पर निर्भर करती है। क्या आप और जानकारी चाहेंगे?'
✅ 'हाँ जी, बचत खाते पर आपको 2.75% ब्याज मिलता है और इसके लिए मेट्रो शहरों में ₹1000 बैलेंस रखना होता है।'

Examples of GOOD responses (READY_TO_APPLY stage):
✅ 'जी बिल्कुल, खाता खोलने के लिए आपका Aadhaar और PAN card चाहिए होगा। क्या आपके पास दोनों हैं?'
✅ 'हाँ जी, आपका FD statement मैं अभी निकाल देता हूँ, एक मिनट।'
✅ 'समझ गया, Home Loan के लिए पहले eligibility check होगी। आपकी monthly income कितनी है?'

=== GROUNDEDNESS & RAG (CRITICAL) ===
- You will be provided with a [BANKING KNOWLEDGE] block.
- If the customer asks a factual question (interest rates, eligibility, charges), you MUST use the data from that block.
- NEVER hallucinate numbers. If the rate is 7.15% in the context, do NOT say 8.5%.
- If the information is not in the context, say: 'जी, इसकी सटीक जानकारी मुझे अभी चेक करनी होगी, मैं अभी पता करता हूँ।'
- Do NOT jump to document collection if the customer is just asking for information. Answer the question FIRST.

Examples of BAD responses (AVOID):
❌ 'Please submit your KYC documents.' (English, robotic)
❌ 'Your request has been noted.' (vague, unhelpful)
❌ 'I understand your concern.' (call-centre language)

=== SENTIMENT HANDLING ===
- calm: Standard helpful response
- confused: Add extra explanation, be more detailed
- frustrated: Start with empathy ('मुझे खेद है कि आपको परेशानी हुई'), then solution
- urgent: Acknowledge urgency first ('मैं समझ सकता हूँ, आपका काम तुरंत होगा'), then act

{intent_block}

=== INTENT DETECTION ===
Detect from ONLY these exact values:
account_opening, loan_enquiry, kyc_update, card_services, balance_enquiry, fixed_deposit, general

Map intelligently:
- 'loan chahiye', 'qarz', 'home loan', 'personal loan', 'karz', 'udhar', 'ghar lena' → loan_enquiry
- 'khata kholna', 'account open', 'nayi bachat', 'Jan Dhan', 'PMJDY' → account_opening
- 'balance', 'paisa kitna hai', 'statement', 'kitna hai', 'jama' → balance_enquiry
- 'FD', 'fixed deposit', 'miaadi jama', 'recurring', 'RD', 'invest karna' → fixed_deposit
- 'KYC', 'Aadhaar update', 'address change', 'mobile number badalna', 'naam change' → kyc_update
- 'card', 'ATM', 'debit card', 'credit card', 'card band karna', 'pin bhool gaya' → card_services

=== INFORMATION EXTRACTION ===
From THIS message AND conversation context, extract ONLY the fields relevant to the detected intent.
For each field, return the extracted value or null if not mentioned yet.
IMPORTANT: Include information from PREVIOUS messages too — accumulate, don't reset.
CRITICAL RULE: customer_name MUST ALWAYS be extracted from ANY message where the customer mentions their name — regardless of intent or stage. If customer says 'mera naam Subodh hai' or 'I am Rahul' or 'naam hai Priya' — ALWAYS set customer_name to that value. Never return null for customer_name if the name was stated in this message or any previous message.

=== SYSTEM STATE AWARENESS (CRITICAL) ===
You may receive a [SYSTEM STATE — STAFF DASHBOARD CURRENT VIEW] block in the conversation.
This block shows the LIVE state of the staff's dashboard — document readiness, info completion, and phase.
You MUST OBEY these rules when a SYSTEM STATE block is present:
1. If document readiness is 100% — NEVER ask about documents. They are all confirmed.
2. If all fields are FILLED — NEVER ask for more information. Guide next steps instead.
3. Your suggested_response_hindi MUST match the dashboard state. Do NOT contradict it.
4. If the SYSTEM STATE says "NEXT field to ask: X", your next_question should ask about X, not something else.
5. When everything is complete, your response should be: acknowledge completion → summarize → guide next action.

=== LOOP PREVENTION & PROGRESSION (STRICT) ===
- NEVER repeat the same suggestion twice in a row.
- NEVER suggest what the staff just said in the previous turn.
- If the customer confirmed an item (e.g., 'Yes I can pay EMI'), MOVE FORWARD to the next field (e.g., 'What is your name?').
- Do NOT re-explain things that were already explained.
- If you find yourself wanting to repeat, check the [SYSTEM STATE] for the 'NEXT field to ask' and focus on that instead.

=== INTENT-SPECIFIC collected_info SCHEMAS ===

For intent = loan_enquiry, return EXACTLY these fields:
  customer_name, loan_type, amount, tenure, monthly_income, employment_type, cibil_score, existing_emis, age, purpose, aadhaar_provided, pan_provided, dob, pan_no, aadhaar_no, occupation_type, work_experience, net_salary, monthly_obligations, academic_record, course_name, university_name, estimated_expenditure, margin_money, property_address, property_type, purchase_price, builder_name, vehicle_model, loan_purpose, place, form_date

For intent = account_opening, return EXACTLY these fields:
  customer_name, account_type, pmjdy_eligible, initial_deposit, nominee_name, aadhaar_provided, pan_provided, address_proof_provided, photos_provided, phone_number_provided, customer_id, account_no

For intent = kyc_update, return EXACTLY these fields:
  customer_name, update_type, aadhaar_status, address_type, mobile_linked, re_kyc_due, aadhaar_provided, address_proof_provided, customer_id, account_no

For intent = fixed_deposit, return EXACTLY these fields:
  customer_name, amount, tenure, fd_type, senior_citizen, pan_provided, form_15g_applicable

For intent = card_services, return EXACTLY these fields:
  customer_name, card_type, card_issue, card_block_reason, pin_issue, phone_number_provided, aadhaar_provided

For intent = balance_enquiry, return EXACTLY these fields:
  customer_name, account_number_provided, phone_number_provided, identity_verified

For intent = general, return EXACTLY these fields:
  customer_name, purpose, phone_number_provided, aadhaar_provided

=== OUTPUT FORMAT ===
Return ONLY valid JSON — no markdown, no explanation, no extra text:
{{
  "translation": "<Hindi translation of what customer said>",
  "intent": "<one of: account_opening|loan_enquiry|kyc_update|card_services|balance_enquiry|fixed_deposit|general>",
  "intent_confidence": <0.0 to 1.0>,
  "sentiment": "<calm|frustrated|confused|urgent>",
  "sentiment_score": <0.0 to 1.0>,
  "conversation_stage": "<exploring|ready_to_apply>",
  "suggested_response_hindi": "<natural Hindi response staff should speak to customer>",
  "suggested_response_customer_lang": "<SAME response translated to {source_language} script — must be in {source_language}, never in Hindi or English>",
  "banking_terms_detected": ["<list of banking terms found in customer speech>"],
  "process_triggered": "<intent name if a new process should start, else null>",
  "collected_info": {{ <ONLY the fields listed above for the detected intent — no extra fields> }},
  "next_question_hindi": "<The SINGLE most important question staff should ask NEXT based on what's still missing — in natural Hindi. Example: 'आपकी monthly income कितनी है?' or 'PAN card लाए हैं क्या?' or null if nothing pending>",
  "next_question_customer_lang": "<Same question translated to {source_language} for TTS — or null>",
  "auto_step_completed": "<step description if a process step was just fulfilled by this message, else null>"
}}

[ignoring loop detection]"""
    )


# ══════════════════════════════════════════════════════════════════════════════
# AI SERVICE
# ══════════════════════════════════════════════════════════════════════════════

class AIService:
    """
    Core AI pipeline service for VaaniBank.

    STT fallback chain:
      1. Sarvam Saarika 2.5       — Primary, Indian-language optimised
      2. Groq Whisper Large-v3-T  — Fallback 1, uses existing GROQ_API_KEY
      3. Reverie RevUp BFSI       — Fallback 2, banking-trained, all 10 langs

    All public methods are async. Instantiate once at module level and
    import the singleton `ai_service` throughout the application.
    """

    def __init__(self) -> None:
        self._audio_path = Path(
            os.getenv("AUDIO_STORAGE_PATH", "./storage/audio")
        )
        self._audio_path.mkdir(parents=True, exist_ok=True)

        self._groq_client = AsyncGroq(api_key=settings.GROQ_API_KEY)
        self._groq_sync_client: Any = None  # lazy-init sync Groq for Whisper STT
        self._redis: Any = None             # injected lazily via _get_redis()

        # P3 S-MED-1: Circuit breaker state for Groq LLM API
        self._groq_failures: int = 0            # consecutive failure count
        self._groq_circuit_open_until: float = 0  # time.time() when circuit re-closes
        self._GROQ_FAILURE_THRESHOLD: int = 3     # open after N consecutive failures
        self._GROQ_COOLDOWN_SECONDS: float = 30.0 # fast-fail duration

        # ── Gemini backup LLM ──
        self._gemini_model = None
        self._gemini_failures: int = 0
        self._gemini_circuit_open_until: float = 0
        self._GEMINI_FAILURE_THRESHOLD: int = 3
        self._GEMINI_COOLDOWN_SECONDS: float = 60.0
        if _GEMINI_AVAILABLE and settings.GEMINI_API_KEY:
            try:
                genai.configure(api_key=settings.GEMINI_API_KEY)
                self._gemini_model = genai.GenerativeModel(
                    model_name=settings.GEMINI_MODEL,
                    generation_config=genai.types.GenerationConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                        max_output_tokens=1200,
                    ),
                )
                logger.info("Gemini backup LLM initialized: %s", settings.GEMINI_MODEL)
            except Exception as exc:
                logger.warning("Gemini init failed (will use Groq only): %s", exc)
                self._gemini_model = None
        else:
            if not _GEMINI_AVAILABLE:
                logger.info("google-generativeai not installed — Gemini backup disabled")
            elif not settings.GEMINI_API_KEY:
                logger.info("GEMINI_API_KEY not set — Gemini backup disabled")

        # ── Shared HTTP client — connection-pooled, avoids TCP+TLS per-request ──
        self._http_client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )

    # ── Redis lazy accessor ────────────────────────────────────────────────────

    async def _get_redis(self):
        """Return the shared Redis client from database module."""
        if self._redis is None:
            try:
                from database import redis_client
                self._redis = redis_client
            except Exception as exc:
                logger.warning(
                    "Redis client unavailable — TTS caching disabled: %s", exc
                )
                return None
        return self._redis

    async def _call_groq_with_fallback(self, **kwargs):
        """
        Wrapper around self._groq_client.chat.completions.create that implements
        an automatic model fallback to llama-3.1-8b-instant if the primary model
        fails due to rate limiting or quota exhaustion (429 errors).
        """
        model = kwargs.get("model", settings.GROQ_MODEL)
        try:
            return await self._groq_client.chat.completions.create(**kwargs)
        except Exception as exc:
            exc_str = str(exc)
            is_rate_limit = "429" in exc_str or "rate_limit" in exc_str or "quota" in exc_str
            if is_rate_limit and model == settings.GROQ_MODEL:
                fallback_model = "llama-3.1-8b-instant"
                logger.warning(
                    "Groq primary model %s rate limited. Retrying with fallback model %s...",
                    model, fallback_model
                )
                kwargs["model"] = fallback_model
                try:
                    return await self._groq_client.chat.completions.create(**kwargs)
                except Exception as fallback_exc:
                    logger.error("Groq fallback model %s also failed: %s", fallback_model, fallback_exc)
                    raise fallback_exc
            raise exc

    # ══════════════════════════════════════════════════════════════════════════
    # 1. TRANSCRIBE  (STT fallback chain)
    # ══════════════════════════════════════════════════════════════════════════

    async def transcribe(
        self,
        audio_bytes: bytes,
        language_code: str,
        session_id: Optional[int] = None,
        skip_pii: bool = False,
    ) -> TranscriptionResult:
        """
        Transcribe audio using the three-step fallback chain.

        Steps:
          0. Convert incoming audio (WebM/Opus/OGG/MP3) → 16 kHz mono WAV
          1. Sarvam Saarika 2.5        (confidence >= 0.6 → accept)
          2. Groq Whisper Large-v3-T   (uses existing GROQ_API_KEY)
          3. Reverie RevUp BFSI        (banking-trained, all 10 Indian languages)

        Returns TranscriptionResult with PII detection applied.
        """
        # ── Step 0: Convert to WAV ─────────────────────────────────────────────
        audio_bytes = await self._ensure_wav(audio_bytes)

        text: Optional[str] = None
        confidence: float = 0.0
        model_used: str = ""
        language_detected: str = language_code

        # ── Step 1: Sarvam Saarika 2.5 ────────────────────────────────────────
        sarvam_ok = (
            settings.SARVAM_API_KEY
            and settings.SARVAM_API_KEY != "<your-sarvam-key>"
            and settings.SARVAM_API_KEY.strip() != ""
        )

        if sarvam_ok and language_code in _SARVAM_LANGUAGES:
            try:
                stt_text, stt_confidence, stt_model_used = await self._stt_sarvam(
                    audio_bytes, language_code
                )
                if stt_text and (stt_confidence >= _STT_CONFIDENCE_THRESHOLD or stt_confidence == 0.0):
                    text = stt_text
                    confidence = stt_confidence if stt_confidence > 0.0 else 0.85
                    model_used = stt_model_used
                    language_detected = language_code
                elif not stt_text:
                    logger.info("Sarvam returned empty transcript — falling back")
                    text = None
                else:
                    logger.info(
                        "Sarvam confidence %.2f < %.2f — falling back",
                        stt_confidence,
                        _STT_CONFIDENCE_THRESHOLD,
                    )
                    text = None
            except Exception as exc:
                logger.warning("Sarvam STT failed: %s — falling back to Groq Whisper", exc)
                text = None
        else:
            if not sarvam_ok:
                logger.info("Sarvam API key not set — using Groq Whisper directly")

        # ── Step 2: Groq Whisper Large-v3-Turbo ───────────────────────────────
        if text is None:
            try:
                text, confidence, model_used = await self._stt_groq_whisper(
                    audio_bytes, language_code
                )
                language_detected = language_code
                logger.info(
                    "Groq Whisper success | model=%s | conf=%.2f", model_used, confidence
                )
            except Exception as exc:
                logger.warning("Groq Whisper failed: %s — falling back to Reverie", exc)
                text = None

        # ── Step 3: Reverie RevUp BFSI ────────────────────────────────────────
        if text is None:
            reverie_ok = (
                settings.REVERIE_APP_ID
                and settings.REVERIE_API_KEY
                and settings.REVERIE_APP_ID.strip() != ""
                and settings.REVERIE_APP_ID != "your_reverie_app_id_here"
            )
            if reverie_ok:
                try:
                    text, confidence, model_used = await self._stt_reverie(
                        audio_bytes, language_code
                    )
                    language_detected = language_code
                    logger.info(
                        "Reverie STT success | lang=%s | conf=%.2f", language_code, confidence
                    )
                except Exception as exc:
                    logger.error("Reverie STT failed: %s", exc)
                    text = None
            else:
                logger.warning(
                    "Reverie credentials not configured — all STT fallbacks exhausted"
                )

        # ── Graceful degradation ───────────────────────────────────────────────
        if not text:
            logger.warning(
                "All STT models failed for session=%s — returning placeholder",
                session_id,
            )
            return TranscriptionResult(
                text="[Voice message received — transcription unavailable]",
                confidence=0.0,
                model_used=model_used or "none",
                language_detected=language_code,
                pii_detected=False,
                pii_types=[],
                masked_text="[Voice message received — transcription unavailable]",
            )

        # ── PII detection on raw transcript (skipped for streaming partials) ──
        if skip_pii:
            return TranscriptionResult(
                text=text,
                confidence=confidence,
                model_used=model_used,
                language_detected=language_detected,
                pii_detected=False,
                pii_types=[],
                masked_text=text,
            )

        pii_result = pii_service.detect_and_mask(text)

        logger.info(
            "STT success | session=%s | model=%s | confidence=%.2f | pii=%s",
            session_id,
            model_used,
            confidence,
            pii_result.pii_types,
        )

        return TranscriptionResult(
            text=text,
            confidence=confidence,
            model_used=model_used,
            language_detected=language_detected,
            pii_detected=pii_result.pii_found,
            pii_types=pii_result.pii_types,
            masked_text=pii_result.masked_text if pii_result.pii_found else text,
        )

    # ── Audio format conversion ────────────────────────────────────────────────

    async def _ensure_wav(self, audio_bytes: bytes) -> bytes:
        """
        Convert incoming audio (WebM/Opus/OGG/MP3 etc.) to 16 kHz mono WAV
        using async ffmpeg subprocess. If already WAV, return as-is.
        """
        import aiofiles

        header = audio_bytes[:12]

        if header[:4] == b"RIFF":
            return audio_bytes
        elif header[:4] == b"OggS":
            src_ext = ".ogg"
            input_fmt = "ogg"
        elif header[:4] == b"\x1aE\xdf\xa3":
            src_ext = ".webm"
            input_fmt = "webm"
        elif b"webm" in header[4:12]:
            src_ext = ".webm"
            input_fmt = "webm"
        else:
            src_ext = ".webm"
            input_fmt = "webm"

        logger.debug(
            "Audio format detected: ext=%s fmt=%s | header=%s",
            src_ext, input_fmt, header[:8].hex(),
        )

        # Use temp files for input (ffmpeg needs seekable input for some formats)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=src_ext, delete=False) as src:
            src.write(audio_bytes)
            src_path = src.name

        dst_path = src_path.rsplit(".", 1)[0] + ".wav"

        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", input_fmt,
                "-i", src_path,
                "-ar", "16000",
                "-ac", "1",
                "-sample_fmt", "s16",
                "-avoid_negative_ts", "make_zero",
                "-strict", "-2",
                "-f", "wav",
                dst_path,
            ]
            # Use native async subprocess — no thread pool slot consumed
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

            if proc.returncode != 0:
                stderr_msg = stderr.decode(errors="replace")[:500]
                logger.error(
                    "ffmpeg conversion failed (rc=%d) | stderr: %s",
                    proc.returncode,
                    stderr_msg,
                )
                return audio_bytes

            # Read output file asynchronously
            async with aiofiles.open(dst_path, "rb") as f:
                wav_bytes = await f.read()

            logger.info(
                "Audio converted to WAV | src=%d bytes (%s) → dst=%d bytes",
                len(audio_bytes),
                src_ext,
                len(wav_bytes),
            )
            return wav_bytes

        except asyncio.TimeoutError:
            logger.error("ffmpeg conversion timed out (15s)")
            return audio_bytes
        except Exception as exc:
            logger.error("Audio conversion exception, using original: %s", exc)
            return audio_bytes

        finally:
            try:
                os.unlink(src_path)
            except OSError:
                pass
            try:
                os.unlink(dst_path)
            except OSError:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # STT HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    async def _stt_sarvam(
        self, audio_bytes: bytes, language_code: str
    ) -> tuple[str, float, str]:
        """Call Sarvam Saarika 2.5 STT API — primary engine."""
        short_code = language_code.split("-")[0].lower()
        bcp47 = _SARVAM_LANG_MAP.get(short_code, "hi-IN")

        is_webm = (
            audio_bytes[:4] == b'\x1aE\xdf\xa3'
            or audio_bytes[4:8] == b'webm'
        )
        mime = "audio/webm" if is_webm else "audio/wav"
        fname = "audio.webm" if is_webm else "audio.wav"

        logger.debug("Sarvam STT upload: %s (%s) | %d bytes", fname, mime, len(audio_bytes))

        response = await self._http_client.post(
            settings.SARVAM_STT_URL,
            headers={"api-subscription-key": settings.SARVAM_API_KEY},
            files={"file": (fname, audio_bytes, mime)},
            data={"language_code": bcp47, "with_timestamps": "false"},
        )
        if response.status_code >= 400:
            logger.error(
                "Sarvam STT error %d: %s",
                response.status_code,
                response.text[:300],
            )
        response.raise_for_status()
        data = response.json()

        transcript = (data.get("transcript") or data.get("text", "")).strip()
        confidence = float(data.get("confidence", 0.85))

        if not transcript:
            logger.info("Sarvam returned empty transcript — will try fallback")
            return "", 0.0, "sarvam_saarika_2.5"

        return transcript, confidence, "sarvam_saarika_2.5"

    async def _stt_groq_whisper(
        self, audio_bytes: bytes, language_code: str
    ) -> tuple[str, float, str]:
        """
        Groq Whisper Large-v3-Turbo — Fallback 1.
        Free, extremely fast (Groq LPU), uses existing GROQ_API_KEY.
        Supports all 10 Indian languages via Whisper multilingual model.
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            transcription = await asyncio.to_thread(
                self._groq_whisper_sync,
                tmp_path,
                language_code,
            )
            text = transcription.strip()
            if not text:
                raise STTError(message="Groq Whisper returned empty transcript.")
            return text, 0.90, "whisper-large-v3-turbo-groq"
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _groq_whisper_sync(self, audio_path: str, language_code: str) -> str:
        """Synchronous Groq Whisper call wrapped in asyncio.to_thread.
        Reuses a single sync Groq client to avoid TCP connection churn."""
        if self._groq_sync_client is None:
            from groq import Groq
            self._groq_sync_client = Groq(api_key=settings.GROQ_API_KEY)

        short_code = language_code.split("-")[0].lower()
        with open(audio_path, "rb") as audio_file:
            transcription = self._groq_sync_client.audio.transcriptions.create(
                file=("audio.wav", audio_file),
                model="whisper-large-v3-turbo",
                # Odia: Whisper uses "ori" not "or"
                language=short_code if short_code != "or" else "ori",
                response_format="text",
            )
        return str(transcription).strip()

    async def _stt_reverie(
        self, audio_bytes: bytes, language_code: str
    ) -> tuple[str, float, str]:
        """
        Reverie RevUp STT — Fallback 2.

        BFSI-domain trained model — purpose-built for banking environments.
        Covers all 10 Indian languages including Odia and Punjabi.
        Free tier: 10 hours on signup at revup.reverieinc.com.
        Docs: https://docs.reverieinc.com/speech-to-text-file-api/setup
        """
        app_id = settings.REVERIE_APP_ID
        api_key = settings.REVERIE_API_KEY

        if not app_id or not api_key:
            raise STTError(message="Reverie credentials not configured.")

        short_code = language_code.split("-")[0].lower()
        src_lang = _REVERIE_LANG_MAP.get(short_code, "hi")

        response = await self._http_client.post(
            "https://revapi.reverieinc.com/",
            headers={
                "REV-APP-ID":  app_id,
                "REV-API-KEY": api_key,
                "REV-APPNAME": "stt_file",
                "src_lang":    src_lang,
                "domain":      "bfsi",   # banking & financial services domain
            },
            files={
                "audio_file": ("audio.wav", audio_bytes, "audio/wav"),
            },
        )
        if response.status_code >= 400:
            logger.error(
                "Reverie STT error %d: %s",
                response.status_code,
                response.text[:300],
            )
        response.raise_for_status()
        data = response.json()

        # Reverie returns both raw `text` and formatted `display_text`
        # Prefer display_text (has punctuation + number formatting)
        text = (
            data.get("display_text")
            or data.get("text")
            or ""
        ).strip()
        confidence = float(data.get("confidence", 0.85))

        if not text:
            raise STTError(message="Reverie STT returned empty transcript.")

        logger.info(
            "Reverie STT success | lang=%s | conf=%.2f | text=%s",
            src_lang, confidence, text[:60],
        )
        return text, confidence, "reverie-revup-bfsi"

    # ══════════════════════════════════════════════════════════════════════════
    # 2. PROCESS WITH LLM
    # ══════════════════════════════════════════════════════════════════════════

    async def process_with_llm(
        self,
        text: str,
        source_language: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        detected_intent: str = "",
        rag_context: str = "",
    ) -> LLMProcessResult:
        """
        Send transcribed text to Groq llama-3.3-70b.

        Args:
            text                  : Customer's transcribed speech
            source_language       : Display name e.g. "Marathi"
            conversation_history  : Prior turns as [{"role":..,"content":..}]
            detected_intent       : Known intent to inject guidance block

        Returns:
            LLMProcessResult with all parsed fields.
        Raises:
            LLMError on API failure.
        """
        system_prompt = _build_system_prompt(source_language, detected_intent=detected_intent)

        messages: List[Dict[str, str]] = []

        # Inject RAG context as the first message pair so the LLM treats it as
        # authoritative banking knowledge before any conversation history.
        # The assistant ack turn prevents the LLM from ignoring it as a user ramble.
        if rag_context:
            messages.append({"role": "user",      "content": rag_context})
            messages.append({"role": "assistant", "content": "Understood. I will answer factual banking questions only from the provided knowledge context."})

        if conversation_history:
            # Keep last 6 turns to stay within context limits
            messages.extend(conversation_history[-6:])
        messages.append({"role": "user", "content": text})

        # P3 S-MED-1: Circuit breaker — fast-fail if Groq is degraded
        groq_circuit_open = False
        if self._groq_failures >= self._GROQ_FAILURE_THRESHOLD:
            if time.time() < self._groq_circuit_open_until:
                logger.warning(
                    "Groq circuit OPEN — skipping to Gemini (resets in %.0fs)",
                    self._groq_circuit_open_until - time.time(),
                )
                groq_circuit_open = True
            else:
                # Cooldown expired — allow a probe request
                logger.info("Groq circuit half-open — allowing probe request")

        # ── Try Groq first, fallback to Gemini ──────────────────────────────
        raw = ""
        llm_source = "groq"
        groq_failed = groq_circuit_open  # skip Groq if circuit is open

        if not groq_circuit_open:
            try:
                completion = await self._call_groq_with_fallback(
                    model=settings.GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        *messages,
                    ],
                    max_tokens=settings.GROQ_MAX_TOKENS,
                    temperature=0.3,
                    response_format={"type": "json_object"},
                    timeout=15.0,
                )
                # Success — reset circuit breaker
                if self._groq_failures > 0:
                    logger.info("Groq circuit CLOSED — API recovered after %d failures", self._groq_failures)
                self._groq_failures = 0
                raw = completion.choices[0].message.content or ""
            except Exception as groq_exc:
                groq_failed = True
                self._groq_failures += 1
                if self._groq_failures >= self._GROQ_FAILURE_THRESHOLD:
                    self._groq_circuit_open_until = time.time() + self._GROQ_COOLDOWN_SECONDS
                    logger.error(
                        "Groq circuit OPENED — %d consecutive failures, cooldown %.0fs",
                        self._groq_failures, self._GROQ_COOLDOWN_SECONDS,
                    )
                logger.warning("Groq LLM failed (%d/%d): %s — trying Gemini backup",
                               self._groq_failures, self._GROQ_FAILURE_THRESHOLD, groq_exc)

        # ── Gemini fallback ────────────────────────────────────────────────────
        if groq_failed:
            if self._gemini_model is None:
                raise LLMError(
                    message="Primary LLM (Groq) failed and Gemini backup is not configured.",
                    model=settings.GROQ_MODEL,
                )

            # Gemini circuit breaker
            if self._gemini_failures >= self._GEMINI_FAILURE_THRESHOLD:
                if time.time() < self._gemini_circuit_open_until:
                    raise LLMError(
                        message="Both LLMs unavailable (circuit breakers open).",
                        model="groq+gemini",
                    )
                else:
                    logger.info("Gemini circuit half-open — allowing probe request")

            try:
                # Build Gemini prompt (system + conversation as single prompt)
                gemini_prompt = f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\n"
                for msg in messages:
                    role_label = "STAFF" if msg["role"] == "assistant" else "CUSTOMER"
                    gemini_prompt += f"{role_label}: {msg['content']}\n"

                gemini_response = await asyncio.to_thread(
                    self._gemini_model.generate_content, gemini_prompt
                )
                raw = gemini_response.text or ""
                llm_source = "gemini"

                # Success — reset Gemini circuit breaker
                if self._gemini_failures > 0:
                    logger.info("Gemini circuit CLOSED — recovered after %d failures", self._gemini_failures)
                self._gemini_failures = 0
                logger.info("✅ Gemini backup LLM succeeded (Groq was down)")
            except Exception as gemini_exc:
                self._gemini_failures += 1
                if self._gemini_failures >= self._GEMINI_FAILURE_THRESHOLD:
                    self._gemini_circuit_open_until = time.time() + self._GEMINI_COOLDOWN_SECONDS
                logger.error("Gemini backup also failed: %s", gemini_exc, exc_info=True)
                raise LLMError(
                    message="Both Groq and Gemini LLMs failed. Please try again.",
                    model="groq+gemini",
                ) from gemini_exc

        # ── Parse JSON (same logic for both Groq and Gemini) ──────────────────
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            parsed: Dict[str, Any] = json.loads(clean)
        except (json.JSONDecodeError, IndexError) as exc:
            logger.warning(
                "%s JSON parse failed — using defaults | raw=%s | err=%s",
                llm_source.upper(), raw[:200], exc,
            )
            parsed = {}

        result = LLMProcessResult(
            translation=parsed.get("translation", text),
            intent=parsed.get("intent", "general"),
            intent_confidence=float(parsed.get("intent_confidence", 0.5)),
            sentiment=parsed.get("sentiment", "calm"),
            sentiment_score=float(parsed.get("sentiment_score", 0.5)),
            suggested_response_hindi=parsed.get(
                "suggested_response_hindi",
                "कृपया एक क्षण रुकें, मैं आपकी सहायता करता हूँ।",
            ),
            suggested_response_customer_lang=parsed.get(
                "suggested_response_customer_lang"
            ) or _WAIT_PHRASES.get(
                _LANG_NAME_TO_CODE.get(source_language.lower(), "hi"),
                _WAIT_PHRASES["hi"],
            ),
            banking_terms_detected=parsed.get("banking_terms_detected", []),
            process_triggered=parsed.get("process_triggered"),
            raw_response=raw,
            collected_info=parsed.get("collected_info", {}),
            next_question_hindi=parsed.get("next_question_hindi", ""),
            next_question_customer_lang=parsed.get("next_question_customer_lang", ""),
            auto_step_completed=parsed.get("auto_step_completed"),
            conversation_stage=parsed.get("conversation_stage", "exploring"),
        )

        # ── Loop Prevention (Post-processing) ────────────────────────────────
        # If the LLM repeats the exact same suggestion from the last turn,
        # we should force it to at least try the next deterministic question.
        if conversation_history:
            last_assistant_msgs = [
                m["content"] for m in reversed(conversation_history) 
                if m["role"] == "assistant"
            ]
            if last_assistant_msgs:
                last_msg = last_assistant_msgs[0]
                # Check for near-exact match (ignoring acknowledgment prefixes)
                norm_last = "".join(ch for ch in last_msg if ch.isalnum())
                norm_curr = "".join(ch for ch in (result.suggested_response_hindi or "") if ch.isalnum())
                
                if norm_curr and (norm_last == norm_curr or norm_curr in norm_last or norm_last in norm_curr):
                    logger.warning("LLM repeated previous turn. Forcing progression.")
                    # If it repeated, we empty the response so the UI defaults 
                    # to the Navigator's deterministic question.
                    result.suggested_response_hindi = None
                    result.suggested_response_customer_lang = None

        logger.info(
            "LLM processed [%s] | intent=%s (%.2f) | sentiment=%s | process=%s",
            llm_source.upper(),
            result.intent,
            result.intent_confidence,
            result.sentiment,
            result.process_triggered,
        )
        return result

    async def summarize_session(
        self,
        exchanges: List[Any],
        customer_language: str,
        intent_detected: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate a bilingual structured summary of a banking session.
        Returns a dictionary with summary, key points, and next steps in both Hindi and customer language.
        """
        conversation_lines = []
        for ex in exchanges:
            if hasattr(ex, "customer_text_original") and ex.customer_text_original:
                conversation_lines.append(
                    f"Customer: {getattr(ex, 'customer_text_translated', None) or ex.customer_text_original}"
                )
            if hasattr(ex, "staff_response_final") and (ex.staff_response_final or getattr(ex, "staff_response_suggested", None)):
                conversation_lines.append(
                    f"Staff: {ex.staff_response_final or getattr(ex, 'staff_response_suggested', None)}"
                )

        conversation_text = "\n".join(conversation_lines)

        summary_prompt = (
            f"You are a professional banking assistant at Union Bank of India.\n"
            f"Summarize this branch conversation in both Hindi and {customer_language}.\n\n"
            f"Intent: {intent_detected or 'General Query'}\n"
            f"Conversation:\n{conversation_text or 'No conversation recorded.'}\n\n"
            f"Return JSON only with this structure:\n"
            f'{{"summary_hindi": ["sentence1", "sentence2"], '
            f'"summary_customer_lang": ["sentence1", "sentence2"], '
            f'"key_points_hindi": ["point1", "point2", "point3"], '
            f'"key_points_customer": ["point1", "point2", "point3"], '
            f'"next_steps_hindi": ["step1", "step2"], '
            f'"next_steps_customer": ["step1", "step2"]}}'
        )

        # Use the raw LLM call with a shorter timeout and focused prompt
        try:
            completion = await self._call_groq_with_fallback(
                model=settings.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": "You are a professional banking summary generator. Return JSON only."},
                    {"role": "user", "content": summary_prompt},
                ],
                max_tokens=2000,
                temperature=0.2,
                response_format={"type": "json_object"},
                timeout=25.0,
            )
            raw = completion.choices[0].message.content or ""
            return json.loads(raw)
        except Exception as e:
            logger.error("Summarization LLM call failed: %s", e)
            return {
                "summary_hindi": ["सत्र पूरा हुआ।"],
                "summary_customer_lang": ["Session completed."],
                "key_points_hindi": [],
                "key_points_customer": [],
                "next_steps_hindi": [],
                "next_steps_customer": [],
            }

    # ══════════════════════════════════════════════════════════════════════════
    # 3. GENERATE TTS
    # ══════════════════════════════════════════════════════════════════════════

    async def generate_tts(
        self,
        text: str,
        language_code: str,
        session_id: Optional[int] = None,
    ) -> TTSResult:
        """
        Convert text to speech via Sarvam Bulbul v3.

        Cache strategy:
          Key  → tts_cache:{md5(text + language_code)}
          Hit  → return cached audio_url immediately
          Miss → Sarvam Bulbul v3 → save file → cache 7 days

        Returns TTSResult.
        Raises TTSError if generation fails.
        """
        # P3 N-MED-3: SHA-256 replaces MD5 for lower collision probability
        cache_key = f"tts_cache:{hashlib.sha256((text + language_code).encode()).hexdigest()}"

        # P3 B-MED-1: Acquire Redis connection once, reuse for read + write
        redis = None
        try:
            redis = await self._get_redis()
        except Exception as exc:
            logger.warning("Redis TTS connection failed: %s", exc)

        # ── Redis cache check ──────────────────────────────────────────────────
        if redis is not None:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    cached_data = json.loads(cached)
                    logger.info("TTS cache hit | key=%s", cache_key)
                    return TTSResult(
                        audio_url=cached_data["audio_url"],
                        duration_seconds=cached_data.get("duration_seconds", 0.0),
                        model_used=cached_data.get("model_used", "cached"),
                        from_cache=True,
                    )
            except Exception as exc:
                logger.warning("Redis TTS cache read failed: %s", exc)

        # ── Sarvam Bulbul v3 ──────────────────────────────────────────────────
        audio_bytes: Optional[bytes] = None
        model_used = ""

        try:
            audio_bytes, model_used = await self._tts_sarvam(text, language_code)
        except Exception as exc:
            logger.error("Sarvam TTS failed: %s", exc)

        if audio_bytes is None:
            raise TTSError(
                message="Text-to-speech audio generation failed.",
                language_code=language_code,
            )

        # ── Validate WAV header ────────────────────────────────────────────────
        if len(audio_bytes) < 44 or audio_bytes[:4] != b'RIFF':
            logger.error(
                "TTS returned invalid WAV! bytes=%d header_hex=%s",
                len(audio_bytes), audio_bytes[:8].hex()
            )
            import struct  # stdlib, cached by Python after first import
            sample_rate = 22050
            byte_rate = sample_rate * 2
            wav_header = struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF", 36 + len(audio_bytes), b"WAVE",
                b"fmt ", 16, 1, 1, sample_rate, byte_rate, 2, 16,
                b"data", len(audio_bytes),
            )
            audio_bytes = wav_header + audio_bytes
            logger.info("WAV header added manually | total=%d bytes", len(audio_bytes))

        # ── Save audio file ────────────────────────────────────────────────────
        timestamp = int(time.time() * 1000)
        filename = f"tts_{session_id or 0}_{language_code}_{timestamp}.wav"
        audio_url = await storage_service.upload_audio_bytes(audio_bytes, filename)

        audio_data_bytes = max(0, len(audio_bytes) - 44)
        duration_seconds = round(audio_data_bytes / (22050 * 2), 2)

        # ── Cache in Redis (reusing connection from above) ─────────────────────
        if redis is not None:
            try:
                await redis.setex(
                    cache_key,
                    _TTS_CACHE_TTL,
                    json.dumps({
                        "audio_url": audio_url,
                        "duration_seconds": duration_seconds,
                        "model_used": model_used,
                    }),
                )
            except Exception as exc:
                logger.warning("Redis TTS cache write failed: %s", exc)

        logger.info(
            "TTS generated | session=%s | model=%s | lang=%s | duration=%.2fs",
            session_id, model_used, language_code, duration_seconds,
        )

        return TTSResult(
            audio_url=audio_url,
            duration_seconds=duration_seconds,
            model_used=model_used,
            from_cache=False,
        )

    # ── TTS helper ─────────────────────────────────────────────────────────────

    async def _tts_sarvam(
        self, text: str, language_code: str
    ) -> tuple[bytes, str]:
        """Call Sarvam Bulbul v3 TTS API with speaker suhani."""
        short_code = language_code.split("-")[0].lower()
        bcp47 = _SARVAM_LANG_MAP.get(short_code, "hi-IN")

        response = await self._http_client.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={
                "api-subscription-key": settings.SARVAM_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "inputs": [text],
                "target_language_code": bcp47,
                "speaker": "suhani",
                "model": "bulbul:v3",
                "pace": 1.1,
                "speech_sample_rate": 22050,
                "output_audio_codec": "wav",
                "enable_preprocessing": True,
            },
        )
        logger.debug("Sarvam TTS response status: %d", response.status_code)
        response.raise_for_status()
        data = response.json()

        audios = data.get("audios", [])
        if not audios:
            raise TTSError(message="Sarvam TTS returned no audio data.")

        audio_bytes = base64.b64decode(audios[0])
        return audio_bytes, "sarvam_bulbul_v3"

    # ══════════════════════════════════════════════════════════════════════════
    # 4. DETECT LANGUAGE
    # ══════════════════════════════════════════════════════════════════════════

    async def detect_language(
        self, audio_bytes: bytes
    ) -> LanguageDetectionResult:
        """
        Detect spoken language from a short audio clip.
        Primary: Sarvam auto-detect. Fallback: default to Hindi.
        """
        try:
            response = await self._http_client.post(
                settings.SARVAM_STT_URL,
                headers={"api-subscription-key": settings.SARVAM_API_KEY},
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                data={"model": "saarika:v2.5", "with_timestamps": "false"},
            )
            response.raise_for_status()
            data = response.json()

            lang_code = data.get("language_code", "hi-IN")
            short_code = lang_code.split("-")[0]
            confidence = float(data.get("confidence", 0.7))

            return LanguageDetectionResult(
                language_code=short_code,
                language_name=_LANG_DISPLAY.get(short_code, "Hindi"),
                confidence=confidence,
            )

        except Exception as exc:
            logger.warning("Sarvam language detect failed: %s — defaulting to Hindi", exc)

        return LanguageDetectionResult(
            language_code="hi",
            language_name="Hindi",
            confidence=0.0,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 5. TRANSLATE TEXT
    # ══════════════════════════════════════════════════════════════════════════

    async def translate_text(
        self,
        text: str,
        target_language_code: str,
        source_language_code: str = "hi",
    ) -> Optional[str]:
        """
        Translate text between supported Indian languages.
        1. Sarvam Translate API  (fast, Indian language optimised)
        2. Groq LLaMA fallback   (if Sarvam fails)
        """
        if not text or not text.strip():
            return text

        short_target = target_language_code.split("-")[0].lower()
        short_source = source_language_code.split("-")[0].lower()

        if short_target == short_source:
            return text

        target_bcp47 = _SARVAM_LANG_MAP.get(short_target, f"{short_target}-IN")
        source_bcp47 = _SARVAM_LANG_MAP.get(short_source, "hi-IN")

        sarvam_ok = (
            settings.SARVAM_API_KEY
            and settings.SARVAM_API_KEY.strip() not in ("", "<your-sarvam-key>")
        )
        if sarvam_ok:
            try:
                resp = await self._http_client.post(
                    "https://api.sarvam.ai/translate",
                    headers={
                        "api-subscription-key": settings.SARVAM_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json={
                        "input": text,
                        "source_language_code": source_bcp47,
                        "target_language_code": target_bcp47,
                        "speaker_gender": "Female",
                        "mode": "formal",
                        "model": "mayura:v1",
                        "enable_preprocessing": True,
                    },
                )
                resp.raise_for_status()
                translated = resp.json().get("translated_text", "").strip()
                if translated:
                    logger.info(
                        "Sarvam translate %s→%s: %s…",
                        source_bcp47, target_bcp47, translated[:60]
                    )
                    return translated
            except Exception as exc:
                logger.warning("Sarvam translate failed: %s", exc)

        source_name = _LANG_DISPLAY.get(short_source, short_source.upper())
        target_name = _LANG_DISPLAY.get(short_target, short_target.upper())
        try:
            completion = await self._call_groq_with_fallback(
                model=settings.GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are a professional Indian banking translator."
                            f" Translate the following {source_name} text to {target_name}."
                            f" Return ONLY the translated text in {target_name} script."
                            f" Preserve masked placeholders like ****, [masked], and last-four digits exactly."
                            f" No English. No explanation."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                max_tokens=512,
                temperature=0.1,
            )
            translated = (completion.choices[0].message.content or "").strip()
            if translated:
                logger.info("Groq translate %s→%s: %s…", source_name, target_name, translated[:60])
                return translated
        except Exception as exc:
            logger.warning("Groq translate fallback failed: %s", exc)

        return None


    async def translate_to_english(self, text: str) -> str:
        """
        Translate any Indian language text to English using the shared
        AsyncGroq client (connection-pooled, non-blocking).

        Returns the translated text, or the original text on failure.
        """
        if not text or not text.strip():
            return "—"

        try:
            completion = await self._call_groq_with_fallback(
                model=settings.GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a translator. Translate the given text to English. "
                            "Return ONLY the English translation, nothing else. "
                            "No explanations, no quotes, just the translated text."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0.1,
                max_tokens=200,
                timeout=15.0,
            )
            return (completion.choices[0].message.content or "").strip() or text
        except Exception as exc:
            logger.warning("translate_to_english failed: %s", exc)
            return text  # graceful fallback


# ── Module-level singleton ────────────────────────────────────────────────────
ai_service = AIService()
