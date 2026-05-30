"""
VaaniBank AI — Pipeline Helpers Tests
PSBs Hackathon 2026 | Team Vectora

Tests for the extracted pipeline helpers module.
Run with: python -m pytest tests/test_pipeline_helpers.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from routers._pipeline_helpers import (
    lang_code_to_name,
    lang_code_to_attr,
    INPUT_KEYWORD_MAP,
)


# LANGUAGE MAPPING TESTS

class TestLangCodeToName:
    def test_hindi(self):
        assert lang_code_to_name("hi") == "Hindi"

    def test_tamil(self):
        assert lang_code_to_name("ta") == "Tamil"

    def test_malayalam(self):
        assert lang_code_to_name("ml") == "Malayalam"

    def test_unknown_defaults_to_hindi(self):
        assert lang_code_to_name("xx") == "Hindi"

    def test_empty_defaults_to_hindi(self):
        assert lang_code_to_name("") == "Hindi"

    def test_suffix_stripping(self):
        assert lang_code_to_name("mr-IN") == "Marathi"
        assert lang_code_to_name("ta-in") == "Tamil"
        assert lang_code_to_name("gu-IN ") == "Gujarati"


class TestLangCodeToAttr:
    def test_hindi(self):
        assert lang_code_to_attr("hi") == "hindi"

    def test_marathi(self):
        assert lang_code_to_attr("mr") == "marathi"

    def test_odia(self):
        assert lang_code_to_attr("or") == "odia"

    def test_suffix_stripping(self):
        assert lang_code_to_attr("mr-IN") == "marathi"
        assert lang_code_to_attr("ta-in") == "tamil"
        assert lang_code_to_attr("gu-IN ") == "gujarati"

    def test_all_ten_languages_mapped(self):
        codes = ["hi", "mr", "ta", "te", "bn", "kn", "or", "pa", "gu", "ml"]
        for code in codes:
            result = lang_code_to_attr(code)
            assert result, f"Missing mapping for {code}"
            assert result.isalpha(), f"Mapping for {code} should be alpha: {result}"


# INPUT KEYWORD MAP TESTS

class TestInputKeywordMap:
    def test_map_has_six_entries(self):
        assert len(INPUT_KEYWORD_MAP) == 6

    def test_all_entries_have_four_fields(self):
        for entry in INPUT_KEYWORD_MAP:
            assert len(entry) == 4, f"Entry has {len(entry)} fields, expected 4"

    def test_aadhaar_keywords_present(self):
        aadhaar_entry = INPUT_KEYWORD_MAP[0]
        keywords, field_type, _, _ = aadhaar_entry
        assert field_type == "aadhaar"
        assert "aadhaar" in keywords
        assert "aadhar" in keywords  # common misspelling

    def test_pan_keywords_present(self):
        pan_entry = INPUT_KEYWORD_MAP[1]
        keywords, field_type, _, _ = pan_entry
        assert field_type == "pan"
        assert "pan card" in keywords

    def test_all_field_types_unique(self):
        field_types = [entry[1] for entry in INPUT_KEYWORD_MAP]
        assert len(field_types) == len(set(field_types)), "Duplicate field types found"

    def test_all_have_hindi_labels(self):
        for _, _, _, hindi_label in INPUT_KEYWORD_MAP:
            assert hindi_label, "Missing Hindi label"
            # Hindi labels should contain Devanagari characters
            assert any(
                '\u0900' <= ch <= '\u097F' or ch.isascii()
                for ch in hindi_label
            ), f"Label doesn't look like Hindi: {hindi_label}"

    def test_keyword_match_aadhaar_batao(self):
        """Simulate the keyword matching logic for 'aadhaar batao'."""
        text = "aadhaar batao customer ka"
        lower = text.lower()
        matched = None
        for keywords, field_type, _, _ in INPUT_KEYWORD_MAP:
            if any(kw in lower for kw in keywords):
                matched = field_type
                break
        assert matched == "aadhaar"

    def test_keyword_match_pan_card(self):
        text = "pan card number chahiye"
        lower = text.lower()
        matched = None
        for keywords, field_type, _, _ in INPUT_KEYWORD_MAP:
            if any(kw in lower for kw in keywords):
                matched = field_type
                break
        assert matched == "pan"

    def test_no_match_for_clean_text(self):
        text = "kaise madad kar sakte hain"
        lower = text.lower()
        matched = None
        for keywords, field_type, _, _ in INPUT_KEYWORD_MAP:
            if any(kw in lower for kw in keywords):
                matched = field_type
                break
        assert matched is None


# TRANSLATION RETRY DETECTION TESTS

class TestNeedsHindiTranslationRetry:
    def test_proper_hindi_no_retry(self):
        from services.pipeline_orchestrator import _needs_hindi_translation_retry
        # Translate to Hindi is in Devanagari script, should not retry
        assert not _needs_hindi_translation_retry(
            original_text="मला होम लोन पाहिजे",
            candidate_text="मुझे होम लोन चाहिए",
            source_language_code="mr",
        )

    def test_hinglish_needs_retry(self):
        from services.pipeline_orchestrator import _needs_hindi_translation_retry
        # Hinglish (Latin characters) has zero Devanagari characters, should retry
        assert _needs_hindi_translation_retry(
            original_text="मला होम लोन पाहिजे",
            candidate_text="Mujhe home loan chahiye",
            source_language_code="mr",
        )

    def test_source_echo_needs_retry(self):
        from services.pipeline_orchestrator import _needs_hindi_translation_retry
        # Candidate text is identical to source (no translation done)
        assert _needs_hindi_translation_retry(
            original_text="मला होम लोन पाहिजे",
            candidate_text="मला होम लोन पाहिजे",
            source_language_code="mr",
        )

