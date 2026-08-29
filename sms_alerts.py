"""
PondSense - SMS Alerts
------------------------
Real-time act-now channel. DRY_RUN prints instead of sending - flip
to False once you're ready to actually send via Twilio. Interface
(send_sms) stays the same either way so orchestrator.py never needs
to change.
 
Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER in
.env - never hardcode these here. Get SID/Token from the Twilio
console dashboard; FROM_NUMBER is the number Twilio assigned you
(e.g. "+17372212163").
 
pip install twilio python-dotenv --break-system-packages
"""
 
import os
from dotenv import load_dotenv
 
from risk_engine import RiskTier, RiskAssessment
 
load_dotenv()
 
# Flip to False only once TWILIO_ACCOUNT_SID / AUTH_TOKEN / FROM_NUMBER
# are confirmed set and you're ready to actually send real SMS.
DRY_RUN = False
 
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
 
TIER_EMOJI = {
    RiskTier.SAFE:   "\U0001F7E2",
    RiskTier.WATCH:  "\U0001F7E1",
    RiskTier.ALERT:  "\U0001F7E0",
    RiskTier.DANGER: "\U0001F534",
}
 
TIER_MESSAGE = {
    RiskTier.SAFE:   "Conditions normal near your pond. No action needed.",
    RiskTier.WATCH:  "Heat's starting to build near your pond. Keep an eye on things.",
    RiskTier.ALERT:  "Heat's been building. Fish may be oxygen-stressed. Run aeration now if you have it. Hold off feeding until evening.",
    RiskTier.DANGER: "Critical heat risk. Run aeration immediately. Do not feed. Check fish for distress.",
}
 
 
def _celsius_to_fahrenheit(temp_c: float) -> float:
    return round((temp_c * 9 / 5) + 32, 1)
 
 
def build_sms_text(assessment: RiskAssessment, pond_id: str, species: str | None = None) -> str:
    """
    pond_id and species are passed in explicitly by the caller
    (orchestrator.py, via pond.pond_id / pond.species) rather than
    read off RiskAssessment - RiskAssessment doesn't carry pond or
    farmer identity, only the physics/tier result. Keeping that
    separation here instead of inventing fields on the dataclass.
    """
    emoji = TIER_EMOJI[assessment.tier]
    tier_label = assessment.tier.value.upper()
    body = TIER_MESSAGE[assessment.tier]
    temp_f = _celsius_to_fahrenheit(assessment.current_temp_c)
 
    species_bit = f" ({species})" if species else ""
    header = f"{emoji} {tier_label} - PondSense: {pond_id}{species_bit}"
    temp_line = f"Current: {temp_f}\u00b0F"
 
    return f"{header}\n{body}\n{temp_line}\nReply INFO for details."
 
 
def send_sms(to_phone: str, assessment: RiskAssessment, pond_id: str, species: str | None = None) -> bool:
    text = build_sms_text(assessment, pond_id, species)
 
    if DRY_RUN:
        print(f"\n[DRY RUN SMS] To: {to_phone}")
        print(text)
        return True
 
    missing = [
        name
        for name, val in [
            ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
            ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
            ("TWILIO_FROM_NUMBER", TWILIO_FROM_NUMBER),
        ]
        if not val
    ]
    if missing:
        print(f"[SMS SEND SKIPPED] Missing env var(s): {', '.join(missing)}")
        return False
 
    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException
 
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            body=text, from_=TWILIO_FROM_NUMBER, to=to_phone
        )
        print(f"[SMS SENT] {to_phone} - sid={message.sid}")
        return True
    except TwilioRestException as e:
        print(f"[SMS SEND FAILED] {to_phone}: {e}")
        return False
    except Exception as e:
        print(f"[SMS SEND FAILED] {to_phone}: unexpected error - {e}")
        return False
 
 
# ---------------------------------------------------------------------------
# Manual smoke test - run this file directly to send yourself a real SMS
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    print("Testing sms_alerts.py ...")
    print(f"DRY_RUN: {DRY_RUN}")
    print(f"TWILIO_ACCOUNT_SID set: {bool(TWILIO_ACCOUNT_SID)}")
    print(f"TWILIO_AUTH_TOKEN set: {bool(TWILIO_AUTH_TOKEN)}")
    print(f"TWILIO_FROM_NUMBER set: {bool(TWILIO_FROM_NUMBER)}")
 
    fake_assessment = RiskAssessment(
        tier=RiskTier.DANGER,
        degree_hours=16.2,
        current_temp_c=34.5,
        hours_above_watch=5,
        depth_category="shallow",
        construction_type="earthen_unlined",
        has_cover=False,
    )
 
    test_recipient = os.environ.get("TEST_TO_NUMBER")
    if not test_recipient:
        print(
            "\nSet TEST_TO_NUMBER in .env to your own phone number "
            "(e.g. +234...) to run a real send test."
        )
    else:
        success = send_sms(test_recipient, fake_assessment, "pond_TEST", "catfish")
        print(f"\nResult: {'SUCCESS' if success else 'FAILED'}")
 