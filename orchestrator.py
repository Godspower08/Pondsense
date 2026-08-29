"""
PondSense - Orchestrator
--------------------------
Ties the whole pipeline together: for every farmer's pond, fetch
readings, run the risk engine, decide whether an alert is warranted,
and send through whichever channels the farmer opted into.

"Decide" matters as its own step, not just a pass-through: we only
alert on a TIER CHANGE, not every run. Without this, a pond sitting
at ALERT for six straight hours would spam the farmer with six
identical texts instead of one.
"""
SMS_ENABLED = False  # Twilio blocked on current network - re-enable once resolved

from datetime import datetime, timedelta
import os

import requests
from dotenv import load_dotenv

from risk_engine import assess_pond, get_mock_readings, RiskTier, HourlyReading
from farmer_data import (
    list_all_ponds, update_pond_tier, log_alert, mark_location_gap_notified,
)
import api_adapter
import email_alerts
import sms_alerts

load_dotenv()

# Tiers that always re-notify even without a change, since silence at
# DANGER is worse than a repeat message. WATCH/ALERT/SAFE only notify
# on change; DANGER re-notifies every run as a safety margin.
ALWAYS_NOTIFY_TIERS = {RiskTier.DANGER}

MOCK_API_URL = "http://localhost:5001/temp"
READING_WINDOW_HOURS = 6

# Mock/live switch. Defaults to mock (0) - flip with
# `export PONDSENSE_LIVE=1` (or set it in .env) once you're ready to
# go live. Kept as an env var rather than a hardcoded flag so you can
# flip it per-environment (dev vs demo) without editing this file.
PONDSENSE_LIVE = os.environ.get("PONDSENSE_LIVE") == "1"


def has_location_gap(pond) -> bool:
    """
    True if this pond is missing anything api_adapter.py wants for an
    accurate reading: lat/lng OR pond_width_m. Used to decide whether
    to send the "please pin your pond" email - broader than
    is_fully_blocked() below, since a missing width still degrades
    quality even though it doesn't stop a live fetch outright.
    """
    return pond.lat is None or pond.lng is None or pond.pond_width_m is None


def is_fully_blocked(pond) -> bool:
    """
    True only if there's NOTHING to query at all - missing lat/lng.
    Missing pond_width_m alone does NOT block a live fetch:
    api_adapter.get_live_readings() already degrades gracefully to a
    conservative default box size when width is absent, so that case
    still produces a real (if less precisely-scoped) reading rather
    than nothing.
    """
    return pond.lat is None or pond.lng is None


def notify_location_gap(farmer, pond) -> None:
    """
    Sends the "please pin your pond" email at most once per unresolved
    gap - gated by pond.location_gap_notified, cleared automatically
    by farmer_data.update_pond_location() the moment the farmer
    actually submits a location (which requires width too, so a
    successful submission always resolves both at once). If the
    farmer has no email on file, there's no channel to reach them on -
    logs and moves on rather than raising.
    """
    if pond.location_gap_notified:
        return
    if not (farmer.email_opted_in and farmer.email):
        print(f"  [{pond.pond_id}] location gap unresolved but no email on file to notify")
        return
    sent = email_alerts.send_location_gap_email(
        farmer.email, pond.pond_id, pond.location_token
    )
    if sent:
        mark_location_gap_notified(pond.pond_id)


def fetch_readings(pond):
    """
    Returns a list[HourlyReading], or None if no reading could be
    obtained this cycle (see PONDSENSE_LIVE branch below) - callers
    MUST check for None before using the result.

    MOCK PATH (PONDSENSE_LIVE=0, the default): calls the local
    mock_temp_api.py for a current test reading. Falls back to the
    old hardcoded scenario if the mock API isn't running, so
    orchestrator.py doesn't crash if you forget to start it - just
    prints a warning so it's obvious readings aren't live. The mock
    API only returns one current reading, not real hourly history, so
    this builds a flat window of READING_WINDOW_HOURS HourlyReading
    entries all at that value - good enough to drive the risk engine
    for testing, not a stand-in for real historical data. Fabricating
    a plausible-looking reading is acceptable here because nothing in
    mock mode is actually deciding whether to alert a real farmer.

    LIVE PATH (PONDSENSE_LIVE=1): calls
    api_adapter.get_latest_available_readings(latitude=pond.lat,
    longitude=pond.lng, pond_width_m=pond.pond_width_m,
    hours=READING_WINDOW_HOURS) for real FortyGuard data - this walks
    back day-by-day past any FortyGuard coverage gap on "today"
    before giving up (see api_adapter.py), rather than calling
    get_live_readings() directly against a single possibly-gapped
    date.

    If the pond has no lat/lng yet (registered but never pinned via
    location.html), skips the live call entirely and returns None -
    there's nothing to query.

    If the live call fails (network blip, a Failed job, a stuck poll
    timing out - all real, already observed on this network), this
    logs it and returns None. It deliberately does NOT fall back to
    get_mock_readings() the way the mock path does: in live mode, a
    fabricated reading isn't a harmless placeholder, it's fake data
    that could drive a real alert (or a false all-clear) to an actual
    farmer. Returning None lets process_pond() skip this pond for the
    cycle instead - no notification sent, last known tier stands,
    rather than guessing.
    """
    if PONDSENSE_LIVE:
        if is_fully_blocked(pond):
            print(f"  [{pond.pond_id}] no location on file yet - skipping live fetch")
            return None
        try:
            # get_latest_available_readings() (not get_live_readings()
            # directly) - FortyGuard has confirmed per-day coverage
            # gaps (see api_adapter.py), so this walks back to the
            # most recent date that actually has full data instead of
            # failing outright whenever "today" happens to be gapped.
            return api_adapter.get_latest_available_readings(
                latitude=pond.lat,
                longitude=pond.lng,
                pond_width_m=pond.pond_width_m,
                hours=READING_WINDOW_HOURS,
            )
        except (RuntimeError, TimeoutError, requests.RequestException) as e:
            print(f"  [{pond.pond_id}] LIVE FETCH FAILED, skipping this cycle: {e}")
            return None

    try:
        resp = requests.get(MOCK_API_URL, timeout=5)
        resp.raise_for_status()
        temp_c = resp.json()["temp_c"]
    except Exception as e:
        print(f"  [MOCK API UNREACHABLE, using hardcoded scenario] {e}")
        return get_mock_readings(scenario="hot_afternoon")

    now = datetime.now()
    return [
        HourlyReading(timestamp=now - timedelta(hours=h), ambient_temp_c=temp_c)
        for h in range(READING_WINDOW_HOURS - 1, -1, -1)
    ]


def should_notify(pond, new_tier: RiskTier) -> bool:
    if new_tier in ALWAYS_NOTIFY_TIERS:
        return True
    return pond.last_tier != new_tier.value


def process_pond(farmer, pond) -> None:
    if has_location_gap(pond):
        # Fires regardless of PONDSENSE_LIVE - a missing location/width
        # is a real registration gap independent of which data source
        # is active, and mock mode's flat test data shouldn't mask it.
        notify_location_gap(farmer, pond)

    readings = fetch_readings(pond)
    if readings is None:
        print(f"  [{pond.pond_id}] no readings available this cycle - skipping (last tier stands)")
        return

    assessment = assess_pond(
        readings,
        depth_category=pond.depth_category,
        construction_type=pond.construction_type,
        species=pond.species,
        # dissolved_oxygen_mg_l intentionally omitted (stays None) -
        # there is no DO reading anywhere yet: not on Pond, not in
        # the schema, not collected on location.html or via any SMS/
        # email path. Without it, hsb_low_do_growth_penalty() still
        # returns 0.0 for every HSB pond even now that species= is
        # wired correctly - the penalty simply never activates until
        # a real DO input exists somewhere. Deferred by decision
        # given the Aug 30 deadline, not an oversight - revisit if
        # HSB DO tracking becomes a demo requirement.
    )

    print(
        f"\n{farmer.farmer_id} / {pond.pond_id}: "
        f"tier={assessment.tier.value} degree_hours={assessment.degree_hours}"
    )

    if not should_notify(pond, assessment.tier):
        print("  -> no tier change, skipping notification")
        return

    if farmer.sms_opted_in and SMS_ENABLED:
        # pond.pond_id and pond.species passed explicitly - RiskAssessment
        # doesn't carry pond/farmer identity, only the risk result, so the
        # caller (here) is responsible for supplying which pond this is.
        sms_alerts.send_sms(farmer.phone_number, assessment, pond.pond_id, pond.species)
        log_alert(pond.pond_id, assessment.tier.value, assessment.degree_hours, "sms")

    if farmer.email_opted_in and farmer.email:
        email_alerts.send_email(farmer.email, assessment, pond.pond_id)
        log_alert(pond.pond_id, assessment.tier.value, assessment.degree_hours, "email")

    update_pond_tier(pond.pond_id, assessment.tier.value)


def run_cycle() -> None:
    """One full pass over every farmer's every pond. Call this on a
    schedule (cron, or a loop with sleep) once wired to the real API.

    Each pond is wrapped in its own try/except - fetch_readings()
    already handles the EXPECTED live-mode failure (returns None,
    handled gracefully in process_pond()). This catches anything
    UNEXPECTED instead (a bug, a farmer_data lookup error, etc.) so
    one pond's problem can't silently stop every other farmer's check
    for the same cycle.
    """
    for farmer, pond in list_all_ponds():
        try:
            process_pond(farmer, pond)
        except Exception as e:
            print(f"  [{pond.pond_id}] UNEXPECTED ERROR, skipping this pond: {e}")


if __name__ == "__main__":
    print("=== Running one full cycle over all farmers/ponds in Supabase ===")
    run_cycle()
