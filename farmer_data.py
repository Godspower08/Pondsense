"""
PondSense - Farmer & Pond Data Model (Supabase)
---------------------------------------------------
Real hosted Postgres via Supabase. Same interface as the earlier
SQLite version (add_farmer, add_pond, list_all_ponds, update_pond_tier,
log_alert) - orchestrator.py doesn't need to change at all.

Requires SUPABASE_URL and SUPABASE_KEY (service_role key, not anon)
set as environment variables. Never commit these - use a .env file
and add it to .gitignore.

Setup:
    pip install supabase python-dotenv
    Run supabase_schema.sql in the Supabase SQL Editor first.
"""

import os
import secrets
from dataclasses import dataclass
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

VALID_SPECIES = {"catfish", "hybrid_striped_bass"}


def get_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set (in .env or environment)."
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)


@dataclass
class Pond:
    pond_id: str
    farmer_id: str
    species: str
    depth_category: str
    construction_type: str
    last_tier: str | None = None
    lat: float | None = None
    lng: float | None = None
    accuracy_m: float | None = None
    location_method: str | None = None  # 'gps_confirmed' | 'maps_link' | 'manual_pin' | None
    location_token: str | None = None
    pond_width_m: float | None = None  # farmer-reported longest dimension, meters
    location_gap_notified: bool = False  # has a gap-notification email already gone out?


@dataclass
class Farmer:
    farmer_id: str
    phone_number: str
    email: str | None
    sms_opted_in: bool
    email_opted_in: bool
    ponds: list[Pond]


def add_farmer(farmer_id, phone_number, email=None, sms_opted_in=False, email_opted_in=True):
    client = get_client()
    client.table("farmers").upsert({
        "farmer_id": farmer_id,
        "phone_number": phone_number,
        "email": email,
        "sms_opted_in": sms_opted_in,
        "email_opted_in": email_opted_in,
    }).execute()


def add_pond(pond_id, farmer_id, species, depth_category, construction_type):
    """
    Registers a pond with NO location info yet - location is captured
    separately via the location-pin flow (location_routes.py), not at
    JOIN time. A location_token is generated on first registration so
    the JOIN confirmation reply can immediately include a working
    link - but if this pond_id already exists (e.g. someone re-sends
    JOIN), we reuse its existing token instead of generating a new
    one, so any link already emailed out doesn't silently die.
    """
    if species not in VALID_SPECIES:
        raise ValueError(f"species must be one of {VALID_SPECIES}, got '{species}'")
    client = get_client()

    existing = client.table("ponds").select("location_token").eq("pond_id", pond_id).execute()
    token = (
        existing.data[0]["location_token"]
        if existing.data and existing.data[0].get("location_token")
        else secrets.token_urlsafe(16)
    )

    client.table("ponds").upsert({
        "pond_id": pond_id,
        "farmer_id": farmer_id,
        "species": species,
        "depth_category": depth_category,
        "construction_type": construction_type,
        "location_token": token,
    }).execute()


def get_pond_by_token(token: str) -> Pond | None:
    """Looks up a pond by its location_token - used by location_routes.py."""
    client = get_client()
    resp = client.table("ponds").select("*").eq("location_token", token).execute()
    if not resp.data:
        return None
    p = resp.data[0]
    return _pond_from_row(p)


def update_pond_location(pond_id: str, lat: float, lng: float,
                          accuracy_m: float | None, method: str,
                          pond_width_m: float | None = None) -> None:
    """
    Saves a confirmed location for a pond. method is one of
    'gps_confirmed', 'maps_link', or 'manual_pin' - see location_routes.py.

    pond_width_m is the farmer's reported longest dimension across the
    pond, in meters - captured on the same page as the lat/lng pin.
    Optional/backward-compatible (defaults to None) so older callers
    that only pass lat/lng/accuracy/method still work, but
    location_routes.py's submit_location now always supplies it since
    the location page requires it before submitting.
    """
    if method not in ("gps_confirmed", "maps_link", "manual_pin"):
        raise ValueError(f"invalid location method: {method}")
    client = get_client()
    update_fields = {
        "lat": lat,
        "lng": lng,
        "accuracy_m": accuracy_m,
        "location_method": method,
        # A successful call here always resolves the location gap -
        # location_routes.py requires pond_width_m before it will even
        # accept the submission, so lat/lng arriving means width did
        # too. Clear the notified flag so a FUTURE gap (e.g. someone
        # manually nulls the location out again) can trigger a fresh
        # email instead of staying silently suppressed forever.
        "location_gap_notified": False,
    }
    if pond_width_m is not None:
        update_fields["pond_width_m"] = pond_width_m
    client.table("ponds").update(update_fields).eq("pond_id", pond_id).execute()


def mark_location_gap_notified(pond_id: str) -> None:
    """
    Sets location_gap_notified=True after orchestrator.py sends a
    "please pin your pond" email - prevents re-sending that same
    nudge every single cycle for a pond that hasn't fixed it yet.
    """
    client = get_client()
    client.table("ponds").update(
        {"location_gap_notified": True}
    ).eq("pond_id", pond_id).execute()


def _pond_from_row(p: dict) -> Pond:
    return Pond(
        pond_id=p["pond_id"], farmer_id=p["farmer_id"], species=p["species"],
        depth_category=p["depth_category"], construction_type=p["construction_type"],
        last_tier=p.get("last_tier"), lat=p.get("lat"), lng=p.get("lng"),
        accuracy_m=p.get("accuracy_m"), location_method=p.get("location_method"),
        location_token=p.get("location_token"), pond_width_m=p.get("pond_width_m"),
        location_gap_notified=p.get("location_gap_notified", False),
    )


def update_pond_tier(pond_id: str, tier: str) -> None:
    client = get_client()
    client.table("ponds").update({"last_tier": tier}).eq("pond_id", pond_id).execute()


def log_alert(pond_id: str, tier: str, degree_hours: float, sent_via: str) -> None:
    client = get_client()
    client.table("alerts").insert({
        "pond_id": pond_id,
        "tier": tier,
        "degree_hours": degree_hours,
        "sent_via": sent_via,
    }).execute()


def list_all_ponds() -> list[tuple[Farmer, Pond]]:
    """Flat list of (farmer, pond) pairs - what the orchestrator loops over."""
    client = get_client()
    ponds_resp = client.table("ponds").select("*, farmers(*)").execute()

    pairs = []
    for row in ponds_resp.data:
        f = row["farmers"]
        farmer = Farmer(
            farmer_id=f["farmer_id"], phone_number=f["phone_number"], email=f["email"],
            sms_opted_in=f["sms_opted_in"], email_opted_in=f["email_opted_in"], ponds=[],
        )
        pond = _pond_from_row(row)
        pairs.append((farmer, pond))
    return pairs


def get_farmer_by_email(email_address: str) -> tuple[Farmer, list[Pond]] | None:
    """
    Looks up a farmer by email and returns (farmer, their_ponds), or
    None if no match. Used by email_reply_handler.py so Gemini can
    reference real pond data instead of staying fully generic when
    replying to an inbound email.

    Case-insensitive match, since email addresses in headers can come
    through with different casing than what's stored.
    """
    client = get_client()
    farmer_resp = (
        client.table("farmers")
        .select("*")
        .ilike("email", email_address.strip())
        .execute()
    )

    if not farmer_resp.data:
        return None

    f = farmer_resp.data[0]
    farmer = Farmer(
        farmer_id=f["farmer_id"], phone_number=f["phone_number"], email=f["email"],
        sms_opted_in=f["sms_opted_in"], email_opted_in=f["email_opted_in"], ponds=[],
    )

    ponds_resp = client.table("ponds").select("*").eq("farmer_id", f["farmer_id"]).execute()
    ponds = [_pond_from_row(p) for p in ponds_resp.data]

    return farmer, ponds


def get_farmer_by_phone(phone_number: str) -> tuple[Farmer, list[Pond]] | None:
    """
    Looks up a farmer by phone and returns (farmer, their_ponds), or
    None if no match. Used by sms_webhook.py so Gemini can reference
    real pond data when replying to an inbound SMS.

    Exact match (not ilike like get_farmer_by_email) - Twilio's `From`
    comes through in strict E.164 format (e.g. "+16625550142"), so as
    long as phone_number is stored the same way in Supabase, an exact
    match is correct and avoids accidental partial matches.
    """
    client = get_client()
    farmer_resp = (
        client.table("farmers")
        .select("*")
        .eq("phone_number", phone_number.strip())
        .execute()
    )

    if not farmer_resp.data:
        return None

    f = farmer_resp.data[0]
    farmer = Farmer(
        farmer_id=f["farmer_id"], phone_number=f["phone_number"], email=f["email"],
        sms_opted_in=f["sms_opted_in"], email_opted_in=f["email_opted_in"], ponds=[],
    )

    ponds_resp = client.table("ponds").select("*").eq("farmer_id", f["farmer_id"]).execute()
    ponds = [_pond_from_row(p) for p in ponds_resp.data]

    return farmer, ponds


def get_ponds_for_farmer(farmer_id: str) -> list[Pond]:
    """
    Returns every pond belonging to a farmer_id. Used to auto-assign
    the next pond number (Pond 1, Pond 2, ...) on registration, so
    farmers never have to invent or type a unique pond ID themselves -
    they just describe the pond and PondSense assigns it.
    """
    client = get_client()
    ponds_resp = client.table("ponds").select("*").eq("farmer_id", farmer_id).execute()
    return [_pond_from_row(p) for p in ponds_resp.data]


def add_manual_reading(pond_id: str, reading_type: str, value: float) -> None:
    """
    Logs a farmer-submitted reading (the no-equipment SMS path) to
    Supabase. Requires a manual_readings table - see accompanying SQL.
    """
    client = get_client()
    client.table("manual_readings").insert({
        "pond_id": pond_id,
        "reading_type": reading_type,
        "value": value,
    }).execute()


def seed_demo_farmer() -> None:
    add_farmer("farmer_001", "+16625550142", "demo.farmer@example.com", sms_opted_in=True)
    add_pond("pond_A", "farmer_001", "catfish", "shallow", "earthen_unlined")
    add_pond("pond_B", "farmer_001", "hybrid_striped_bass", "deep", "concrete")


if __name__ == "__main__":
    seed_demo_farmer()
    for farmer, pond in list_all_ponds():
        print(f"{farmer.farmer_id} -> {pond.pond_id} ({pond.species}, {pond.depth_category})")
