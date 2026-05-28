"""
VaaniBank AI — Language Configuration
PSBs Hackathon 2026 | Team Vectora

Supported Indian languages + their Sarvam TTS voice codes.
Used by: intent_engine.py, process_loader.py, websocket/manager.py
"""

from __future__ import annotations

# ── Language registry ────────────────────────────────────────────────────────
# key  : BCP-47 short code (what Sarvam STT returns)
# value: name, tts_voice (Sarvam Bulbul v3 format), display name

LANGUAGE_CONFIG: dict[str, dict] = {
    "hi": {"name": "Hindi",     "tts_voice": "hi-IN", "display": "हिन्दी"},
    "ta": {"name": "Tamil",     "tts_voice": "ta-IN", "display": "தமிழ்"},
    "gu": {"name": "Gujarati",  "tts_voice": "gu-IN", "display": "ગુજરાતી"},
    "ml": {"name": "Malayalam", "tts_voice": "ml-IN", "display": "മലയാളം"},
    "te": {"name": "Telugu",    "tts_voice": "te-IN", "display": "తెలుగు"},
    "mr": {"name": "Marathi",   "tts_voice": "mr-IN", "display": "मराठी"},
    "bn": {"name": "Bengali",   "tts_voice": "bn-IN", "display": "বাংলা"},
    "kn": {"name": "Kannada",   "tts_voice": "kn-IN", "display": "ಕನ್ನಡ"},
    "or": {"name": "Odia",      "tts_voice": "or-IN", "display": "ଓଡ଼ିଆ"},
    "pa": {"name": "Punjabi",   "tts_voice": "pa-IN", "display": "ਪੰਜਾਬੀ"},
    "en": {"name": "English",   "tts_voice": "en-IN", "display": "English"},
}

# ── Intent registry ───────────────────────────────────────────────────────────
SUPPORTED_INTENTS: list[str] = [
    "HOME_LOAN",
    "PERSONAL_LOAN",
    "EDUCATION_LOAN",
    "VEHICLE_LOAN",
    "FIXED_DEPOSIT",
    "ACCOUNT_OPENING",
    "CIBIL_INFO",
    "GENERAL",
]

# Aliases — LLM might return lowercase/different strings; normalise here
INTENT_ALIASES: dict[str, str] = {
    # lowercase variants
    "home_loan":       "HOME_LOAN",
    "personal_loan":   "PERSONAL_LOAN",
    "education_loan":  "EDUCATION_LOAN",
    "vehicle_loan":    "VEHICLE_LOAN",
    "fixed_deposit":   "FIXED_DEPOSIT",
    "account_opening": "ACCOUNT_OPENING",
    "cibil_info":      "CIBIL_INFO",
    "cibil":           "CIBIL_INFO",
    "general":         "GENERAL",
    # common LLM phrasings
    "fd":              "FIXED_DEPOSIT",
    "car_loan":        "VEHICLE_LOAN",
    "bike_loan":       "VEHICLE_LOAN",
    "student_loan":    "EDUCATION_LOAN",
    "housing_loan":    "HOME_LOAN",
    "property_loan":   "HOME_LOAN",
}


def get_tts_voice(lang_code: str) -> str:
    """Return Sarvam TTS voice code for a BCP-47 short code."""
    short = lang_code.split("-")[0].lower()
    cfg = LANGUAGE_CONFIG.get(short, LANGUAGE_CONFIG["hi"])
    return cfg["tts_voice"]


def get_language_name(lang_code: str) -> str:
    """Return English name for a BCP-47 short code."""
    short = lang_code.split("-")[0].lower()
    return LANGUAGE_CONFIG.get(short, LANGUAGE_CONFIG["hi"])["name"]


def get_language_display(lang_code: str) -> str:
    """Return native-script display name."""
    short = lang_code.split("-")[0].lower()
    return LANGUAGE_CONFIG.get(short, LANGUAGE_CONFIG["hi"])["display"]


def normalise_intent(raw_intent: str) -> str:
    """Normalise raw LLM intent string to a SUPPORTED_INTENTS value."""
    if not raw_intent:
        return "GENERAL"
    upper = raw_intent.strip().upper()
    if upper in SUPPORTED_INTENTS:
        return upper
    lower = raw_intent.strip().lower()
    return INTENT_ALIASES.get(lower, "GENERAL")
