"""
VaaniBank AI — Database Seed Script
PSBs Hackathon 2026 | Team Vectora

Run directly:
    cd backend
    python seed_data.py

Inserts:
  - 3 Branches (Nagpur, Mumbai, Chennai)
  - 3 StaffMembers (demo/manager/admin)
  - Process steps for 6 intents:
      account_opening (4), loan_enquiry (4), kyc_update (3),
      card_services (3), balance_enquiry (2), fixed_deposit (3)

Duplicate-safe: existing rows are skipped, not duplicated.
Uses synchronous SQLAlchemy — no asyncio required.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List

from passlib.context import CryptContext
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SyncSession, sessionmaker

# Sync engine (seed script — no async needed)
# Convert asyncpg URL back to psycopg2 for sync usage
from config import settings
from models import Branch, ProcessStep, Session, StaffMember

_sync_url: str = (
    settings.DATABASE_URL
    .replace("postgresql+asyncpg://", "postgresql://")
    .replace("postgresql+psycopg2://", "postgresql://")
    .replace("ssl=require", "sslmode=require")
)

engine = create_engine(_sync_url, echo=False)
session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# SEED DATA DEFINITIONS

BRANCHES_DATA: List[Dict[str, Any]] = [
    {
        "branch_code": "NGP-CVL-01",
        "branch_name": "Nagpur Civil Lines Branch",
        "bank_name": "Union Bank of India",
        "city": "Nagpur",
        "state": "Maharashtra",
        "region": "West",
        "address": "Civil Lines, Nagpur, Maharashtra 440001",
        "pincode": "440001",
        "is_active": True,
    },
    {
        "branch_code": "MUM-AND-01",
        "branch_name": "Mumbai Andheri Branch",
        "bank_name": "Union Bank of India",
        "city": "Mumbai",
        "state": "Maharashtra",
        "region": "West",
        "address": "Andheri West, Mumbai, Maharashtra 400058",
        "pincode": "400058",
        "is_active": True,
    },
    {
        "branch_code": "CHN-TNG-01",
        "branch_name": "Chennai T Nagar Branch",
        "bank_name": "Union Bank of India",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "region": "South",
        "address": "T Nagar, Chennai, Tamil Nadu 600017",
        "pincode": "600017",
        "is_active": True,
    },
]

STAFF_DATA: List[Dict[str, Any]] = [
    {
        "staff_id": "UBI-NGP-001",
        "username": "demo",
        "password_plain": "demo123",
        "full_name": "Rajesh Kumar",
        "role": "teller",
        "branch_code": "NGP-CVL-01",
        "languages_known": ["Hindi", "Marathi"],
        "is_active": True,
    },
    {
        "staff_id": "UBI-NGP-002",
        "username": "manager",
        "password_plain": "manager123",
        "full_name": "Priya Sharma",
        "role": "manager",
        "branch_code": "NGP-CVL-01",
        "languages_known": ["Hindi", "English"],
        "is_active": True,
    },
    {
        "staff_id": "UBI-MUM-042",
        "username": "admin",
        "password_plain": "admin123",
        "full_name": "Amit Patel",
        "role": "admin",
        "branch_code": "MUM-AND-01",
        "languages_known": ["Hindi", "Gujarati", "English"],
        "is_active": True,
    },
]

# Process Steps
# Each step: dict with all language text fields.
# Languages: hi (required), mr, ta, te, bn, kn, or, pa (optional)

# Demo Sessions Data
# Pre-filled profile data for hackathon judges
# Token numbers are fixed so QR codes remain stable in demo

DEMO_SESSIONS_DATA: List[Dict[str, Any]] = [
    {
        "token_number": "DEMO-0001",
        "branch_code": "NGP-CVL-01",
        "customer_language": "Hindi",
        "customer_language_code": "hi",
        "status": "active",
        "entry_method": "qr_scan",
        "intent_detected": "account_opening",
        "sentiment_overall": "calm",
        # Customer PII fields
        "customer_name": "Ramesh Kumar Sharma",
        "customer_account_number": "040802340012789",
        "customer_mobile_number": "9823456701",
        "customer_pan": "ABCPS1234D",
        "customer_aadhaar_last4": "6789",
        "customer_account_type": "Savings Account",
        "customer_kyc_status": "Verified",
        "customer_balance": "₹ 24,580.00",
        "customer_dob": "15/08/1985",
    },
    {
        "token_number": "DEMO-0002",
        "branch_code": "NGP-CVL-01",
        "customer_language": "Marathi",
        "customer_language_code": "mr",
        "status": "active",
        "entry_method": "qr_scan",
        "intent_detected": "loan_enquiry",
        "sentiment_overall": "calm",
        "customer_name": "Sunita Patil",
        "customer_account_number": "040802340098456",
        "customer_mobile_number": "9876500123",
        "customer_pan": "DXMSP4321F",
        "customer_aadhaar_last4": "4321",
        "customer_account_type": "Savings Account",
        "customer_kyc_status": "Verified",
        "customer_balance": "₹ 1,12,340.00",
        "customer_dob": "22/03/1990",
    },
    {
        "token_number": "DEMO-0003",
        "branch_code": "NGP-CVL-01",
        "customer_language": "Tamil",
        "customer_language_code": "ta",
        "status": "active",
        "entry_method": "qr_scan",
        "intent_detected": "kyc_update",
        "sentiment_overall": "calm",
        "customer_name": "Murugan Selvaraj",
        "customer_account_number": "040802340056123",
        "customer_mobile_number": "7845123690",
        "customer_pan": None,
        "customer_aadhaar_last4": "8812",
        "customer_account_type": "Current Account",
        "customer_kyc_status": "Pending",
        "customer_balance": "₹ 67,200.00",
        "customer_dob": "01/01/1978",
    },
]


PROCESS_STEPS: List[Dict[str, Any]] = [

    # account_opening
    {
        "intent_type": "account_opening",
        "step_number": 1,
        "step_text_hindi": "ग्राहक को बताएं कि खाता खोलने के लिए आधार कार्ड, पैन कार्ड और पासपोर्ट साइज फोटो आवश्यक है।",
        "step_text_marathi": "ग्राहकाला सांगा की खाते उघडण्यासाठी आधार कार्ड, पॅन कार्ड आणि पासपोर्ट आकाराचा फोटो आवश्यक आहे.",
        "step_text_tamil": "கணக்கு திறக்க ஆதார் அட்டை, பான் அட்டை மற்றும் பாஸ்போர்ட் அளவு புகைப்படம் தேவை என்று வாடிக்கையாளருக்கு தெரிவிக்கவும்.",
        "step_text_telugu": "ఖాతా తెరవడానికి ఆధార్ కార్డ్, పాన్ కార్డ్ మరియు పాస్‌పోర్ట్ సైజ్ ఫోటో అవసరమని కస్టమర్‌కు తెలియజేయండి.",
        "step_text_bengali": "গ্রাহককে জানান যে অ্যাকাউন্ট খোলার জন্য আধার কার্ড, প্যান কার্ড এবং পাসপোর্ট সাইজের ছবি প্রয়োজন।",
        "step_text_kannada": "ಖಾತೆ ತೆರೆಯಲು ಆಧಾರ್ ಕಾರ್ಡ್, ಪ್ಯಾನ್ ಕಾರ್ಡ್ ಮತ್ತು ಪಾಸ್‌ಪೋರ್ಟ್ ಗಾತ್ರದ ಫೋಟೋ ಅಗತ್ಯ ಎಂದು ಗ್ರಾಹಕರಿಗೆ ತಿಳಿಸಿ.",
        "step_text_odia": "ଖାତା ଖୋଲିବା ପାଇଁ ଆଧାର କାର୍ଡ, ପାନ କାର୍ଡ ଏବଂ ପାସପୋର୍ଟ ଆକାରର ଫଟୋ ଆବଶ୍ୟକ ବୋଲି ଗ୍ରାହକଙ୍କୁ ଜଣାନ୍ତୁ।",
        "step_text_punjabi": "ਗਾਹਕ ਨੂੰ ਦੱਸੋ ਕਿ ਖਾਤਾ ਖੋਲਣ ਲਈ ਆਧਾਰ ਕਾਰਡ, ਪੈਨ ਕਾਰਡ ਅਤੇ ਪਾਸਪੋਰਟ ਸਾਈਜ਼ ਫੋਟੋ ਜ਼ਰੂਰੀ ਹੈ।",
        "step_text_gujarati": "ખાતું ખોલવા માટે આધાર કાર્ડ, પૅન કાર્ડ અને પાસપૉર્ટ સાઈઝ ફોટો જરૂરી છે તે ગ્રાહકને જણાવો.",
        "step_text_malayalam": "അക്കൗണ്ട് തുറക്കാൻ ആധാർ കാർഡ്, പാൻ കാർഡ്, പാസ്‌പോർട്ട് സൈസ് ഫോട്ടോ ആവശ്യമാണെന്ന് ഉപഭോക്താവിനെ അറിയിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "account_opening",
        "step_number": 2,
        "step_text_hindi": "ग्राहक द्वारा प्रस्तुत दस्तावेज़ों की जांच करें और सुनिश्चित करें कि सभी दस्तावेज़ स्पष्ट और वैध हैं।",
        "step_text_marathi": "ग्राहकाने सादर केलेल्या कागदपत्रांची तपासणी करा आणि सर्व कागदपत्रे स्पष्ट आणि वैध असल्याची खात्री करा.",
        "step_text_tamil": "வாடிக்கையாளர் சமர்ப்பித்த ஆவணங்களை சரிபார்த்து, அனைத்து ஆவணங்களும் தெளிவாகவும் செல்லுபடியாகும் என்பதை உறுதிசெய்யவும்.",
        "step_text_telugu": "కస్టమర్ సమర్పించిన పత్రాలను తనిఖీ చేసి, అన్ని పత్రాలు స్పష్టంగా మరియు చెల్లుబాటు అయ్యేలా ఉన్నాయని నిర్ధారించుకోండి.",
        "step_text_bengali": "গ্রাহক কর্তৃক উপস্থাপিত নথিগুলি পরীক্ষা করুন এবং নিশ্চিত করুন যে সমস্ত নথি স্পষ্ট এবং বৈধ।",
        "step_text_kannada": "ಗ್ರಾಹಕರು ಸಲ್ಲಿಸಿದ ದಾಖಲೆಗಳನ್ನು ಪರಿಶೀಲಿಸಿ ಮತ್ತು ಎಲ್ಲಾ ದಾಖಲೆಗಳು ಸ್ಪಷ್ಟ ಮತ್ತು ಮಾನ್ಯವಾಗಿವೆ ಎಂದು ಖಚಿತಪಡಿಸಿ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କ ଦ୍ୱାରା ଦାଖଲ ହୋଇଥିବା ଦଲିଲଗୁଡ଼ିକ ଯାଞ୍ଚ କରନ୍ତୁ ଏବଂ ସବୁ ଦଲିଲ ସ୍ପଷ୍ଟ ଓ ବୈଧ ବୋଲି ନିଶ୍ଚିତ କରନ୍ତୁ।",
        "step_text_punjabi": "ਗਾਹਕ ਵੱਲੋਂ ਪੇਸ਼ ਕੀਤੇ ਦਸਤਾਵੇਜ਼ਾਂ ਦੀ ਜਾਂਚ ਕਰੋ ਅਤੇ ਯਕੀਨੀ ਕਰੋ ਕਿ ਸਾਰੇ ਦਸਤਾਵੇਜ਼ ਸਾਫ਼ ਅਤੇ ਵੈਧ ਹਨ।",
        "step_text_gujarati": "ગ્રાહક દ્વારા સમર્પિત દસ્તાવેજો ચકાસો અને ખાતરી કરો કે બધા દસ્તાવેજો સ્પષ્ટ અને માન્ય છે.",
        "step_text_malayalam": "ഉപഭോക്താവ് സമർപ്പിച്ച രേഖകൾ പരിശോധിച്ച് എല്ലാം വ്യക്തവും സാധുവുമാണെന്ന് ഉറപ്പാക്കുക.",
        "speak_to_customer": False,
        "is_active": True,
    },
    {
        "intent_type": "account_opening",
        "step_number": 3,
        "step_text_hindi": "ग्राहक का KYC पूरा करें — आधार नंबर सिस्टम में दर्ज करें और बायोमेट्रिक या OTP वेरिफिकेशन करें।",
        "step_text_marathi": "ग्राहकाचे KYC पूर्ण करा — आधार नंबर सिस्टममध्ये नोंदवा आणि बायोमेट्रिक किंवा OTP पडताळणी करा.",
        "step_text_tamil": "வாடிக்கையாளரின் KYC முடிக்கவும் — ஆதார் எண்ணை சிஸ்டத்தில் பதிவு செய்து பயோமெட்ரிக் அல்லது OTP சரிபார்ப்பு செய்யவும்.",
        "step_text_telugu": "కస్టమర్ KYC పూర్తి చేయండి — ఆధార్ నంబర్ సిస్టమ్‌లో నమోదు చేసి బయోమెట్రిక్ లేదా OTP వెరిఫికేషన్ చేయండి.",
        "step_text_bengali": "গ্রাহকের KYC সম্পন্ন করুন — সিস্টেমে আধার নম্বর এন্ট্রি করুন এবং বায়োমেট্রিক বা OTP যাচাইকরণ করুন।",
        "step_text_kannada": "ಗ್ರಾಹಕರ KYC ಪೂರ್ಣಗೊಳಿಸಿ — ಸಿಸ್ಟಮ್‌ನಲ್ಲಿ ಆಧಾರ್ ಸಂಖ್ಯೆ ನಮೂದಿಸಿ ಮತ್ತು ಬಯೋಮೆಟ್ರಿಕ್ ಅಥವಾ OTP ಪರಿಶೀಲನೆ ಮಾಡಿ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କ KYC ସଂପୂର୍ଣ୍ଣ କରନ୍ତୁ — ସିଷ୍ଟମରେ ଆଧାର ନମ୍ବର ଏଣ୍ଟ୍ରି କରନ୍ତୁ ଏବଂ ବାୟୋମେଟ୍ରିକ ବା OTP ଯାଞ୍ଚ କରନ୍ତୁ।",
        "step_text_punjabi": "ਗਾਹਕ ਦੀ KYC ਪੂਰੀ ਕਰੋ — ਸਿਸਟਮ ਵਿੱਚ ਆਧਾਰ ਨੰਬਰ ਦਰਜ ਕਰੋ ਅਤੇ ਬਾਇਓਮੈਟ੍ਰਿਕ ਜਾਂ OTP ਵੇਰੀਫਿਕੇਸ਼ਨ ਕਰੋ।",
        "step_text_gujarati": "ગ્રાહક KYC પૂર્ણ કરો — સિસ્ટમમાં આધાર નંબર નોંધો અને બાયોમેટ્રિક અથવા OTP ચકાસણી કરો.",
        "step_text_malayalam": "ഉപഭോക്തൃ KYC പൂർത്തിയാക്കുക — സിസ്റ്റത്തിൽ ആധാർ നമ്പർ നൽകി ബയോമെട്രിക് അല്ലെങ്കിൽ OTP പരിശോധന ചെയ്യുക.",
        "speak_to_customer": False,
        "is_active": True,
    },
    {
        "intent_type": "account_opening",
        "step_number": 4,
        "step_text_hindi": "खाता खोलने का फॉर्म स्कैन करके सिस्टम में अपलोड करें और ग्राहक को पासबुक व स्वागत किट प्रदान करें।",
        "step_text_marathi": "खाते उघडण्याचा फॉर्म स्कॅन करून सिस्टममध्ये अपलोड करा आणि ग्राहकाला पासबुक व वेलकम किट द्या.",
        "step_text_tamil": "கணக்கு திறக்கும் படிவத்தை ஸ்கேன் செய்து சிஸ்டத்தில் பதிவேற்றவும், வாடிக்கையாளருக்கு பாஸ்புக் மற்றும் வரவேற்பு கிட் வழங்கவும்.",
        "step_text_telugu": "ఖాతా తెరిచే ఫారమ్ స్కాన్ చేసి సిస్టమ్‌లో అప్‌లోడ్ చేయండి మరియు కస్టమర్‌కు పాస్‌బుక్ మరియు వెల్‌కమ్ కిట్ అందించండి.",
        "step_text_bengali": "অ্যাকাউন্ট খোলার ফর্ম স্ক্যান করে সিস্টেমে আপলোড করুন এবং গ্রাহককে পাসবুক ও ওয়েলকাম কিট প্রদান করুন।",
        "step_text_kannada": "ಖಾತೆ ತೆರೆಯುವ ಫಾರ್ಮ್ ಸ್ಕ್ಯಾನ್ ಮಾಡಿ ಸಿಸ್ಟಮ್‌ನಲ್ಲಿ ಅಪ್‌ಲೋಡ್ ಮಾಡಿ ಮತ್ತು ಗ್ರಾಹಕರಿಗೆ ಪಾಸ್‌ಬುಕ್ ಮತ್ತು ವೆಲ್‌ಕಮ್ ಕಿಟ್ ಒದಗಿಸಿ.",
        "step_text_odia": "ଖାତା ଖୋଲିବା ଫର୍ମ ସ୍କ୍ୟାନ୍ କରି ସିଷ୍ଟମରେ ଅପଲୋଡ୍ କରନ୍ତୁ ଏବଂ ଗ୍ରାହକଙ୍କୁ ପାସ୍‌ବୁକ ଓ ୱେଲ୍‌କମ୍ କିଟ୍ ଦିଅନ୍ତୁ।",
        "step_text_punjabi": "ਖਾਤਾ ਖੋਲਣ ਦਾ ਫਾਰਮ ਸਕੈਨ ਕਰਕੇ ਸਿਸਟਮ ਵਿੱਚ ਅਪਲੋਡ ਕਰੋ ਅਤੇ ਗਾਹਕ ਨੂੰ ਪਾਸਬੁੱਕ ਅਤੇ ਵੈਲਕਮ ਕਿੱਟ ਦਿਓ।",
        "step_text_gujarati": "ખાતું ખોલવાનું ફૉર્મ સ્કૅન કરી સિસ્ટમમાં અપલોડ કરો અને ગ્રાહકને પાસબુક અને વેલ્કમ કિટ આપો.",
        "step_text_malayalam": "അക്കൗണ്ട് ഓപ്പണിങ് ഫോം സ്കാൻ ചെയ്ത് സിസ്റ്റത്തിൽ അപ്‌ലോഡ് ചെയ്ത് ഉപഭോക്താവിന് പാസ്‌ബുക്കും വെൽക്കം കിറ്റും നൽകുക.",
        "speak_to_customer": True,
        "is_active": True,
    },

    # loan_enquiry
    {
        "intent_type": "loan_enquiry",
        "step_number": 1,
        "step_text_hindi": "ग्राहक से पूछें कि वे किस प्रकार का ऋण चाहते हैं — होम लोन, पर्सनल लोन, एजुकेशन लोन या व्यापार ऋण।",
        "step_text_marathi": "ग्राहकाला विचारा की त्यांना कोणत्या प्रकारचे कर्ज हवे आहे — गृह कर्ज, वैयक्तिक कर्ज, शैक्षणिक कर्ज किंवा व्यावसायिक कर्ज.",
        "step_text_tamil": "வாடிக்கையாளர் எந்த வகையான கடன் விரும்புகிறார் என்று கேளுங்கள் — வீட்டுக் கடன், தனிநபர் கடன், கல்விக் கடன் அல்லது வணிகக் கடன்.",
        "step_text_telugu": "కస్టమర్ ఏ రకమైన రుణం కావాలో అడగండి — హోమ్ లోన్, పర్సనల్ లోన్, ఎడ్యుకేషన్ లోన్ లేదా బిజినెస్ లోన్.",
        "step_text_bengali": "গ্রাহককে জিজ্ঞেস করুন তারা কোন ধরনের ঋণ চান — হোম লোন, ব্যক্তিগত ঋণ, শিক্ষা ঋণ বা ব্যবসায়িক ঋণ।",
        "step_text_kannada": "ಗ್ರಾಹಕರು ಯಾವ ರೀತಿಯ ಸಾಲ ಬಯಸುತ್ತಾರೆ ಎಂದು ಕೇಳಿ — ಗೃಹ ಸಾಲ, ವೈಯಕ್ತಿಕ ಸಾಲ, ಶಿಕ್ಷಣ ಸಾಲ ಅಥವಾ ವ್ಯವಹಾರ ಸಾಲ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କୁ ପଚାରନ୍ତୁ ସେ କି ପ୍ରକାରର ଋଣ ଚାହୁଁଛନ୍ତି — ଗୃହ ଋଣ, ବ୍ୟକ୍ତିଗତ ଋଣ, ଶିକ୍ଷା ଋଣ ବା ବ୍ୟବସାୟ ଋଣ।",
        "step_text_punjabi": "ਗਾਹਕ ਤੋਂ ਪੁੱਛੋ ਕਿ ਉਹ ਕਿਸ ਕਿਸਮ ਦਾ ਕਰਜ਼ਾ ਚਾਹੁੰਦੇ ਹਨ — ਹੋਮ ਲੋਨ, ਪਰਸਨਲ ਲੋਨ, ਐਜੂਕੇਸ਼ਨ ਲੋਨ ਜਾਂ ਕਾਰੋਬਾਰੀ ਕਰਜ਼ਾ।",
        "step_text_gujarati": "ગ્રાહક કયા પ્રકારની લોન ઇચ્છે છે — હોમ લોન, પર્સનલ લોન, એજ્યુકેશન લોન અથવા બિઝનેસ લોન — તે પૂછો.",
        "step_text_malayalam": "ഉപഭോക്താവിന് ഏത് തരം വായ്പ വേണം — ഭവന വായ്പ, വ്യക്തിഗത വായ്പ, വിദ്യാഭ്യാസ വായ്പ അല്ലെങ്കിൽ ബിസിനസ് വായ്പ — എന്ന് ചോദിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "loan_enquiry",
        "step_number": 2,
        "step_text_hindi": "ग्राहक की पात्रता जांचें — आय, क्रेडिट स्कोर और मौजूदा ऋणों की जानकारी सिस्टम में देखें।",
        "step_text_marathi": "ग्राहकाची पात्रता तपासा — उत्पन्न, क्रेडिट स्कोर आणि विद्यमान कर्जाची माहिती सिस्टममध्ये पाहा.",
        "step_text_tamil": "வாடிக்கையாளரின் தகுதியை சரிபார்க்கவும் — வருமானம், கிரெடிட் ஸ்கோர் மற்றும் தற்போதைய கடன்கள் பற்றிய தகவலை சிஸ்டத்தில் பார்க்கவும்.",
        "step_text_telugu": "కస్టమర్ అర్హతను తనిఖీ చేయండి — ఆదాయం, క్రెడిట్ స్కోర్ మరియు ప్రస్తుత రుణాల సమాచారం సిస్టమ్‌లో చూడండి.",
        "step_text_bengali": "গ্রাহকের যোগ্যতা পরীক্ষা করুন — আয়, ক্রেডিট স্কোর এবং বিদ্যমান ঋণ সম্পর্কিত তথ্য সিস্টেমে দেখুন।",
        "step_text_kannada": "ಗ್ರಾಹಕರ ಅರ್ಹತೆ ಪರಿಶೀಲಿಸಿ — ಆದಾಯ, ಕ್ರೆಡಿಟ್ ಸ್ಕೋರ್ ಮತ್ತು ಅಸ್ತಿತ್ವದಲ್ಲಿರುವ ಸಾಲಗಳ ಮಾಹಿತಿ ಸಿಸ್ಟಮ್‌ನಲ್ಲಿ ನೋಡಿ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କ ଯୋଗ୍ୟତା ଯାଞ୍ଚ କରନ୍ତୁ — ଆୟ, ଋଣ ସ୍କୋର ଏବଂ ବିଦ୍ୟମାନ ଋଣ ବିଷୟରେ ସିଷ୍ଟମ ଦେଖନ୍ତୁ।",
        "step_text_punjabi": "ਗਾਹਕ ਦੀ ਯੋਗਤਾ ਜਾਂਚੋ — ਆਮਦਨ, ਕ੍ਰੈਡਿਟ ਸਕੋਰ ਅਤੇ ਮੌਜੂਦਾ ਕਰਜ਼ਿਆਂ ਦੀ ਜਾਣਕਾਰੀ ਸਿਸਟਮ ਵਿੱਚ ਦੇਖੋ।",
        "step_text_gujarati": "ગ્રાહકની પાત્રતા ચકાસો — આવક, ક્રેડિટ સ્કોર અને હાલની લોનની માહિતી સિસ્ટમમાં જુઓ.",
        "step_text_malayalam": "ഉപഭോക്താവിന്റെ യോഗ്യത പരിശോധിക്കുക — വരുമാനം, ക്രെഡിറ്റ് സ്കോർ, നിലവിലുള്ള വായ്പകൾ സിസ്റ്റത്തിൽ നോക്കുക.",
        "speak_to_customer": False,
        "is_active": True,
    },
    {
        "intent_type": "loan_enquiry",
        "step_number": 3,
        "step_text_hindi": "ग्राहक को आवश्यक दस्तावेज़ों की सूची दें — आय प्रमाण, पता प्रमाण, पिछले 6 महीनों का बैंक स्टेटमेंट।",
        "step_text_marathi": "ग्राहकाला आवश्यक कागदपत्रांची यादी द्या — उत्पन्नाचा पुरावा, पत्त्याचा पुरावा, मागील 6 महिन्यांचे बँक स्टेटमेंट.",
        "step_text_tamil": "வாடிக்கையாளருக்கு தேவையான ஆவணங்களின் பட்டியலை வழங்கவும் — வருமான சான்று, முகவரி சான்று, கடந்த 6 மாதங்களின் வங்கி அறிக்கை.",
        "step_text_telugu": "కస్టమర్‌కు అవసరమైన పత్రాల జాబితా ఇవ్వండి — ఆదాయ నిరూపణ, చిరునామా నిరూపణ, గత 6 నెలల బ్యాంక్ స్టేట్‌మెంట్.",
        "step_text_bengali": "গ্রাহককে প্রয়োজনীয় নথির তালিকা দিন — আয়ের প্রমাণ, ঠিকানার প্রমাণ, গত ৬ মাসের ব্যাংক স্টেটমেন্ট।",
        "step_text_kannada": "ಗ್ರಾಹಕರಿಗೆ ಅಗತ್ಯ ದಾಖಲೆಗಳ ಪಟ್ಟಿ ನೀಡಿ — ಆದಾಯ ಪ್ರಮಾಣ, ವಿಳಾಸ ಪ್ರಮಾಣ, ಕಳೆದ 6 ತಿಂಗಳ ಬ್ಯಾಂಕ್ ಸ್ಟೇಟ್‌ಮೆಂಟ್.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କୁ ଆବଶ୍ୟକ ଦଲିଲଗୁଡ଼ିକର ତାଲିକା ଦିଅନ୍ତୁ — ଆୟ ପ୍ରମାଣ, ଠିକଣା ପ୍ରମାଣ, ଗତ ୬ ମାସର ବ୍ୟାଙ୍କ ବିବୃତ୍ତି।",
        "step_text_punjabi": "ਗਾਹਕ ਨੂੰ ਲੋੜੀਂਦੇ ਦਸਤਾਵੇਜ਼ਾਂ ਦੀ ਸੂਚੀ ਦਿਓ — ਆਮਦਨ ਦਾ ਸਬੂਤ, ਪਤੇ ਦਾ ਸਬੂਤ, ਪਿਛਲੇ 6 ਮਹੀਨਿਆਂ ਦਾ ਬੈਂਕ ਸਟੇਟਮੈਂਟ।",
        "step_text_gujarati": "ગ્રાહકને જરૂરી દસ્તાવેજોની યાદી આપો — આવકનો પુરાવો, સરનામાનો પુરાવો, છેલ્લા 6 મહિનાનું બૅંક સ્ટેટ્મેન્ટ.",
        "step_text_malayalam": "ഉപഭോക്താവിന് ആവശ്യമായ രേഖകളുടെ പട്ടിക നൽകുക — വരുമാന തെളിവ്, മേൽവിലാസ തെളിവ്, കഴിഞ്ഞ 6 മാസത്തെ ബാങ്ക് സ്റ്റേറ്റ്മെന്റ്.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "loan_enquiry",
        "step_number": 4,
        "step_text_hindi": "ग्राहक को ऋण आवेदन प्रक्रिया समझाएं — ऑनलाइन आवेदन का लिंक दें या शाखा में फॉर्म भरने में सहायता करें।",
        "step_text_marathi": "ग्राहकाला कर्ज अर्ज प्रक्रिया समजावून सांगा — ऑनलाइन अर्जाचा दुवा द्या किंवा शाखेत फॉर्म भरण्यात मदत करा.",
        "step_text_tamil": "வாடிக்கையாளருக்கு கடன் விண்ணப்ப செயல்முறையை விளக்கவும் — ஆன்லைன் விண்ணப்பத்திற்கான இணைப்பு வழங்கவும் அல்லது கிளையில் படிவம் நிரப்ப உதவி செய்யவும்.",
        "step_text_telugu": "కస్టమర్‌కు రుణ దరఖాస్తు ప్రక్రియ వివరించండి — ఆన్‌లైన్ దరఖాస్తు లింక్ ఇవ్వండి లేదా శాఖలో ఫారమ్ నింపడంలో సహాయం చేయండి.",
        "step_text_bengali": "গ্রাহককে ঋণ আবেদন প্রক্রিয়া বুঝিয়ে দিন — অনলাইন আবেদনের লিংক দিন বা শাখায় ফর্ম পূরণে সহায়তা করুন।",
        "step_text_kannada": "ಗ್ರಾಹಕರಿಗೆ ಸಾಲ ಅರ್ಜಿ ಪ್ರಕ್ರಿಯೆ ವಿವರಿಸಿ — ಆನ್‌ಲೈನ್ ಅರ್ಜಿ ಲಿಂಕ್ ಒದಗಿಸಿ ಅಥವಾ ಶಾಖೆಯಲ್ಲಿ ಫಾರ್ಮ್ ತುಂಬಲು ಸಹಾಯ ಮಾಡಿ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କୁ ଋଣ ଆବେଦନ ପ୍ରକ୍ରିୟା ବୁଝାନ୍ତୁ — ଅନ୍‌ଲାଇନ ଆବେଦନ ଲିଙ୍କ ଦିଅନ୍ତୁ ବା ଶାଖାରେ ଫର୍ମ ଭରିବାରେ ସାହାଯ୍ୟ କରନ୍ତୁ।",
        "step_text_punjabi": "ਗਾਹਕ ਨੂੰ ਕਰਜ਼ਾ ਅਰਜ਼ੀ ਪ੍ਰਕਿਰਿਆ ਸਮਝਾਓ — ਔਨਲਾਈਨ ਅਰਜ਼ੀ ਦਾ ਲਿੰਕ ਦਿਓ ਜਾਂ ਸ਼ਾਖਾ ਵਿੱਚ ਫਾਰਮ ਭਰਨ ਵਿੱਚ ਮਦਦ ਕਰੋ।",
        "step_text_gujarati": "ગ્રાહકને લોન અરજી પ્રક્રિયા સમજાવો — ઑનલાઇન અરજી લિંક આપો અથવા શાખામાં ફૉર્મ ભરવામાં મદદ કરો.",
        "step_text_malayalam": "ഉപഭോക്താവിന് വായ്പ അപേക്ഷ പ്രക്രിയ വിശദീകരിക്കുക — ഓൺലൈൻ അപേക്ഷ ലിങ്ക് നൽകുക അല്ലെങ്കിൽ ശാഖയിൽ ഫോം പൂരിപ്പിക്കാൻ സഹായിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },

    # kyc_update
    {
        "intent_type": "kyc_update",
        "step_number": 1,
        "step_text_hindi": "ग्राहक से पूछें कि वे किस जानकारी को अपडेट करना चाहते हैं — पता, मोबाइल नंबर, ईमेल या अन्य विवरण।",
        "step_text_marathi": "ग्राहकाला विचारा की ते कोणती माहिती अपडेट करू इच्छितात — पत्ता, मोबाइल नंबर, ईमेल किंवा इतर तपशील.",
        "step_text_tamil": "வாடிக்கையாளர் என்ன தகவலை புதுப்பிக்க விரும்புகிறார் என்று கேளுங்கள் — முகவரி, மொபைல் எண், மின்னஞ்சல் அல்லது பிற விவரங்கள்.",
        "step_text_telugu": "కస్టమర్ ఏ సమాచారాన్ని అప్‌డేట్ చేయాలనుకుంటున్నారో అడగండి — చిరునామా, మొబైల్ నంబర్, ఇమెయిల్ లేదా ఇతర వివరాలు.",
        "step_text_bengali": "গ্রাহককে জিজ্ঞেস করুন তারা কোন তথ্য আপডেট করতে চান — ঠিকানা, মোবাইল নম্বর, ইমেইল বা অন্যান্য বিবরণ।",
        "step_text_kannada": "ಗ್ರಾಹಕರು ಯಾವ ಮಾಹಿತಿಯನ್ನು ಅಪ್‌ಡೇಟ್ ಮಾಡಲು ಬಯಸುತ್ತಾರೆ ಎಂದು ಕೇಳಿ — ವಿಳಾಸ, ಮೊಬೈಲ್ ಸಂಖ್ಯೆ, ಇಮೇಲ್ ಅಥವಾ ಇತರ ವಿವರಗಳು.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କୁ ପଚାରନ୍ତୁ ସେ କି ତଥ୍ୟ ଅପଡେଟ କରିବାକୁ ଚାହୁଁଛନ୍ତି — ଠିକଣା, ମୋବାଇଲ ନମ୍ବର, ଇମେଲ ବା ଅନ୍ୟ ବିବରଣ।",
        "step_text_punjabi": "ਗਾਹਕ ਤੋਂ ਪੁੱਛੋ ਕਿ ਉਹ ਕਿਹੜੀ ਜਾਣਕਾਰੀ ਅਪਡੇਟ ਕਰਨਾ ਚਾਹੁੰਦੇ ਹਨ — ਪਤਾ, ਮੋਬਾਈਲ ਨੰਬਰ, ਈਮੇਲ ਜਾਂ ਹੋਰ ਵੇਰਵੇ।",
        "step_text_gujarati": "ગ્રાહક કઈ માહિતી અપડેટ કરવા ઇચ્છે છે — સરનામું, મોબાઇલ નંબર, ઇમેઇલ અથવા અન્ય — તે પૂછો.",
        "step_text_malayalam": "ഉപഭോക്താവ് ഏത് വിവരം അപ്‌ഡേറ്റ് ചെയ്യണം — വിലാസം, മൊബൈൽ നമ്പർ, ഇ-മെയിൽ അല്ലെങ്കിൽ മറ്റ് — എന്ന് ചോദിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "kyc_update",
        "step_number": 2,
        "step_text_hindi": "अपडेट के लिए सहायक दस्तावेज़ लें — नए पते के लिए उपयोगिता बिल या सरकारी पत्र, मोबाइल अपडेट के लिए OTP वेरिफिकेशन।",
        "step_text_marathi": "अपडेटसाठी सहाय्यक कागदपत्रे घ्या — नवीन पत्त्यासाठी उपयोगिता बिल किंवा सरकारी पत्र, मोबाइल अपडेटसाठी OTP पडताळणी.",
        "step_text_tamil": "புதுப்பிப்பிற்கான ஆதார ஆவணங்களை வாங்குங்கள் — புதிய முகவரிக்கு பயன்பாட்டு பில் அல்லது அரசு கடிதம், மொபைல் புதுப்பிப்பிற்கு OTP சரிபார்ப்பு.",
        "step_text_telugu": "అప్‌డేట్ కోసం సహాయక పత్రాలు తీసుకోండి — కొత్త చిరునామాకు యుటిలిటీ బిల్ లేదా ప్రభుత్వ లేఖ, మొబైల్ అప్‌డేట్‌కు OTP వెరిఫికేషన్.",
        "step_text_bengali": "আপডেটের জন্য সহায়ক নথি নিন — নতুন ঠিকানার জন্য ইউটিলিটি বিল বা সরকারি চিঠি, মোবাইল আপডেটের জন্য OTP যাচাইকরণ।",
        "step_text_kannada": "ಅಪ್‌ಡೇಟ್‌ಗಾಗಿ ಬೆಂಬಲ ದಾಖಲೆಗಳು ತೆಗೆದುಕೊಳ್ಳಿ — ಹೊಸ ವಿಳಾಸಕ್ಕಾಗಿ ಯುಟಿಲಿಟಿ ಬಿಲ್ ಅಥವಾ ಸರ್ಕಾರಿ ಪತ್ರ, ಮೊಬೈಲ್ ಅಪ್‌ಡೇಟ್‌ಗಾಗಿ OTP ಪರಿಶೀಲನೆ.",
        "step_text_odia": "ଅପଡେଟ ପାଇଁ ସହାୟକ ଦଲିଲ ନିଅନ୍ତୁ — ନୂଆ ଠିକଣା ପାଇଁ ୟୁଟିଲିଟି ବିଲ ବା ସରକାରୀ ପତ୍ର, ମୋବାଇଲ ଅପଡେଟ ପାଇଁ OTP ଯାଞ୍ଚ।",
        "step_text_punjabi": "ਅਪਡੇਟ ਲਈ ਸਹਾਇਕ ਦਸਤਾਵੇਜ਼ ਲਓ — ਨਵੇਂ ਪਤੇ ਲਈ ਯੂਟਿਲਿਟੀ ਬਿੱਲ ਜਾਂ ਸਰਕਾਰੀ ਪੱਤਰ, ਮੋਬਾਈਲ ਅਪਡੇਟ ਲਈ OTP ਵੇਰੀਫਿਕੇਸ਼ਨ।",
        "step_text_gujarati": "અપડેટ માટે આધાર રેખો લો — નવા સરનામા માટે યુટિલિટી બિલ અથવા સરકારી પત્ર, મોબાઇલ અપડેટ માટે OTP ચકાસણી.",
        "step_text_malayalam": "അപ്‌ഡേറ്റിനായി ആധാര രേഖ വാങ്ങുക — പുതിയ വിലാസത്തിന് ഉപയോഗ ബിൽ അല്ലെങ്കിൽ സർക്കാർ കത്ത്, മൊബൈൽ അപ്‌ഡേറ്റിന് OTP പരിശോധന.",
        "speak_to_customer": False,
        "is_active": True,
    },
    {
        "intent_type": "kyc_update",
        "step_number": 3,
        "step_text_hindi": "सिस्टम में KYC अपडेट करें, ग्राहक को पुष्टि दें और बताएं कि परिवर्तन 24 घंटों में प्रभावी होगा।",
        "step_text_marathi": "सिस्टममध्ये KYC अपडेट करा, ग्राहकाला पुष्टी द्या आणि सांगा की बदल 24 तासांत प्रभावी होईल.",
        "step_text_tamil": "சிஸ்டத்தில் KYC புதுப்பிக்கவும், வாடிக்கையாளருக்கு உறுதிப்படுத்தல் கொடுங்கள் மற்றும் மாற்றம் 24 மணி நேரத்தில் நடைமுறைக்கு வரும் என்று தெரிவிக்கவும்.",
        "step_text_telugu": "సిస్టమ్‌లో KYC అప్‌డేట్ చేయండి, కస్టమర్‌కు నిర్ధారణ ఇవ్వండి మరియు మార్పు 24 గంటల్లో అమల్లోకి వస్తుందని చెప్పండి.",
        "step_text_bengali": "সিস্টেমে KYC আপডেট করুন, গ্রাহককে নিশ্চিতকরণ দিন এবং জানান যে পরিবর্তন ২৪ ঘণ্টার মধ্যে কার্যকর হবে।",
        "step_text_kannada": "ಸಿಸ್ಟಮ್‌ನಲ್ಲಿ KYC ಅಪ್‌ಡೇಟ್ ಮಾಡಿ, ಗ್ರಾಹಕರಿಗೆ ದೃಢೀಕರಣ ನೀಡಿ ಮತ್ತು ಬದಲಾವಣೆ 24 ಗಂಟೆಗಳಲ್ಲಿ ಜಾರಿಗೆ ಬರುತ್ತದೆ ಎಂದು ತಿಳಿಸಿ.",
        "step_text_odia": "ସିଷ୍ଟମରେ KYC ଅପଡେଟ କରନ୍ତୁ, ଗ୍ରାହକଙ୍କୁ ନିଶ୍ଚିତ ଦିଅନ୍ତୁ ଏବଂ ଜଣାନ୍ତୁ ଯେ ପରିବର୍ତ୍ତନ ୨୪ ଘଣ୍ଟାରେ ପ୍ରଭାବଶାଳୀ ହେବ।",
        "step_text_punjabi": "ਸਿਸਟਮ ਵਿੱਚ KYC ਅਪਡੇਟ ਕਰੋ, ਗਾਹਕ ਨੂੰ ਪੁਸ਼ਟੀ ਦਿਓ ਅਤੇ ਦੱਸੋ ਕਿ ਬਦਲਾਅ 24 ਘੰਟਿਆਂ ਵਿੱਚ ਪ੍ਰਭਾਵੀ ਹੋਵੇਗਾ।",
        "step_text_gujarati": "સિસ્ટમમાં KYC અપડેટ કરો, ગ્રાહકને પુષ્ટિ આપો અને જણાવો કે ફેરફાર 24 કલાકમાં અમલી થશે.",
        "step_text_malayalam": "സിസ്റ്റത്തിൽ KYC അപ്‌ഡേറ്റ് ചെയ്ത് ഉപഭോക്താവിന് സ്ഥിരീകരണം നൽകി മാറ്റം 24 മണിക്കൂറിൽ നടപ്പാകുമെന്ന് അറിയിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },

    # balance_enquiry
    {
        "intent_type": "balance_enquiry",
        "step_number": 1,
        "step_text_hindi": "ग्राहक की पहचान सत्यापित करें — आधार नंबर के अंतिम 4 अंक या रजिस्टर्ड मोबाइल पर OTP के माध्यम से।",
        "step_text_marathi": "ग्राहकाची ओळख सत्यापित करा — आधार नंबरचे शेवटचे 4 अंक किंवा नोंदणीकृत मोबाइलवर OTP द्वारे.",
        "step_text_tamil": "வாடிக்கையாளரின் அடையாளத்தை சரிபார்க்கவும் — ஆதார் எண்ணின் கடைசி 4 இலக்கங்கள் அல்லது பதிவுசெய்யப்பட்ட மொபைலில் OTP மூலம்.",
        "step_text_telugu": "కస్టమర్ గుర్తింపు ధృవీకరించండి — ఆధార్ నంబర్ చివరి 4 అంకెలు లేదా రిజిస్టర్డ్ మొబైల్‌లో OTP ద్వారా.",
        "step_text_bengali": "গ্রাহকের পরিচয় যাচাই করুন — আধার নম্বরের শেষ ৪ সংখ্যা বা নিবন্ধিত মোবাইলে OTP এর মাধ্যমে।",
        "step_text_kannada": "ಗ್ರಾಹಕರ ಗುರುತಿನ ಪರಿಶೀಲನೆ ಮಾಡಿ — ಆಧಾರ್ ಸಂಖ್ಯೆಯ ಕೊನೆ 4 ಅಂಕಿಗಳು ಅಥವಾ ನೋಂದಾಯಿತ ಮೊಬೈಲ್‌ನಲ್ಲಿ OTP ಮೂಲಕ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କ ପରିଚୟ ଯାଞ୍ଚ କରନ୍ତୁ — ଆଧାର ନମ୍ବରର ଶେଷ ୪ ଅଙ୍କ ବା ପଞ୍ଜୀଭୁକ୍ତ ମୋବାଇଲରେ OTP ମାଧ୍ୟମରେ।",
        "step_text_punjabi": "ਗਾਹਕ ਦੀ ਪਛਾਣ ਪੁਸ਼ਟੀ ਕਰੋ — ਆਧਾਰ ਨੰਬਰ ਦੇ ਆਖਰੀ 4 ਅੰਕ ਜਾਂ ਰਜਿਸਟਰਡ ਮੋਬਾਈਲ 'ਤੇ OTP ਦੁਆਰਾ।",
        "step_text_gujarati": "ગ્રાહકની ઓળખ ચકાસો — આધાર નંબરના છેલ્લા 4 અંક અથવા નોંધાયેલ મોબાઇલ પર OTP દ્વારા.",
        "step_text_malayalam": "ഉപഭോക്താവിന്റെ ഐഡന്റിറ്റി പരിശോധിക്കുക — ആധാർ നമ്പറിന്റെ അവസാന 4 അക്കങ്ങൾ അല്ലെങ്കിൽ രജിസ്റ്റേർഡ് മൊബൈലിൽ OTP വഴി.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "balance_enquiry",
        "step_number": 2,
        "step_text_hindi": "सिस्टम से बैलेंस देखें और ग्राहक को बताएं। मिनी-स्टेटमेंट की आवश्यकता हो तो प्रिंट करें या SMS भेजें।",
        "step_text_marathi": "सिस्टमवरून शिल्लक पाहा आणि ग्राहकाला सांगा. मिनी-स्टेटमेंट आवश्यक असल्यास प्रिंट करा किंवा SMS पाठवा.",
        "step_text_tamil": "சிஸ்டத்தில் இருந்து இருப்பு பார்த்து வாடிக்கையாளருக்கு தெரிவிக்கவும். மினி-ஸ்டேட்மென்ட் தேவைப்பட்டால் அச்சிடவும் அல்லது SMS அனுப்பவும்.",
        "step_text_telugu": "సిస్టమ్ నుండి బ్యాలెన్స్ చూసి కస్టమర్‌కు తెలియజేయండి. మినీ-స్టేట్‌మెంట్ అవసరమైతే ప్రింట్ చేయండి లేదా SMS పంపండి.",
        "step_text_bengali": "সিস্টেম থেকে ব্যালেন্স দেখুন এবং গ্রাহককে জানান। মিনি-স্টেটমেন্ট প্রয়োজন হলে প্রিন্ট করুন বা SMS পাঠান।",
        "step_text_kannada": "ಸಿಸ್ಟಮ್‌ನಿಂದ ಬ್ಯಾಲೆನ್ಸ್ ನೋಡಿ ಗ್ರಾಹಕರಿಗೆ ತಿಳಿಸಿ. ಮಿನಿ-ಸ್ಟೇಟ್‌ಮೆಂಟ್ ಬೇಕಾದರೆ ಪ್ರಿಂಟ್ ಮಾಡಿ ಅಥವಾ SMS ಕಳುಹಿಸಿ.",
        "step_text_odia": "ସିଷ୍ଟମ ରୁ ଅବଶ୍ୟ ଦେଖନ୍ତୁ ଏବଂ ଗ୍ରାହକଙ୍କୁ ଜଣାନ୍ତୁ। ମିନି-ଷ୍ଟେଟ୍‌ମେଣ୍ଟ ଦରକାର ହେଲେ ପ୍ରିଣ୍ଟ କରନ୍ତୁ ବା SMS ପଠାନ୍ତୁ।",
        "step_text_punjabi": "ਸਿਸਟਮ ਤੋਂ ਬੈਲੇਂਸ ਦੇਖੋ ਅਤੇ ਗਾਹਕ ਨੂੰ ਦੱਸੋ। ਮਿਨੀ-ਸਟੇਟਮੈਂਟ ਦੀ ਲੋੜ ਹੋਵੇ ਤਾਂ ਪ੍ਰਿੰਟ ਕਰੋ ਜਾਂ SMS ਭੇਜੋ।",
        "step_text_gujarati": "સિસ્ટમ પરથી બૅલૅન્સ જુઓ અને ગ્રાહકને જણાવો. મિની-સ્ટેટ્મેન્ટ જરૂરી હોય તો પ્રિન્ટ કરો અથવા SMS મોકલો.",
        "step_text_malayalam": "സിസ്റ്റത്തിൽ നിന്ന് ബാലൻസ് നോക്കി ഉപഭോക്താവിനെ അറിയിക്കുക. മിനി-സ്റ്റേറ്റ്മെന്റ് ആവശ്യമെങ്കിൽ പ്രിന്റ് ചെയ്യുക അല്ലെങ്കിൽ SMS അയയ്ക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },

    # card_services
    {
        "intent_type": "card_services",
        "step_number": 1,
        "step_text_hindi": "ग्राहक से पूछें कि वे किस कार्ड से संबंधित सेवा चाहते हैं — नया कार्ड, कार्ड ब्लॉक, PIN बदलना या लिमिट बढ़ाना।",
        "step_text_marathi": "ग्राहकाला विचारा की त्यांना कोणत्या कार्ड सेवेची आवश्यकता आहे — नवीन कार्ड, कार्ड ब्लॉक, PIN बदलणे किंवा मर्यादा वाढवणे.",
        "step_text_tamil": "வாடிக்கையாளர் என்ன அட்டை சேவை தேவை என்று கேளுங்கள் — புதிய அட்டை, அட்டை தடுப்பு, PIN மாற்றம் அல்லது வரம்பு அதிகரிப்பு.",
        "step_text_telugu": "కస్టమర్ ఏ కార్డ్ సేవ కావాలో అడగండి — కొత్త కార్డ్, కార్డ్ బ్లాక్, PIN మార్పు లేదా లిమిట్ పెంపు.",
        "step_text_bengali": "গ্রাহককে জিজ্ঞেস করুন তারা কোন কার্ড সেবা চান — নতুন কার্ড, কার্ড ব্লক, PIN পরিবর্তন বা সীমা বৃদ্ধি।",
        "step_text_kannada": "ಗ್ರಾಹಕರು ಯಾವ ಕಾರ್ಡ್ ಸೇವೆ ಬಯಸುತ್ತಾರೆ ಎಂದು ಕೇಳಿ — ಹೊಸ ಕಾರ್ಡ್, ಕಾರ್ಡ್ ಬ್ಲಾಕ್, PIN ಬದಲಾವಣೆ ಅಥವಾ ಮಿತಿ ಹೆಚ್ಚಳ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କୁ ପଚାରନ୍ତୁ ସେ କି କାର୍ଡ ସେବା ଚାହୁଁଛନ୍ତି — ନୂଆ କାର୍ଡ, କାର୍ଡ ବ୍ଲକ, PIN ବଦଳାଇବା ବା ସୀମା ବଢ଼ାଇବା।",
        "step_text_punjabi": "ਗਾਹਕ ਤੋਂ ਪੁੱਛੋ ਕਿ ਉਹ ਕਿਹੜੀ ਕਾਰਡ ਸੇਵਾ ਚਾਹੁੰਦੇ ਹਨ — ਨਵਾਂ ਕਾਰਡ, ਕਾਰਡ ਬਲਾਕ, PIN ਬਦਲਣਾ ਜਾਂ ਲਿਮਿਟ ਵਧਾਉਣਾ।",
        "step_text_gujarati": "ગ્રાહક કઈ કાર્ડ સેવા ઇચ્છે છે — નવું કાર્ડ, કાર્ડ બ્લૉક, PIN બદલવો અથવા લિમિટ વધારવી — તે પૂછો.",
        "step_text_malayalam": "ഉപഭോക്താവ് ഏത് കാർഡ് സേവനം ആഗ്രഹിക്കുന്നു — പുതിയ കാർഡ്, കാർഡ് ബ്ലോക്ക്, PIN മാറ്റൽ അല്ലെങ്കിൽ പരിധി വർദ്ധന — എന്ന് ചോദിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "card_services",
        "step_number": 2,
        "step_text_hindi": "ग्राहक की पहचान सत्यापित करें और कार्ड विवरण सिस्टम में जांचें।",
        "step_text_marathi": "ग्राहकाची ओळख सत्यापित करा आणि कार्ड तपशील सिस्टममध्ये तपासा.",
        "step_text_tamil": "வாடிக்கையாளரின் அடையாளத்தை சரிபார்த்து அட்டை விவரங்களை சிஸ்டத்தில் சரிபார்க்கவும்.",
        "step_text_telugu": "కస్టమర్ గుర్తింపు ధృవీకరించి కార్డ్ వివరాలు సిస్టమ్‌లో తనిఖీ చేయండి.",
        "step_text_bengali": "গ্রাহকের পরিচয় যাচাই করুন এবং কার্ডের বিবরণ সিস্টেমে পরীক্ষা করুন।",
        "step_text_kannada": "ಗ್ರಾಹಕರ ಗುರುತಿನ ಪರಿಶೀಲನೆ ಮಾಡಿ ಮತ್ತು ಕಾರ್ಡ್ ವಿವರಗಳನ್ನು ಸಿಸ್ಟಮ್‌ನಲ್ಲಿ ಪರಿಶೀಲಿಸಿ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କ ପରିଚୟ ଯାଞ୍ଚ କରନ୍ତୁ ଏବଂ କାର୍ଡ ବିବରଣ ସିଷ୍ଟମରେ ଦେଖନ୍ତୁ।",
        "step_text_punjabi": "ਗਾਹਕ ਦੀ ਪਛਾਣ ਪੁਸ਼ਟੀ ਕਰੋ ਅਤੇ ਕਾਰਡ ਵੇਰਵੇ ਸਿਸਟਮ ਵਿੱਚ ਚੈੱਕ ਕਰੋ।",
        "step_text_gujarati": "ગ્રાહકની ઓળખ ચકાસો અને કાર્ડ વિગત સિસ્ટમમાં તપાસો.",
        "step_text_malayalam": "ഉപഭോക്താവിന്റെ ഐഡന്റിറ്റി സ്ഥിരീകരിച്ച് കാർഡ് വിവരങ്ങൾ സിസ്റ്റത്തിൽ പരിശോധിക്കുക.",
        "speak_to_customer": False,
        "is_active": True,
    },
    {
        "intent_type": "card_services",
        "step_number": 3,
        "step_text_hindi": "अनुरोधित कार्ड सेवा प्रोसेस करें और ग्राहक को पुष्टि दें कि कार्य पूरा हुआ।",
        "step_text_marathi": "विनंती केलेली कार्ड सेवा प्रक्रिया करा आणि ग्राहकाला कार्य पूर्ण झाल्याची पुष्टी द्या.",
        "step_text_tamil": "கோரப்பட்ட அட்டை சேவையை செயல்படுத்தி வாடிக்கையாளருக்கு பணி முடிந்ததை உறுதிப்படுத்தவும்.",
        "step_text_telugu": "అభ్యర్థించిన కార్డ్ సేవను ప్రాసెస్ చేసి కస్టమర్‌కు పని పూర్తయిందని నిర్ధారణ ఇవ్వండి.",
        "step_text_bengali": "অনুরোধকৃত কার্ড সেবা প্রক্রিয়া করুন এবং গ্রাহককে কাজ সম্পন্ন হয়েছে বলে নিশ্চিত করুন।",
        "step_text_kannada": "ವಿನಂತಿಸಿದ ಕಾರ್ಡ್ ಸೇವೆ ಪ್ರಕ್ರಿಯೆಗೊಳಿಸಿ ಮತ್ತು ಗ್ರಾಹಕರಿಗೆ ಕಾರ್ಯ ಪೂರ್ಣಗೊಂಡಿದೆ ಎಂದು ದೃಢೀಕರಿಸಿ.",
        "step_text_odia": "ଅନୁରୋଧ କରାଯାଇଥିବା କାର୍ଡ ସେବା ପ୍ରକ୍ରିୟା କରନ୍ତୁ ଏବଂ ଗ୍ରାହକଙ୍କୁ କାର୍ଯ୍ୟ ସଂପୂର୍ଣ୍ଣ ହୋଇଛି ବୋଲି ନିଶ୍ଚିତ ଦିଅନ୍ତୁ।",
        "step_text_punjabi": "ਬੇਨਤੀ ਕੀਤੀ ਕਾਰਡ ਸੇਵਾ ਪ੍ਰੋਸੈਸ ਕਰੋ ਅਤੇ ਗਾਹਕ ਨੂੰ ਪੁਸ਼ਟੀ ਦਿਓ ਕਿ ਕੰਮ ਪੂਰਾ ਹੋਇਆ।",
        "step_text_gujarati": "વિનંતી કરેલ કાર્ડ સેવા પ્રક્રિયા કરો અને ગ્રાહકને પુષ્ટિ આપો કે કાર્ય પૂર્ણ થયું.",
        "step_text_malayalam": "അഭ്യർത്ഥിച്ച കാർഡ് സേവനം പ്രോസസ്സ് ചെയ്ത് ഉപഭോക്താവിന് ജോലി പൂർത്തിയായി എന്ന് ഉറപ്പ് നൽകുക.",
        "speak_to_customer": True,
        "is_active": True,
    },

    # fixed_deposit
    {
        "intent_type": "fixed_deposit",
        "step_number": 1,
        "step_text_hindi": "ग्राहक से FD की राशि, अवधि और नॉमिनी विवरण पूछें।",
        "step_text_marathi": "ग्राहकाला FD ची रक्कम, कालावधी आणि नॉमिनी तपशील विचारा.",
        "step_text_tamil": "வாடிக்கையாளரிடம் FD தொகை, காலம் மற்றும் நாமினி விவரங்கள் கேளுங்கள்.",
        "step_text_telugu": "కస్టమర్ నుండి FD మొత్తం, కాలావధి మరియు నామినీ వివరాలు అడగండి.",
        "step_text_bengali": "গ্রাহকের কাছ থেকে FD এর পরিমাণ, মেয়াদ এবং মনোনীত ব্যক্তির বিবরণ জিজ্ঞেস করুন।",
        "step_text_kannada": "ಗ್ರಾಹಕರಿಂದ FD ಮೊತ್ತ, ಅವಧಿ ಮತ್ತು ನಾಮಿನಿ ವಿವರಗಳನ್ನು ಕೇಳಿ.",
        "step_text_odia": "ଗ୍ରାହକଙ୍କଠାରୁ FD ପରିମାଣ, ଅବଧି ଏବଂ ନୋମିନି ବିବରଣ ପଚାରନ୍ତୁ।",
        "step_text_punjabi": "ਗਾਹਕ ਤੋਂ FD ਦੀ ਰਕਮ, ਮਿਆਦ ਅਤੇ ਨਾਮਿਨੀ ਵੇਰਵੇ ਪੁੱਛੋ।",
        "step_text_gujarati": "ગ્રાહક પાસેથી FD ની રકમ, મુદ્દત અને નૉમિની વિગત પૂછો.",
        "step_text_malayalam": "ഉപഭോക്താവിൽ നിന്ന് FD തുക, കാലാവധി, നോമിനി വിവരങ്ങൾ ചോദിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "fixed_deposit",
        "step_number": 2,
        "step_text_hindi": "वर्तमान ब्याज दरें बताएं और FD कैलकुलेटर से मैच्योरिटी राशि दिखाएं।",
        "step_text_marathi": "सध्याचे व्याजदर सांगा आणि FD कॅल्क्युलेटरमधून मॅच्युरिटी रक्कम दाखवा.",
        "step_text_tamil": "தற்போதைய வட்டி விகிதங்கள் சொல்லி FD கால்குலேட்டரில் முதிர்வு தொகை காட்டுங்கள்.",
        "step_text_telugu": "ప్రస్తుత వడ్డీ రేట్లు చెప్పి FD కాల్క్యులేటర్ నుండి మెచ్యూరిటీ మొత్తం చూపించండి.",
        "step_text_bengali": "বর্তমান সুদের হার জানান এবং FD ক্যালকুলেটর থেকে ম্যাচিউরিটি পরিমাণ দেখান।",
        "step_text_kannada": "ಪ್ರಸ್ತುತ ಬಡ್ಡಿ ದರಗಳನ್ನು ತಿಳಿಸಿ FD ಕ್ಯಾಲ್ಕುಲೇಟರ್‌ನಿಂದ ಮೆಚ್ಯೂರಿಟಿ ಮೊತ್ತ ತೋರಿಸಿ.",
        "step_text_odia": "ବର୍ତ୍ତମାନ ସୁଧ ହାର ଜଣାନ୍ତୁ ଏବଂ FD କ୍ୟାଲ୍‌କୁଲେଟରରୁ ପରିପକ୍ୱ ରାଶି ଦେଖାନ୍ତୁ।",
        "step_text_punjabi": "ਮੌਜੂਦਾ ਵਿਆਜ ਦਰਾਂ ਦੱਸੋ ਅਤੇ FD ਕੈਲਕੁਲੇਟਰ ਤੋਂ ਮੈਚਿਓਰਿਟੀ ਰਕਮ ਦਿਖਾਓ।",
        "step_text_gujarati": "વর્તmaan વ્યાજ દર જણાવો અને FD કૅલ્ક્યુલેટર પ્રથી મૅચ્યૉરિટી રકમ દેखावો.",
        "step_text_malayalam": "നിലവിലെ പലിശ നിരക്ക് പറഞ്ഞ് FD കാൽക്കുലേറ്ററിൽ മെച്യൂരിറ്റി തുക കാണിക്കുക.",
        "speak_to_customer": True,
        "is_active": True,
    },
    {
        "intent_type": "fixed_deposit",
        "step_number": 3,
        "step_text_hindi": "FD बुक करें, रसीद प्रिंट करें और ग्राहक को FD प्रमाणपत्र दें।",
        "step_text_marathi": "FD बुक करा, पावती प्रिंट करा आणि ग्राहकाला FD प्रमाणपत्र द्या.",
        "step_text_tamil": "FD புக் செய்து ரசீது அச்சிட்டு வாடிக்கையாளருக்கு FD சான்றிதழ் வழங்கவும்.",
        "step_text_telugu": "FD బుక్ చేసి రసీదు ప్రింట్ చేసి కస్టమర్‌కు FD సర్టిఫికేట్ ఇవ్వండి.",
        "step_text_bengali": "FD বুক করুন, রসিদ প্রিন্ট করুন এবং গ্রাহককে FD সার্টিফিকেট দিন।",
        "step_text_kannada": "FD ಬುಕ್ ಮಾಡಿ, ರಸೀದಿ ಪ್ರಿಂಟ್ ಮಾಡಿ ಮತ್ತು ಗ್ರಾಹಕರಿಗೆ FD ಪ್ರಮಾಣಪತ್ರ ನೀಡಿ.",
        "step_text_odia": "FD ବୁକ କରନ୍ତୁ, ରସିଦ ପ୍ରିଣ୍ଟ କରନ୍ତୁ ଏବଂ ଗ୍ରାହକଙ୍କୁ FD ପ୍ରମାଣପତ୍ର ଦିଅନ୍ତୁ।",
        "step_text_punjabi": "FD ਬੁੱਕ ਕਰੋ, ਰਸੀਦ ਪ੍ਰਿੰਟ ਕਰੋ ਅਤੇ ਗਾਹਕ ਨੂੰ FD ਸਰਟੀਫਿਕੇਟ ਦਿਓ।",
        "step_text_gujarati": "FD બુક કરો, રસીદ પ્રિન્ટ કરો અને ગ્રાહકને FD સર્ટિફિકેટ આપો.",
        "step_text_malayalam": "FD ബുക്ക് ചെയ്ത്, രസീദ് പ്രിന്റ് ചെയ്ത് ഉപഭോക്താവിന് FD സർട്ടിഫിക്കറ്റ് നൽകുക.",
        "speak_to_customer": True,
        "is_active": True,
    },
]


# SEED FUNCTIONS

def seed_branches(db: SyncSession) -> Dict[str, Branch]:
    """Insert all branches, return {branch_code: Branch} map."""
    branch_map: Dict[str, Branch] = {}
    for bdata in BRANCHES_DATA:
        existing = db.execute(
            select(Branch).where(Branch.branch_code == bdata["branch_code"])
        ).scalar_one_or_none()
        if existing:
            print(f"  [SKIP] Branch '{bdata['branch_code']}' already exists (id={existing.id})")
            branch_map[bdata["branch_code"]] = existing
        else:
            branch = Branch(**bdata)
            db.add(branch)
            db.flush()
            print(f"  [OK]   Branch '{branch.branch_code}' — {branch.branch_name} (id={branch.id})")
            branch_map[bdata["branch_code"]] = branch
    return branch_map


def seed_staff(db: SyncSession, branch_map: Dict[str, Branch]) -> None:
    """Insert all staff members, linking to correct branch."""
    for sdata in STAFF_DATA:
        existing = db.execute(
            select(StaffMember).where(StaffMember.staff_id == sdata["staff_id"])
        ).scalar_one_or_none()
        if existing:
            print(f"  [SKIP] Staff '{sdata['username']}' already exists (id={existing.id})")
            continue
        branch = branch_map[sdata["branch_code"]]
        staff = StaffMember(
            staff_id=sdata["staff_id"],
            username=sdata["username"],
            password_hash=pwd_context.hash(sdata["password_plain"]),
            full_name=sdata["full_name"],
            role=sdata["role"],
            branch_id=branch.id,
            languages_known=sdata["languages_known"],
            is_active=sdata["is_active"],
        )
        db.add(staff)
        db.flush()
        print(f"  [OK]   Staff '{staff.username}' — {staff.full_name} ({staff.role}) (id={staff.id})")


def seed_process_steps(db: SyncSession) -> None:
    inserted = 0
    skipped = 0
    for step_data in PROCESS_STEPS:
        existing = db.execute(
            select(ProcessStep).where(
                ProcessStep.intent_type == step_data["intent_type"],
                ProcessStep.step_number == step_data["step_number"],
            )
        ).scalar_one_or_none()
        if existing:
            skipped += 1
            continue
        step = ProcessStep(**step_data)
        db.add(step)
        inserted += 1
    db.flush()
    intent_counts: dict[str, int] = {}
    for s in PROCESS_STEPS:
        intent_counts[s["intent_type"]] = intent_counts.get(s["intent_type"], 0) + 1
    for intent, count in intent_counts.items():
        print(f"  [OK]   ProcessSteps '{intent}' — {count} steps")
    if skipped:
        print(f"  [SKIP] {skipped} process step(s) already existed")
    print(f"  [OK]   Total inserted: {inserted}")


def seed_demo_sessions(db: SyncSession, branch_map: Dict[str, Branch]) -> None:
    """Insert demo sessions with pre-filled customer PII for hackathon demo."""
    from datetime import datetime, timezone
    inserted = 0
    skipped = 0
    for sdata in DEMO_SESSIONS_DATA:
        existing = db.execute(
            select(Session).where(Session.token_number == sdata["token_number"])
        ).scalar_one_or_none()
        if existing:
            # Update PII fields even if session exists (so re-seeding refreshes data)
            existing.customer_name           = sdata.get("customer_name")
            existing.customer_account_number = sdata.get("customer_account_number")
            existing.customer_mobile_number  = sdata.get("customer_mobile_number")
            existing.customer_pan            = sdata.get("customer_pan")
            existing.customer_aadhaar_last4  = sdata.get("customer_aadhaar_last4")
            existing.customer_account_type   = sdata.get("customer_account_type")
            existing.customer_kyc_status     = sdata.get("customer_kyc_status")
            existing.customer_balance        = sdata.get("customer_balance")
            existing.customer_dob            = sdata.get("customer_dob")
            skipped += 1
            print(f"  [UPD]  Demo session '{sdata['token_number']}' — PII fields refreshed")
            continue

        branch = branch_map.get(sdata["branch_code"])
        if not branch:
            print(f"  [WARN] Branch '{sdata['branch_code']}' not found for demo session {sdata['token_number']}")
            continue

        # Find any active staff at this branch
        staff = db.execute(
            select(StaffMember).where(
                StaffMember.branch_id == branch.id,
                StaffMember.is_active == True,
            )
        ).scalars().first()

        now = datetime.now(timezone.utc)
        session = Session(
            token_number=sdata["token_number"],
            branch_id=branch.id,
            staff_id=staff.id if staff else None,
            customer_language=sdata["customer_language"],
            customer_language_code=sdata["customer_language_code"],
            staff_language="Hindi",
            entry_method=sdata.get("entry_method", "qr_scan"),
            status=sdata.get("status", "active"),
            intent_detected=sdata.get("intent_detected"),
            sentiment_overall=sdata.get("sentiment_overall"),
            offline_mode=False,
            pii_detected=True,
            started_at=now,
            created_at=now,
            # Customer PII fields
            customer_name=sdata.get("customer_name"),
            customer_account_number=sdata.get("customer_account_number"),
            customer_mobile_number=sdata.get("customer_mobile_number"),
            customer_pan=sdata.get("customer_pan"),
            customer_aadhaar_last4=sdata.get("customer_aadhaar_last4"),
            customer_account_type=sdata.get("customer_account_type"),
            customer_kyc_status=sdata.get("customer_kyc_status"),
            customer_balance=sdata.get("customer_balance"),
            customer_dob=sdata.get("customer_dob"),
        )
        db.add(session)
        db.flush()
        inserted += 1
        print(f"  [OK]   Demo session '{session.token_number}' — {sdata['customer_name']} ({sdata['customer_language']})")

    print(f"  [OK]   Demo sessions: {inserted} inserted, {skipped} updated")


# MAIN

def main() -> None:
    print("\n" + "=" * 60)
    print("  VaaniBank AI — Database Seeder")
    print("  PSBs Hackathon 2026 | Team Vectora")
    print("=" * 60)

    with session_local() as db:
        try:
            print("\n► Seeding Branches (3)...")
            branch_map = seed_branches(db)

            print("\n► Seeding Staff Members (3)...")
            seed_staff(db, branch_map)

            print("\n► Seeding Process Steps (6 intents)...")
            seed_process_steps(db)

            db.commit()
            print("\n" + "=" * 60)
            print("  ✅  Seed completed successfully!")
            print("=" * 60)
            print("\n  Demo login credentials:")
            for s in STAFF_DATA:
                print(f"    {s['staff_id']} / {s['username']} / {s['password_plain']} ({s['role']})")
            print()

        except Exception as exc:
            db.rollback()
            print(f"\n  ❌  Seed FAILED: {exc}")
            sys.exit(1)


if __name__ == "__main__":
    main()