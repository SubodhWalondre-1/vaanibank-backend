"""
VaaniBank AI — Canonical Language Registry
PSBs Hackathon 2026 | Team Vectora

Single source of truth for ALL language-related mappings.
Every module that needs language code → name, attribute, or BCP-47 mapping
MUST import from here — never define its own copy.

Usage:
    from core.language import lang_code_to_name, lang_code_to_attr, SARVAM_LANG_MAP
"""

from __future__ import annotations

from typing import Dict


# Language code → English display name
LANG_CODE_TO_NAME: Dict[str, str] = {
    "hi": "Hindi",
    "mr": "Marathi",
    "ta": "Tamil",
    "te": "Telugu",
    "bn": "Bengali",
    "kn": "Kannada",
    "or": "Odia",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "ml": "Malayalam",
    "en": "English",
}


# Language code → DB column suffix (e.g., ProcessStep.step_text_{attr})
LANG_CODE_TO_ATTR: Dict[str, str] = {
    "hi": "hindi",
    "mr": "marathi",
    "ta": "tamil",
    "te": "telugu",
    "bn": "bengali",
    "kn": "kannada",
    "or": "odia",
    "pa": "punjabi",
    "gu": "gujarati",
    "ml": "malayalam",
}


# BCP-47 mapping for Sarvam API
SARVAM_LANG_MAP: Dict[str, str] = {
    "hi": "hi-IN",
    "mr": "mr-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "bn": "bn-IN",
    "kn": "kn-IN",
    "or": "od-IN",
    "pa": "pa-IN",
    "gu": "gu-IN",
    "ml": "ml-IN",
    "en": "en-IN",
}


# Languages supported by Sarvam STT
SARVAM_LANGUAGES = frozenset(SARVAM_LANG_MAP.keys())


# Reverie language codes
REVERIE_LANG_MAP: Dict[str, str] = {
    "hi": "hi",
    "mr": "mr",
    "ta": "ta",
    "te": "te",
    "bn": "bn",
    "kn": "kn",
    "gu": "gu",
    "ml": "ml",
    "pa": "pa",
    "or": "or",
    "en": "en",
}


# Reverse map: display name (lowercase) → language code
LANG_NAME_TO_CODE: Dict[str, str] = {
    name.lower(): code for code, name in LANG_CODE_TO_NAME.items()
}


# Convenience functions

def lang_code_to_name(code: str) -> str:
    """Convert a BCP-47 short language code to its English name."""
    if not code:
        return "Hindi"
    clean_code = code.split("-")[0].strip().lower()
    return LANG_CODE_TO_NAME.get(clean_code, "Hindi")


def lang_code_to_attr(code: str) -> str:
    """Convert a language code to the DB column suffix (e.g., 'hi' → 'hindi')."""
    if not code:
        return "hindi"
    clean_code = code.split("-")[0].strip().lower()
    return LANG_CODE_TO_ATTR.get(clean_code, "hindi")
