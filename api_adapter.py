"""
FortyGuard API Adapter
------------------------
This is the ONLY file that should need real changes once API access
arrives. Everything else (risk_engine.py) just calls
get_live_readings() and doesn't care where the data came from - mock
or real.

STATUS: real /v1/heatmap docs confirmed (not the guessed shape from
before), and FORTYGUARD_API_KEY is expected to be set in your
environment now that you have real access.

  CONFIRMED (from FortyGuard's own docs, incl. the env_params /
  heat_intelligence example payloads):
    - Every submit and status response is wrapped in an outer
      envelope: {"error", "status_code", "message", "data": {...}}.
      activity_id, status, and result all live under data, NOT at
      the top level.
    - The submit payload is NOT flat. start_date, filter_type,
      start_time, end_time all live under a nested date_time object.
      granularity is NOT an arbitrary int - only 60, 80, or 100
      (meters) are valid.
    - status values are "Processing" / "Completed" / "Failed"
      (capitalized), not "done". "Failed" is terminal.
    - There is no "hours" request parameter and never was. ONE
      heatmap job returns stats for ONE time window:
        filter_type 1 = single hour (auto end_time = start+1h)
        filter_type 2 = range of hours, same day
        filter_type 3 = single day
        filter_type 4 = range of days (<=1 month)
      A single filter_type=2/3/4 job gives you AGGREGATE stats for
      the whole window (one Mean/Max/Min for the period), not a
      per-hour series. To get true hourly readings for degree-hour
      accumulation, this loops one filter_type=1 job per hour and
      reads each job's own Temperature_stats.Mean. Latency scales
      with `hours` - N hours = N submit+poll cycles, not one call.

  CONFIRMED LIVE (real submit+poll+parse cycle run against Gluckstadt,
  DEBUG_PRINT_RAW=1, 2026-08-26):
    - stats_data's real keys are LOWERCASE, not the capitalized
      Temperature_stats.Mean shown in the docs - the docs were wrong
      (or describing a different version). Real shape:
        stats_data = {
          "temperature_stats": {"minimum", "maximum", "mean",
                                 "standard_deviation"},
          "overall_temperature_distribution": [...],
          "normal_temperature_distribution": {"x_axis", "y_axis"},
          "temperature_frequency": {"x_axis", "y_axis"},
        }
    - mean is populated and usable - confirms Mean (now "mean") is a
      real, working field for "the" hourly ambient temp.
    - _extract_hourly_temp_c() already checked both casings
      defensively before this was confirmed, so no code change was
      needed once the real shape came back - it just fell through to
      the lowercase branch and returned the correct value
      (26.226438888888882 for that test run). Left the dual-casing
      check in place rather than trimming it to lowercase-only, in
      case this ever reverts or varies by endpoint version.

  STILL UNCONFIRMED:
    - Whether mean is the right statistic to treat as "the" hourly
      ambient temp for degree-hour math, vs maximum. mean was
      confirmed to exist and populate correctly; which one is the
      more defensible choice biologically hasn't been revisited.

Quickstart repo (has cached responses, run without a live key first):
  https://github.com/FortyGuard-Tech/temperature-api-quickstart

Docs:
  https://docs-api.fortyguard.com/docs
"""

import hashlib
import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests  # pip install requests --break-system-packages
from dotenv import load_dotenv

from risk_engine import HourlyReading

load_dotenv()


# ---------------------------------------------------------------------------
# Config - confirmed real values
# ---------------------------------------------------------------------------

FORTYGUARD_BASE_URL = "https://api.fortyguard.com"

FORTYGUARD_HEATMAP_ENDPOINT = "/v1/heatmap"
FORTYGUARD_STATUS_ENDPOINT = "/v1/status/{activity_id}"

VALID_GRANULARITY_METERS = (60, 80, 100)

FORTYGUARD_API_KEY = os.environ.get("FORTYGUARD_API_KEY")

DEBUG_PRINT_RAW = os.environ.get("DEBUG_PRINT_RAW") == "1"

# Per-hour disk cache, keyed by (polygon_aoi, date, hour, granularity).
# Two things this buys us: (1) get_latest_available_readings() below
# can re-probe the same date/AOI combo repeatedly (e.g. across
# retries or a re-run of orchestrator.py) without re-submitting jobs
# FortyGuard already answered, and (2) a day that's already confirmed
# to have full coverage stays fast on every later run instead of
# re-paying the full N-hour submit+poll cost every single cycle.
# Disabled entirely via FORTYGUARD_CACHE_DISABLED=1 if you ever need
# to force a live re-fetch (e.g. suspect FortyGuard backfilled a
# previously-gapped hour).
FORTYGUARD_CACHE_DIR = Path(
    os.environ.get("FORTYGUARD_CACHE_DIR", ".fortyguard_cache")
)
FORTYGUARD_CACHE_ENABLED = os.environ.get("FORTYGUARD_CACHE_DISABLED") != "1"

# Demo/default pond location - Gluckstadt, MS. NOT a restriction on
# what this adapter can query - get_live_readings() takes latitude/
# longitude as its primary input and works anywhere in FortyGuard's
# confirmed US-only coverage. This is just what fills the slot when
# you call it with no args.
GLUCKSTADT_POND_CENTER = (32.546736, -90.105653)  # (lat, lon)

# Fallback half-width, used ONLY when a pond has no pond_width_m on
# file (e.g. registered before that field existed - see
# location_schema_migration.sql). Every pond registered through the
# current location-pinning flow supplies a real width, which drives
# half_width_km_for_pond() below instead of this constant.
DEFAULT_POND_HALF_WIDTH_KM = 0.5

DEG_LATITUDE_PER_KM = 1 / 111.0  # ~constant everywhere

# ---------------------------------------------------------------------------
# Pond size categories - see chat discussion, grounded in SRAC/MSU
# extension figures for commercial catfish and hybrid striped bass
# ponds. These are LABELS for logging/debugging only - they do NOT
# feed into the AOI math below. The actual box size is computed
# continuously from the farmer's reported pond_width_m via
# half_width_km_for_pond(); a farmer's pond doesn't need to match a
# bucket exactly, the bucket just tells you roughly what kind of
# operation you're looking at when eyeballing logs.
#   small:      <120m  (<=~3 acres - small HSB ponds, fry/nursery ponds)
#   medium:     120-300m (~3-20 acres - modern commercial catfish
#               standard of 8-12 acres, typical HSB grow-out)
#   large:      300-500m (~20-25 acres - older/legacy catfish ponds)
#   very_large: 500m+ (25+ acres - historical large ponds, reservoir-
#               scale systems)
POND_SIZE_CATEGORY_BOUNDS_M = [
    ("small", 0, 120),
    ("medium", 120, 300),
    ("large", 300, 500),
    ("very_large", 500, math.inf),
]

# Multiplier applied to half of the farmer's reported width when
# converting to an AOI half-width. A rough farmer estimate is more
# likely to undershoot the pond's true extent than overshoot it, so
# this errs toward a slightly bigger box (a bit more averaged-in land)
# rather than risk cropping part of the pond out of the reading -
# same "false alarm costs less than a missed one" reasoning used for
# DEPTH_MULTIPLIER's rounding in risk_engine.py.
AOI_SAFETY_MARGIN = 1.3


def classify_pond_size(width_m: float | None) -> str | None:
    """Human-readable size label for a given width - logging only,
    does not affect any AOI or risk-engine math. Returns None if
    width_m is None (pond has no recorded width)."""
    if width_m is None:
        return None
    for label, lo, hi in POND_SIZE_CATEGORY_BOUNDS_M:
        if lo <= width_m < hi:
            return label
    return "very_large"


def half_width_km_for_pond(pond_width_m: float | None) -> float:
    """
    Converts a farmer-reported pond width (meters, longest dimension
    across) into the half-width (km) build_polygon_aoi() needs.

    Continuous, not bucketed - two ponds of 140m and 290m both land in
    the "medium" label above, but get correctly different box sizes
    here rather than sharing one fixed value. Falls back to
    DEFAULT_POND_HALF_WIDTH_KM only when pond_width_m is missing
    entirely (pre-migration ponds with no width on file).
    """
    if pond_width_m is not None and pond_width_m > 0:
        return (pond_width_m / 2.0 / 1000.0) * AOI_SAFETY_MARGIN
    return DEFAULT_POND_HALF_WIDTH_KM


def build_polygon_aoi(
    latitude: float,
    longitude: float,
    half_width_km: float = DEFAULT_POND_HALF_WIDTH_KM,
) -> dict:
    """
    Convert a single lat/long point into the small square GeoJSON
    polygon_aoi that /v1/heatmap actually requires - FortyGuard's
    endpoint takes a polygon, not a point, so this is the point ->
    polygon conversion that lets PondSense accept "just a lat/long"
    (plus a width) as its primary location input instead of requiring
    a hand-built boundary.

    IMPORTANT CAVEAT: this is a bounding-box APPROXIMATION centered on
    the point, not the pond's real shape. Fine for "get me a
    temperature reading near this point" - not a substitute for a
    real traced pond boundary if precision ever matters.

    Longitude degrees compress with latitude (1 deg lon = ~111km *
    cos(latitude)), so this corrects for that using the input
    latitude - a fixed degree offset drifts more the further you get
    from whatever latitude it was tuned against.
    """
    lat_offset = half_width_km * DEG_LATITUDE_PER_KM
    lon_offset = half_width_km / (111.320 * math.cos(math.radians(latitude)))

    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [longitude - lon_offset, latitude - lat_offset],
                    [longitude + lon_offset, latitude - lat_offset],
                    [longitude + lon_offset, latitude + lat_offset],
                    [longitude - lon_offset, latitude + lat_offset],
                    [longitude - lon_offset, latitude - lat_offset],
                ]],
            },
        }],
    }


# Backoff sequence rather than a flat interval - most jobs finish on
# the first couple of checks, so polling every 3s stays responsive
# for the common case; the wait stretching to 6s then 12s (holding at
# 12s thereafter) cuts total request volume for the slow/stuck jobs
# without shrinking POLL_TIMEOUT_SECONDS. Index with min(attempt,
# len-1) so this never runs off the end of the tuple.
POLL_BACKOFF_SECONDS = (3, 6, 12)
POLL_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Per-hour disk cache - see FORTYGUARD_CACHE_* config above
# ---------------------------------------------------------------------------

def _cache_key(polygon_aoi: dict, date_str: str, time_str: str, granularity: int) -> str:
    """Hash the exact query shape (AOI + date/hour/granularity), not
    just lat/lon - two ponds at slightly different coordinates, or
    the same pond re-queried with a different granularity, must NOT
    collide on the same cache entry."""
    raw = json.dumps(
        {"aoi": polygon_aoi, "date": date_str, "time": time_str, "granularity": granularity},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(cache_key: str) -> float | None:
    if not FORTYGUARD_CACHE_ENABLED:
        return None
    path = FORTYGUARD_CACHE_DIR / f"{cache_key}.json"
    if not path.exists():
        return None
    try:
        return float(json.loads(path.read_text())["temp_c"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        # A corrupt/partial cache file should degrade to "cache miss",
        # not crash a live fetch - so log it and fall through to a
        # real API call rather than raising.
        print(f"[CACHE READ FAILED, refetching live] {path.name}: {e}")
        return None


def _cache_set(cache_key: str, temp_c: float) -> None:
    if not FORTYGUARD_CACHE_ENABLED:
        return
    try:
        FORTYGUARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = FORTYGUARD_CACHE_DIR / f"{cache_key}.json"
        path.write_text(json.dumps({
            "temp_c": temp_c,
            "cached_at": datetime.utcnow().isoformat(),
        }))
    except OSError as e:
        # A failed cache WRITE must never fail the reading itself -
        # the temp_c we already have is still good, we just won't be
        # fast on the next run.
        print(f"[CACHE WRITE FAILED, continuing without cache] {e}")


def _get_hourly_temp_c_cached(
    polygon_aoi: dict,
    date_str: str,
    time_str: str,
    granularity: int,
    headers: dict,
    debug: bool = False,
) -> float:
    """Cache-checked wrapper around one hour's submit+poll+extract.
    This is the single choke point get_live_readings()'s loop and
    get_latest_available_readings()'s day-by-day retries both go
    through, so a day that's already been fully fetched once - by
    either function - never re-hits the API for the same hour again.
    """
    cache_key = _cache_key(polygon_aoi, date_str, time_str, granularity)
    cached = _cache_get(cache_key)
    if cached is not None:
        if debug:
            print(f"[CACHE HIT] {date_str} {time_str} -> {cached}C")
        return cached

    activity_id = _submit_heatmap_job(
        polygon_aoi=polygon_aoi,
        start_date=date_str,
        start_time=time_str,
        granularity=granularity,
        headers=headers,
    )
    result = _poll_heatmap_job(activity_id, headers=headers)
    temp_c = _extract_hourly_temp_c(result, debug=debug)
    _cache_set(cache_key, temp_c)
    return temp_c


# ---------------------------------------------------------------------------
# Internal helpers - one heatmap job (submit + poll) for a single window
# ---------------------------------------------------------------------------

def _submit_heatmap_job(
    polygon_aoi: dict,
    start_date: str,
    start_time: str,
    granularity: int,
    headers: dict,
) -> str:
    """POST one /v1/heatmap job for a single-hour window (filter_type=1).
    Returns the activity_id. Raises on non-2xx or a malformed envelope.
    """
    if granularity not in VALID_GRANULARITY_METERS:
        raise ValueError(
            f"granularity must be one of {VALID_GRANULARITY_METERS}, got {granularity}"
        )

    payload = {
        "polygon_aoi": polygon_aoi,
        "date_time": {
            "start_date": start_date,
            "start_time": start_time,
            "filter_type": 1,  # single hour, end_time auto = start_time + 1h
        },
        "granularity": granularity,
        # analytic_type defaults to "tcm" (raw temperature snapshot),
        # which is what we want for ambient readings - left implicit.
    }

    resp = requests.post(
        f"{FORTYGUARD_BASE_URL}{FORTYGUARD_HEATMAP_ENDPOINT}",
        headers=headers,
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()

    activity_id = (body.get("data") or {}).get("activity_id")
    if not activity_id:
        raise RuntimeError(f"No activity_id in submit response: {body}")
    return activity_id


def _poll_heatmap_job(activity_id: str, headers: dict) -> dict:
    """Poll /v1/status/{activity_id} until Completed or Failed.
    Returns the unwrapped result dict (body["data"]["result"]).
    """
    status_url = f"{FORTYGUARD_BASE_URL}{FORTYGUARD_STATUS_ENDPOINT.format(activity_id=activity_id)}"

    elapsed = 0
    attempt = 0
    while elapsed < POLL_TIMEOUT_SECONDS:
        resp = requests.get(status_url, headers=headers, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or {}
        status = data.get("status")

        if status == "Completed":
            result = data.get("result")
            if result is None:
                raise RuntimeError(f"Activity {activity_id} completed with no result: {body}")
            return result

        if status == "Failed":
            raise RuntimeError(f"Activity {activity_id} failed: {body}")

        sleep_for = POLL_BACKOFF_SECONDS[min(attempt, len(POLL_BACKOFF_SECONDS) - 1)]
        time.sleep(sleep_for)
        elapsed += sleep_for
        attempt += 1

    raise TimeoutError(
        f"FortyGuard job {activity_id} did not complete within {POLL_TIMEOUT_SECONDS}s."
    )


def _extract_hourly_temp_c(result: dict, debug: bool = False) -> float:
    """Pull a single representative temperature (°C) out of one hour's
    stats_data. Real key casing confirmed lowercase via a live call
    (see module docstring) - docs' capitalized casing was wrong. Kept
    the dual-casing check below rather than trimming it, as cheap
    insurance against this varying by endpoint version.
    """
    stats_data = result.get("stats_data") or {}
    if debug:
        print(f"[DEBUG_PRINT_RAW] stats_data = {stats_data}")

    temp_stats = stats_data.get("Temperature_stats") or stats_data.get("temperature_stats")
    if not temp_stats:
        raise RuntimeError(f"stats_data missing Temperature_stats: {stats_data}")

    mean = temp_stats.get("Mean", temp_stats.get("mean"))
    if mean is None:
        raise RuntimeError(f"Temperature_stats missing Mean: {temp_stats}")
    return float(mean)


# ---------------------------------------------------------------------------
# The adapter function
# ---------------------------------------------------------------------------

def get_live_readings(
    latitude: float = GLUCKSTADT_POND_CENTER[0],
    longitude: float = GLUCKSTADT_POND_CENTER[1],
    pond_width_m: float | None = None,
    polygon_aoi: dict | None = None,
    start_date: str | None = None,
    start_time: str = "00:00",
    hours: int = 6,
    granularity: int = 100,
) -> list[HourlyReading]:
    """
    Fetch real hourly temperature readings from FortyGuard's Temperature
    API. Drop-in replacement for get_mock_readings() - returns the same
    HourlyReading objects so risk_engine.py doesn't need to change.

    PRIMARY INPUT is latitude/longitude plus pond_width_m (the
    farmer's reported longest dimension across the pond, in meters -
    see location.html / location_routes.py / farmer_data.py). Passing
    pond_width_m sizes the AOI box precisely via
    half_width_km_for_pond(); omitting it (None) falls back to
    DEFAULT_POND_HALF_WIDTH_KM, which is only safe for smaller ponds -
    a pond with no recorded width and a large real footprint risks
    getting cropped.

    NOTE: pond width does NOT affect risk_engine.py's degree-hour math
    at all - depth already captures the surface-area-to-volume physics
    that drives heating rate, and doubling a pond's area at constant
    depth doesn't change that ratio. Width only affects whether THIS
    adapter reads an accurate temperature for the pond in the first
    place - a data-quality concern, not a risk-model concern.

    If you have a real traced pond boundary (not just a center point
    and a width), pass polygon_aoi directly and it overrides the
    lat/long + width conversion entirely.

    IMPORTANT: unlike the old guessed version, this is NOT one API call.
    FortyGuard's heatmap endpoint returns stats for one time window per
    job, so this runs `hours` separate submit+poll cycles (one per
    clock hour, filter_type=1) and reads Temperature_stats.Mean out of
    each. Real latency scales with `hours`.
    """
    if not FORTYGUARD_API_KEY:
        raise RuntimeError(
            "FORTYGUARD_API_KEY not set. Export it in your shell or add it "
            "to your .env - never hardcode it in this file."
        )

    if polygon_aoi is not None:
        aoi = polygon_aoi
    else:
        half_width_km = half_width_km_for_pond(pond_width_m)
        aoi = build_polygon_aoi(latitude, longitude, half_width_km)

    if start_date is None:
        start_date = datetime.utcnow().strftime("%Y-%m-%d")

    headers = {
        "api-key": FORTYGUARD_API_KEY,
        "Content-Type": "application/json",
    }

    base_dt = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")

    readings: list[HourlyReading] = []
    for i in range(hours):
        hour_dt = base_dt + timedelta(hours=i)
        hour_date_str = hour_dt.strftime("%Y-%m-%d")
        hour_time_str = hour_dt.strftime("%H:%M")

        temp_c = _get_hourly_temp_c_cached(
            polygon_aoi=aoi,
            date_str=hour_date_str,
            time_str=hour_time_str,
            granularity=granularity,
            headers=headers,
            debug=(DEBUG_PRINT_RAW and i == 0),
        )

        readings.append(HourlyReading(timestamp=hour_dt, ambient_temp_c=temp_c))

    return readings


# Confirmed via a manual date sweep against Gluckstadt on 2026-08-28:
# some calendar days return zero coverage (every hour's job comes
# back "Failed") while adjacent days are fine - Aug 23, 27, 28 came
# back empty; Aug 21, 22, 24, 25, 26 all had full data. There's no
# known pattern (not "today only", not weekly) so the only reliable
# strategy is to actually check.
MAX_FALLBACK_DAYS_BACK = 7


def get_latest_available_readings(
    latitude: float = GLUCKSTADT_POND_CENTER[0],
    longitude: float = GLUCKSTADT_POND_CENTER[1],
    pond_width_m: float | None = None,
    polygon_aoi: dict | None = None,
    hours: int = 6,
    granularity: int = 100,
    max_days_back: int = MAX_FALLBACK_DAYS_BACK,
) -> list[HourlyReading]:
    """
    Resilience wrapper around get_live_readings() for FortyGuard's
    observed per-day coverage gaps (see comment above). The window
    ends at the last fully-completed hour (UTC, right now) and covers
    the `hours` hours before that - a genuine trailing window, not a
    fixed midnight start. If today has no coverage for that window,
    walks backward one whole day at a time, keeping the SAME
    time-of-day window on each earlier day, up to max_days_back days
    back.

    Returns the SAME HourlyReading list shape as get_live_readings() -
    orchestrator.py's risk_engine.assess_pond() doesn't know or care
    which calendar date the readings actually came from. Timestamps
    on the returned readings reflect whichever date worked, not
    necessarily "today" - the risk engine only consumes relative
    windows (hours_above_watch, degree-hour accumulation over the
    window), so a recent-but-not-today day, AT THE SAME TIME OF DAY,
    stands in validly for "current conditions" until fresher data
    shows up. This is NOT the same tradeoff as orchestrator.py's
    mock-mode fallback (which fabricates a plausible-looking reading) -
    every value here is a real FortyGuard reading, just possibly from
    a slightly older date.

    Raises RuntimeError if no date within max_days_back has full
    coverage - matching orchestrator.py's live-mode
    fetch_readings(), which deliberately does NOT fall back to
    get_mock_readings() when live data can't be obtained: a
    fabricated reading in live mode risks driving a real alert (or
    false all-clear) off fake data, so "nothing available" has to
    surface as a real failure, not get papered over.
    """
    last_error: Exception | None = None

    # THE FIX: without this, get_live_readings()'s start_time default
    # ("00:00") went unset on every call from here, so every "live"
    # fetch silently pulled midnight-to-(hours)am data - regardless of
    # what time it actually was when orchestrator.py ran. A DANGER-
    # level 2pm heat spike would have been invisible; the system was
    # structurally incapable of ever seeing afternoon conditions.
    #
    # Floor to the last FULLY COMPLETED hour (not the current partial
    # hour, which FortyGuard can't have finished processing yet), then
    # step back (hours - 1) more hours to get the window START. This
    # target datetime is computed ONCE against real "now" and then
    # shifted back by whole days for the coverage-gap fallback below -
    # so a day that has to fall back still queries the SAME time-of-day
    # window (e.g. always ending ~2 hours ago), not midnight, keeping
    # "current conditions" honest even when the freshest date is gapped.
    last_completed_hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0) \
        - timedelta(hours=1)
    window_start_dt = last_completed_hour - timedelta(hours=hours - 1)

    for days_back in range(max_days_back + 1):
        candidate_start_dt = window_start_dt - timedelta(days=days_back)
        date_str = candidate_start_dt.strftime("%Y-%m-%d")
        time_str = candidate_start_dt.strftime("%H:%M")
        try:
            readings = get_live_readings(
                latitude=latitude,
                longitude=longitude,
                pond_width_m=pond_width_m,
                polygon_aoi=polygon_aoi,
                start_date=date_str,
                start_time=time_str,
                hours=hours,
                granularity=granularity,
            )
        except (RuntimeError, TimeoutError, requests.RequestException) as e:
            last_error = e
            print(f"[NO COVERAGE] {date_str}: {e}")
            continue

        if days_back > 0:
            print(f"[FALLBACK] no coverage for {days_back} day(s) back - using {date_str}")
        return readings

    raise RuntimeError(
        f"No FortyGuard coverage found in the last {max_days_back} days "
        f"(most recent error: {last_error})"
    )


# ---------------------------------------------------------------------------
# Quick manual test - run this once against a real key to sanity-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        # hours=1 first time - confirms envelope/key names on one cheap
        # job before you burn a multi-hour loop on unverified parsing.
        # Gluckstadt demo pond: ~5ft deep, earthen_lined, channel
        # catfish - width unconfirmed for this specific test pond, so
        # left at the fallback default half-width rather than guessed.
        readings = get_live_readings(
            latitude=32.546736, longitude=-90.105653, hours=1
        )
        print(f"Got {len(readings)} readings:")
        for r in readings:
            print(f"  {r.timestamp} - {r.ambient_temp_c}C")
    except RuntimeError as e:
        print(f"Not ready yet / bad response shape: {e}")
    except requests.RequestException as e:
        print(f"API call failed: {e}")
        print("Check FORTYGUARD_BASE_URL, endpoint path, and auth method above.")
    except TimeoutError as e:
        print(f"Job timed out: {e}")

    # Second check: confirms the fallback wrapper actually walks back
    # to a known-good date instead of just working by accident on
    # days that happen to have coverage. As of the 2026-08-28 sweep,
    # today (08-28) has no coverage and should fall back to 08-26.
    print("\n--- get_latest_available_readings() fallback check ---")
    try:
        readings = get_latest_available_readings(
            latitude=32.546736, longitude=-90.105653, hours=1
        )
        print(f"Got {len(readings)} readings, most recent timestamp: {readings[-1].timestamp}")
    except RuntimeError as e:
        print(f"No coverage found within the fallback window: {e}")
