"""
VaaniBank AI — Language Config & Intent Normalisation Tests
PSBs Hackathon 2026 | Team Vectora

Tests for language_config.py utility functions.
Run with: python -m pytest tests/test_language_config.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from language_config import (
    LANGUAGE_CONFIG,
    SUPPORTED_INTENTS,
    normalise_intent,
    get_language_name,
    get_language_display,
    get_tts_voice,
)


# INTENT NORMALISATION

class TestNormaliseIntent:
    def test_uppercase_passthrough(self):
        assert normalise_intent("HOME_LOAN") == "HOME_LOAN"

    def test_lowercase_to_uppercase(self):
        assert normalise_intent("home_loan") == "HOME_LOAN"

    def test_alias_fd(self):
        assert normalise_intent("fd") == "FIXED_DEPOSIT"

    def test_alias_car_loan(self):
        assert normalise_intent("car_loan") == "VEHICLE_LOAN"

    def test_alias_housing_loan(self):
        assert normalise_intent("housing_loan") == "HOME_LOAN"

    def test_alias_student_loan(self):
        assert normalise_intent("student_loan") == "EDUCATION_LOAN"

    def test_unknown_returns_general(self):
        assert normalise_intent("random_unknown") == "GENERAL"

    def test_empty_string_returns_general(self):
        assert normalise_intent("") == "GENERAL"

    def test_none_returns_general(self):
        assert normalise_intent(None) == "GENERAL"

    def test_mixed_case_normalised(self):
        assert normalise_intent("Home_Loan") == "HOME_LOAN"

    def test_all_supported_intents_passthrough(self):
        for intent in SUPPORTED_INTENTS:
            assert normalise_intent(intent) == intent


# LANGUAGE HELPERS

class TestLanguageHelpers:
    def test_get_language_name_hindi(self):
        assert get_language_name("hi") == "Hindi"

    def test_get_language_name_tamil(self):
        assert get_language_name("ta") == "Tamil"

    def test_get_language_name_with_suffix(self):
        """Should handle BCP-47 format like 'hi-IN'."""
        assert get_language_name("hi-IN") == "Hindi"

    def test_get_language_name_unknown_fallback(self):
        assert get_language_name("xx") == "Hindi"

    def test_get_language_display_hindi(self):
        assert get_language_display("hi") == "हिन्दी"

    def test_get_language_display_tamil(self):
        assert get_language_display("ta") == "தமிழ்"

    def test_get_tts_voice_hindi(self):
        assert get_tts_voice("hi") == "hi-IN"

    def test_get_tts_voice_malayalam(self):
        assert get_tts_voice("ml") == "ml-IN"

    def test_get_tts_voice_with_suffix(self):
        assert get_tts_voice("ta-IN") == "ta-IN"

    def test_all_languages_have_tts_voice(self):
        for code in LANGUAGE_CONFIG:
            voice = get_tts_voice(code)
            assert voice.endswith("-IN"), f"{code} has unexpected TTS voice: {voice}"

    def test_all_languages_have_display_name(self):
        for code in LANGUAGE_CONFIG:
            display = get_language_display(code)
            assert display, f"{code} has empty display name"
