"""
VaaniBank AI — Session Navigator (Deterministic State Machine)
Union Bank of India | Team Vectora

Pure-code engine that replaces LLM-based next_question generation.
100% deterministic: same inputs → same outputs. Never repeats.

Architecture:
    LLM → translation, intent, info extraction (what LLMs are good at)
    This engine → phase detection, next action, guidance (what code is good at)
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SESSION PHASES
# ══════════════════════════════════════════════════════════════════════════════

class Phase:
    GREET    = "greet"       # No intent yet, welcome the customer
    EDUCATE  = "educate"     # Intent detected, customer is exploring
    COLLECT  = "collect"     # Ready to apply, gathering missing fields
    VERIFY   = "verify"      # Info collected, confirming documents
    PROCESS  = "process"     # All ready, guide through form/submission
    CLOSE    = "close"       # Done — summary and farewell


# ══════════════════════════════════════════════════════════════════════════════
# QUESTION BANK — Static, deterministic, per-intent field priorities
# Each field has: priority (lower = ask first), hindi question, english label
# ══════════════════════════════════════════════════════════════════════════════

QUESTION_BANK = {
    "loan_enquiry": [
        # Priority-ordered: most important fields first
        ("customer_name",    1,  "आपका शुभ नाम क्या है?",                         "Customer Name"),
        ("loan_type",        2,  "आपको कौन सा लोन चाहिए — Home, Personal, Education, या Vehicle?", "Loan Type"),
        ("amount",           3,  "आपको कितनी राशि का लोन चाहिए?",                "Loan Amount"),
        ("monthly_income",   4,  "आपकी monthly income कितनी है?",                 "Monthly Income"),
        ("employment_type",  5,  "आप Salaried हैं, Self-employed, या Business करते हैं?", "Employment Type"),
        ("tenure",           6,  "कितने साल या महीने का लोन चाहिए?",              "Loan Tenure"),
        ("age",              7,  "आपकी उम्र कितनी है?",                            "Customer Age"),
        ("cibil_score",      8,  "क्या आपका CIBIL score पता है?",                 "CIBIL Score"),
        ("existing_emis",    9,  "अभी कोई और loan EMI चल रही है क्या?",            "Existing EMIs"),
        ("purpose",         10,  "यह लोन किस काम के लिए चाहिए?",                  "Loan Purpose"),
        ("aadhaar_provided", 11, "क्या आप Aadhaar card लाए हैं?",                  "Aadhaar Card"),
        ("pan_provided",    12,  "क्या आपके पास PAN card है?",                     "PAN Card"),
    ],
    "account_opening": [
        ("customer_name",        1,  "आपका शुभ नाम क्या है?",                      "Customer Name"),
        ("account_type",         2,  "कौन सा खाता चाहिए — Savings, Current, या Jan Dhan?", "Account Type"),
        ("purpose",              3,  "यह खाता किस काम के लिए चाहिए?",               "Purpose"),
        ("initial_deposit",      4,  "खाता खोलने के लिए कितनी राशि जमा कराएंगे?",    "Initial Deposit"),
        ("nominee_name",         5,  "खाते में nominee किसका नाम रखना है?",          "Nominee Name"),
        ("phone_number_provided", 6, "आपका registered mobile number क्या है?",       "Phone Number"),
        ("aadhaar_provided",     7,  "क्या आप Aadhaar card लाए हैं?",               "Aadhaar Card"),
        ("pan_provided",         8,  "क्या आपके पास PAN card है?",                  "PAN Card"),
        ("address_proof_provided", 9, "क्या आपके पास address proof है — utility bill या Aadhaar?", "Address Proof"),
        ("photos_provided",     10,  "क्या आप 2 passport size photos लाए हैं?",     "Passport Photos"),
        ("pmjdy_eligible",      11,  "क्या आप Jan Dhan (zero balance) खाता खोलना चाहते हैं?", "PMJDY Eligibility"),
    ],
    "kyc_update": [
        ("customer_name",         1, "आपका शुभ नाम क्या है?",                       "Customer Name"),
        ("update_type",           2, "क्या update करना है — address, mobile, Aadhaar seeding, या nominee?", "Update Type"),
        ("aadhaar_status",        3, "क्या आपका Aadhaar bank account से link है?",   "Aadhaar Linked"),
        ("aadhaar_provided",      4, "क्या आप Aadhaar card लाए हैं?",               "Aadhaar Card"),
        ("address_proof_provided", 5, "क्या आपके पास नया address proof है?",         "Address Proof"),
        ("address_type",          6, "नया address kya है?",                          "New Address"),
        ("mobile_linked",         7, "क्या आपका mobile number account से link है?",  "Mobile Linked"),
        ("re_kyc_due",            8, "आपका re-KYC कब तक करना है?",                  "Re-KYC Due Date"),
    ],
    "fixed_deposit": [
        ("customer_name",     1,  "आपका शुभ नाम क्या है?",                         "Customer Name"),
        ("amount",            2,  "FD में कितनी राशि जमा कराना चाहते हैं?",          "FD Amount"),
        ("tenure",            3,  "कितने समय के लिए FD करना है?",                   "FD Tenure"),
        ("fd_type",           4,  "Regular FD चाहिए या Sweep-in FD?",               "FD Type"),
        ("senior_citizen",    5,  "क्या आप 60 साल या उससे ज़्यादा हैं? (extra rate मिलेगा)", "Senior Citizen"),
        ("pan_provided",      6,  "क्या आपके पास PAN card है?",                     "PAN Card"),
        ("form_15g_applicable", 7, "क्या आपकी income taxable नहीं है? (Form 15G/H चाहिए)", "Form 15G/H"),
    ],
    "card_services": [
        ("customer_name",      1, "आपका शुभ नाम क्या है?",                          "Customer Name"),
        ("card_type",          2, "कौन सा card है — RuPay, VISA, या Mastercard?",   "Card Type"),
        ("card_issue",         3, "Card के साथ क्या problem है?",                    "Card Issue"),
        ("card_block_reason",  4, "Card क्यूँ block करना है — खोया, चोरी, या damaged?", "Block Reason"),
        ("pin_issue",          5, "PIN भूल गए हैं या PIN reset करना है?",             "PIN Issue"),
        ("phone_number_provided", 6, "आपका registered mobile number क्या है?",       "Phone Number"),
        ("aadhaar_provided",   7, "क्या आप Aadhaar card लाए हैं?",                  "Aadhaar Card"),
    ],
    "balance_enquiry": [
        ("customer_name",          1, "आपका शुभ नाम क्या है?",                      "Customer Name"),
        ("account_number_provided", 2, "आपका account number क्या है?",              "Account Number"),
        ("phone_number_provided",   3, "आपका registered mobile number क्या है?",    "Phone Number"),
        ("identity_verified",       4, "Identity verify करने के लिए DOB या OTP बताइए", "Identity Verified"),
    ],
    "general": [
        ("customer_name",          1, "आपका शुभ नाम क्या है?",                      "Customer Name"),
        ("purpose",                2, "आज आप किस काम से आए हैं?",                   "Purpose"),
        ("phone_number_provided",  3, "आपका mobile number क्या है?",                "Phone Number"),
    ],
}


# ── Greeting scripts per intent ──────────────────────────────────────────────

GREETING_SCRIPTS = {
    "general":         "नमस्ते, Union Bank of India में आपका स्वागत है। मैं आपकी क्या मदद कर सकता हूँ?",
    "loan_enquiry":    "जी बिल्कुल, लोन के बारे में मैं आपकी पूरी मदद करूंगा। कुछ जानकारी लेनी होगी।",
    "account_opening": "जी हाँ, खाता खोलने में मैं आपकी मदद करता हूँ। कुछ details चाहिए होंगे।",
    "kyc_update":      "KYC update के लिए आ गए हैं? बिल्कुल सही, मैं अभी कर देता हूँ।",
    "fixed_deposit":   "FD करना चाहते हैं? बहुत अच्छा! कुछ जानकारी लेता हूँ।",
    "card_services":   "Card से related समस्या है? मैं अभी solve करता हूँ।",
    "balance_enquiry":  "Balance check करना है? जी बिल्कुल, verification करता हूँ।",
}

FAREWELL_SCRIPTS = {
    "general":         "आपका काम हो गया है। कोई और मदद चाहिए तो बताइए। धन्यवाद!",
    "loan_enquiry":    "आपकी loan application की सारी जानकारी ले ली है। Processing शुरू हो जाएगी। धन्यवाद!",
    "account_opening": "आपका खाता खोलने की सारी details ले ली हैं। Account जल्द active हो जाएगा। धन्यवाद!",
    "kyc_update":      "आपका KYC update हो गया है। कोई और मदद चाहिए तो बताइए।",
    "fixed_deposit":   "आपकी FD की सारी details ले ली हैं। FD जल्द open हो जाएगी। धन्यवाद!",
    "card_services":   "Card की समस्या solve हो गई है। कोई और मदद चाहिए तो बताइए।",
    "balance_enquiry":  "Balance enquiry complete हो गई है। धन्यवाद!",
}


# ── Multilingual greeting messages — auto-sent to customer on connect ────────
# Key = ISO 639-1 language code (matches customer_language_code)

GREETING_MULTILINGUAL = {
    "hi": "🙏 नमस्ते! Union Bank of India में आपका स्वागत है। मैं आपकी क्या सेवा कर सकती हूँ?",
    "mr": "🙏 नमस्कार! Union Bank of India मध्ये आपले स्वागत आहे. मी तुमची कशी सेवा करू शकते?",
    "ta": "🙏 வணக்கம்! Union Bank of India-க்கு வரவேற்கிறோம். நான் உங்களுக்கு எவ்வாறு உதவ முடியும்?",
    "te": "🙏 నమస్కారం! Union Bank of India కి స్వాగతం. నేను మీకు ఎలా సేవ చేయగలను?",
    "bn": "🙏 নমস্কার! Union Bank of India-তে আপনাকে স্বাগতম। আমি আপনার কী সেবা করতে পারি?",
    "kn": "🙏 ನಮಸ್ಕಾರ! Union Bank of India ಗೆ ಸ್ವಾಗತ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸೇವೆ ಮಾಡಬಹುದು?",
    "or": "🙏 ନମସ୍କାର! Union Bank of India ରେ ଆପଣଙ୍କୁ ସ୍ବାଗତ। ମୁଁ ଆପଣଙ୍କ କି ସେବା କରିପାରିବି?",
    "pa": "🙏 ਸਤ ਸ੍ਰੀ ਅਕਾਲ! Union Bank of India ਵਿੱਚ ਤੁਹਾਡਾ ਸੁਆਗਤ ਹੈ। ਮੈਂ ਤੁਹਾਡੀ ਕੀ ਸੇਵਾ ਕਰ ਸਕਦੀ ਹਾਂ?",
    "gu": "🙏 નમસ્તે! Union Bank of India માં આપનું સ્વાગત છે. હું આપની શું સેવા કરી શકું?",
    "ml": "🙏 നമസ്കാരം! Union Bank of India ലേക്ക് സ്വാഗതം. എനിക്ക് നിങ്ങളെ എങ്ങനെ സേവിക്കാം?",
}

# ── Multilingual farewell + "how else can I help?" messages ──────────────────

FAREWELL_MULTILINGUAL = {
    "hi": "🙏 धन्यवाद! आपका काम हो गया है। मैं आपकी और क्या सेवा कर सकती हूँ?",
    "mr": "🙏 धन्यवाद! तुमचे काम झाले आहे. मी तुमची अजून कशी सेवा करू शकते?",
    "ta": "🙏 நன்றி! உங்கள் பணி முடிந்தது. நான் உங்களுக்கு வேறு எப்படி உதவ முடியும்?",
    "te": "🙏 ధన్యవాదాలు! మీ పని పూర్తయింది. నేను మీకు ఇంకా ఎలా సేవ చేయగలను?",
    "bn": "🙏 ধন্যবাদ! আপনার কাজ হয়ে গেছে। আমি আপনার আর কী সেবা করতে পারি?",
    "kn": "🙏 ಧನ್ಯವಾದ! ನಿಮ್ಮ ಕೆಲಸ ಮುಗಿದಿದೆ. ನಾನು ನಿಮಗೆ ಇನ್ನೂ ಹೇಗೆ ಸೇವೆ ಮಾಡಬಹುದು?",
    "or": "🙏 ଧନ୍ୟବାଦ! ଆପଣଙ୍କ କାମ ହୋଇଗଲା। ମୁଁ ଆପଣଙ୍କ ଆଉ କି ସେବା କରିପାରିବି?",
    "pa": "🙏 ਧੰਨਵਾਦ! ਤੁਹਾਡਾ ਕੰਮ ਹੋ ਗਿਆ ਹੈ। ਮੈਂ ਤੁਹਾਡੀ ਹੋਰ ਕੀ ਸੇਵਾ ਕਰ ਸਕਦੀ ਹਾਂ?",
    "gu": "🙏 આભાર! તમારું કામ થઈ ગયું છે. હું તમારી બીજી શું સેવા કરી શકું?",
    "ml": "🙏 നന്ദി! നിങ്ങളുടെ കാര്യം കഴിഞ്ഞു. എനിക്ക് നിങ്ങളെ വേറെ എങ്ങനെ സേവിക്കാം?",
}

# ── Verification time per intent — Hindi + multilingual messages ─────────────
# Each key has: time_text_hi (Hindi), time_text map per language

VERIFICATION_TIME_MAP = {
    "account_opening": {
        "time": "same day",
        "hi": "📋 आपके खाता खोलने की प्रक्रिया Same Day में पूरी हो जाएगी। सभी documents सही हैं।",
        "mr": "📋 तुमचे खाते उघडण्याची प्रक्रिया Same Day पूर्ण होईल. सर्व कागदपत्रे योग्य आहेत.",
        "ta": "📋 உங்கள் கணக்கு திறக்கும் செயல்முறை Same Day முடிவடையும். அனைத்து ஆவணங்களும் சரியாக உள்ளன.",
        "te": "📋 మీ ఖాతా తెరవడం Same Day పూర్తవుతుంది. అన్ని పత్రాలు సరిగ్గా ఉన్నాయి.",
        "bn": "📋 আপনার অ্যাকাউন্ট খোলার প্রক্রিয়া Same Day সম্পন্ন হবে। সমস্ত নথি সঠিক আছে।",
        "en": "📋 Your account opening will be completed same day. All documents are verified.",
    },
    "loan_enquiry": {
        "time": "3-5 working days",
        "hi": "📋 आपके लोन की Verification में 3-5 कार्य दिवस लगेंगे। सभी documents verified हैं।",
        "mr": "📋 तुमच्या कर्जाच्या Verification ला 3-5 कार्य दिवस लागतील. सर्व कागदपत्रे verified आहेत.",
        "ta": "📋 உங்கள் கடன் சரிபார்ப்பு 3-5 வேலை நாட்கள் ஆகும். அனைத்து ஆவணங்களும் verified.",
        "te": "📋 మీ రుణ Verification కు 3-5 పని దినాలు పడుతుంది. అన్ని పత్రాలు verified.",
        "bn": "📋 আপনার ঋণ Verification-এ 3-5 কর্ম দিবস লাগবে। সমস্ত নথি verified।",
        "en": "📋 Your loan verification will take 3-5 working days. All documents are verified.",
    },
    "kyc_update": {
        "time": "2-3 working days",
        "hi": "📋 आपके KYC update की processing में 2-3 कार्य दिवस लगेंगे। सभी documents verified हैं।",
        "mr": "📋 तुमच्या KYC update ला 2-3 कार्य दिवस लागतील. सर्व कागदपत्रे verified आहेत.",
        "ta": "📋 உங்கள் KYC update 2-3 வேலை நாட்கள் ஆகும். அனைத்து ஆவணங்களும் verified.",
        "te": "📋 మీ KYC update కు 2-3 పని దినాలు పడుతుంది. అన్ని పత్రాలు verified.",
        "bn": "📋 আপনার KYC update-এ 2-3 কর্ম দিবস লাগবে। সমস্ত নথি verified।",
        "en": "📋 Your KYC update will take 2-3 working days. All documents are verified.",
    },
    "fixed_deposit": {
        "time": "same day",
        "hi": "📋 आपकी FD Same Day में open हो जाएगी। सभी details verified हैं।",
        "mr": "📋 तुमची FD Same Day उघडेल. सर्व details verified आहेत.",
        "ta": "📋 உங்கள் FD Same Day திறக்கப்படும். அனைத்து details verified.",
        "te": "📋 మీ FD Same Day తెరవబడుతుంది. అన్ని details verified.",
        "bn": "📋 আপনার FD Same Day খোলা হবে। সমস্ত details verified।",
        "en": "📋 Your FD will be opened same day. All details are verified.",
    },
    "card_services": {
        "time": "7-10 working days",
        "hi": "📋 आपके Card की processing में 7-10 कार्य दिवस लगेंगे। Request submit हो गई है।",
        "mr": "📋 तुमच्या Card processing ला 7-10 कार्य दिवस लागतील. Request submit झाली आहे.",
        "ta": "📋 உங்கள் Card processing 7-10 வேலை நாட்கள் ஆகும். Request submit ஆகிவிட்டது.",
        "te": "📋 మీ Card processing కు 7-10 పని దినాలు పడుతుంది. Request submit అయింది.",
        "bn": "📋 আপনার Card processing-এ 7-10 কর্ম দিবস লাগবে। Request submit হয়ে গেছে।",
        "en": "📋 Your card processing will take 7-10 working days. Request is submitted.",
    },
    "balance_enquiry": {
        "time": "immediate",
        "hi": "📋 आपकी Balance enquiry complete हो गई है।",
        "mr": "📋 तुमची Balance enquiry पूर्ण झाली आहे.",
        "ta": "📋 உங்கள் Balance enquiry முடிந்தது.",
        "te": "📋 మీ Balance enquiry పూర్తయింది.",
        "bn": "📋 আপনার Balance enquiry সম্পন্ন হয়েছে।",
        "en": "📋 Your balance enquiry is complete.",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# CORE ENGINE — Pure functions, no LLM dependency
# ══════════════════════════════════════════════════════════════════════════════

def _is_filled(val) -> bool:
    """Check if a collected_info field has a real value."""
    if val is None or val == "" or val is False:
        return False
    if isinstance(val, str) and val.strip().lower() in ("null", "none", "n/a", ""):
        return False
    return True


def _normalize_intent(intent: Optional[str]) -> str:
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


def compute_phase(
    intent: Optional[str],
    collected_info: Dict,
    doc_readiness_score: int = 0,
    conversation_stage: str = "exploring",
    exchange_count: int = 0,
) -> str:
    """
    Deterministic phase detection based on session state.

    Returns one of: greet, educate, collect, verify, process, close
    """
    intent_key = _normalize_intent(intent)
    # No intent or first exchange → GREET
    if intent_key == "general" and exchange_count <= 1:
        return Phase.GREET

    # Get field definitions for this intent
    fields = QUESTION_BANK.get(intent_key, QUESTION_BANK["general"])
    total = len(fields)
    filled = sum(1 for f_id, _, _, _ in fields if _is_filled(collected_info.get(f_id)))

    fill_pct = (filled / max(total, 1)) * 100

    # Phase logic (deterministic rules)
    if conversation_stage == "exploring" and fill_pct < 30:
        return Phase.EDUCATE

    if fill_pct < 80:
        return Phase.COLLECT

    if doc_readiness_score < 80 and intent_key not in ("general", "balance_enquiry"):
        return Phase.VERIFY

    if fill_pct >= 80 and doc_readiness_score >= 80:
        return Phase.PROCESS

    if fill_pct >= 95 and doc_readiness_score >= 95:
        return Phase.CLOSE

    return Phase.COLLECT


def compute_next_actions(
    intent: Optional[str],
    collected_info: Dict,
    doc_readiness_score: int = 0,
    conversation_stage: str = "exploring",
    exchange_count: int = 0,
) -> Dict:
    """
    Core deterministic engine. Returns the complete navigation state.

    Returns:
        {
            "phase": "collect",
            "phase_label": "📋 Collecting Information",
            "greeting_script": "...",
            "farewell_script": "...",
            "total_fields": 12,
            "filled_fields": 5,
            "fill_percent": 42,
            "collected": [{"key": "loan_type", "value": "Home Loan", "label": "Loan Type"}],
            "missing": [{"key": "amount", "priority": 3, "question_hi": "...", "label": "Loan Amount"}],
            "next_question": {"key": "amount", "question_hi": "...", "label": "Loan Amount"},
            "all_complete": false
        }
    """
    intent_key = _normalize_intent(intent)

    phase = compute_phase(
        intent, collected_info, doc_readiness_score,
        conversation_stage, exchange_count,
    )

    fields = QUESTION_BANK.get(intent_key, QUESTION_BANK["general"])

    # Separate collected vs missing
    collected = []
    missing = []

    for field_id, priority, question_hi, label in fields:
        val = collected_info.get(field_id)
        if _is_filled(val):
            collected.append({
                "key": field_id,
                "value": val if isinstance(val, str) else str(val),
                "label": label,
            })
        else:
            missing.append({
                "key": field_id,
                "priority": priority,
                "question_hi": question_hi,
                "label": label,
            })

    # Sort missing by priority (lower = ask first)
    missing.sort(key=lambda x: x["priority"])

    total = len(fields)
    filled_count = len(collected)
    fill_pct = round((filled_count / max(total, 1)) * 100)

    # Next question = first missing field by priority
    next_q = missing[0] if missing else None

    # Phase labels
    PHASE_LABELS = {
        Phase.GREET:   "🤝 Welcome",
        Phase.EDUCATE: "📚 Explaining Service",
        Phase.COLLECT: "📋 Collecting Information",
        Phase.VERIFY:  "📎 Verifying Documents",
        Phase.PROCESS: "✅ Processing Application",
        Phase.CLOSE:   "👋 Session Complete",
    }

    return {
        "phase": phase,
        "phase_label": PHASE_LABELS.get(phase, "📋 Active"),
        "intent": intent_key,
        "greeting_script": GREETING_SCRIPTS.get(intent_key, GREETING_SCRIPTS["general"]),
        "farewell_script": FAREWELL_SCRIPTS.get(intent_key, FAREWELL_SCRIPTS["general"]),
        "total_fields": total,
        "filled_fields": filled_count,
        "fill_percent": fill_pct,
        "collected": collected,
        "missing": missing,
        "next_question": next_q,
        "all_complete": len(missing) == 0,
        "doc_readiness_score": doc_readiness_score,
        "verification_submitted": collected_info.get("verification_submitted", False),
    }

