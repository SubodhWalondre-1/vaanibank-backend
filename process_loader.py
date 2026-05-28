"""
VaaniBank AI — Process Loader
PSBs Hackathon 2026 | Team Vectora

Loads Union Bank of India process JSONs from the processes/ folder.
Maps intent strings → JSON file → parsed dict.

Usage:
    from process_loader import load_process
    data = load_process("HOME_LOAN")    # returns full home_loan.json as dict
    data = load_process("UNKNOWN")       # falls back to default.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from language_config import normalise_intent

logger = logging.getLogger("vaanibank.process_loader")

# ── Intent → filename map ────────────────────────────────────────────────────
INTENT_FILE_MAP: dict[str, str] = {
    "HOME_LOAN":       "home_loan.json",
    "PERSONAL_LOAN":   "personal_loan.json",
    "EDUCATION_LOAN":  "education_loan.json",
    "VEHICLE_LOAN":    "vehicle_loan.json",
    "FIXED_DEPOSIT":   "fixed_deposit.json",
    "ACCOUNT_OPENING": "account_opening.json",
    "CIBIL_INFO":      "cibil_info.json",
    "GENERAL":         "default.json",
}

_DEFAULT_FILE = "default.json"
_PROCESSES_DIR = Path(__file__).parent / "processes"


def _read_json(filepath: Path) -> dict:
    """Read and parse a JSON file; raise FileNotFoundError / json.JSONDecodeError on failure."""
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def load_process(intent: str) -> dict:
    """
    Load Union Bank process data for a given intent.

    Args:
        intent: Raw intent string (e.g. "HOME_LOAN", "home_loan", "GENERAL")

    Returns:
        Parsed dict from the corresponding processes/*.json file.
        Falls back to default.json on any error.
    """
    normalised = normalise_intent(intent)
    filename   = INTENT_FILE_MAP.get(normalised, _DEFAULT_FILE)
    filepath   = _PROCESSES_DIR / filename

    try:
        data = _read_json(filepath)
        logger.debug("Loaded process | intent=%s | file=%s", normalised, filename)
        return data
    except FileNotFoundError:
        logger.warning(
            "Process file not found: %s | falling back to default.json", filepath
        )
    except json.JSONDecodeError as exc:
        logger.warning(
            "JSON parse error in %s: %s | falling back to default.json", filename, exc
        )
    except Exception as exc:
        logger.warning(
            "Unexpected error loading %s: %s | falling back to default.json", filename, exc
        )

    # ── Fallback ──────────────────────────────────────────────────────────────
    default_path = _PROCESSES_DIR / _DEFAULT_FILE
    try:
        return _read_json(default_path)
    except Exception as exc:
        logger.error("Cannot load default.json either: %s", exc)
        # Last resort empty structure
        return {
            "intent": "GENERAL",
            "product_name": "Union Bank of India",
            "process_steps": [],
        }


def get_process_steps(intent: str) -> list[dict]:
    """
    Convenience helper — returns only the process_steps list for an intent.
    Returns [] if the JSON has no process_steps field.
    """
    data = load_process(intent)
    return data.get("process_steps") or data.get("general_process_steps") or []


def get_key_info(intent: str) -> dict:
    """
    Returns a simplified key-info dict for the frontend badge row.
    Extracts the most useful fields depending on intent.
    """
    data     = load_process(intent)
    norm     = normalise_intent(intent)
    key_info: dict = {}

    if norm == "HOME_LOAN":
        rates = data.get("interest_rates", {})
        slabs = rates.get("slabs", [])
        best  = slabs[0].get("rate", "8.35%") if slabs else "8.35%"
        limits = data.get("loan_limits", {})
        tenure = data.get("tenure", {}).get("maximum", "30 saal")
        key_info = {
            "Interest Rate": best,
            "Max Loan":      limits.get("metro_urban", "₹5 Crore"),
            "Max Tenure":    tenure,
            "CIBIL Min":     data.get("eligibility", {}).get("cibil_minimum", "700"),
        }

    elif norm == "PERSONAL_LOAN":
        rates = data.get("interest_rates", {})
        slabs = rates.get("slabs", [])
        best  = slabs[0].get("rate", "10.70%") if slabs else "10.70%"
        key_info = {
            "Interest Rate": best,
            "Max Loan":      data.get("loan_limits", {}).get("maximum", "15 Lakh"),
            "Max Tenure":    data.get("tenure", {}).get("maximum", "5 saal"),
        }

    elif norm == "FIXED_DEPOSIT":
        rates = data.get("interest_rates", {})
        gen_rates = rates.get("general_public", [])
        best = gen_rates[0].get("rate", "6.5%") if gen_rates else "6.5%"
        key_info = {
            "Best Rate":    best,
            "Senior Bonus": "+0.50% extra",
            "Min Deposit":  data.get("deposit_limits", {}).get("minimum", "₹1,000"),
        }

    elif norm == "EDUCATION_LOAN":
        key_info = {
            "Max India":   data.get("loan_limits", {}).get("domestic", "10 Lakh"),
            "Max Abroad":  data.get("loan_limits", {}).get("abroad", "20 Lakh"),
            "Moratorium":  "Course + 12 months",
        }

    elif norm == "VEHICLE_LOAN":
        key_info = {
            "New Car LTV":  "90% of ex-showroom",
            "Max Tenure":   data.get("tenure", {}).get("new_vehicle", "84 months"),
        }

    elif norm == "ACCOUNT_OPENING":
        key_info = {
            "Min Balance": data.get("account_types", [{}])[0].get("min_balance", "₹500"),
            "KYC":         "Aadhaar + PAN",
        }

    elif norm == "CIBIL_INFO":
        key_info = {"Good Score": "700+", "Excellent": "750+"}

    else:
        key_info = {"Helpline": "1800-22-2244"}

    return key_info
