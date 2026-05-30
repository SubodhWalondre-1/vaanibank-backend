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

# CONSTANTS

# Language maps — imported from canonical source (core.language)
from core.language import (
    LANG_CODE_TO_NAME as _LANG_DISPLAY,
    SARVAM_LANG_MAP as _SARVAM_LANG_MAP,
    SARVAM_LANGUAGES as _SARVAM_LANGUAGES,
    REVERIE_LANG_MAP as _REVERIE_LANG_MAP,
)

_STT_CONFIDENCE_THRESHOLD = 0.6
_TTS_CACHE_TTL = 7 * 24 * 3600   # 7 days in seconds
_HTTP_TIMEOUT = 30.0              # seconds


# RESULT DATACLASSES

# TranscriptionResult is now defined in stt_service.py (Single Responsibility).
# Re-export here for backward compatibility with existing callers.
from services.stt_service import TranscriptionResult  # noqa: E402
from services.stt_service import stt_service as _stt_service  # noqa: E402


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
    # Conversation Intelligence (P0 feature)
    collected_info: Dict[str, Any] = field(default_factory=dict)
    next_question_hindi: str = ""
    next_question_customer_lang: str = ""
    auto_step_completed: Optional[str] = None
    # Conversation Stage (P2 — exploring vs ready_to_apply)
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


# GROQ SYSTEM PROMPT BUILDER

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
1. Translate what the customer said into Hindi in Devanagari script (for staff to read)
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
  "translation": "<Hindi translation of what customer said in Devanagari script — e.g. 'मुझे होम लोन चाहिए', NEVER Hinglish/Latin script>",
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


# AI SERVICE

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
        self._audio_path = Path(settings.AUDIO_STORAGE_PATH)
        self._audio_path.mkdir(parents=True, exist_ok=True)

        self._groq_client = AsyncGroq(api_key=settings.GROQ_API_KEY)
        self._groq_sync_client: Any = None  # lazy-init sync Groq for Whisper STT
        self._redis: Any = None             # injected lazily via _get_redis()

        # P3 S-MED-1: Circuit breaker state for Groq LLM API
        self._groq_failures: int = 0            # consecutive failure count
        self._groq_circuit_open_until: float = 0  # time.time() when circuit re-closes
        self._GROQ_FAILURE_THRESHOLD: int = 3     # open after N consecutive failures
        self._GROQ_COOLDOWN_SECONDS: float = 30.0 # fast-fail duration

        # Gemini backup LLM
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

        # Shared HTTP client — connection-pooled, avoids TCP+TLS per-request
        self._http_client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )

    # Redis lazy accessor

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

    # 1. TRANSCRIBE  (delegated to STTService — see stt_service.py)

    async def transcribe(
        self,
        audio_bytes: bytes,
        language_code: str,
        session_id: Optional[int] = None,
        skip_pii: bool = False,
    ) -> TranscriptionResult:
        """
        Transcribe audio using the multi-engine fallback chain.

        Delegates to STTService (services/stt_service.py) which handles:
            - Input validation (size, format)
            - WAV conversion (ffmpeg with availability check)
            - 3-engine fallback chain with per-engine latency tracking
            - PII detection and masking

        This method is a backward-compatible thin wrapper. All new STT
        features should be added to stt_service.py, not here.

        Returns:
            TranscriptionResult with text, confidence, PII info, and metrics.
        """
        return await _stt_service.transcribe(
            audio_bytes=audio_bytes,
            language_code=language_code,
            session_id=session_id,
            skip_pii=skip_pii,
        )


    # 2. PROCESS WITH LLM

    async def process_with_llm(
        self,
        text: str,
        source_language: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        detected_intent: str = "",
        rag_context: str = "",
        system_context: str = "",
    ) -> LLMProcessResult:
        """
        Send transcribed text to Groq llama-3.3-70b.

        Args:
            text                  : Customer's transcribed speech
            source_language       : Display name e.g. "Marathi"
            conversation_history  : Prior turns as [{"role":..,"content":..}]
            detected_intent       : Known intent to inject guidance block
            rag_context           : Context retrieved from vector store
            system_context        : Dynamic system/dashboard context (missing docs, fields)

        Returns:
            LLMProcessResult with all parsed fields.
        Raises:
            LLMError on API failure.
        """
        system_prompt = _build_system_prompt(source_language, detected_intent=detected_intent)
        if system_context:
            system_prompt += f"\n\n=== CURRENT DASHBOARD & STATE CONTEXT ===\n{system_context}"

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

        # P3 S-MED-1: Circuit breaker wrappers
        async def call_groq() -> str:
            if self._groq_failures >= self._GROQ_FAILURE_THRESHOLD:
                if time.time() < self._groq_circuit_open_until:
                    logger.warning("Groq circuit OPEN — skipping")
                    raise LLMError(message="Groq circuit OPEN", model=settings.GROQ_MODEL)
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
                if self._groq_failures > 0:
                    logger.info("Groq circuit CLOSED — API recovered after %d failures", self._groq_failures)
                self._groq_failures = 0
                return completion.choices[0].message.content or ""
            except Exception as e:
                self._groq_failures += 1
                if self._groq_failures >= self._GROQ_FAILURE_THRESHOLD:
                    self._groq_circuit_open_until = time.time() + self._GROQ_COOLDOWN_SECONDS
                raise e

        async def call_gemini() -> str:
            if self._gemini_model is None:
                raise LLMError(message="Gemini not configured", model="gemini")
            if self._gemini_failures >= self._GEMINI_FAILURE_THRESHOLD:
                if time.time() < self._gemini_circuit_open_until:
                    logger.warning("Gemini circuit OPEN — skipping")
                    raise LLMError(message="Gemini circuit OPEN", model=settings.GEMINI_MODEL)
            try:
                gemini_prompt = f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\n"
                for msg in messages:
                    role_label = "STAFF" if msg["role"] == "assistant" else "CUSTOMER"
                    gemini_prompt += f"{role_label}: {msg['content']}\n"
                
                gemini_response = await asyncio.to_thread(
                    self._gemini_model.generate_content, gemini_prompt
                )
                if self._gemini_failures > 0:
                    logger.info("Gemini circuit CLOSED — recovered after %d failures", self._gemini_failures)
                self._gemini_failures = 0
                return gemini_response.text or ""
            except Exception as e:
                self._gemini_failures += 1
                if self._gemini_failures >= self._GEMINI_FAILURE_THRESHOLD:
                    self._gemini_circuit_open_until = time.time() + self._GEMINI_COOLDOWN_SECONDS
                raise e

        async def call_openrouter() -> str:
            if not settings.OPENROUTER_API_KEY:
                raise LLMError(message="OpenRouter credentials not configured in .env", model="openrouter")
            try:
                headers = {
                    "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://vaanibank.ai",
                    "X-Title": "VaaniBank AI",
                }
                payload = {
                    "model": settings.OPENROUTER_MODEL or "google/gemini-2.0-flash:free",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        *messages,
                    ],
                    "temperature": 0.3,
                }
                response = await self._http_client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=15.0
                )
                if response.status_code != 200:
                    raise LLMError(
                        message=f"OpenRouter HTTP {response.status_code}: {response.text}",
                        model="openrouter"
                    )
                data = response.json()
                choices = data.get("choices", [])
                if not choices:
                    raise LLMError(message="OpenRouter returned empty choices", model="openrouter")
                return choices[0].get("message", {}).get("content", "")
            except Exception as e:
                logger.error("OpenRouter fallback failed: %s", e)
                raise e

        # Fetch Dynamic Settings for LLM
        from services.settings_service import settings_service
        dyn_settings = await settings_service.get_all_settings()
        primary_llm = dyn_settings.get("llm_model", "groq_llama_3.3_70b")

        raw = ""
        llm_source = "groq"

        if primary_llm == "gemini_2.0_flash":
            # Gemini is primary
            try:
                raw = await call_gemini()
                llm_source = "gemini"
                logger.info("✅ Primary LLM (Gemini) succeeded")
            except Exception as gemini_exc:
                logger.warning("Gemini primary failed: %s — trying Groq fallback", gemini_exc)
                try:
                    raw = await call_groq()
                    llm_source = "groq"
                    logger.info("✅ Groq fallback succeeded")
                except Exception as groq_exc:
                    logger.warning("Groq fallback failed: %s — trying OpenRouter fallback", groq_exc)
                    try:
                        raw = await call_openrouter()
                        llm_source = "openrouter"
                        logger.info("✅ OpenRouter fallback succeeded")
                    except Exception as or_exc:
                        logger.error("All LLMs failed under Gemini-primary mode")
                        raise LLMError(
                            message="All primary and fallback LLMs (Gemini, Groq, OpenRouter) failed. Please try again.",
                            model="gemini+groq+openrouter",
                        ) from or_exc
        else:
            # Groq is primary
            try:
                raw = await call_groq()
                llm_source = "groq"
                logger.info("✅ Primary LLM (Groq) succeeded")
            except Exception as groq_exc:
                logger.warning("Groq primary failed: %s — trying Gemini fallback", groq_exc)
                try:
                    raw = await call_gemini()
                    llm_source = "gemini"
                    logger.info("✅ Gemini fallback succeeded")
                except Exception as gemini_exc:
                    logger.warning("Gemini fallback failed: %s — trying OpenRouter fallback", gemini_exc)
                    try:
                        raw = await call_openrouter()
                        llm_source = "openrouter"
                        logger.info("✅ OpenRouter fallback succeeded")
                    except Exception as or_exc:
                        logger.error("All LLMs failed under Groq-primary mode")
                        raise LLMError(
                            message="All primary and fallback LLMs (Groq, Gemini, OpenRouter) failed. Please try again.",
                            model="groq+gemini+openrouter",
                        ) from or_exc


        # Parse JSON (same logic for both Groq and Gemini)
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

        # Loop Prevention (Post-processing)
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

    # 3. GENERATE TTS

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
        if language_code:
            language_code = language_code.split("-")[0].strip().lower()

        # Fetch Dynamic Settings
        from services.settings_service import settings_service
        dyn_settings = await settings_service.get_all_settings()
        tts_engine = dyn_settings.get("tts_engine", "sarvam_bulbul_v3")

        if tts_engine == "none":
            logger.info("TTS generation bypassed (tts_engine == 'none')")
            return TTSResult(
                audio_url="",
                duration_seconds=0.0,
                model_used="none",
                from_cache=False,
            )

        # P3 N-MED-3: SHA-256 replaces MD5 for lower collision probability
        cache_key = f"tts_cache:{hashlib.sha256((text + language_code).encode()).hexdigest()}"

        # P3 B-MED-1: Acquire Redis connection once, reuse for read + write
        redis = None
        try:
            redis = await self._get_redis()
        except Exception as exc:
            logger.warning("Redis TTS connection failed: %s", exc)

        # Redis cache check
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

        # Sarvam Bulbul v3
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

        # Validate WAV header
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

        # Save audio file
        timestamp = int(time.time() * 1000)
        filename = f"tts_{session_id or 0}_{language_code}_{timestamp}.wav"
        audio_url = await storage_service.upload_audio_bytes(audio_bytes, filename)

        audio_data_bytes = max(0, len(audio_bytes) - 44)
        duration_seconds = round(audio_data_bytes / (22050 * 2), 2)

        # Cache in Redis (reusing connection from above)
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

    # TTS helper

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

    # 4. DETECT LANGUAGE

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

    # 5. TRANSLATE TEXT

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

        # Fetch Dynamic Settings
        from services.settings_service import settings_service
        dyn_settings = await settings_service.get_all_settings()
        engine = dyn_settings.get("translation_engine", "sarvam_translate")

        sarvam_ok = (
            engine == "sarvam_translate"
            and settings.SARVAM_API_KEY
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
                            f" Return ONLY the translated text in {target_name} script (if target is Hindi, use Devanagari script e.g. 'मुझे होम लोन चाहिए', NEVER Hinglish/Latin script)."
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


    _BANKING_TRANSLATIONS: Dict[str, str] = {
        # Greeting scripts
        "नमस्ते, Union Bank of India में आपका स्वागत है। मैं आपकी क्या मदद कर सकता हूँ?": "Hello, welcome to Union Bank of India. How can I help you?",
        "जी बिल्कुल, लोन के बारे में मैं आपकी पूरी मदद करूंगा। कुछ जानकारी लेनी होगी।": "Sure, I will assist you with the loan. I will need to gather some information.",
        "जी हाँ, खाता खोलने में मैं आपकी मदद करता हूँ। कुछ details चाहिए होंगे।": "Yes, I will help you with opening an account. Some details will be required.",
        "KYC update के लिए आ गए हैं? बिल्कुल सही, मैं अभी कर देता हूँ।": "Have you come for a KYC update? Certainly, I will process it right now.",
        "FD करना चाहते हैं? बहुत अच्छा! कुछ जानकारी लेता हूँ।": "Do you want to create a Fixed Deposit? That's great! Let me take some details.",
        "Card से related समस्या है? मैं अभी solve करता हूँ।": "Is it a card-related issue? I will resolve it right away.",
        "Balance check करना है? जी बिल्कुल, verification करता हूँ।": "Do you want to check your balance? Sure, let me perform the verification.",
        
        # Farewell scripts
        "आपका काम हो गया है। कोई और मदद चाहिए तो बताइए। धन्यवाद!": "Your request is completed. Let me know if you need any other help. Thank you!",
        "आपकी loan application की सारी जानकारी ले ली है। Processing शुरू हो जाएगी। धन्यवाद!": "All information for your loan application has been gathered. Processing will begin. Thank you!",
        "आपका खाता खोलने की सारी details ले ली हैं। Account जल्द active हो जाएगा। धन्यवाद!": "All details for opening your account have been gathered. The account will be activated soon. Thank you!",
        "आपका KYC update हो गया है। कोई और मदद चाहिए तो बताइए।": "Your KYC has been updated. Please let me know if you need any other help.",
        "आपकी FD की सारी details ले ली हैं। FD जल्द open हो जाएगी। धन्यवाद!": "All details for your Fixed Deposit have been gathered. It will be opened soon. Thank you!",
        "Card की समस्या solve हो गई है। कोई और मदद चाहिए तो बताइए।": "The card issue has been resolved. Let know if you need any other help.",
        "Balance enquiry complete हो गई है। धन्यवाद!": "Balance enquiry is complete. Thank you!",

        # Predefined questions (loan_enquiry)
        "आपका शुभ नाम क्या है?": "What is your full name?",
        "आपको कौन सा लोन चाहिए — Home, Personal, Education, या Vehicle?": "Which loan do you want — Home, Personal, Education, or Vehicle?",
        "आपको कितनी राशि का लोन चाहिए?": "How much loan amount do you need?",
        "आपकी monthly income कितनी है?": "What is your monthly income?",
        "आप Salaried हैं, Self-employed, या Business करते हैं?": "Are you Salaried, Self-employed, or running a Business?",
        "कितने साल या महीने का लोन चाहिए?": "What loan tenure do you want (years or months)?",
        "आपकी उम्र कितनी है?": "How old are you?",
        "क्या आपका CIBIL score पता है?": "Do you know your CIBIL score?",
        "अभी कोई और loan EMI चल रही है क्या?": "Are you currently paying any other loan EMIs?",
        "यह लोन किस काम के लिए चाहिए?": "What is the purpose of this loan?",
        "क्या आप Aadhaar card लाए हैं?": "Have you brought your Aadhaar card?",
        "क्या आपके पास PAN card है?": "Do you have a PAN card?",

        # Predefined questions (account_opening)
        "कौन सा खाता चाहिए — Savings, Current, या Jan Dhan?": "Which account do you want to open — Savings, Current, or Jan Dhan?",
        "यह खाता किस काम के लिए चाहिए?": "What is the purpose of this account?",
        "खाता खोलने के लिए कितनी राशि जमा कराएंगे?": "How much initial deposit will you make to open the account?",
        "खाते में nominee किसका नाम रखना है?": "Whom do you want to name as the nominee for the account?",
        "आपका registered mobile number क्या है?": "What is your registered mobile number?",
        "क्या आपके पास address proof है — utility bill या Aadhaar?": "Do you have address proof — a utility bill or Aadhaar?",
        "क्या आप 2 passport size photos लाए हैं?": "Have you brought 2 passport size photographs?",
        "क्या आप Jan Dhan (zero balance) खाता खोलना चाहते हैं?": "Do you want to open a Jan Dhan (zero balance) account?",

        # Predefined questions (kyc_update)
        "क्या update करना है — address, mobile, Aadhaar seeding, या nominee?": "What do you want to update — address, mobile, Aadhaar seeding, or nominee?",
        "क्या आपका Aadhaar bank account से link है?": "Is your Aadhaar linked with your bank account?",
        "क्या आपके पास नया address proof है?": "Do you have a new address proof?",
        "नया address kya है?": "What is the new address?",
        "क्या आपका mobile number account से link है?": "Is your mobile number linked to the account?",
        "आपका re-KYC कब तक करना है?": "When is your re-KYC due?",

        # Predefined questions (fixed_deposit)
        "FD में कितनी राशि जमा कराना चाहते हैं?": "How much amount do you want to deposit in the FD?",
        "कितने समय के लिए FD करना है?": "What is the tenure for the FD?",
        "Regular FD चाहिए या Sweep-in FD?": "Do you want a Regular FD or a Sweep-in FD?",
        "क्या आप 60 साल या उससे ज़्यादा हैं? (extra rate मिलेगा)": "Are you 60 years or older? (You will get an extra interest rate)",
        "क्या आपकी income taxable नहीं है? (Form 15G/H चाहिए)": "Is your income non-taxable? (Form 15G/H required)",

        # Predefined questions (card_services)
        "कौन सा card है — RuPay, VISA, या Mastercard?": "Which card do you have — RuPay, VISA, or Mastercard?",
        "Card के साथ क्या problem है?": "What is the issue with your card?",
        "Card क्यूँ block करना है — खोया, चोरी, या damaged?": "Why do you want to block the card — lost, stolen, or damaged?",
        "PIN भूल गए हैं या PIN reset करना है?": "Have you forgotten your PIN or do you want to reset it?",

        # Predefined questions (balance_enquiry)
        "आपका account number क्या है?": "What is your account number?",
        "Identity verify करने के लिए DOB या OTP बताइए": "Please provide your Date of Birth or OTP to verify your identity.",

        # Predefined questions (general)
        "आज आप किस काम से आए हैं?": "What brings you to the bank today?",
        "आपका mobile number क्या है?": "What is your mobile number?",
    }

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""
        import re
        # Remove common punctuation: ? ! . , ।
        text = re.sub(r'[?!.,।]', '', text)
        # Normalize whitespace
        text = " ".join(text.split())
        return text.lower()

    def _has_indian_chars(self, text: str) -> bool:
        if not text:
            return False
        import re
        # Match any character outside standard ASCII range (non-ASCII)
        return bool(re.search(r'[^\x00-\x7F]', text))

    async def translate_to_english(self, text: str) -> str:
        """
        Translate any Indian language text to English using a multi-engine fallback chain:
        1. Predefined dictionary (with text normalization)
        2. Groq LLaMA (llama-3.3-70b-versatile, fallback: llama-3.1-8b-instant)
        3. Google Gemini (gemini-2.0-flash)
        4. OpenRouter (google/gemini-2.0-flash:free)
        5. Sarvam Translate (formal, mayura:v1)

        Returns the translated text, or the original text on failure.
        """
        if not text or not text.strip():
            return "—"

        # Check standard translations dictionary first (zero-latency, offline friendly)
        normalized_text = self._normalize_text(text)
        
        # Build normalized lookup dictionary on the fly (or cache it)
        if not hasattr(self, "_normalized_banking_translations"):
            self._normalized_banking_translations = {
                self._normalize_text(k): v for k, v in self._BANKING_TRANSLATIONS.items()
            }
            
        if normalized_text in self._normalized_banking_translations:
            logger.info("Translation cache hit (predefined) for: %s", text[:40])
            return self._normalized_banking_translations[normalized_text]

        # 1. Groq LLaMA
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
            result = (completion.choices[0].message.content or "").strip()
            if result:
                # If result is identical to input and input has Indian chars, the model failed/echoed
                if result.strip() != text.strip() or not self._has_indian_chars(text):
                    return result
                else:
                    logger.warning("Groq translate returned identical text; trying next fallback.")
        except Exception as exc:
            logger.warning("Groq translate_to_english failed: %s — trying Gemini fallback", exc)

        # 2. Gemini Fallback
        if self._gemini_model is not None:
            try:
                gemini_prompt = (
                    "SYSTEM INSTRUCTIONS:\n"
                    "You are a professional bank translator. Translate the given Hindi/Indian language banking text to English. "
                    "Return ONLY the English translation, nothing else. No explanations, no quotes.\n\n"
                    f"TEXT: {text}"
                )
                gemini_response = await asyncio.to_thread(
                    self._gemini_model.generate_content, gemini_prompt
                )
                result = (gemini_response.text or "").strip()
                if result:
                    if result.strip() != text.strip() or not self._has_indian_chars(text):
                        return result
                    else:
                        logger.warning("Gemini translate returned identical text; trying next fallback.")
            except Exception as gemini_exc:
                logger.warning("Gemini translate_to_english fallback failed: %s — trying OpenRouter fallback", gemini_exc)

        # 3. OpenRouter Fallback
        if settings.OPENROUTER_API_KEY:
            try:
                headers = {
                    "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://vaanibank.ai",
                    "X-Title": "VaaniBank AI",
                }
                payload = {
                    "model": settings.OPENROUTER_MODEL or "google/gemini-2.0-flash:free",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a professional bank translator. Translate the given text to English. "
                                "Return ONLY the English translation, nothing else. "
                                "No explanations, no quotes, just the translated text."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.1,
                }
                response = await self._http_client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=15.0
                )
                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices", [])
                    if choices:
                        result = (choices[0].get("message", {}).get("content", "")).strip()
                        if result:
                            if result.strip() != text.strip() or not self._has_indian_chars(text):
                                return result
                            else:
                                logger.warning("OpenRouter translate returned identical text; trying next fallback.")
            except Exception as or_exc:
                logger.warning("OpenRouter translate_to_english fallback failed: %s — trying Sarvam Translate fallback", or_exc)

        # 4. Sarvam Translate Fallback (hi-IN -> en-IN)
        if settings.SARVAM_API_KEY:
            try:
                resp = await self._http_client.post(
                    "https://api.sarvam.ai/translate",
                    headers={
                        "api-subscription-key": settings.SARVAM_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json={
                        "input": text,
                        "source_language_code": "hi-IN",
                        "target_language_code": "en-IN",
                        "speaker_gender": "Female",
                        "mode": "formal",
                        "model": "mayura:v1",
                        "enable_preprocessing": True,
                    },
                    timeout=15.0,
                )
                if resp.status_code == 200:
                    result = resp.json().get("translated_text", "").strip()
                    if result:
                        if result.strip() != text.strip() or not self._has_indian_chars(text):
                            return result
            except Exception as sarvam_exc:
                logger.warning("Sarvam translate_to_english fallback failed: %s", sarvam_exc)

        return text  # final graceful fallback


# Module-level singleton
ai_service = AIService()
