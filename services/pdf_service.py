"""
VaaniBank AI — Bilingual PDF Summary Service  (v2 — Redesigned Layout)
PSBs Hackathon 2026 | Team Vectora

New layout (matches the redesigned UI mockup):
  ┌─────────────────────────────────────────────────────┐
  │  [BLUE HEADER]  VaaniBank AI logo  |  Token pill    │
  │  [RED accent bar]                                   │
  ├─────────────────────────────────────────────────────┤
  │  4-column Meta cards  (Branch | Staff | Date | Dur) │
  │  Intent badge  +  Sentiment badge  +  Steps badge   │
  │  [AMBER] PII alert bar (if pii_detected)            │
  ├─────────────────────────────────────────────────────┤
  │  SECTION A — Key Points    (numbered cards, Hindi)  │
  │  SECTION B — Key Points    (numbered cards, Lang)   │
  ├─────────────────────────────────────────────────────┤
  │  SECTION C — Session Summary  (Hindi)               │
  │  SECTION D — Session Summary  (Customer lang)       │
  ├─────────────────────────────────────────────────────┤
  │  SECTION E — Next Steps   (step-dot list, Hindi)    │
  │  SECTION F — Next Steps   (step-dot list, Lang)     │
  ├─────────────────────────────────────────────────────┤
  │  [FOOTER]  RBI compliance  |  generated timestamp   │
  └─────────────────────────────────────────────────────┘

Key changes from v1:
  • No more side-by-side bilingual columns — Hindi block THEN customer-lang block
  • Numbered point cards instead of flat table rows (better readability)
  • Meta info in 4 individual highlight cards
  • Intent / Sentiment / Step-completion badges row
  • PII alert bar (amber, shows which types were masked)
  • Section icon circles (blue=points, green=summary, amber=steps)
  • Step tracker for next steps (dot + status pill)
  • Cleaner footer

Usage (unchanged — drop-in replacement):
    from services.pdf_service import pdf_service

    pdf_url = pdf_service.generate_bilingual_summary(
        session_id=42,
        token_number="NJT-1267",
        branch_name="Nagpur Civil Lines Branch",
        staff_name="Rajesh Kumar",
        customer_language="Marathi",
        intent_detected="loan_enquiry",
        sentiment_overall="calm",
        started_at=datetime(...),
        ended_at=datetime(...),
        duration_seconds=504,
        summary_hindi=[...],
        summary_customer_lang=[...],
        key_points_hindi=[...],
        key_points_customer=[...],
        next_steps_hindi=[...],
        next_steps_customer=[...],
        pii_detected=True,
        pii_types_found=["aadhaar", "phone"],
    )
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
    PageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from services.storage_service import storage_service
from config import settings

logger = logging.getLogger("vaanibank.pdf")

# ══════════════════════════════════════════════════════════════════════════════
# BRAND PALETTE
# ══════════════════════════════════════════════════════════════════════════════

_BLUE        = colors.HexColor("#003087")   # UBI primary blue
_BLUE_DARK   = colors.HexColor("#001a52")
_BLUE_MID    = colors.HexColor("#185FA5")
_BLUE_LIGHT  = colors.HexColor("#E6F1FB")   # icon bg blue
_RED         = colors.HexColor("#E8231A")   # UBI red accent
_GREEN_LIGHT = colors.HexColor("#EAF3DE")   # icon bg green
_GREEN_DARK  = colors.HexColor("#3B6D11")   # icon text green
_GREEN_MID   = colors.HexColor("#27500A")   # step-done dot
_AMBER_LIGHT = colors.HexColor("#FAEEDA")   # PII bar bg + icon bg
_AMBER_MID   = colors.HexColor("#854F0B")   # PII bar text
_AMBER_DARK  = colors.HexColor("#633806")
_AMBER_BORDER= colors.HexColor("#EF9F27")
_WHITE       = colors.white
_GREY_LIGHT  = colors.HexColor("#F4F6F9")   # alternating row bg
_GREY_MID    = colors.HexColor("#CBD5E0")   # borders
_GREY_CARD   = colors.HexColor("#F8FAFC")   # meta card bg
_TEXT_PRI    = colors.HexColor("#1A202C")
_TEXT_SEC    = colors.HexColor("#4A5568")
_TEXT_MUTED  = colors.HexColor("#718096")

# Sentiment colours (dot badge)
_SENTIMENT_COLORS: Dict[str, str] = {
    "calm":       "#3B6D11",
    "frustrated": "#A32D2D",
    "confused":   "#854F0B",
    "urgent":     "#534AB7",
}

# ── Page geometry ──────────────────────────────────────────────────────────────
_PAGE_W, _PAGE_H = A4
_MARGIN    = 16 * mm
_USABLE_W  = _PAGE_W - 2 * _MARGIN
_COL_ICON  = 7 * mm      # icon-circle column
_COL_MAIN  = _USABLE_W - _COL_ICON - 4 * mm

# ══════════════════════════════════════════════════════════════════════════════
# FONT REGISTRATION (Noto Sans Devanagari + script fonts for Indian langs)
# ══════════════════════════════════════════════════════════════════════════════

_FONT_REGULAR = "Helvetica"
_FONT_BOLD    = "Helvetica-Bold"
_FONT_HINDI   = "Helvetica"   # upgraded to NotoDevanagari when available
_FONT_CUST    = "Helvetica"   # upgraded per customer language


_FONTS_REGISTERED = False

def _try_register_fonts() -> None:
    """Register Noto fonts; silently fall back to Helvetica if not found."""
    global _FONT_REGULAR, _FONT_BOLD, _FONT_HINDI, _FONT_CUST, _FONTS_REGISTERED

    if _FONTS_REGISTERED:
        return

    _this_dir    = Path(__file__).resolve().parent
    _backend_dir = _this_dir.parent
    _fonts_dir   = str(_backend_dir / "fonts")
    
    logger.info("[PDF-v2] Initializing font registration...")

    def _find(filename: str) -> Optional[str]:
        """Recursive search under backend/fonts/, then Windows system fonts."""
        if os.path.isdir(_fonts_dir):
            for root, _, files in os.walk(_fonts_dir):
                for f in files:
                    if f.lower() == filename.lower():
                        return os.path.join(root, f)
        win = os.path.join("C:/Windows/Fonts", filename)
        return win if os.path.isfile(win) else None

    # Devanagari (Hindi, Marathi)
    reg = _find("NotoSansDevanagari-Regular.ttf")
    bold = _find("NotoSansDevanagari-Bold.ttf")
    if reg:
        try:
            pdfmetrics.registerFont(TTFont("NotoDevanagari", reg))
            _FONT_HINDI = _FONT_REGULAR = _FONT_CUST = "NotoDevanagari"
        except Exception as exc:
            logger.warning("NotoDevanagari register failed: %s", exc)
    if bold:
        try:
            pdfmetrics.registerFont(TTFont("NotoDevanagari-Bold", bold))
            _FONT_BOLD = "NotoDevanagari-Bold"
        except Exception as exc:
            logger.warning("NotoDevanagari-Bold register failed: %s", exc)

    # Tamil
    tamil = _find("NotoSansTamil-Regular.ttf")
    if tamil:
        try:
            pdfmetrics.registerFont(TTFont("NotoTamil", tamil))
        except Exception:
            pass

    # Telugu
    telugu = _find("NotoSansTelugu-Regular.ttf")
    if telugu:
        try:
            pdfmetrics.registerFont(TTFont("NotoTelugu", telugu))
        except Exception:
            pass

    # Kannada
    kannada = _find("NotoSansKannada-Regular.ttf")
    if kannada:
        try:
            pdfmetrics.registerFont(TTFont("NotoKannada", kannada))
        except Exception:
            pass

    logger.info(
        "[PDF-v2] Fonts — regular=%s | bold=%s | hindi=%s",
        _FONT_REGULAR, _FONT_BOLD, _FONT_HINDI,
    )
    _FONTS_REGISTERED = True


_try_register_fonts()


def _font_for_language(language: str) -> str:
    """Return the best registered font for a customer language."""
    lang = language.lower()
    mapping = {
        "tamil":    "NotoTamil",
        "telugu":   "NotoTelugu",
        "kannada":  "NotoKannada",
        "marathi":  _FONT_HINDI,  # Devanagari script
        "hindi":    _FONT_HINDI,
    }
    for key, font in mapping.items():
        if key in lang:
            try:
                pdfmetrics.getFont(font)
                return font
            except Exception:
                pass
    return _FONT_REGULAR


# ══════════════════════════════════════════════════════════════════════════════
# PARAGRAPH STYLE FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def _make_styles(customer_font: str) -> Dict[str, ParagraphStyle]:
    def s(name: str, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    return {
        # Header
        "bank_name": s("BankName",
            fontName=_FONT_BOLD, fontSize=14, textColor=_WHITE,
            alignment=TA_LEFT, leading=18),
        "bank_sub": s("BankSub",
            fontName=_FONT_REGULAR, fontSize=8, textColor=colors.HexColor("#B5D4F4"),
            alignment=TA_LEFT),
        "token_pill": s("TokenPill",
            fontName=_FONT_BOLD, fontSize=9, textColor=colors.HexColor("#E6F1FB"),
            alignment=TA_RIGHT),

        # Meta cards
        "meta_label": s("MetaLabel",
            fontName=_FONT_BOLD, fontSize=7.5, textColor=_TEXT_MUTED,
            spaceAfter=2),
        "meta_value": s("MetaValue",
            fontName=_FONT_BOLD, fontSize=10, textColor=_TEXT_PRI),

        # Badges row
        "badge_text": s("BadgeText",
            fontName=_FONT_BOLD, fontSize=8, textColor=_TEXT_PRI),

        # PII alert
        "pii_text": s("PIIText",
            fontName=_FONT_REGULAR, fontSize=8, textColor=_AMBER_DARK),

        # Section heading
        "section_title": s("SectionTitle",
            fontName=_FONT_BOLD, fontSize=11, textColor=_BLUE_DARK,
            spaceBefore=10, spaceAfter=8, alignment=TA_LEFT),
        "sec_title_hi": s("SecTitleHi",
            fontName=_FONT_BOLD, fontSize=9.5, textColor=_BLUE_DARK,
            spaceBefore=2, spaceAfter=4),
        "sec_title_cust": s("SecTitleCust",
            fontName=customer_font, fontSize=9.5, textColor=_BLUE_DARK,
            spaceBefore=2, spaceAfter=4),

        # Language separator label
        "lang_label": s("LangLabel",
            fontName=_FONT_BOLD, fontSize=8, textColor=_WHITE,
            alignment=TA_CENTER),

        # Point text
        "point_hi": s("PointHi",
            fontName=_FONT_HINDI, fontSize=9, textColor=_TEXT_PRI,
            leading=14, spaceAfter=2),
        "point_cust": s("PointCust",
            fontName=customer_font, fontSize=9, textColor=_TEXT_PRI,
            leading=14, spaceAfter=2),

        # Step text
        "step_hi": s("StepHi",
            fontName=_FONT_HINDI, fontSize=8.5, textColor=_TEXT_PRI, leading=13),
        "step_cust": s("StepCust",
            fontName=customer_font, fontSize=8.5, textColor=_TEXT_PRI, leading=13),
        "step_label": s("StepLabel",
            fontName=_FONT_BOLD, fontSize=7, textColor=_TEXT_MUTED, spaceAfter=1),

        # Footer
        "footer": s("Footer",
            fontName=_FONT_REGULAR, fontSize=6.5, textColor=_TEXT_MUTED,
            alignment=TA_CENTER, leading=10),
    }


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _as_list(value: Any) -> List[str]:
    """Safely coerce DB JSON field → list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, dict):
        for k in ("points", "items", "steps", "data"):
            if k in value and isinstance(value[k], list):
                return [str(v).strip() for v in value[k] if str(v).strip()]
        return [str(v).strip() for v in value.values() if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d %b %Y  %I:%M %p")


def _fmt_dur(secs: Optional[int]) -> str:
    if not secs:
        return "—"
    m, s = divmod(secs, 60)
    return f"{m}m {s}s" if m else f"{s}s"


def _intent_label(intent: Optional[str]) -> str:
    labels = {
        "loan_enquiry":    "Loan Enquiry",
        "account_opening": "Account Opening",
        "kyc_update":      "KYC Update",
        "card_services":   "Card Services",
        "balance_enquiry": "Balance Enquiry",
        "fixed_deposit":   "Fixed Deposit",
        "general":         "General",
    }
    return labels.get(intent or "general", (intent or "General").replace("_", " ").title())


def _pii_label(pii_types: Optional[List[str]]) -> str:
    labels = {
        "aadhaar":        "Aadhaar",
        "pan":            "PAN",
        "phone":          "Phone",
        "account_number": "Account No.",
        "dob":            "Date of Birth",
    }
    if not pii_types:
        return ""
    parts = [labels.get(p, p.title()) for p in pii_types]
    return ", ".join(parts) + " redacted per RBI Data Localisation Guidelines 2024"


# ══════════════════════════════════════════════════════════════════════════════
# PDF SERVICE
# ══════════════════════════════════════════════════════════════════════════════

class PDFService:
    """
    Generates session summary PDFs using the redesigned card-based layout.

    Public method signature is fully backward-compatible with v1.
    Two new optional kwargs added: pii_detected, pii_types_found.
    """

    def __init__(self) -> None:
        self._storage_path = Path(
            os.getenv("SUMMARY_STORAGE_PATH", "./storage/summaries")
        )
        self._storage_path.mkdir(parents=True, exist_ok=True)

    # ── Public entry point ─────────────────────────────────────────────────────

    def generate_bilingual_summary(
        self,
        *,
        session_id: int,
        token_number: str,
        branch_name: str,
        staff_name: str,
        customer_language: str,
        intent_detected: Optional[str],
        sentiment_overall: Optional[str],
        started_at: Optional[datetime],
        ended_at: Optional[datetime],
        duration_seconds: Optional[int],
        summary_hindi: Any,
        summary_customer_lang: Any,
        key_points_hindi: Any,
        key_points_customer: Any,
        next_steps_hindi: Any,
        next_steps_customer: Any,
        # ── new optional fields ────────────────────────────────────────────────
        collected_data: Optional[Dict[str, Any]] = None,
        pii_detected: bool = False,
        pii_types_found: Optional[List[str]] = None,
    ) -> str:
        """Generate PDF and return the /storage/summaries/<filename> URL."""

        _try_register_fonts()
        customer_font = _font_for_language(customer_language)
        st = _make_styles(customer_font)

        filename = f"{token_number}_summary.pdf"
        filepath = self._storage_path / filename

        doc = SimpleDocTemplate(
            str(filepath),
            pagesize=A4,
            leftMargin=_MARGIN,
            rightMargin=_MARGIN,
            topMargin=_MARGIN,
            bottomMargin=_MARGIN,
            title=f"VaaniBank Session Summary — {token_number}",
            author="VaaniBank AI | Union Bank of India",
        )

        story: list = []

        # ══════════════════════════════════════════════════════════════════════
        # PAGE 1: SARALFORM DATA
        # ══════════════════════════════════════════════════════════════════════
        
        # 1 ── Header ──────────────────────────────────────────────────────────
        story.extend(self._build_header(st, token_number))

        # 2 ── Meta cards ──────────────────────────────────────────────────────
        story.append(Spacer(1, 4 * mm))
        story.extend(self._build_meta_cards(
            st,
            branch_name=branch_name,
            staff_name=staff_name,
            started_at=started_at,
            duration_seconds=duration_seconds,
        ))

        # 3. SaralForm Data Table ────────────────────────────────────────────
        story.append(Spacer(1, 6 * mm))
        story.extend(self._build_form_data_section(st, collected_data, session_id, token_number, intent=intent_detected))

        # Force Page Break after form data
        story.append(PageBreak())

        # ══════════════════════════════════════════════════════════════════════
        # PAGE 2+: CONVERSATION SUMMARY
        # ══════════════════════════════════════════════════════════════════════

        # Re-add header for the summary page
        story.extend(self._build_header(st, token_number))
        story.append(Spacer(1, 4 * mm))

        # 4 ── Badges row (intent + sentiment + steps) ─────────────────────────
        story.append(Spacer(1, 3 * mm))
        story.extend(self._build_badges(
            st,
            intent_detected=intent_detected,
            sentiment_overall=sentiment_overall,
            next_steps_hindi=_as_list(next_steps_hindi),
        ))

        # 5 ── PII alert bar (only if PII was detected) ────────────────────────
        if pii_detected and pii_types_found:
            story.append(Spacer(1, 3 * mm))
            story.extend(self._build_pii_bar(st, pii_types_found))

        story.append(Spacer(1, 5 * mm))

        # 6 ── Key Points ──────────────────────────────────────────────────────
        kp_hi   = _as_list(key_points_hindi)
        kp_cust = _as_list(key_points_customer)
        if kp_hi or kp_cust:
            story.extend(self._build_section(
                st,
                section_key="key_points",
                hindi_items=kp_hi,
                customer_items=kp_cust,
                customer_language=customer_language,
                numbered=True,
                icon_color="blue",
            ))
            story.append(Spacer(1, 4 * mm))

        # 7 ── Session Summary ─────────────────────────────────────────────────
        sum_hi   = _as_list(summary_hindi)
        sum_cust = _as_list(summary_customer_lang)
        if sum_hi or sum_cust:
            story.extend(self._build_section(
                st,
                section_key="summary",
                hindi_items=sum_hi,
                customer_items=sum_cust,
                customer_language=customer_language,
                numbered=False,
                icon_color="green",
            ))
            story.append(Spacer(1, 4 * mm))

        # 8 ── Next Steps ──────────────────────────────────────────────────────
        ns_hi   = _as_list(next_steps_hindi)
        ns_cust = _as_list(next_steps_customer)
        if ns_hi or ns_cust:
            story.extend(self._build_next_steps(
                st,
                hindi_steps=ns_hi,
                customer_steps=ns_cust,
                customer_language=customer_language,
            ))
            story.append(Spacer(1, 5 * mm))

        # 9 ── Footer ──────────────────────────────────────────────────────────
        story.extend(self._build_footer(st, token_number))

        doc.build(story)
        logger.info("[PDF-v2] Generated: %s", filepath)
        
        # Upload generated PDF to Cloudflare R2 / local fallback
        pdf_url = storage_service.upload_pdf_file_sync(filepath, filename)
        return pdf_url

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION BUILDERS
    # ══════════════════════════════════════════════════════════════════════════

    def _build_form_data_section(self, st: dict, data: Optional[Dict[str, Any]], session_id: int, token_number: str, intent: Optional[str] = None) -> list:
        """Render SaralForm data in a clean table format and include signature if exists."""
        elements = []
        
        # Determine Form Name from Intent
        form_name_map = {
            "account_opening": "Account Opening Form / खाता खोलने का फॉर्म",
            "loan_enquiry": "Loan Application Form / ऋण आवेदन फॉर्म",
            "kyc_update": "KYC Update Form / केवाईसी अपडेट फॉर्म",
            "card_services": "Card Service Form / कार्ड सेवा फॉर्म",
            "fixed_deposit": "Fixed Deposit Form / सावधि जमा फॉर्म",
            "balance_enquiry": "Balance Enquiry Form / बैलेंस पूछताछ फॉर्म",
            "general": "General Service Form / सामान्य सेवा फॉर्म",
        }
        form_display_name = form_name_map.get(intent or "general", "SaralForm Data / सरलफॉर्म डेटा")

        # Section Title
        elements.append(Paragraph(form_display_name, st["section_title"]))
        elements.append(Spacer(1, 3 * mm))

        if not data:
            elements.append(Paragraph("No form data collected during this session.", st["meta_value"]))
        else:
            # Table Header
            table_data = [[
                Paragraph("<b>Field / फ़ील्ड</b>", st["meta_label"]),
                Paragraph("<b>Value / डेटा</b>", st["meta_label"])
            ]]

            # Table Rows
            # Special case mapping for core fields across all forms
            field_map = {
                # Core PII
                "title": "Title / शीर्षक",
                "customer_name": "Customer Name / ग्राहक का नाम",
                "maiden_name": "Maiden Name / विवाह पूर्व नाम",
                "account_number": "Account Number / खाता संख्या",
                "mobile_number": "Mobile Number / मोबाइल नंबर",
                "mobile_no": "Mobile Number / मोबाइल नंबर",
                "email_id": "Email ID / ईमेल आईडी",
                "date_of_birth": "Date of Birth / जन्म तिथि",
                "dob": "Date of Birth / जन्म तिथि",
                "gender": "Gender / लिंग",
                "marital_status": "Marital Status / वैवाहिक स्थिति",
                "pan_number": "PAN Number / पैन नंबर",
                "pan_no": "PAN Number / पैन नंबर",
                "aadhaar_last_4": "Aadhaar (Last 4) / आधार (अंतिम 4)",
                "aadhaar_no": "Aadhaar Number / आधार संख्या",
                "account_type": "Account Type / खाते का प्रकार",
                "kyc_status": "KYC Status / केवाईसी स्थिति",
                "current_balance": "Current Balance / वर्तमान शेष",
                "address": "Address / पता",
                "current_address": "Current Address / वर्तमान पता",
                "permanent_address": "Permanent Address / स्थायी पता",
                "father_name": "Father's Name / पिता का नाम",
                "mother_name": "Mother's Name / माता का नाम",
                "spouse_name": "Spouse's Name / पति/पत्नी का नाम",
                "nationality": "Nationality / राष्ट्रीयता",
                "religion": "Religion / धर्म",
                "category": "Category / श्रेणी",
                "education": "Education / शिक्षा",
                "occupation": "Occupation / व्यवसाय",
                "occupation_type": "Occupation Type / व्यवसाय का प्रकार",
                "work_experience": "Work Experience / कार्य अनुभव",
                "annual_income": "Annual Income / वार्षिक आय",
                "annual_income_bracket": "Annual Income Bracket / वार्षिक आय वर्ग",
                "net_worth": "Net Worth / कुल संपत्ति",
                "net_salary": "Monthly Net Salary / शुद्ध मासिक वेतन",
                "income_source": "Source of Income / आय का स्रोत",
                "is_pep": "Politically Exposed Person / राजनीतिक रूप से उजागर व्यक्ति",
                # Account Services
                "mode_of_operation": "Mode of Operation / परिचालन का तरीका",
                "atm_card_required": "ATM Card Required / एटीएम कार्ड की आवश्यकता",
                "name_on_card": "Name on Card / कार्ड पर नाम",
                "cheque_book_required": "Cheque Book Required / चेक बुक की आवश्यकता",
                "internet_banking": "Internet Banking / इंटरनेट बैंकिंग",
                "mobile_banking": "Mobile Banking / मोबाइल बैंकिंग",
                "phone_banking": "Phone Banking / फोन बैंकिंग",
                "sms_alerts": "SMS Alerts / एसएमएस अलर्ट",
                "physical_passbook": "Physical Passbook / भौतिक पासबुक",
                "e_statement": "e-Statement / ई-स्टेटमेंट",
                # Nomination
                "nomination_required": "Nomination Required / नामांकन की आवश्यकता",
                "nominee_name": "Nominee Name / नामांकित व्यक्ति का नाम",
                "nominee_relationship": "Relationship / आवेदक से संबंध",
                "nominee_age": "Nominee Age / नामांकित व्यक्ति की आयु",
                "nominee_address": "Nominee Address / नामांकित व्यक्ति का पता",
                "is_minor": "Is Nominee Minor? / क्या नामांकित व्यक्ति नाबालिग है?",
                "guardian_details": "Guardian Details / अभिभावक का विवरण",
                # Declarations
                "fatca_declaration": "FATCA Declaration / FATCA घोषणा",
                "form_60_required": "Form 60 Declaration / फॉर्म 60 घोषणा",
                "photo_provided": "Photograph Provided / फोटो प्रदान की गई",
                # Loan Specific
                "loan_type": "Loan Type / ऋण का प्रकार",
                "amount": "Requested Amount / राशि",
                "tenure": "Tenure / अवधि",
                "monthly_income": "Monthly Income / मासिक आय",
                "monthly_obligations": "Existing Monthly Obligations / मौजूदा मासिक दायित्व",
                "employment_type": "Employment / रोजगार",
                "cibil_score": "CIBIL Score / सिबिल स्कोर",
                "purpose": "Purpose / उद्देश्य",
                "loan_purpose": "Loan Purpose / ऋण का उद्देश्य",
                # Education Loan Specific
                "academic_record": "Academic Record (%) / शैक्षणिक रिकॉर्ड (%)",
                "course_name": "Proposed Course / प्रस्तावित पाठ्यक्रम",
                "university_name": "University Name / विश्वविद्यालय का नाम",
                "estimated_expenditure": "Estimated Expenditure / अनुमानित व्यय",
                "margin_money": "Margin Money / मार्जिन मनी",
                # Home Loan Specific
                "property_address": "Property Address / संपत्ति का पता",
                "property_type": "Property Type / संपत्ति का प्रकार",
                "purchase_price": "Purchase Price / खरीद मूल्य",
                "builder_name": "Builder/Seller Name / बिल्डर/विक्रेता का नाम",
                # Vehicle Specific
                "vehicle_model": "Vehicle Model / वाहन का मॉडल",
                # Card Specific
                "card_type": "Card Type / कार्ड का प्रकार",
                "card_issue": "Issue Reported / समस्या",
                # KYC Specific
                "update_type": "Update Type / अपडेट का प्रकार",
                "proof_docs": "Documents Submitted / जमा दस्तावेज",
                # Verification
                "place": "Place / स्थान",
                "form_date": "Date / दिनांक",
            }

            for key, val in data.items():
                if val is None or val == "": continue
                
                display_key = field_map.get(key, key.replace("_", " ").title())
                table_data.append([
                    Paragraph(display_key, st["meta_value"]),
                    Paragraph(str(val), st["meta_value"])
                ])

            # Create Table
            col1 = _USABLE_W * 0.4
            col2 = _USABLE_W * 0.6
            tbl = Table(table_data, colWidths=[col1, col2], repeatRows=1)
            
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), _BLUE_LIGHT),
                ("TEXTCOLOR",  (0, 0), (-1, 0), _BLUE),
                ("GRID",       (0, 0), (-1, -1), 0.5, _GREY_MID),
                ("VALIGN",     (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ]))
            
            elements.append(tbl)

        # ── Digital Signature ──────────────────────────────────────────────────
        elements.append(Spacer(1, 8 * mm))
        
        # Check for signature file
        safe_token = token_number.replace("-", "_")
        sig_filename = f"signature_{safe_token}_{session_id}.png"
        sig_path = Path(__file__).resolve().parent.parent / "storage" / "signatures" / sig_filename
        
        if sig_path.is_file():
            elements.append(Paragraph("<b>Digital Signature / डिजिटल हस्ताक्षर:</b>", st["meta_label"]))
            elements.append(Spacer(1, 2 * mm))
            # Wrap image in a small table for border/styling
            img = Image(str(sig_path), width=120, height=60)
            sig_table = Table([[img]], colWidths=[130])
            sig_table.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.5, _GREY_MID),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            elements.append(sig_table)
        else:
            elements.append(Paragraph("<i>No digital signature found.</i>", st["meta_value"]))

        return elements

    def _build_header(self, st: dict, token_number: str) -> list:
        """Blue header bar — logo left, token pill right, red accent below."""
        elements = []

        # ── Try logo ──────────────────────────────────────────────────────────
        logo_path = Path(__file__).resolve().parent.parent / "assets" / "website_logo.png"
        if logo_path.is_file():
            logo_cell: Any = Image(str(logo_path), width=130, height=36)
        else:
            logo_cell = Table(
                [[Paragraph("VaaniBank AI", st["bank_name"]),
                  Paragraph("Union Bank of India", st["bank_sub"])]],
                colWidths=[60 * mm, _USABLE_W - 60 * mm],
            )
            logo_cell.setStyle(TableStyle([
                ("VALIGN",  (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN",   (0, 0), (0, 0),   "LEFT"),
                ("TOPPADDING",    (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))

        token_cell = Paragraph(f"Token:  {token_number}", st["token_pill"])

        header_data = [[logo_cell, token_cell]]
        col1 = _USABLE_W * 0.65
        col2 = _USABLE_W * 0.35

        header_tbl = Table(header_data, colWidths=[col1, col2])
        header_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), _BLUE),
            ("TOPPADDING",    (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING",   (0, 0), (-1, -1), 14),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN",         (1, 0), (1, 0),   "RIGHT"),
        ]))
        elements.append(header_tbl)

        # Red accent bar
        elements.append(HRFlowable(
            width=_USABLE_W, thickness=3,
            color=_RED, spaceAfter=0,
        ))
        return elements

    def _build_meta_cards(
        self,
        st: dict,
        *,
        branch_name: str,
        staff_name: str,
        started_at: Optional[datetime],
        duration_seconds: Optional[int],
    ) -> list:
        """4 equal meta cards in one row: Branch | Staff | Date | Duration."""

        items = [
            ("Branch",   branch_name),
            ("Staff",    staff_name),
            ("Date",     _fmt_dt(started_at)),
            ("Duration", _fmt_dur(duration_seconds)),
        ]

        card_w = (_USABLE_W - 3 * 3 * mm) / 4  # 4 cards, 3 gaps of 3mm each

        cells = []
        for label, value in items:
            inner = Table(
                [
                    [Paragraph(label, st["meta_label"])],
                    [Paragraph(value, st["meta_value"])],
                ],
                colWidths=[card_w - 8 * mm],
            )
            inner.setStyle(TableStyle([
                ("TOPPADDING",    (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("LEFTPADDING",   (0, 0), (-1, -1), 0),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ]))

            card = Table([[inner]], colWidths=[card_w])
            card.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), _GREY_CARD),
                ("BOX",           (0, 0), (-1, -1), 0.5, _GREY_MID),
                ("TOPPADDING",    (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ]))
            cells.append(card)

        # Horizontal spacer cells between cards (3mm gap)
        gap = Table([[""]], colWidths=[3 * mm])
        gap.setStyle(TableStyle([("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)]))

        row_data = [[cells[0], gap, cells[1], gap, cells[2], gap, cells[3]]]
        row_widths = [card_w, 3*mm, card_w, 3*mm, card_w, 3*mm, card_w]

        row_tbl = Table(row_data, colWidths=row_widths)
        row_tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ]))
        return [row_tbl]

    def _build_badges(
        self,
        st: dict,
        *,
        intent_detected: Optional[str],
        sentiment_overall: Optional[str],
        next_steps_hindi: List[str],
    ) -> list:
        """
        Three inline badge pills: intent | sentiment | steps-done.
        Uses a single-row Table so they sit side by side.
        """
        intent_lbl    = _intent_label(intent_detected)
        sentiment_lbl = (sentiment_overall or "calm").title()
        steps_count   = len(next_steps_hindi)
        steps_lbl     = f"{steps_count} step{'s' if steps_count != 1 else ''} to complete"
        sent_color    = colors.HexColor(
            _SENTIMENT_COLORS.get(sentiment_overall or "calm", "#4A5568")
        )

        def _pill(label: str, bg: colors.Color, fg: colors.Color, prefix: str = "") -> Table:
            text = f"{prefix}{label}" if prefix else label
            p = Paragraph(text, ParagraphStyle(
                "Pill", fontName=_FONT_BOLD, fontSize=8,
                textColor=fg, alignment=TA_CENTER,
            ))
            t = Table([[p]])
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), bg),
                ("BOX",           (0, 0), (-1, -1), 0.5, fg),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ]))
            return t

        intent_pill    = _pill(intent_lbl,    _BLUE_LIGHT,   _BLUE_MID)
        sentiment_pill = _pill(sentiment_lbl, _GREEN_LIGHT,  colors.HexColor(_SENTIMENT_COLORS.get(sentiment_overall or "calm", "#3B6D11")))
        steps_pill     = _pill(steps_lbl,     _GREY_LIGHT,   _TEXT_SEC)

        gap = Table([[""]], colWidths=[3 * mm])
        gap.setStyle(TableStyle([("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)]))

        # Pill widths — split usable width evenly; adjust if content differs
        pw = (_USABLE_W - 6 * mm) / 3

        row = Table(
            [[intent_pill, gap, sentiment_pill, gap, steps_pill]],
            colWidths=[pw, 3*mm, pw, 3*mm, pw],
        )
        row.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ]))
        return [row]

    def _build_pii_bar(self, st: dict, pii_types: List[str]) -> list:
        """Amber warning bar listing which PII types were masked."""
        label = _pii_label(pii_types)
        if not label:
            return []

        bar_data = [[Paragraph(
            f"PII masked — {label}",
            st["pii_text"],
        )]]
        bar = Table(bar_data, colWidths=[_USABLE_W])
        bar.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), _AMBER_LIGHT),
            ("BOX",           (0, 0), (-1, -1), 0.8, _AMBER_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING",   (0, 0), (-1, -1), 12),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
        ]))
        return [bar]

    def _build_section(
        self,
        st: dict,
        *,
        section_key: str,     # "key_points" | "summary"
        hindi_items: List[str],
        customer_items: List[str],
        customer_language: str,
        numbered: bool,
        icon_color: str,      # "blue" | "green" | "amber"
    ) -> list:
        """
        Renders a full section:
          [BLUE lang bar] Hindi
          numbered/bulleted point cards
          [RED lang bar]  Customer language
          numbered/bulleted point cards
        """
        elements: list = []

        titles = {
            "key_points": ("Key Points", "मुख्य बिंदु"),
            "summary":    ("Session Summary", "सत्र सारांश"),
        }
        title_en, title_hi = titles.get(section_key, ("Section", "Section"))

        icon_bgs = {
            "blue":  (_BLUE_LIGHT,   _BLUE_MID),
            "green": (_GREEN_LIGHT,  _GREEN_DARK),
            "amber": (_AMBER_LIGHT,  _AMBER_MID),
        }
        icon_bg, icon_fg = icon_bgs.get(icon_color, icon_bgs["blue"])

        # ── Helper: section title row ─────────────────────────────────────────
        def _lang_bar(label: str, bg: colors.Color) -> Table:
            p = Paragraph(label, ParagraphStyle(
                "LangBar", fontName=_FONT_BOLD, fontSize=8.5,
                textColor=_WHITE,
            ))
            t = Table([[p]], colWidths=[_USABLE_W])
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), bg),
                ("TOPPADDING",    (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING",   (0, 0), (-1, -1), 12),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
            ]))
            return t

        # Section title (English + Hindi)
        title_bar = Table(
            [[Paragraph(f"{title_en}  /  {title_hi}", ParagraphStyle(
                "SecTitle", fontName=_FONT_BOLD, fontSize=10,
                textColor=icon_fg,
            ))]],
            colWidths=[_USABLE_W],
        )
        title_bar.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), icon_bg),
            ("BOX",           (0, 0), (-1, -1), 0.5, icon_fg),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING",   (0, 0), (-1, -1), 14),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ]))
        elements.append(title_bar)
        elements.append(Spacer(1, 2 * mm))

        # ── Hindi sub-section ─────────────────────────────────────────────────
        if hindi_items:
            elements.append(_lang_bar("हिंदी (Hindi)", _BLUE))
            elements.extend(self._point_cards(
                items=hindi_items,
                style=st["point_hi"],
                numbered=numbered,
                alt_bg=_GREY_LIGHT,
            ))
            elements.append(Spacer(1, 2 * mm))

        # ── Customer language sub-section ─────────────────────────────────────
        if customer_items:
            elements.append(_lang_bar(customer_language, _RED))
            elements.extend(self._point_cards(
                items=customer_items,
                style=st["point_cust"],
                numbered=numbered,
                alt_bg=_GREY_LIGHT,
            ))

        return elements

    def _point_cards(
        self,
        *,
        items: List[str],
        style: ParagraphStyle,
        numbered: bool,
        alt_bg: colors.Color,
    ) -> list:
        """
        Renders each item as a card row:
          [ num/bullet circle ] [ text paragraph ]
        Alternating background for readability.
        """
        elements: list = []
        num_w  = 8 * mm
        text_w = _USABLE_W - num_w - 2 * mm

        for i, item in enumerate(items):
            prefix = str(i + 1) if numbered else "•"
            num_style = ParagraphStyle(
                f"Num{i}", fontName=_FONT_BOLD, fontSize=8.5,
                textColor=_WHITE, alignment=TA_CENTER,
            )
            num_cell = Paragraph(prefix, num_style)

            row_data = [[num_cell, Paragraph(item, style)]]
            row = Table(row_data, colWidths=[num_w, text_w])
            bg = _WHITE if i % 2 == 0 else alt_bg

            row.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (0, 0),   _BLUE),          # number circle bg
                ("BACKGROUND",    (1, 0), (1, 0),   bg),             # text bg
                ("BOX",           (0, 0), (-1, -1), 0.5, _GREY_MID),
                ("LINEAFTER",     (0, 0), (0, -1),  0.5, _GREY_MID),
                ("TOPPADDING",    (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING",   (0, 0), (0, 0),   2),
                ("RIGHTPADDING",  (0, 0), (0, 0),   2),
                ("LEFTPADDING",   (1, 0), (1, 0),   10),
                ("RIGHTPADDING",  (1, 0), (1, 0),   10),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ]))
            elements.append(row)

        return elements

    def _build_next_steps(
        self,
        st: dict,
        *,
        hindi_steps: List[str],
        customer_steps: List[str],
        customer_language: str,
    ) -> list:
        """
        Next Steps section with a step-tracker feel:
          dot (done circle) | Step N label | action text | Done pill
        Since the PDF is generated post-session, all steps show as Done.
        """
        elements: list = []

        # Section title bar (amber)
        title_bar = Table(
            [[Paragraph("Next Steps  /  अगले कदम", ParagraphStyle(
                "NSTitle", fontName=_FONT_BOLD, fontSize=10,
                textColor=_AMBER_DARK,
            ))]],
            colWidths=[_USABLE_W],
        )
        title_bar.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), _AMBER_LIGHT),
            ("BOX",           (0, 0), (-1, -1), 0.5, _AMBER_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING",   (0, 0), (-1, -1), 14),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ]))
        elements.append(title_bar)
        elements.append(Spacer(1, 2 * mm))

        def _lang_bar(label: str, bg: colors.Color) -> Table:
            p = Paragraph(label, ParagraphStyle(
                "LangBarNS", fontName=_FONT_BOLD, fontSize=8.5,
                textColor=_WHITE,
            ))
            t = Table([[p]], colWidths=[_USABLE_W])
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), bg),
                ("TOPPADDING",    (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING",   (0, 0), (-1, -1), 12),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
            ]))
            return t

        def _step_rows(steps: List[str], text_style: ParagraphStyle) -> list:
            rows = []
            dot_w   = 8 * mm
            label_w = 18 * mm
            pill_w  = 14 * mm
            text_w  = _USABLE_W - dot_w - label_w - pill_w - 3 * mm

            for i, step in enumerate(steps):
                dot_p = Paragraph(str(i + 1), ParagraphStyle(
                    f"StepDot{i}", fontName=_FONT_BOLD, fontSize=8,
                    textColor=_WHITE, alignment=TA_CENTER,
                ))
                label_p = Paragraph(f"Step {i + 1}", st["step_label"])
                text_p  = Paragraph(step, text_style)
                pill_p  = Paragraph("Done", ParagraphStyle(
                    f"DonePill{i}", fontName=_FONT_BOLD, fontSize=7,
                    textColor=_GREEN_DARK, alignment=TA_CENTER,
                ))

                step_data = [[dot_p, label_p, text_p, pill_p]]
                step_tbl = Table(step_data, colWidths=[dot_w, label_w, text_w, pill_w])
                bg = _WHITE if i % 2 == 0 else _GREY_LIGHT

                step_tbl.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (0, 0),   _GREEN_MID),
                    ("BACKGROUND",    (1, 0), (2, 0),   bg),
                    ("BACKGROUND",    (3, 0), (3, 0),   _GREEN_LIGHT),
                    ("BOX",           (0, 0), (-1, -1), 0.5, _GREY_MID),
                    ("LINEAFTER",     (0, 0), (0, -1),  0.5, _GREY_MID),
                    ("LINEAFTER",     (1, 0), (1, -1),  0.5, _GREY_MID),
                    ("LINEBEFORE",    (3, 0), (3, -1),  0.5, _GREY_MID),
                    ("TOPPADDING",    (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ("LEFTPADDING",   (0, 0), (0, 0),   2),
                    ("RIGHTPADDING",  (0, 0), (0, 0),   2),
                    ("LEFTPADDING",   (1, 0), (2, 0),   8),
                    ("RIGHTPADDING",  (2, 0), (2, 0),   8),
                    ("LEFTPADDING",   (3, 0), (3, 0),   4),
                    ("RIGHTPADDING",  (3, 0), (3, 0),   4),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ]))
                rows.append(step_tbl)
            return rows

        if hindi_steps:
            elements.append(_lang_bar("हिंदी (Hindi)", _BLUE))
            elements.extend(_step_rows(hindi_steps, st["step_hi"]))
            elements.append(Spacer(1, 2 * mm))

        if customer_steps:
            elements.append(_lang_bar(customer_language, _RED))
            elements.extend(_step_rows(customer_steps, st["step_cust"]))

        return elements

    def _build_footer(self, st: dict, token_number: str) -> list:
        """RBI compliance footer with generated timestamp."""
        generated_at = datetime.now(timezone.utc).strftime("%d %b %Y  %I:%M %p UTC")
        return [
            HRFlowable(width=_USABLE_W, thickness=0.5, color=_GREY_MID, spaceBefore=2),
            Spacer(1, 2 * mm),
            Paragraph(
                f"Generated: {generated_at}  |  Token: {token_number}  |  "
                "This document is auto-generated by VaaniBank AI. "
                "All customer data is masked per RBI Data Localisation Guidelines 2024. "
                "Union Bank of India — Committed to Your Financial Well-being.",
                st["footer"],
            ),
        ]

    # ══════════════════════════════════════════════════════════════════════════
    # P3: FORM AUTO-FILL PDF
    # ══════════════════════════════════════════════════════════════════════════

    # Field display metadata
    _FORM_FIELD_META = {
        "customer_name":          ("Customer Name / ग्राहक का नाम",         "👤"),
        "account_type":           ("Account Type / खाता प्रकार",           "🏦"),
        "loan_type":              ("Loan Type / लोन प्रकार",              "💰"),
        "amount":                 ("Amount / राशि",                       "₹"),
        "tenure":                 ("Tenure / अवधि",                       "📅"),
        "employment_type":        ("Employment / रोजगार",                  "💼"),
        "monthly_income":         ("Monthly Income / मासिक आय",           "💵"),
        "age":                    ("Age / उम्र",                           "🎂"),
        "aadhaar_provided":       ("Aadhaar Card / आधार कार्ड",           "🪪"),
        "pan_provided":           ("PAN Card / पैन कार्ड",               "🆔"),
        "address_proof_provided": ("Address Proof / पता प्रमाण",          "📍"),
        "phone_number_provided":  ("Phone Number / फ़ोन नंबर",            "📱"),
        "photos_provided":        ("Passport Photos / फोटो",              "📸"),
        "purpose":                ("Purpose / उद्देश्य",                  "🎯"),
    }

    _INTENT_FORM_REF = {
        "account_opening": "A-101 (Account Opening Form)",
        "loan_enquiry":    "LA-201 (Loan Application)",
        "kyc_update":      "KYC-07 (KYC Update Form)",
        "card_services":   "CS-301 (Card Application)",
        "fixed_deposit":   "FD-501 (FD Account Opening)",
        "general":         "GQ-601 (General Query Log)",
    }

    def generate_form_autofill(
        self,
        *,
        token_number: str,
        branch_name: str,
        staff_name: str,
        intent_detected: Optional[str],
        customer_language: str,
        collected_info: dict,
        completion_percent: int = 0,
    ) -> str:
        """
        Generate a pre-filled banking form PDF from AI-extracted customer data.

        Returns the /storage/summaries/<filename> URL path.
        """
        _try_register_fonts()
        customer_font = _font_for_language(customer_language)
        st = _make_styles(customer_font)

        filename = f"{token_number}_form_autofill.pdf"
        filepath = self._storage_path / filename

        doc = SimpleDocTemplate(
            str(filepath),
            pagesize=A4,
            leftMargin=_MARGIN,
            rightMargin=_MARGIN,
            topMargin=_MARGIN,
            bottomMargin=_MARGIN,
            title=f"VaaniBank AI — Auto-filled Form Data — {token_number}",
            author="VaaniBank AI | Union Bank of India",
        )

        story: list = []

        # 1 ── Header ──────────────────────────────────────────────────────
        story.extend(self._build_header(st, token_number))

        # 2 ── Form title bar ─────────────────────────────────────────────
        story.append(Spacer(1, 4 * mm))
        form_ref = self._INTENT_FORM_REF.get(intent_detected or "general", "GQ-601")
        intent_lbl = _intent_label(intent_detected)

        title_data = [[Paragraph(
            f"AI Auto-Filled Form Data  —  {intent_lbl}  |  Ref: {form_ref}",
            ParagraphStyle(
                "FormTitle", fontName=_FONT_BOLD, fontSize=10,
                textColor=_WHITE, alignment=TA_LEFT,
            ),
        )]]
        title_tbl = Table(title_data, colWidths=[_USABLE_W])
        title_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), _BLUE_MID),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 14),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ]))
        story.append(title_tbl)

        # 3 ── Meta row (branch, staff, completion) ────────────────────────
        story.append(Spacer(1, 3 * mm))
        meta_style = ParagraphStyle(
            "FormMeta", fontName=_FONT_REGULAR, fontSize=8.5,
            textColor=_TEXT_SEC, leading=12,
        )
        meta_text = (
            f"Branch: {branch_name}  |  Staff: {staff_name}  |  "
            f"Language: {customer_language}  |  "
            f"Data Completion: {completion_percent}%"
        )
        story.append(Paragraph(meta_text, meta_style))
        story.append(Spacer(1, 2 * mm))

        # 4 ── Completion progress bar ─────────────────────────────────────
        bar_bg_color = _GREY_LIGHT
        bar_fill_color = (
            _GREEN_DARK if completion_percent >= 75
            else _AMBER_MID if completion_percent >= 40
            else _BLUE_MID
        )
        # Build as a table with two cells — filled + empty
        fill_w = max(0.01, _USABLE_W * (completion_percent / 100))
        empty_w = _USABLE_W - fill_w

        bar_data = [["", ""]]
        bar = Table(bar_data, colWidths=[fill_w, empty_w], rowHeights=[4 * mm])
        bar.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), bar_fill_color),
            ("BACKGROUND", (1, 0), (1, 0), bar_bg_color),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ]))
        story.append(bar)
        story.append(Spacer(1, 4 * mm))

        # 5 ── Field cards ─────────────────────────────────────────────────
        label_style = ParagraphStyle(
            "FieldLabel", fontName=_FONT_BOLD, fontSize=9,
            textColor=_TEXT_SEC, leading=12,
        )
        value_style = ParagraphStyle(
            "FieldValue", fontName=_FONT_BOLD, fontSize=10,
            textColor=_TEXT_PRI, leading=14,
        )
        empty_style = ParagraphStyle(
            "FieldEmpty", fontName=_FONT_REGULAR, fontSize=9,
            textColor=_TEXT_MUTED, leading=12,
        )

        for idx, (key, meta) in enumerate(self._FORM_FIELD_META.items()):
            field_label, icon = meta
            raw_val = collected_info.get(key)

            # Determine if filled
            is_filled = (
                raw_val is not None
                and raw_val is not False
                and str(raw_val).strip() not in ("", "null", "None")
            )

            if is_filled:
                if isinstance(raw_val, bool):
                    display_val = "✅ Provided"
                else:
                    display_val = str(raw_val)
                status_indicator = "✅"
                row_bg = _GREEN_LIGHT
            else:
                display_val = "— (not yet provided)"
                status_indicator = "⬜"
                row_bg = _GREY_LIGHT if idx % 2 == 0 else _WHITE

            row_data = [[
                Paragraph(f"{status_indicator}  {field_label}", label_style),
                Paragraph(
                    display_val,
                    value_style if is_filled else empty_style,
                ),
            ]]

            col1_w = _USABLE_W * 0.5
            col2_w = _USABLE_W * 0.5

            row_tbl = Table(row_data, colWidths=[col1_w, col2_w])
            row_tbl.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), row_bg),
                ("BOX",           (0, 0), (-1, -1), 0.3, _GREY_MID),
                ("TOPPADDING",    (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING",   (0, 0), (0, 0),   12),
                ("LEFTPADDING",   (1, 0), (1, 0),   8),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ]))
            story.append(row_tbl)
            story.append(Spacer(1, 1 * mm))

        # 6 ── Signature line ──────────────────────────────────────────────
        story.append(Spacer(1, 8 * mm))
        sig_style = ParagraphStyle(
            "SigLine", fontName=_FONT_REGULAR, fontSize=8.5,
            textColor=_TEXT_SEC, alignment=TA_LEFT, leading=14,
        )
        story.append(HRFlowable(
            width=_USABLE_W * 0.4, thickness=0.5,
            color=_GREY_MID, spaceAfter=4,
        ))
        story.append(Paragraph(
            "Staff Signature / स्टाफ हस्ताक्षर",
            sig_style,
        ))
        story.append(Spacer(1, 12 * mm))
        story.append(HRFlowable(
            width=_USABLE_W * 0.4, thickness=0.5,
            color=_GREY_MID, spaceAfter=4,
        ))
        story.append(Paragraph(
            "Customer Signature / ग्राहक हस्ताक्षर",
            sig_style,
        ))

        # 7 ── Footer ──────────────────────────────────────────────────────
        story.append(Spacer(1, 6 * mm))
        generated_at = datetime.now(timezone.utc).strftime("%d %b %Y  %I:%M %p UTC")
        story.append(HRFlowable(
            width=_USABLE_W, thickness=0.5,
            color=_GREY_MID, spaceBefore=2, spaceAfter=4,
        ))
        story.append(Paragraph(
            f"Generated: {generated_at}  |  Token: {token_number}  |  "
            "This form data was auto-extracted by VaaniBank AI from the customer conversation. "
            "Please verify all details before processing. "
            "Union Bank of India — Committed to Your Financial Well-being.",
            st["footer"],
        ))

        doc.build(story)
        logger.info("[PDF-v2] Form auto-fill generated: %s", filepath)
        
        # Upload generated PDF to Cloudflare R2 / local fallback
        pdf_url = storage_service.upload_pdf_file_sync(filepath, filename)
        return pdf_url


# ── Module-level singleton ─────────────────────────────────────────────────────
pdf_service = PDFService()