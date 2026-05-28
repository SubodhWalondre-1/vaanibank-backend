"""
VaaniBank AI — PII Detection & Masking Service
PSBs Hackathon 2026 | Team Vectora

Detects and masks Personally Identifiable Information from customer speech
transcriptions before storing in DB or sending to LLM.

RBI-compliant patterns:
  Aadhaar  → "**** **** {last4}"
  PAN      → "*****{last5}"
  Phone    → "******{last4}"
  Account  → "****{last4}"
  DOB      → "**/**/****"

Usage:
    from services.pii_service import pii_service

    result = pii_service.detect_and_mask("My Aadhaar is 2345 6789 0123")
    # result.masked_text  → "My Aadhaar is **** **** 0123"
    # result.pii_found    → True
    # result.pii_types    → ["aadhaar"]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("vaanibank.pii")


# ══════════════════════════════════════════════════════════════════════════════
# RESULT DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PIIResult:
    """Returned by detect_and_mask()."""

    masked_text: str
    pii_found: bool
    pii_types: List[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# COMPILED REGEX PATTERNS
# ══════════════════════════════════════════════════════════════════════════════

# Aadhaar: 12-digit number issued to Indian residents
# First digit 2-9, optional spaces every 4 digits
_AADHAAR_RE = re.compile(r"\b([2-9]\d{3})\s?(\d{4})\s?(\d{4})\b")

# PAN: ABCDE1234F — exactly 5 uppercase letters, 4 digits, 1 uppercase letter
_PAN_RE = re.compile(r"\b([A-Z]{5}\d{4}[A-Z])\b")

# Indian mobile numbers: start with 6-9, total 10 digits
_PHONE_RE = re.compile(r"\b([6-9]\d{5})(\d{4})\b")

# Bank account numbers: 9 to 18 digit sequences
# Placed AFTER aadhaar/phone so shorter matches don't swallow them.
# Negative lookbehind/lookahead prevents matching inside longer digit strings.
_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{5,14})(\d{4})(?!\d)")

# Contextual keywords that must appear near a digit sequence for it to
# be treated as an account number — prevents false positives on random
# long numbers (amounts, timestamps, ZIP codes, etc.).
_ACCOUNT_CONTEXT_KEYWORDS = re.compile(
    r"(?:account|khata|a/c|ac\s*no|saving|current|bank)",
    re.IGNORECASE,
)

# DOB: common Indian date formats — DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
_DOB_RE = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])[\/\-\.](0?[1-9]|1[0-2])[\/\-\.](\d{4})\b"
)


# ══════════════════════════════════════════════════════════════════════════════
# SERVICE CLASS
# ══════════════════════════════════════════════════════════════════════════════

class PIIService:
    """
    Detects and masks PII from free-form text.

    Methods:
        detect(text)          → list of PII type strings found
        mask(text)            → text with all PII replaced by masked tokens
        detect_and_mask(text) → PIIResult(masked_text, pii_found, pii_types)
    """

    # ── Individual maskers ─────────────────────────────────────────────────────

    @staticmethod
    def _mask_aadhaar(text: str) -> tuple[str, int]:
        """Replace Aadhaar numbers; return (new_text, count_replaced)."""
        def _replace(m: re.Match) -> str:
            last4 = m.group(3)
            return f"**** **** {last4}"

        result, count = _AADHAAR_RE.subn(_replace, text)
        return result, count

    @staticmethod
    def _mask_pan(text: str) -> tuple[str, int]:
        """Replace PAN card numbers; return (new_text, count_replaced)."""
        def _replace(m: re.Match) -> str:
            pan = m.group(1)
            last5 = pan[-5:]          # 4 digits + last letter, e.g. "1234F"
            return f"*****{last5}"

        result, count = _PAN_RE.subn(_replace, text)
        return result, count

    @staticmethod
    def _mask_phone(text: str) -> tuple[str, int]:
        """Replace Indian mobile numbers; return (new_text, count_replaced)."""
        def _replace(m: re.Match) -> str:
            last4 = m.group(2)
            return f"******{last4}"

        result, count = _PHONE_RE.subn(_replace, text)
        return result, count

    @staticmethod
    def _mask_account(text: str) -> tuple[str, int]:
        """Replace bank account numbers; return (new_text, count_replaced).
        Only masks if contextual banking keywords are present in the text."""
        if not _ACCOUNT_CONTEXT_KEYWORDS.search(text):
            return text, 0

        def _replace(m: re.Match) -> str:
            last4 = m.group(2)
            return f"****{last4}"

        result, count = _ACCOUNT_RE.subn(_replace, text)
        return result, count

    @staticmethod
    def _mask_dob(text: str) -> tuple[str, int]:
        """Replace date-of-birth patterns; return (new_text, count_replaced)."""
        result, count = _DOB_RE.subn("**/**/****", text)
        return result, count

    # ── Public API ─────────────────────────────────────────────────────────────

    def detect(self, text: str) -> List[str]:
        """
        Scan *text* for PII and return a list of detected PII type strings.

        Returns:
            List of zero or more strings from:
            ["aadhaar", "pan", "phone", "account_number", "dob"]
        """
        found: List[str] = []

        if _AADHAAR_RE.search(text):
            found.append("aadhaar")
        if _PAN_RE.search(text):
            found.append("pan")
        if _PHONE_RE.search(text):
            found.append("phone")
        if _ACCOUNT_CONTEXT_KEYWORDS.search(text) and _ACCOUNT_RE.search(text):
            found.append("account_number")
        if _DOB_RE.search(text):
            found.append("dob")

        if found:
            logger.info("PII detected: %s", found)

        return found

    def mask(self, text: str) -> str:
        """
        Replace all detected PII in *text* with masked tokens.

        Masking order matters — Aadhaar/phone first (12/10 digit) so the
        account_number pattern (9-18 digits) cannot swallow them.

        Returns:
            Text string with PII replaced.
        """
        # Order: most-specific digit patterns first
        text, _ = self._mask_aadhaar(text)
        text, _ = self._mask_pan(text)
        text, _ = self._mask_phone(text)
        text, _ = self._mask_account(text)
        text, _ = self._mask_dob(text)
        return text

    def detect_and_mask(self, text: str) -> PIIResult:
        """
        Detect and mask PII in a single pass.

        Each regex runs exactly once — the mask count tells us whether
        that PII type was present.  Previous implementation ran every
        pattern twice (once for detect, once for mask).

        Returns:
            PIIResult with masked_text, pii_found flag, and pii_types list.
        """
        if not text or not text.strip():
            return PIIResult(masked_text=text, pii_found=False, pii_types=[])

        orig_len = len(text)
        pii_types: List[str] = []

        # Order: most-specific digit patterns first (same as mask())
        text, count = self._mask_aadhaar(text)
        if count:
            pii_types.append("aadhaar")

        text, count = self._mask_pan(text)
        if count:
            pii_types.append("pan")

        text, count = self._mask_phone(text)
        if count:
            pii_types.append("phone")

        text, count = self._mask_account(text)
        if count:
            pii_types.append("account_number")

        text, count = self._mask_dob(text)
        if count:
            pii_types.append("dob")

        pii_found = len(pii_types) > 0

        if pii_found:
            logger.info(
                "PII masked | types=%s | original_len=%d | masked_len=%d",
                pii_types,
                orig_len,
                len(text),
            )

        return PIIResult(
            masked_text=text,
            pii_found=pii_found,
            pii_types=pii_types,
        )


# ── Module-level singleton ─────────────────────────────────────────────────────
pii_service = PIIService()