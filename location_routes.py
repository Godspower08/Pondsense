"""
PondSense - location pinning routes.

INTEGRATION:
  Register this blueprint on whatever Flask `app` your webhook server
  already runs (the same app.py/sms_webhook.py that ngrok tunnels to -
  no second tunnel needed):

      from location_routes import location_bp
      app.register_blueprint(location_bp)

  Also set LOCATION_BASE_URL in .env to your ngrok URL (matches what
  email_reply_handler.py reads to build the link it emails out).

Depends on farmer_data.py's get_pond_by_token() and
update_pond_location() - both already added there.
"""

import re
from pathlib import Path

import requests
from flask import Blueprint, request, jsonify, abort

from farmer_data import get_pond_by_token, update_pond_location

location_bp = Blueprint("location", __name__)

# encoding="utf-8" is required here, not optional - Path.read_text()
# without it falls back to locale.getpreferredencoding(), which on
# Windows is cp1252, not UTF-8. That silently mangles any non-ASCII
# character in location.html (em dashes, curly quotes, the ± symbol)
# into garbage like "â€"" - this was happening BEFORE the page ever
# reached the browser, so the <meta charset="UTF-8"> tag in the HTML
# itself couldn't fix it; the string was already corrupted in memory.
_PAGE_HTML = Path(__file__).parent.joinpath("location.html").read_text(encoding="utf-8")

# Matches the common coordinate patterns Google Maps URLs carry, most
# precise first: a URL can contain BOTH a !3d/!4d pin (exact) and an
# @lat,lng viewport center (approximate, can be off by the width of
# the visible map) - check the exact one first or precision silently
# regresses.
#   ...!3d37.4219999!4d-122.0862462...        (place-detail pin - exact)
#   ...?q=37.4219999,-122.0862462             (share/search URLs)
#   .../@37.4219999,-122.0862462,17z...       (viewport center - approximate)
_COORD_PATTERNS = [
    re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"),
    re.compile(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)"),
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)"),
]


def _extract_coords(url: str):
    for pattern in _COORD_PATTERNS:
        match = pattern.search(url)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None


@location_bp.route("/location/<token>", methods=["GET"])
def location_page(token):
    pond = get_pond_by_token(token)
    if pond is None:
        abort(404)
    return _PAGE_HTML


@location_bp.route("/location/<token>/parse-link", methods=["POST"])
def parse_maps_link(token):
    """
    Tier 2: farmer pastes a Google Maps share link. Shortened links
    (maps.app.goo.gl, goo.gl/maps) don't carry coordinates in the URL
    itself - they only appear after following the redirect - so we
    resolve the URL server-side (browsers can't do this cross-origin)
    before searching for coordinates.
    """
    pond = get_pond_by_token(token)
    if pond is None:
        abort(404)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400

    # Try the pasted URL as-is first (covers full, unshortened links
    # without an extra network round trip).
    coords = _extract_coords(url)

    if coords is None:
        # Follow redirects to resolve shortened links, then search the
        # final URL. GET (not HEAD) because Maps' redirect chain
        # sometimes depends on it; short timeout since this blocks the
        # farmer's browser waiting on a response.
        try:
            resp = requests.get(
                url,
                allow_redirects=True,
                timeout=8,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                },
            )
            coords = _extract_coords(resp.url)
        except requests.RequestException:
            coords = None

    if coords is None:
        return jsonify({"error": "could not extract coordinates from that link"}), 422

    lat, lng = coords
    return jsonify({"lat": lat, "lng": lng})


@location_bp.route("/location/<token>", methods=["POST"])
def submit_location(token):
    pond = get_pond_by_token(token)
    if pond is None:
        abort(404)

    data = request.get_json(silent=True) or {}
    lat = data.get("lat")
    lng = data.get("lng")
    accuracy_m = data.get("accuracy_m")
    method = data.get("method")
    pond_width_m = data.get("pond_width_m")

    if lat is None or lng is None:
        return jsonify({"error": "lat/lng required"}), 400
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lng must be numbers"}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return jsonify({"error": "lat must be -90..90 and lng must be -180..180"}), 400
    if method not in ("gps_confirmed", "maps_link", "manual_pin"):
        return jsonify({"error": "invalid method"}), 400
    if pond_width_m is None:
        return jsonify({"error": "pond_width_m required"}), 400
    try:
        pond_width_m = float(pond_width_m)
    except (TypeError, ValueError):
        return jsonify({"error": "pond_width_m must be a number"}), 400
    if pond_width_m <= 0:
        return jsonify({"error": "pond_width_m must be positive"}), 400

    update_pond_location(
        pond_id=pond.pond_id,
        lat=lat,
        lng=lng,
        accuracy_m=float(accuracy_m) if accuracy_m is not None else None,
        method=method,
        pond_width_m=pond_width_m,
    )

    return jsonify({"status": "ok"})
