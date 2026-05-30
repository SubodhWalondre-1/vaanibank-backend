"""
VaaniBank AI — Document Readiness Registry
PSBs Hackathon 2026 | Team Vectora

Maps each banking intent to its required/optional documents.
Each document entry has:
  - id:                 Unique key (matches bankingKnowledge.js doc entries)
  - label_en:           English display label
  - labels:             Dict of 10 Indian language labels
  - required:           True if mandatory for that service
  - tag:                Short tag (e.g. "Salaried — Required")
  - collected_info_key: Maps to LLM collected_info field (for auto-verification)
"""

from __future__ import annotations

# Shared multilingual label fragments
_REQUIRED = {
    "hi": "आवश्यक", "mr": "आवश्यक", "ta": "அவசியம்", "te": "అవసరం",
    "bn": "আবশ্যক", "kn": "ಅಗತ್ಯ", "or": "ଆବଶ୍ୟକ", "pa": "ਲੋੜੀਂਦਾ",
    "gu": "જરૂરી", "ml": "ആവശ്യമാണ്",
}
_OPTIONAL = {
    "hi": "वैकल्पिक", "mr": "पर्यायी", "ta": "விருப்பம்", "te": "ఐచ్ఛికం",
    "bn": "ঐচ্ছিক", "kn": "ಐಚ್ಛಿಕ", "or": "ଇଚ୍ଛାଧୀନ", "pa": "ਵਿਕਲਪਿਕ",
    "gu": "વૈકલ્પિક", "ml": "ഓപ്ഷണൽ",
}


# Shared multilingual labels for common documents
_AADHAAR_LABELS = {
    "hi": "आधार कार्ड", "mr": "आधार कार्ड", "ta": "ஆதார் அட்டை",
    "te": "ఆధార్ కార్డ్", "bn": "আধার কার্ড", "kn": "ಆಧಾರ್ ಕಾರ್ಡ್",
    "or": "ଆଧାର କାର୍ଡ", "pa": "ਆਧਾਰ ਕਾਰਡ", "gu": "આધાર કાર્ડ",
    "ml": "ఆധാർ കാർഡ്",
}

_PAN_LABELS = {
    "hi": "पैन कार्ड", "mr": "पॅन कार्ड", "ta": "பான் अட்டை",
    "te": "పాన్ కార్డ్", "bn": "প্যান কার্ড", "kn": "ಪ್ಯಾನ್ ಕಾರ್ಡ್",
    "or": "ପ୍ୟାନ କାର୍ଡ", "pa": "ਪੈਨ ਕਾਰਡ", "gu": "પાન કાર્ડ",
    "ml": "പാൻ കാർഡ്",
}

_AADHAAR_LABEL_EN = "Aadhaar Card"
_PAN_LABEL_EN = "PAN Card"


DOCUMENT_REGISTRY: dict = {
    # 1. ACCOUNT OPENING
    "account_opening": [
        {
            "id": "aadhaar",
            "label_en": _AADHAAR_LABEL_EN,
            "labels": _AADHAAR_LABELS,
            "required": True,
            "tag": "Required",
            "collected_info_key": "aadhaar_provided",
        },
        {
            "id": "pan",
            "label_en": _PAN_LABEL_EN,
            "labels": _PAN_LABELS,
            "required": True,
            "tag": "Required",
            "collected_info_key": "pan_provided",
        },
        {
            "id": "photos",
            "label_en": "Passport Size Photos (2)",
            "labels": {
                "hi": "पासपोर्ट साइज फोटो (2)", "mr": "पासपोर्ट फोटो (2)",
                "ta": "பாஸ்போர்ட் புகைப்படம் (2)", "te": "పాస్‌పోర్ట్ ఫోటో (2)",
                "bn": "পাসপোর্ট ফটো (2)", "kn": "ಪಾಸ್‌ಪೋರ್ಟ್ ಫೋಟೋ (2)",
                "or": "ପାସପୋର୍ଟ ଫଟୋ (2)", "pa": "ਪਾਸਪੋਰਟ ਫੋਟੋ (2)",
                "gu": "પાસપોર્ટ ફોટો (2)", "ml": "പാസ്പോർട്ട് ഫോട്ടോ (2)",
            },
            "required": True,
            "tag": "Required",
            "collected_info_key": "photos_provided",
        },
        {
            "id": "address_proof",
            "label_en": "Address Proof",
            "labels": {
                "hi": "पता प्रमाण", "mr": "पत्त्याचा पुरावा", "ta": "முகவரி ஆதாரம்",
                "te": "చిరునామా రుజువు", "bn": "ঠিকানার প্রমাণ", "kn": "ವಿಳಾಸ ಪ್ರಮಾಣ",
                "or": "ଠିକଣା ପ୍ରମାଣ", "pa": "ਪਤੇ ਦਾ ਸਬੂਤ", "gu": "સરનામાનો પુરાવો",
                "ml": "വിലാസ തെളിവ്",
            },
            "required": False,
            "tag": "If Aadhaar address differs",
            "collected_info_key": "address_proof_provided",
        },
    ],

    # 2. LOAN ENQUIRY
    "loan_enquiry": [
        {
            "id": "aadhaar",
            "label_en": _AADHAAR_LABEL_EN,
            "labels": _AADHAAR_LABELS,
            "required": True,
            "tag": "Required",
            "collected_info_key": "aadhaar_provided",
        },
        {
            "id": "pan",
            "label_en": _PAN_LABEL_EN,
            "labels": _PAN_LABELS,
            "required": True,
            "tag": "Required",
            "collected_info_key": "pan_provided",
        },
        {
            "id": "salary_slips",
            "label_en": "Salary Slips (3 months)",
            "labels": {
                "hi": "सैलरी स्लिप (3 महीने)", "mr": "पगाराच्या स्लिप (3 महिने)",
                "ta": "சம்பள சீட்டு (3 மாதம்)", "te": "జీతం స్లిప్‌లు (3 నెలలు)",
                "bn": "বেতন স্লিপ (3 মাস)", "kn": "ವೇತನ ಸ್ಲಿಪ್ (3 ತಿಂಗಳು)",
                "or": "ଦରମା ସ୍ଲିପ (3 ମାସ)", "pa": "ਤਨਖ਼ਾਹ ਸਲਿੱਪ (3 ਮਹੀਨੇ)",
                "gu": "પગાર સ્લિપ (3 મહિના)", "ml": "ശമ്പള സ്ലിപ്പ് (3 മാസം)",
            },
            "required": True,
            "tag": "Salaried — Required",
            "collected_info_key": None,
        },
        {
            "id": "bank_statement",
            "label_en": "Bank Statement (6 months)",
            "labels": {
                "hi": "बैंक स्टेटमेंट (6 महीने)", "mr": "बँक स्टेटमेंट (6 महिने)",
                "ta": "வங்கி அறிக்கை (6 மாதம்)", "te": "బ్యాంక్ స్టేట్‌మెంట్ (6 నెలలు)",
                "bn": "ব্যাংক স্টেটমেন্ট (6 মাস)", "kn": "ಬ್ಯಾಂಕ್ ಹೇಳಿಕೆ (6 ತಿಂಗಳು)",
                "or": "ବ୍ୟାଙ୍କ ବିବରଣୀ (6 ମାସ)", "pa": "ਬੈਂਕ ਸਟੇਟਮੈਂਟ (6 ਮਹੀਨੇ)",
                "gu": "બેંક સ્ટેટમેન્ટ (6 મહિના)", "ml": "ബാങ്ക് സ്റ്റേറ്റ്‌മെന്റ് (6 മാസം)",
            },
            "required": True,
            "tag": "Required",
            "collected_info_key": None,
        },
        {
            "id": "employer_id",
            "label_en": "Employer ID / Appointment Letter",
            "labels": {
                "hi": "नियोक्ता आईडी / नियुक्ति पत्र", "mr": "नियोक्ता ओळखपत्र",
                "ta": "பணியமர்த்தல் கடிதம்", "te": "ఉద్యోగ నియామక లేఖ",
                "bn": "নিয়োগকর্তা আইডি", "kn": "ಉದ್ಯೋಗದಾತ ಐಡಿ",
                "or": "ନିଯୁକ୍ତି ପତ୍ର", "pa": "ਰੁਜ਼ਗਾਰਦਾਤਾ ਆਈਡੀ",
                "gu": "એમ્પ્લોયર આઈડી", "ml": "തൊഴിലുടമ ഐഡി",
            },
            "required": True,
            "tag": "Salaried — Required",
            "collected_info_key": None,
        },
        {
            "id": "itr",
            "label_en": "ITR (last 2-3 years)",
            "labels": {
                "hi": "आयकर रिटर्न (2-3 साल)", "mr": "आयकर रिटर्न (2-3 वर्षे)",
                "ta": "வருமான வரி ரிட்டர்ன் (2-3 ஆண்டு)", "te": "ఆదాయపు పన్ను రిటర్న్",
                "bn": "আয়কর রিটার্ন (2-3 বছর)", "kn": "ಆದಾಯ ತೆರಿಗೆ ರಿಟರ್ನ್",
                "or": "ଆୟକର ରିଟର୍ଣ୍ଣ", "pa": "ਇਨਕਮ ਟੈਕਸ ਰਿਟਰਨ",
                "gu": "આવકવેરા રિટર્ન", "ml": "ആദായനികുതി റിട്ടേൺ",
            },
            "required": False,
            "tag": "Self-Employed",
            "collected_info_key": None,
        },
        {
            "id": "property_docs",
            "label_en": "Property Documents",
            "labels": {
                "hi": "संपत्ति दस्तावेज", "mr": "मालमत्ता कागदपत्रे",
                "ta": "சொத்து ஆவணங்கள்", "te": "ఆస్తి పత్రాలు",
                "bn": "সম্পত্তির দলিল", "kn": "ಆಸ್ತಿ ದಾಖಲೆಗಳು",
                "or": "ସମ୍ପତ୍ତି ଦଲିଲ", "pa": "ਜਾਇਦਾਦ ਦਸਤਾਵੇਜ਼",
                "gu": "મિલકત દસ્તાવેજો", "ml": "സ്വത്ത് രേഖകൾ",
            },
            "required": False,
            "tag": "Home Loan only",
            "collected_info_key": None,
        },
    ],

    # 3. KYC UPDATE
    "kyc_update": [
        {
            "id": "aadhaar",
            "label_en": "Updated Aadhaar Card",
            "labels": {
                "hi": "अपडेटेड आधार कार्ड", "mr": "अपडेटेड आधार कार्ड",
                "ta": "புதுப்பிக்கப்பட்ட ஆதார்", "te": "అప్‌డేట్ ఆధార్",
                "bn": "আপডেটেড আধার", "kn": "ನವೀಕರಿಸಿದ ಆಧಾರ್",
                "or": "ଅପଡେଟେଡ ଆଧାର", "pa": "ਅੱਪਡੇਟਿਡ ਆਧਾਰ",
                "gu": "અપડેટેડ આધાર", "ml": "അപ്‌ഡേറ്റ് ആധാർ",
            },
            "required": True,
            "tag": "Required",
            "collected_info_key": "aadhaar_provided",
        },
        {
            "id": "address_proof",
            "label_en": "New Address Proof",
            "labels": {
                "hi": "नया पता प्रमाण", "mr": "नवीन पत्त्याचा पुरावा",
                "ta": "புதிய முகவரி ஆதாரம்", "te": "కొత్త చిరునామా రుజువు",
                "bn": "নতুন ঠিকানার প্রমাণ", "kn": "ಹೊಸ ವಿಳಾಸ ಪ್ರಮಾಣ",
                "or": "ନୂଆ ଠିକଣା ପ୍ରମାଣ", "pa": "ਨਵਾਂ ਪਤਾ ਸਬੂਤ",
                "gu": "નવું સરનામાનો પુરાવો", "ml": "പുതിയ വിലാസ തെളിവ്",
            },
            "required": False,
            "tag": "Address Update",
            "collected_info_key": "address_proof_provided",
        },
        {
            "id": "nominee_kyc",
            "label_en": "Nominee's Aadhaar + Photo",
            "labels": {
                "hi": "नॉमिनी का आधार + फोटो", "mr": "नॉमिनीचे आधार + फोटो",
                "ta": "நாமினி ஆதார் + புகைப்படம்", "te": "నామినీ ఆధార్ + ఫోటో",
                "bn": "নমিনির আধার + ফটো", "kn": "ನಾಮಿನಿ ಆಧಾರ್ + ಫೋಟೋ",
                "or": "ନମିନି ଆଧାର + ଫଟୋ", "pa": "ਨਾਮਿਨੀ ਆਧਾਰ + ਫੋਟੋ",
                "gu": "નોમિની આધાર + ફોટો", "ml": "നോമിനി ആധാർ + ഫോട്ടോ",
            },
            "required": False,
            "tag": "Nominee Change",
            "collected_info_key": None,
        },
    ],

    # 4. FIXED DEPOSIT
    "fixed_deposit": [
        {
            "id": "aadhaar",
            "label_en": _AADHAAR_LABEL_EN,
            "labels": _AADHAAR_LABELS,
            "required": True,
            "tag": "Required",
            "collected_info_key": "aadhaar_provided",
        },
        {
            "id": "pan",
            "label_en": _PAN_LABEL_EN,
            "labels": _PAN_LABELS,
            "required": True,
            "tag": "Required — TDS 20% without PAN",
            "collected_info_key": "pan_provided",
        },
        {
            "id": "form_15g",
            "label_en": "Form 15G / 15H",
            "labels": {
                "hi": "फॉर्म 15G / 15H", "mr": "फॉर्म 15G / 15H",
                "ta": "படிவம் 15G / 15H", "te": "ఫారం 15G / 15H",
                "bn": "ফর্ম 15G / 15H", "kn": "ಫಾರ್ಮ್ 15G / 15H",
                "or": "ଫର୍ମ 15G / 15H", "pa": "ਫਾਰਮ 15G / 15H",
                "gu": "ફોર્મ 15G / 15H", "ml": "ഫോം 15G / 15H",
            },
            "required": False,
            "tag": "Below tax bracket",
            "collected_info_key": "form_15g_applicable",
        },
        {
            "id": "nominee_details",
            "label_en": "Nominee Details",
            "labels": {
                "hi": "नॉमिनी विवरण", "mr": "नॉमिनी तपशील",
                "ta": "நாமினி விவரங்கள்", "te": "నామినీ వివరాలు",
                "bn": "নমিনি বিবরণ", "kn": "ನಾಮಿನಿ ವಿವರಗಳು",
                "or": "ନମିନି ବିବରଣୀ", "pa": "ਨਾਮਿਨੀ ਵੇਰਵੇ",
                "gu": "નોમિની વિગતો", "ml": "നോമിനി വിശദാംശങ്ങൾ",
            },
            "required": True,
            "tag": "Required for new FD",
            "collected_info_key": None,
        },
    ],

    # 5. CARD SERVICES
    "card_services": [
        {
            "id": "photo_id",
            "label_en": "Photo ID Proof",
            "labels": {
                "hi": "फोटो आईडी", "mr": "फोटो ओळखपत्र",
                "ta": "புகைப்பட அடையாளம்", "te": "ఫోటో ఐడి",
                "bn": "ফটো আইডি", "kn": "ಫೋಟೋ ಐಡಿ",
                "or": "ଫଟୋ ଆଇଡି", "pa": "ਫੋਟੋ ਆਈਡੀ",
                "gu": "ફોટો આઈડી", "ml": "ഫോട്ടോ ഐഡി",
            },
            "required": True,
            "tag": "New Card — Required",
            "collected_info_key": "aadhaar_provided",
        },
        {
            "id": "card_form",
            "label_en": "Card Application Form",
            "labels": {
                "hi": "कार्ड आवेदन फॉर्म", "mr": "कार्ड अर्ज फॉर्म",
                "ta": "அட்டை விண்ணப்பம்", "te": "కార్డ్ దరఖాస్తు",
                "bn": "কার্ড আবেদন ফর্ম", "kn": "ಕಾರ್ಡ್ ಅರ್ಜಿ",
                "or": "କାର୍ଡ ଆବେଦନ ଫର୍ମ", "pa": "ਕਾਰਡ ਅਰਜ਼ੀ ਫਾਰਮ",
                "gu": "કાર્ડ અરજી ફોર્મ", "ml": "കാർഡ് അപേക്ഷ",
            },
            "required": True,
            "tag": "Required",
            "collected_info_key": None,
        },
    ],

    # 6. BALANCE ENQUIRY
    "balance_enquiry": [
        {
            "id": "identity_proof",
            "label_en": "Identity Verification",
            "labels": {
                "hi": "पहचान सत्यापन", "mr": "ओळख पडताळणी",
                "ta": "அடையாள சரிபார்ப்பு", "te": "గుర్తింపు ధృవీకరణ",
                "bn": "পরিচয় যাচাই", "kn": "ಗುರುತಿನ ಪರಿಶೀಲನೆ",
                "or": "ପରିଚୟ ଯାଞ୍ଚ", "pa": "ਪਛਾਣ ਤਸਦੀਕ",
                "gu": "ઓળખ ચકાસણી", "ml": "ഐഡന്റിറ്റി വെരിഫിക്കേഷൻ",
            },
            "required": True,
            "tag": "Required",
            "collected_info_key": "identity_verified",
        },
    ],

    # 7. GENERAL — no document requirements
    "general": [],
}
