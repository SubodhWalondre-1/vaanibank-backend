"""
VaaniBank AI — Mock CBS (Core Banking System) Service
PSBs Hackathon 2026 | Team Vectora

Real UBI CBS (Finacle) is restricted. This service simulates the CBS lookup
layer that a real bank teller would use — returns complete customer profile
based on account number OR Aadhaar last-4.

In production this would call:
  POST https://cbs.unionbankofindia.co.in/api/v2/customer/profile
  with OAuth2 token + account_number
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from typing import Optional

# Realistic Indian name pools
_FIRST_NAMES = [
    "Ramesh", "Suresh", "Mahesh", "Rajesh", "Dinesh",
    "Priya", "Sunita", "Kavita", "Anita", "Geeta",
    "Amit", "Sumit", "Rohit", "Mohit", "Vikas",
    "Pooja", "Rekha", "Meena", "Seema", "Neha",
    "Arun", "Varun", "Tarun", "Karun", "Vijay",
    "Laxmi", "Savitri", "Durga", "Parvati", "Radha",
    "Sanjay", "Ranjay", "Ajay", "Vijay", "Uday",
    "Santosh", "Ganesh", "Sunil", "Anil", "Kapil",
]

_LAST_NAMES = [
    "Kumar", "Sharma", "Verma", "Gupta", "Singh",
    "Patel", "Shah", "Joshi", "Nair", "Reddy",
    "Yadav", "Mishra", "Tiwari", "Pandey", "Dubey",
    "Patil", "Desai", "Mehta", "Chopra", "Arora",
    "Iyer", "Pillai", "Menon", "Naidu", "Rao",
    "Bose", "Das", "Ghosh", "Roy", "Sen",
]

_CITIES = [
    ("Nagpur",     "Maharashtra", "440001"),
    ("Mumbai",     "Maharashtra", "400001"),
    ("Pune",       "Maharashtra", "411001"),
    ("Nashik",     "Maharashtra", "422001"),
    ("Aurangabad", "Maharashtra", "431001"),
    ("Chennai",    "Tamil Nadu",  "600001"),
    ("Coimbatore", "Tamil Nadu",  "641001"),
    ("Hyderabad",  "Telangana",   "500001"),
    ("Bengaluru",  "Karnataka",   "560001"),
    ("Ahmedabad",  "Gujarat",     "380001"),
    ("Surat",      "Gujarat",     "395001"),
    ("Kolkata",    "West Bengal", "700001"),
    ("Delhi",      "Delhi",       "110001"),
    ("Lucknow",    "Uttar Pradesh","226001"),
    ("Jaipur",     "Rajasthan",   "302001"),
]

_STREET_NAMES = [
    "Gandhi Nagar", "Nehru Colony", "Shivaji Nagar", "Ambedkar Ward",
    "Sadar Bazar", "Civil Lines", "MG Road", "Station Road",
    "Laxmi Nagar", "Rajiv Nagar", "Indira Colony", "Subhash Nagar",
    "Tilak Nagar", "Patel Ward", "Narendra Nagar", "Bharat Nagar",
]

_ACCOUNT_TYPES = [
    "Savings Account",
    "Jan Dhan Account",
    "Current Account",
    "Salary Account",
    "Senior Citizen Savings Account",
    "Pradhan Mantri Jan Dhan Yojana",
]

_KYC_STATUSES = ["Complete", "Complete", "Complete", "Pending", "Expired"]  # weighted toward Complete

_OCCUPATIONS = [
    "Farmer", "Teacher", "Government Employee", "Private Employee",
    "Self Employed", "Business Owner", "Homemaker", "Student",
    "Retired", "Daily Wage Worker", "Shop Keeper", "Doctor",
]

_LOAN_TYPES = [
    None, None, None,  # most customers have no loan
    "Home Loan (₹12.5L @ 8.5%)",
    "Personal Loan (₹2L @ 11.5%)",
    "Kisan Credit Card (₹1.5L @ 7%)",
    "Gold Loan (₹75K @ 9.5%)",
    "Education Loan (₹4L @ 8.15%)",
    "Mudra Loan - Kishor (₹3L @ 10%)",
]

_FD_DETAILS = [
    None, None,  # some customers have no FD
    "1 FD — ₹50,000 @ 7.00% (matures in 8 months)",
    "2 FDs — ₹1,20,000 total (1 maturing in 45 days ⚠️)",
    "1 FD — ₹2,00,000 @ 7.40% Samridhi Scheme",
    "1 FD — ₹25,000 @ 6.80% (1 year)",
]


def _seed(value: str) -> int:
    """Deterministic seed from any string — same input → same 'random' output."""
    return int(hashlib.md5(value.encode()).hexdigest(), 16)


def _fake_balance(seed: int) -> str:
    """Generate realistic savings account balance."""
    rng = random.Random(seed)
    # Weight toward lower balances (realistic rural/semi-urban banking)
    tier = rng.choices(
        ["low", "mid", "high", "wealthy"],
        weights=[40, 35, 20, 5]
    )[0]
    if tier == "low":
        amount = rng.randint(500, 15000)
    elif tier == "mid":
        amount = rng.randint(15000, 1_50_000)
    elif tier == "high":
        amount = rng.randint(1_50_000, 10_00_000)
    else:
        amount = rng.randint(10_00_000, 50_00_000)
    return f"₹{amount:,.2f}"


def _fake_dob(seed: int) -> tuple[str, int]:
    """Return (dob_str, age). Age weighted 18-75."""
    rng = random.Random(seed + 1)
    age = rng.randint(18, 75)
    today = date.today()
    birth_year = today.year - age
    birth_month = rng.randint(1, 12)
    birth_day = rng.randint(1, 28)
    dob = date(birth_year, birth_month, birth_day)
    return dob.strftime("%d/%m/%Y"), age


def _fake_mobile(seed: int) -> str:
    rng = random.Random(seed + 2)
    prefixes = ["6", "7", "8", "9"]
    prefix = rng.choice(prefixes)
    rest = "".join([str(rng.randint(0, 9)) for _ in range(9)])
    return f"{prefix}{rest}"


def _fake_pan(seed: int, name: str) -> str:
    rng = random.Random(seed + 3)
    initials = (name[:3]).upper()
    p_type = "P"  # Person
    num = "".join([str(rng.randint(0, 9)) for _ in range(4)])
    last = chr(rng.randint(65, 90))
    return f"{initials}{p_type}{num}{last}"


def _fake_aadhaar_masked(seed: int) -> str:
    rng = random.Random(seed + 4)
    last4 = "".join([str(rng.randint(0, 9)) for _ in range(4)])
    return f"XXXX XXXX {last4}"


def _fake_ifsc(city_idx: int) -> str:
    ifsc_map = {
        0: "UBIN0539805",  # Nagpur
        1: "UBIN0531804",  # Mumbai
        2: "UBIN0532118",  # Pune
        3: "UBIN0534013",  # Nashik
        4: "UBIN0534782",  # Aurangabad
        5: "UBIN0540153",  # Chennai
        6: "UBIN0542334",  # Coimbatore
        7: "UBIN0543218",  # Hyderabad
        8: "UBIN0544715",  # Bengaluru
        9: "UBIN0546011",  # Ahmedabad
    }
    return ifsc_map.get(city_idx, "UBIN0539805")


def _kyc_expiry(kyc_status: str, seed: int) -> Optional[str]:
    rng = random.Random(seed + 9)
    today = date.today()
    if kyc_status == "Complete":
        years = rng.randint(2, 8)
        expiry = today + timedelta(days=years * 365)
        return expiry.strftime("%d/%m/%Y")
    elif kyc_status == "Pending":
        return None
    elif kyc_status == "Expired":
        days_ago = rng.randint(30, 400)
        expiry = today - timedelta(days=days_ago)
        return expiry.strftime("%d/%m/%Y")
    return None


def _last_txn_date(seed: int) -> str:
    rng = random.Random(seed + 10)
    days_ago = rng.randint(0, 30)
    txn_date = date.today() - timedelta(days=days_ago)
    return txn_date.strftime("%d %b %Y")


def _account_open_date(seed: int) -> str:
    rng = random.Random(seed + 11)
    years_ago = rng.randint(1, 20)
    open_date = date.today() - timedelta(days=years_ago * 365 + rng.randint(0, 364))
    return open_date.strftime("%d %b %Y")


# MAIN CBS LOOKUP FUNCTION

def lookup_by_account_number(account_number: str) -> Optional[dict]:
    """
    Simulate CBS lookup by account number.
    Returns full customer profile dict or None if account invalid.

    Account number validation:
      - Must be 9–18 digits
      - Must be numeric only
    """
    clean = account_number.strip().replace(" ", "").replace("-", "")
    if not clean.isdigit() or not (9 <= len(clean) <= 18):
        return None

    seed = _seed(clean)
    rng = random.Random(seed)

    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    full_name = f"{first} {last}"

    city_idx = rng.randint(0, len(_CITIES) - 1)
    city, state, pincode = _CITIES[city_idx]
    street = rng.choice(_STREET_NAMES)
    house_num = rng.randint(1, 999)
    address = f"{house_num}, {street}, {city}, {state} - {pincode}"

    dob_str, age = _fake_dob(seed)
    mobile = _fake_mobile(seed)
    pan = _fake_pan(seed, full_name)
    aadhaar_masked = _fake_aadhaar_masked(seed)
    balance = _fake_balance(seed)

    kyc_status = rng.choice(_KYC_STATUSES)
    kyc_expiry = _kyc_expiry(kyc_status, seed)

    account_type = rng.choice(_ACCOUNT_TYPES)
    occupation = rng.choice(_OCCUPATIONS)
    loan = rng.choice(_LOAN_TYPES)
    fd = rng.choice(_FD_DETAILS)
    ifsc = _fake_ifsc(city_idx)

    last_txn = _last_txn_date(seed)
    account_opened = _account_open_date(seed)

    # Nominee (70% chance)
    has_nominee = rng.random() > 0.30
    nominee_name = f"{rng.choice(_FIRST_NAMES)} {last}" if has_nominee else None
    nominee_relation = rng.choice(["Spouse", "Son", "Daughter", "Father", "Mother", "Brother", "Sister"]) if has_nominee else None

    # Linked debit card
    has_debit_card = rng.random() > 0.25
    card_type = rng.choice(["RuPay Classic", "RuPay Platinum", "Visa Classic"]) if has_debit_card else None

    # Internet banking
    net_banking = rng.random() > 0.40

    return {
        # Identity
        "customer_id":         f"CIF{clean[:3]}{seed % 100000:05d}",
        "full_name":           full_name,
        "dob":                 dob_str,
        "age":                 age,
        "gender":              rng.choice(["Male", "Female"]),
        "occupation":          occupation,
        "pan":                 pan,
        "aadhaar_masked":      aadhaar_masked,
        "mobile_number":       mobile,
        "email":               f"{first.lower()}.{last.lower()}{rng.randint(10,99)}@gmail.com",
        "address":             address,
        "city":                city,
        "state":               state,
        "pincode":             pincode,

        # Account
        "account_number":      clean,
        "account_type":        account_type,
        "ifsc_code":           ifsc,
        "branch_name":         f"Union Bank of India — {city} Branch",
        "account_opened":      account_opened,
        "last_txn_date":       last_txn,
        "balance":             balance,
        "available_balance":   balance,

        # KYC
        "kyc_status":          kyc_status,
        "kyc_expiry_date":     kyc_expiry,
        "kyc_mode":            rng.choice(["Aadhaar OTP", "Biometric", "In-Person Verification"]),

        # Linked products
        "linked_accounts":     "1 " + account_type,
        "active_loans":        loan,
        "active_fds":          fd,
        "fd_maturing_soon":    fd is not None and "45 days" in (fd or ""),
        "debit_card":          card_type,
        "net_banking":         "Active" if net_banking else "Not Registered",

        # Nominee
        "nominee_name":        nominee_name,
        "nominee_relation":    nominee_relation,

        # Risk & Compliance
        "risk_category":       rng.choice(["Low", "Low", "Low", "Medium", "High"]),
        "cibil_score":         rng.randint(550, 900) if occupation != "Farmer" else None,
        "pmjdy_account":       account_type == "Jan Dhan Account",
    }


def lookup_by_aadhaar_last4(last4: str, mobile: Optional[str] = None) -> Optional[dict]:
    """
    Simulate CBS lookup by Aadhaar last 4 digits.
    Less precise than account number — returns partial profile.
    In real CBS, this would require OTP verification first.
    """
    if not last4 or not last4.isdigit() or len(last4) != 4:
        return None

    # Generate a plausible account number from last4
    seed = _seed(f"aadhaar_{last4}_{mobile or 'x'}")
    rng = random.Random(seed)

    # Fake account number derived from aadhaar last4
    acc_suffix = "".join([str(rng.randint(0, 9)) for _ in range(11)])
    fake_acc = f"5201{last4}{acc_suffix[:7]}"

    profile = lookup_by_account_number(fake_acc)
    if profile:
        # Override aadhaar to match the last4 provided
        profile["aadhaar_masked"] = f"XXXX XXXX {last4}"
        profile["account_number"] = f"XXXX...{fake_acc[-4:]}"  # partially masked for Aadhaar lookup
        profile["_lookup_method"] = "aadhaar"
        profile["_note"] = "Profile retrieved via Aadhaar — account number partially masked per RBI guidelines"
    return profile


def lookup_customer(
    account_number: Optional[str] = None,
    aadhaar_last4: Optional[str] = None,
    mobile: Optional[str] = None,
) -> Optional[dict]:
    """
    Master lookup function — tries account_number first, then Aadhaar.
    Returns None if no valid identifier provided.
    """
    if account_number and account_number.strip():
        result = lookup_by_account_number(account_number.strip())
        if result:
            result["_lookup_method"] = "account_number"
            return result

    if aadhaar_last4 and aadhaar_last4.strip():
        # Extract just the digits (could be "XXXX XXXX 1234" or just "1234")
        digits_only = "".join(c for c in aadhaar_last4 if c.isdigit())[-4:]
        if len(digits_only) == 4:
            return lookup_by_aadhaar_last4(digits_only, mobile)

    return None


# Module-level singleton (stateless, so just functions — no class needed)
cbs_service = type("CBSService", (), {
    "lookup_customer": staticmethod(lookup_customer),
    "lookup_by_account_number": staticmethod(lookup_by_account_number),
    "lookup_by_aadhaar_last4": staticmethod(lookup_by_aadhaar_last4),
})()
