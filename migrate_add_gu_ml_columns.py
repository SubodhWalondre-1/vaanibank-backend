"""
VaaniBank AI — Migration: Add Gujarati & Malayalam columns to process_steps
PSBs Hackathon 2026 | Team Vectora

Run once:
    cd backend
    python migrate_add_gu_ml_columns.py

Adds step_text_gujarati and step_text_malayalam columns to process_steps table.
Also re-seeds the new translations into existing rows.
Safe to run multiple times (idempotent).
"""

import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config import settings

_sync_url = (
    settings.DATABASE_URL
    .replace("postgresql+asyncpg://", "postgresql://")
    .replace("postgresql+psycopg2://", "postgresql://")
    .replace("ssl=require", "sslmode=require")
)

engine = create_engine(_sync_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


def add_columns():
    """Add gujarati and malayalam columns if they don't exist."""
    with engine.connect() as conn:
        # Check and add step_text_gujarati
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='process_steps' AND column_name='step_text_gujarati'
        """))
        if not result.fetchone():
            conn.execute(text("ALTER TABLE process_steps ADD COLUMN step_text_gujarati TEXT"))
            print("  [OK] Added column: step_text_gujarati")
        else:
            print("  [SKIP] Column step_text_gujarati already exists")

        # Check and add step_text_malayalam
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='process_steps' AND column_name='step_text_malayalam'
        """))
        if not result.fetchone():
            conn.execute(text("ALTER TABLE process_steps ADD COLUMN step_text_malayalam TEXT"))
            print("  [OK] Added column: step_text_malayalam")
        else:
            print("  [SKIP] Column step_text_malayalam already exists")

        conn.commit()


# Translations for all existing steps
TRANSLATIONS = {
    ("account_opening", 1): {
        "gu": "ખાતું ખોલવા માટે આધાર કાર્ડ, પૅન કાર્ડ અને પાસપૉર્ટ સાઈઝ ફોટો જરૂરી છે તે ગ્રાહકને જણાવો.",
        "ml": "അക്കൗണ്ട് തുറക്കാൻ ആധാർ കാർഡ്, പാൻ കാർഡ്, പാസ്‌പോർട്ട് സൈസ് ഫോട്ടോ ആവശ്യമാണെന്ന് ഉപഭോക്താവിനെ അറിയിക്കുക.",
    },
    ("account_opening", 2): {
        "gu": "ગ્રાહક દ્વારા સમર્પિત દસ્તાવેજો ચકાસો અને ખાતરી કરો કે બધા દસ્તાવેજો સ્પષ્ટ અને માન્ય છે.",
        "ml": "ഉപഭോക്താവ് സമർപ്പിച്ച രേഖകൾ പരിശോധിച്ച് എല്ലാം വ്യക്തവും സാധുവുമാണെന്ന് ഉറപ്പാക്കുക.",
    },
    ("account_opening", 3): {
        "gu": "ગ્રાહક KYC પૂર્ણ કરો — સિસ્ટમમાં આધાર નંબર નોંધો અને બાયોમેટ્રિક અથવા OTP ચકાસણી કરો.",
        "ml": "ഉപഭോക്തൃ KYC പൂർത്തിയാക്കുക — സിസ്റ്റത്തിൽ ആധാർ നമ്പർ നൽകി ബയോമെട്രിക് അല്ലെങ്കിൽ OTP പരിശോധന ചെയ്യുക.",
    },
    ("account_opening", 4): {
        "gu": "ખાતું ખોલવાનું ફૉર્મ સ્કૅન કરી સિસ્ટમમાં અપલોડ કરો અને ગ્રાહકને પાસબુક અને વેલ્કમ કિટ આપો.",
        "ml": "അക്കൗണ്ട് ഓപ്പണിങ് ഫോം സ്കാൻ ചെയ്ത് സിസ്റ്റത്തിൽ അപ്‌ലോഡ് ചെയ്ത് ഉപഭോക്താവിന് പാസ്‌ബുക്കും വെൽക്കം കിറ്റും നൽകുക.",
    },
    ("loan_enquiry", 1): {
        "gu": "ગ્રાહક કયા પ્રકારની લોન ઇચ્છે છે — હોમ લોન, પર્સનલ લોન, એજ્યુકેશન લોન અથવા બિઝનેસ લોન — તે પૂછો.",
        "ml": "ഉപഭോക്താവിന് ഏത് തരം വായ്പ വേണം — ഭവന വായ്പ, വ്യക്തിഗത വായ്പ, വിദ്യാഭ്യാസ വായ്പ അല്ലെങ്കിൽ ബിസിനസ് വായ്പ — എന്ന് ചോദിക്കുക.",
    },
    ("loan_enquiry", 2): {
        "gu": "ગ્રાહકની પાત્રતા ચકાસો — આવક, ક્રેડિટ સ્કોર અને હાલની લોનની માહિતી સિસ્ટમમાં જુઓ.",
        "ml": "ഉപഭോക്താവിന്റെ യോഗ്യത പരിശോധിക്കുക — വരുമാനം, ക്രെഡിറ്റ് സ്കോർ, നിലവിലുള്ള വായ്പകൾ സിസ്റ്റത്തിൽ നോക്കുക.",
    },
    ("loan_enquiry", 3): {
        "gu": "ગ્રાહકને જરૂરી દસ્તાવેજોની યાદી આપો — આવકનો પુરાવો, સરનામાનો પુરાવો, છેલ્લા 6 મહિનાનું બૅંક સ્ટેટ્મેન્ટ.",
        "ml": "ഉപഭോക്താവിന് ആവശ്യമായ രേഖകളുടെ പട്ടിക നൽകുക — വരുമാന തെളിവ്, മേൽവിലാസ തെളിവ്, കഴിഞ്ഞ 6 മാസത്തെ ബാങ്ക് സ്റ്റേറ്റ്മെന്റ്.",
    },
    ("loan_enquiry", 4): {
        "gu": "ગ્રાહકને લોન અરજી પ્રક્રિયા સમજાવો — ઑનલાઇન અરજી લિંક આપો અથવા શાખામાં ફૉર્મ ભરવામાં મદદ કરો.",
        "ml": "ഉപഭോക്താവിന് വായ്പ അപേക്ഷ പ്രക്രിയ വിശദീകരിക്കുക — ഓൺലൈൻ അപേക്ഷ ലിങ്ക് നൽകുക അല്ലെങ്കിൽ ശാഖയിൽ ഫോം പൂരിപ്പിക്കാൻ സഹായിക്കുക.",
    },
    ("kyc_update", 1): {
        "gu": "ગ્રાહક કઈ માહિતી અપડેટ કરવા ઇચ્છે છે — સરનામું, મોબાઇલ નંબર, ઇમેઇલ અથવા અન્ય — તે પૂછો.",
        "ml": "ഉപഭോക്താവ് ഏത് വിവരം അപ്‌ഡേറ്റ് ചെയ്യണം — വിലാസം, മൊബൈൽ നമ്പർ, ഇ-മെയിൽ അല്ലെങ്കിൽ മറ്റ് — എന്ന് ചോദിക്കുക.",
    },
    ("kyc_update", 2): {
        "gu": "અપડેટ માટે આધાર રેખો લો — નવા સરનામા માટે યુટિલિટી બિલ અથવા સરકારી પત્ર, મોબાઇલ અપડેટ માટે OTP ચકાસણી.",
        "ml": "അപ്‌ഡേറ്റിനായി ആധാര രേഖ വാങ്ങുക — പുതിയ വിലാസത്തിന് ഉപയോഗ ബിൽ അല്ലെങ്കിൽ സർക്കാർ കത്ത്, മൊബൈൽ അപ്‌ഡേറ്റിന് OTP പരിശോധന.",
    },
    ("kyc_update", 3): {
        "gu": "સિસ્ટમમાં KYC અપડેટ કરો, ગ્રાહકને પુષ્ટિ આપો અને જણાવો કે ફેરફાર 24 કલાકમાં અમલી થશે.",
        "ml": "സിസ്റ്റത്തിൽ KYC അപ്‌ഡേറ്റ് ചെയ്ത് ഉപഭോക്താവിന് സ്ഥിരീകരണം നൽകി മാറ്റം 24 മണിക്കൂറിൽ നടപ്പാകുമെന്ന് അറിയിക്കുക.",
    },
    ("balance_enquiry", 1): {
        "gu": "ગ્રાહકની ઓળખ ચકાસો — આધાર નંબરના છેલ્લા 4 અંક અથવા નોંધાયેલ મોબાઇલ પર OTP દ્વારા.",
        "ml": "ഉപഭോക്താവിന്റെ ഐഡന്റിറ്റി പരിശോധിക്കുക — ആധാർ നമ്പറിന്റെ അവസാന 4 അക്കങ്ങൾ അല്ലെങ്കിൽ രജിസ്റ്റേർഡ് മൊബൈലിൽ OTP വഴി.",
    },
    ("balance_enquiry", 2): {
        "gu": "સિસ્ટમ પરથી બૅલૅન્સ જુઓ અને ગ્રાહકને જણાવો. મિની-સ્ટેટ્મેન્ટ જરૂરી હોય તો પ્રિન્ટ કરો અથવા SMS મોકલો.",
        "ml": "സിസ്റ്റത്തിൽ നിന്ന് ബാലൻസ് നോക്കി ഉപഭോക്താവിനെ അറിയിക്കുക. മിനി-സ്റ്റേറ്റ്മെന്റ് ആവശ്യമെങ്കിൽ പ്രിന്റ് ചെയ്യുക അല്ലെങ്കിൽ SMS അയയ്ക്കുക.",
    },
    ("card_services", 1): {
        "gu": "ગ્રાહક કઈ કાર્ડ સેવા ઇચ્છે છે — નવું કાર્ડ, કાર્ડ બ્લૉક, PIN બદલવો અથવા લિમિટ વધારવી — તે પૂછો.",
        "ml": "ഉപഭോക്താവ് ഏത് കാർഡ് സേവനം ആഗ്രഹിക്കുന്നു — പുതിയ കാർഡ്, കാർഡ് ബ്ലോക്ക്, PIN മാറ്റൽ അല്ലെങ്കിൽ പരിധി വർദ്ധന — എന്ന് ചോദിക്കുക.",
    },
    ("card_services", 2): {
        "gu": "ગ્રાહકની ઓળખ ચકાસો અને કાર્ડ વિગત સિસ્ટમમાં તપાસો.",
        "ml": "ഉപഭോക്താവിന്റെ ഐഡന്റിറ്റി സ്ഥിരീകരിച്ച് കാർഡ് വിവരങ്ങൾ സിസ്റ്റത്തിൽ പരിശോധിക്കുക.",
    },
    ("card_services", 3): {
        "gu": "વિનંતી કરેલ કાર્ડ સેવા પ્રક્રિયા કરો અને ગ્રાહકને પુષ્ટિ આપો કે કાર્ય પૂર્ણ થયું.",
        "ml": "അഭ്യർത്ഥിച്ച കാർഡ് സേവനം പ്രോസസ്സ് ചെയ്ത് ഉപഭോക്താവിന് ജോലി പൂർത്തിയായി എന്ന് ഉറപ്പ് നൽകുക.",
    },
    ("fixed_deposit", 1): {
        "gu": "ગ્રાહક પાસેથી FD ની રકમ, મુદ્દત અને નૉમિની વિગત પૂછો.",
        "ml": "ഉപഭോക്താവിൽ നിന്ന് FD തുക, കാലാവധി, നോമിനി വിവരങ്ങൾ ചോദിക്കുക.",
    },
    ("fixed_deposit", 2): {
        "gu": "વર્તમાન વ્યાજ દર જણાવો અને FD કૅલ્ક્યુલેટર પ્રથી મૅચ્યૉરિટી રકમ દેખાડો.",
        "ml": "നിലവിലെ പലിശ നിരക്ക് പറഞ്ഞ് FD കാൽക്കുലേറ്ററിൽ മെച്യൂരിറ്റി തുക കാണിക്കുക.",
    },
    ("fixed_deposit", 3): {
        "gu": "FD બુક કરો, રસીદ પ્રિન્ટ કરો અને ગ્રાહકને FD સર્ટિફિકેટ આપો.",
        "ml": "FD ബുക്ക് ചെയ്ത്, രസീദ് പ്രിന്റ് ചെയ്ത് ഉപഭോക്താവിന് FD സർട്ടിഫിക്കറ്റ് നൽകുക.",
    },
}


def update_translations():
    """Update existing rows with Gujarati and Malayalam translations."""
    with engine.connect() as conn:
        updated = 0
        for (intent, step_num), langs in TRANSLATIONS.items():
            conn.execute(
                text("""
                    UPDATE process_steps
                    SET step_text_gujarati = :gu,
                        step_text_malayalam = :ml
                    WHERE intent_type = :intent AND step_number = :step
                """),
                {
                    "gu": langs["gu"],
                    "ml": langs["ml"],
                    "intent": intent,
                    "step": step_num,
                }
            )
            updated += 1
        conn.commit()
        print(f"  [OK] Updated translations for {updated} steps")


def main():
    print("\n" + "=" * 60)
    print("  VaaniBank AI — Migration: Add gu/ml columns")
    print("=" * 60)

    print("\n► Step 1: Adding columns to process_steps...")
    add_columns()

    print("\n► Step 2: Populating Gujarati & Malayalam translations...")
    update_translations()

    print("\n" + "=" * 60)
    print("  ✅  Migration completed!")
    print("  Run this once. Safe to re-run (idempotent).")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n  ❌  Migration FAILED: {e}")
        sys.exit(1)
