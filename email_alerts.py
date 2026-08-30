"""
PondSense - Email Alerts
--------------------------
Email is the formal-record channel: same risk information as SMS, but
written for a documentation trail (institutional buyers - co-ops,
processors, insurers - want a paper trail, not just a text ping).
 
Two-part body, deliberately kept separate:
  1. A short formal paragraph, WRITTEN BY GEMINI - framing/tone only.
  2. A deterministic data block, built directly from RiskAssessment -
     tier, degree-hours, temp, timestamp. Gemini never sees or invents
     these numbers; it only gets them as input to write ABOUT, and the
     block below is assembled in Python regardless of what Gemini
     returns. This matters because a hallucinated number in a "formal
     record" email is worse than no email at all.
 
Requires GEMINI_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD in .env.
GMAIL_APP_PASSWORD must be a Gmail App Password (Google Account ->
Security -> 2-Step Verification -> App Passwords), NOT your normal
Gmail login password - Gmail blocks plain-password SMTP login.
"""
 
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
 
import requests  # pip install requests --break-system-packages
from dotenv import load_dotenv
 
from risk_engine import RiskTier, RiskAssessment
from email_reply_handler import get_location_base_url
 
load_dotenv()
 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
 
# gemini-2.5-flash was deprecated for new API keys as of testing on
# Aug 10 2026 ("no longer available to new users"). Using the
# "-latest" alias instead so this doesn't silently break again if
# Google deprecates the next pinned version mid-hackathon.
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
 
# Switched from port 587/STARTTLS to port 465/implicit SSL - this
# network blocks outbound 587 (WinError 10060 timeouts) while 465 is
# confirmed clean (see email_reply_handler.py fix, same root cause).
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
 
TIER_SUBJECT = {
    RiskTier.SAFE:   "PondSense: Conditions normal",
    RiskTier.WATCH:  "PondSense Watch: Heat building near your pond",
    RiskTier.ALERT:  "PondSense ALERT: Elevated heat risk",
    RiskTier.DANGER: "PondSense DANGER: Critical heat risk - act now",
}
 
TIER_ACTION = {
    RiskTier.SAFE:   "No action needed. We'll keep monitoring.",
    RiskTier.WATCH:  "Keep an eye on your pond. Consider aeration if you have it available.",
    RiskTier.ALERT:  "Run aeration now if available. Hold off feeding until temperatures ease.",
    RiskTier.DANGER: "Run aeration immediately. Do not feed. Check for signs of fish distress.",
}
 
 
# ---------------------------------------------------------------------------
# Gemini: formal-tone paragraph only. Never the source of truth for numbers.
# ---------------------------------------------------------------------------
 
def generate_formal_paragraph(assessment: RiskAssessment, pond_id: str) -> str:
    """
    Asks Gemini for a short, formal-record-style paragraph framing the
    situation. Falls back to a plain hardcoded sentence if the API
    call fails for any reason - the email must still send even if
    Gemini is down, since the deterministic data block below is the
    part that actually matters for a paper trail.
    """
    fallback = (
        f"This is an automated PondSense record for pond {pond_id}, "
        f"currently at {assessment.tier.value.upper()} risk level. "
        f"Details are recorded below."
    )
 
    if not GEMINI_API_KEY:
        return fallback
 
    prompt = (
        "Write ONE short, formal paragraph (2-3 sentences max) for an "
        "automated aquaculture pond-monitoring alert email. It should "
        "read like a professional institutional record, not a casual "
        "text message. Do not invent or restate specific numbers - "
        "those appear separately below your paragraph. Just frame the "
        "situation and its severity in plain, formal language.\n\n"
        f"Pond ID: {pond_id}\n"
        f"Risk tier: {assessment.tier.value.upper()}\n"
        f"Species context: pond depth {assessment.depth_category}, "
        f"construction {assessment.construction_type}."
    )
 
    try:
        response = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except Exception as e:
        print(f"[GEMINI FAILED, using fallback paragraph] {e}")
        return fallback
 
 
# ---------------------------------------------------------------------------
# Deterministic data block - always built straight from RiskAssessment
# ---------------------------------------------------------------------------
 
def build_data_block(assessment: RiskAssessment, pond_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"Pond: {pond_id}\n"
        f"Risk tier: {assessment.tier.value.upper()}\n"
        f"Current temperature: {assessment.current_temp_c}C\n"
        f"Accumulated degree-hours (6hr window): {assessment.degree_hours}\n"
        f"Hours above watch threshold: {assessment.hours_above_watch}\n"
        f"Pond depth: {assessment.depth_category}\n"
        f"Construction type: {assessment.construction_type}\n"
        f"Recorded: {timestamp}\n\n"
        f"Recommended action: {TIER_ACTION[assessment.tier]}"
    )
 
 
def build_email_body(assessment: RiskAssessment, pond_id: str) -> str:
    paragraph = generate_formal_paragraph(assessment, pond_id)
    data_block = build_data_block(assessment, pond_id)
    return (
        f"{paragraph}\n\n"
        f"{'-' * 40}\n\n"
        f"{data_block}\n\n"
        f"{'-' * 40}\n\n"
        f"This is an automated record from PondSense. Reply to this email "
        f"with questions, or contact support to adjust your alert settings.\n\n"
        f"Note: please keep \"PondSense\" in the subject line when replying "
        f"so your message reaches our team correctly."
    )
 
 
# ---------------------------------------------------------------------------
# Location gap notification - deliberately deterministic, no Gemini.
# This is an operational instruction (a working link the farmer must
# click), not a formal risk record - exact, unambiguous wording matters
# more than a personalized tone here, and there's nothing for an AI
# paragraph to add. Kept as its own function/subject rather than
# folded into TIER_SUBJECT/build_email_body, since it isn't a risk-tier
# alert at all - it's telling the farmer we CAN'T assess risk yet.
# ---------------------------------------------------------------------------

LOCATION_GAP_SUBJECT = "PondSense: We need your pond's location to send alerts"


def build_location_gap_body(pond_id: str, location_link: str) -> str:
    return (
        f"We can't send accurate heat alerts for {pond_id} yet - we're "
        f"missing its exact location.\n\n"
        f"To fix this, open the link below while standing at the pond "
        f"(takes under a minute):\n{location_link}\n\n"
        f"This one link handles everything we need: your GPS position "
        f"(or a Google Maps link, or a manual pin if GPS isn't "
        f"available), plus roughly how wide the pond is - that's what "
        f"lets us pull the right temperature data for your specific "
        f"pond instead of a generic estimate.\n\n"
        f"Until this is done, {pond_id} won't receive heat-risk alerts.\n\n"
        f"- PondSense\n\n"
        f"Note: please keep \"PondSense\" in the subject line when replying "
        f"so your message reaches our team correctly."
    )


def send_location_gap_email(to_email: str, pond_id: str, location_token: str) -> bool:
    """
    Sends the "please pin your pond" nudge. Called by orchestrator.py
    when a pond is missing lat/lng or pond_width_m - at most once per
    unresolved gap, gated by Pond.location_gap_notified /
    farmer_data.mark_location_gap_notified() so this doesn't re-fire
    every cycle.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print(
            "[EMAIL SEND SKIPPED] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set "
            "in .env - check both are present."
        )
        return False

    location_link = f"{get_location_base_url()}/location/{location_token}"
    body = build_location_gap_body(pond_id, location_link)

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = LOCATION_GAP_SUBJECT
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        print(f"[LOCATION GAP EMAIL SENT] {to_email} - {pond_id}")
        return True
    except Exception as e:
        print(f"[LOCATION GAP EMAIL FAILED] {to_email}: {e}")
        return False
 
 
# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
 
def send_email(to_email: str, assessment: RiskAssessment, pond_id: str) -> bool:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print(
            "[EMAIL SEND SKIPPED] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set "
            "in .env - check both are present."
        )
        return False
 
    subject = TIER_SUBJECT[assessment.tier]
    body = build_email_body(assessment, pond_id)
 
    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
 
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        print(f"[EMAIL SENT] {to_email} - {subject}")
        return True
    except Exception as e:
        print(f"[EMAIL SEND FAILED] {to_email}: {e}")
        return False
 
 
# ---------------------------------------------------------------------------
# Manual smoke test - run this file directly to test Gemini + Gmail live
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    print("Testing email_alerts.py ...")
    print(f"GEMINI_API_KEY set: {bool(GEMINI_API_KEY)}")
    print(f"GMAIL_ADDRESS set: {bool(GMAIL_ADDRESS)}")
    print(f"GMAIL_APP_PASSWORD set: {bool(GMAIL_APP_PASSWORD)}")
 
    fake_assessment = RiskAssessment(
        tier=RiskTier.ALERT,
        degree_hours=9.5,
        current_temp_c=34.0,
        hours_above_watch=4,
        depth_category="shallow",
        construction_type="earthen_unlined",
        has_cover=False,
    )
 
    # Sends to yourself by default so you don't need a second inbox to test.
    test_recipient = GMAIL_ADDRESS
    success = send_email(test_recipient, fake_assessment, "pond_TEST")
    print(f"\nResult: {'SUCCESS' if success else 'FAILED'}")
 