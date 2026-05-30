"""
VaaniBank AI — PII Service Tests
PSBs Hackathon 2026 | Team Vectora

Tests for Aadhaar, PAN, Phone, Account Number, and DOB detection + masking.
Run with: python -m pytest tests/test_pii_service.py -v
"""

import sys
from pathlib import Path

# Allow imports from backend root
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.pii_service import pii_service, PIIResult


# AADHAAR TESTS

class TestAadhaarDetection:
    def test_aadhaar_with_spaces(self):
        result = pii_service.detect_and_mask("My Aadhaar is 2345 6789 0123")
        assert result.pii_found is True
        assert "aadhaar" in result.pii_types
        assert "2345" not in result.masked_text
        assert "0123" in result.masked_text  # last 4 preserved

    def test_aadhaar_without_spaces(self):
        result = pii_service.detect_and_mask("Aadhaar: 234567890123")
        assert result.pii_found is True
        assert "aadhaar" in result.pii_types

    def test_aadhaar_first_digit_validation(self):
        """Aadhaar cannot start with 0 or 1."""
        result = pii_service.detect("Number is 0345 6789 0123")
        assert "aadhaar" not in result

    def test_no_false_aadhaar_on_short_number(self):
        """8-digit numbers should NOT trigger Aadhaar."""
        result = pii_service.detect("Amount is 23456789")
        assert "aadhaar" not in result


# PAN TESTS

class TestPANDetection:
    def test_valid_pan(self):
        result = pii_service.detect_and_mask("My PAN is ABCDE1234F")
        assert result.pii_found is True
        assert "pan" in result.pii_types
        assert "ABCDE" not in result.masked_text
        assert "1234F" in result.masked_text  # last 5 preserved

    def test_lowercase_pan_not_detected(self):
        """PAN must be uppercase."""
        result = pii_service.detect("My PAN is abcde1234f")
        assert "pan" not in result

    def test_invalid_format_not_detected(self):
        """Wrong pattern should not match."""
        result = pii_service.detect("Code: 12345ABCDE")
        assert "pan" not in result


# PHONE TESTS

class TestPhoneDetection:
    def test_valid_indian_mobile(self):
        result = pii_service.detect_and_mask("Call me at 9876543210")
        assert result.pii_found is True
        assert "phone" in result.pii_types
        assert "987654" not in result.masked_text
        assert "3210" in result.masked_text  # last 4 preserved

    def test_number_starting_with_5_not_detected(self):
        """Indian mobiles start with 6-9 only."""
        result = pii_service.detect("Number: 5876543210")
        assert "phone" not in result

    def test_short_number_not_detected(self):
        """Less than 10 digits should not match."""
        result = pii_service.detect("Code: 98765")
        assert "phone" not in result


# ACCOUNT NUMBER TESTS

class TestAccountNumberDetection:
    def test_account_with_context(self):
        """Account numbers need banking context keywords."""
        result = pii_service.detect_and_mask("My account number is 12345678901234")
        assert result.pii_found is True
        assert "account_number" in result.pii_types

    def test_no_account_without_context(self):
        """Random long numbers without banking keywords should NOT match."""
        result = pii_service.detect("Reference ID: 12345678901234")
        assert "account_number" not in result

    def test_account_with_khata_keyword(self):
        """Hindi keyword 'khata' should trigger context."""
        result = pii_service.detect("Mera khata 98765432101234")
        assert "account_number" in result


# DOB TESTS

class TestDOBDetection:
    def test_dob_slash_format(self):
        result = pii_service.detect_and_mask("DOB: 15/08/1990")
        assert result.pii_found is True
        assert "dob" in result.pii_types
        assert "15/08/1990" not in result.masked_text
        assert "**/**/****" in result.masked_text

    def test_dob_dash_format(self):
        result = pii_service.detect_and_mask("Born on 01-12-1985")
        assert result.pii_found is True
        assert "dob" in result.pii_types

    def test_dob_dot_format(self):
        result = pii_service.detect("Date: 25.03.2000")
        assert "dob" in result

    def test_invalid_month_not_detected(self):
        """Month > 12 should not match."""
        result = pii_service.detect("Value: 15/15/1990")
        assert "dob" not in result

    def test_invalid_day_not_detected(self):
        """Day > 31 should not match."""
        result = pii_service.detect("Value: 35/08/1990")
        assert "dob" not in result


# COMBINED / EDGE CASE TESTS

class TestCombined:
    def test_multiple_pii_types(self):
        text = "Aadhaar 2345 6789 0123 aur PAN ABCDE1234F hai"
        result = pii_service.detect_and_mask(text)
        assert result.pii_found is True
        assert "aadhaar" in result.pii_types
        assert "pan" in result.pii_types

    def test_empty_text(self):
        result = pii_service.detect_and_mask("")
        assert result.pii_found is False
        assert result.pii_types == []

    def test_none_text(self):
        result = pii_service.detect_and_mask(None)
        assert result.pii_found is False

    def test_no_pii_in_clean_text(self):
        result = pii_service.detect_and_mask("I want to open a savings account")
        assert result.pii_found is False
        assert result.pii_types == []
        assert result.masked_text == "I want to open a savings account"

    def test_masking_preserves_surrounding_text(self):
        result = pii_service.detect_and_mask("Before 2345 6789 0123 after")
        assert result.masked_text.startswith("Before")
        assert result.masked_text.endswith("after")

    def test_masking_order_aadhaar_before_account(self):
        """Aadhaar (12 digits) must be masked BEFORE account number
        pattern (9-18 digits) to prevent account regex from swallowing it."""
        text = "account khata Aadhaar 2345 6789 0123"
        result = pii_service.detect_and_mask(text)
        assert "aadhaar" in result.pii_types
        # Verify aadhaar is properly masked (not eaten by account pattern)
        assert "**** **** 0123" in result.masked_text
