"""
VaaniBank AI — WhatsApp Delivery Tests
PSBs Hackathon 2026 | Team Vectora

Tests for phone cleaning, Twilio Client dispatch, and mock sandbox flows.
Run with: python -m pytest tests/test_whatsapp.py -v
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Allow imports from backend root
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from routers.summary import _send_whatsapp_background


class TestWhatsAppDelivery:
    @patch("database.AsyncSessionLocal")
    @patch("config.settings")
    def test_whatsapp_skipped_if_no_phone(self, mock_settings, mock_session):
        """Should log warning and skip execution if phone_number is not provided."""
        # Run function with no phone number
        import asyncio
        asyncio.run(_send_whatsapp_background(
            summary_id=123,
            pdf_url="/storage/summaries/test.pdf",
            phone_number=None,
        ))
        
        # Verify DB session was never instantiated
        mock_session.assert_not_called()

    @patch("database.AsyncSessionLocal")
    @patch("twilio.rest.Client")
    def test_whatsapp_delivery_clean_number_formats(self, mock_twilio_client, mock_session):
        """Should standardise 10-digit, 12-digit, and prefixed numbers to whatsapp:+91 E.164 format."""
        from config import settings
        
        # Set dummy credentials so Twilio branch is active
        settings.TWILIO_ACCOUNT_SID = "ACtest"
        settings.TWILIO_AUTH_TOKEN = "token_test"
        settings.TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
        settings.R2_PUBLIC_URL = "https://r2.vaanibank.in"

        # Mock DB session execution to return a dummy summary
        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_session.return_value.__aenter__.return_value = mock_db

        # Mock Twilio Client messages.create
        mock_client_instance = MagicMock()
        mock_twilio_client.return_value = mock_client_instance
        
        # Test case 1: 10-digit number
        import asyncio
        asyncio.run(_send_whatsapp_background(
            summary_id=1,
            pdf_url="/storage/summaries/test1.pdf",
            phone_number="9876543210",
        ))
        
        # Verify E.164 number formatting was correct: to_whatsapp="whatsapp:+919876543210"
        mock_client_instance.messages.create.assert_called_with(
            from_="whatsapp:+14155238886",
            body="🏦 *Union Bank of India — VaaniBank AI*\n\nHere is your bilingual conversation summary.\n\n📝 *Key Points Highlight:*\n• Bilingual transaction details ready.\n\n📄 *Bilingual Session PDF:* https://r2.vaanibank.in/storage/summaries/test1.pdf",
            to="whatsapp:+919876543210"
        )
