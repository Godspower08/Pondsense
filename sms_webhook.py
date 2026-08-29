"""
PondSense - SMS Webhook (inbound Twilio -> Supabase + Gemini)
-----------------------------------------------------------------
Twilio POSTs here whenever a verified number texts your Twilio
number. Handles two paths:

  1. Structured command - a farmer reporting a manual reading with
     no equipment/API access:
         TEMP pond_1 31.5
         DO pond_1 4.2
     Parsed deterministically and written straight to Supabase, no
     Gemini involved - this path should never be flaky.

  2. Free text - anything else gets routed to Gemini, grounded in
     that farmer's real pond record (same pattern as
     email_reply_handler.py, just keyed by phone instead of email
     and capped for SMS length).

Requires ngrok (or similar tunnel) pointed at this Flask app, with
the resulting https URL pasted into:
  Twilio Console -> Phone Numbers -> your number -> Messaging ->
  "A MESSAGE COMES IN" -> webhook -> https://<ngrok-id>.ngrok.io/sms

Also requires get_farmer_by_phone() and add_manual_reading() added
to farmer_data.py, and a manual_readings table in Supabase - see the
snippet provided alongside this file.

Run:
  pip install flask twilio python-dotenv requests --break-system-packages
  python sms_webhook.py
  ngrok http 5000
"""

import os
import re
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

from farmer_data import get_farmer_by_phone, add_manual_reading, add_farmer, add_pond

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

app = Flask(__name__)

# TEMP pond_1 31.5   /   DO pond_1 4.2
COMMAND_RE = re.compile(r"^\s*(TEMP|DO)\s+(\S+)\s+([\d.]+)\s*$", re.IGNORECASE)

# JOIN pond_1 catfish shallow concrete 38773
# Species is restricted to exactly these two - matches VALID_SPECIES in
# farmer_data.py. Anything else (tilapia, "cat fish", typos) falls
# through and gets the format-help reply instead of a bad insert.
JOIN_RE = re.compile(
    r"^\s*JOIN\s+(\S+)\s+(catfish|hybrid_striped_bass)\s+(\S+)\s+(\S+)\s+(\S+)\s*$",
    re.IGNORECASE,
)


def _build_pond_context(phone: str) -> str:
    result = get_farmer_by_phone(phone)
    if result is None:
        return "No farmer record found for this phone number."

    farmer, ponds = result
    if not ponds:
        return f"Farmer record found ({farmer.farmer_id}), but no ponds on file."

    lines = [f"Farmer ID: {farmer.farmer_id}"]
    for p in ponds:
        tier = p.last_tier.upper() if p.last_tier else "no data yet"
        lines.append(
            f"  - {p.pond_id}: {p.species}, depth={p.depth_category}, "
            f"construction={p.construction_type}, last known tier={tier}"
        )
    return "\n".join(lines)


def generate_sms_reply(body: str, phone: str) -> str:
    fallback = "Thanks, got your message. We'll follow up shortly. - PondSense"

    if not GEMINI_API_KEY:
        return fallback

    pond_context = _build_pond_context(phone)

    prompt = (
        "You are replying by SMS on behalf of PondSense, an aquaculture "
        "pond heat-risk alert service. Keep the reply under 300 characters "
        "total - plain text, no markdown, SMS-appropriate. Use the farmer's "
        "real pond record below if relevant. If it says no record was "
        "found, or doesn't answer their question, say you'll follow up "
        "rather than guessing. Never invent tier, temperature, or pond "
        "data not shown below. Sign off '- PondSense'.\n\n"
        f"Farmer's message:\n{body.strip()[:500]}\n\n"
        f"Farmer's pond record:\n{pond_context}"
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
        print(f"[GEMINI FAILED, using fallback reply] {e}")
        return fallback


@app.route("/sms", methods=["POST"])
def sms_reply():
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    print(f"\n--- Inbound SMS from {from_number} ---\n{body}")

    resp = MessagingResponse()

    join_match = JOIN_RE.match(body)
    if join_match:
        pond_id, species, depth_category, construction_type, zip_code = join_match.groups()
        # Deterministic farmer_id from phone, so re-texting JOIN with the
        # same number updates the same farmer instead of creating a
        # duplicate (add_farmer/add_pond both upsert).
        farmer_id = f"farmer_{re.sub(r'[^0-9]', '', from_number)}"
        try:
            add_farmer(farmer_id, from_number, sms_opted_in=True)
            add_pond(
                pond_id, farmer_id,
                species.lower(), depth_category.lower(), construction_type.lower(), zip_code,
            )
            resp.message(
                f"Welcome to PondSense! Registered {pond_id} ({species.lower()}) "
                f"for zip {zip_code}. You'll get alerts here. - PondSense"
            )
            print(f"[JOIN] {farmer_id} -> {pond_id} ({species.lower()})")
        except Exception as e:
            print(f"[JOIN FAILED] {e}")
            resp.message(
                "Couldn't register that pond - check the format and try "
                "again. - PondSense"
            )
        return str(resp)

    if body.strip().upper().startswith("JOIN"):
        # Looked like an attempted JOIN but didn't match - most likely
        # bad/misspelled species. Tell them exactly what's valid instead
        # of silently falling through to the Gemini fallback.
        resp.message(
            "To register, text: JOIN <pond_id> <species> <depth> "
            "<construction> <zip>. Species must be catfish or "
            "hybrid_striped_bass. - PondSense"
        )
        return str(resp)

    match = COMMAND_RE.match(body)
    if match:
        kind, pond_id, value = match.groups()
        try:
            add_manual_reading(pond_id, kind.upper(), float(value))
            resp.message(
                f"Got it - logged {kind.upper()}={value} for {pond_id}. "
                f"Thanks for the update. - PondSense"
            )
            print(f"[READING LOGGED] {pond_id} {kind.upper()}={value}")
        except Exception as e:
            print(f"[READING LOG FAILED] {e}")
            resp.message(
                "Couldn't log that reading - check the pond ID and try "
                "again, e.g. TEMP pond_1 31.5. - PondSense"
            )
        return str(resp)

    reply_text = generate_sms_reply(body, from_number)
    resp.message(reply_text)
    return str(resp)


if __name__ == "__main__":
    print("SMS webhook running. Point ngrok at port 5000.")
    print(f"GEMINI_API_KEY set: {bool(GEMINI_API_KEY)}")
    app.run(port=5000)
