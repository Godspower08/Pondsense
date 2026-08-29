"""
FortyGuard Hackathon - Pond Heat Risk Engine
------------------------------------------------------
Core logic: converts ambient temperature readings into a pond heat-risk
tier, using degree-hour accumulation (not just instantaneous temp) and
a depth multiplier for earthen ponds of varying depth.

Demo pond: Gluckstadt, Mississippi, USA (32.546736, -90.105653), used
here as a hardcoded default/test location for the adapter - NOT a
constraint on what the system can query (see api_adapter.py, which
takes polygon_aoi as a parameter; any US-coverage polygon works).
Pond is earthen/lined construction, ~5 feet (~1.52m) deep, large
surface area, stocked with channel catfish. 1.52m falls right at the
top edge of the "deep" bucket in DEPTH_MULTIPLIER below (deep =
1.5-2m+), so - unlike the earlier 5-meter mistake - this is actually
within the range that multiplier was calibrated against, not an
extrapolation.

Also includes an optional hybrid striped bass (HSB) DO x temperature
growth-penalty model (see hsb_low_do_growth_penalty below), added on
request. NOTE: the pond described above is stocked with channel
catfish, not hybrid striped bass - the HSB logic is here as an
available option (species="hybrid_striped_bass") but is not what
applies to this specific pond unless that's incorrect and it's
actually mixed/HSB stock.

This runs on MOCK data until FortyGuard API access comes through.
Swap `get_mock_readings()` for the real API call later - everything
downstream (accumulation, tiering, messaging) stays the same.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


# ---------------------------------------------------------------------------
# Config - tune these as you get more research / real farmer feedback
# ---------------------------------------------------------------------------

WATCH_THRESHOLD_C = 30.0   # accumulation only starts counting above this

# How far apart readings are, in hours. FortyGuard's actual update
# frequency isn't confirmed yet - 1.0 for hourly, 0.5 for 30-min.
# This MUST match the real data cadence or degree-hour totals will be
# wrong (e.g. treating 30-min excess as a full hour's worth of heat).
READING_INTERVAL_HOURS = 1.0

# Depth multiplier: derived from Epe earthen pond depth midpoints
# (shallow ~0.75m, medium ~1.25m, deep ~1.75m), using the principle
# that heating rate scales roughly inversely with water depth for a
# given surface area exposed to sun. Normalized to medium = 1.0:
#   shallow: 1.25 / 0.75 = 1.67
#   deep:    1.25 / 1.75 = 0.71
# Rounded here. This deliberately errs toward over-alerting shallow
# ponds rather than under-alerting them - a false alarm costs a farmer
# an unnecessary check; a missed alert can cost them fish. The two
# errors aren't equally bad, so the rounding isn't neutral on purpose.
DEPTH_MULTIPLIER = {
    "shallow": 1.7,   # <1m
    "medium":  1.0,   # 1-1.5m - baseline
    "deep":    0.7,   # 1.5-2m+
}

# Construction-type multiplier: unlined earthen ponds get ground-soil
# thermal buffering that lined/concrete/above-ground systems
# progressively lose. Kept modest and conservative - cross-checked
# against Adeosun, Olaifa & Akande (2017, International Journal of
# Aquaculture, doi:10.5376/ija.2017.07.0002), a real Ibadan field
# study comparing earthen and concrete catfish culture systems, which
# found only a ~0.9C difference between the two - supporting small,
# not dramatic, construction effects. These are reasoned engineering
# assumptions, not measured constants for Epe specifically.
CONSTRUCTION_MULTIPLIER = {
    "earthen_unlined": 1.00,
    "earthen_lined": 1.05,
    "concrete": 1.10,
    "above_ground": 1.20,
}

# Cover override: a shade net/tarp materially cuts direct solar
# gain, which is the dominant driver of pond heating (see the module
# docstring reasoning in CONCEPT_NOTE - solar exposure > ambient air
# temp alone). Halving degree-hours is a blunt, conservative
# approximation - not "covered ponds don't heat up," just "they heat
# up meaningfully slower." Applied AFTER depth/construction, since
# it's a farmer-controllable mitigation layered on top of the pond's
# fixed physical properties, not a substitute for them.
COVER_MULTIPLIER = 0.5

# Degree-hour thresholds for each risk tier (after depth multiplier applied)
# "Degree-hours" = sum of (temp - WATCH_THRESHOLD_C) for every hour temp
# was above the watch threshold, over the trailing window.
class RiskTier(Enum):
    SAFE = "safe"
    WATCH = "watch"
    ALERT = "alert"
    DANGER = "danger"


TIER_THRESHOLDS = [
    (RiskTier.DANGER, 15.0),   # 15+ accumulated degree-hours -> danger
    (RiskTier.ALERT, 8.0),     # 8-15 -> alert
    (RiskTier.WATCH, 3.0),     # 3-8 -> watch
    (RiskTier.SAFE, 0.0),      # below 3 -> safe
]

ACCUMULATION_WINDOW_HOURS = 6  # trailing window we sum degree-hours over


# ---------------------------------------------------------------------------
# Species-specific DO x temperature interaction - Hybrid Striped Bass (HSB)
# ---------------------------------------------------------------------------
# Source: Marcek et al. (pasted study), Figure 1 - "growth" panel, read as
# ESTIMATED MARGINAL MEANS DIGITIZED OFF THE PLOT, not exact published
# numbers (the paper reports no numeric table for this figure). DO was
# tested categorically at three levels - Low (~3.0-3.1 mg/L), Medium
# (~5.1 mg/L), High (~7.6-7.8 mg/L) - crossed with three temps (20C,
# 22C, 29C). The temp x DO interaction on growth was significant overall
# (F=10.00, P<0.01), but the digitized points show it's concentrated at
# 29C - at 20C and 22C the low-vs-high-DO gap was NOT statistically
# significant (overlapping Tukey letter groups).
#
# HSB_DO_INTERACTION_THRESHOLD_C = 26.0 is NOT a published biological
# threshold - the paper never tested 26C. It is a derived engineering
# midpoint: (22 [not significant] + 29 [significant]) / 2 = 25.5,
# rounded to 26.0. Treat it as a placeholder pending real data between
# 23-28C, not a validated cutoff.
#
# HSB_DO_INTERACTION_ANCHOR_C = 29.0 IS a directly tested point with a
# significant result - this one is not derived.
HSB_DO_INTERACTION_THRESHOLD_C = 26.0   # derived midpoint - unvalidated
HSB_DO_INTERACTION_ANCHOR_C = 29.0      # direct evidence - F=10.00, P<0.01

# "Low DO" here means at/near the paper's tested Low treatment
# (~3.0-3.1 mg/L). DO was tested categorically, not continuously, so a
# pond reading of e.g. 4.5 mg/L is NOT "partway" between low and safe -
# it's simply outside the tested design. This threshold is a rough
# proxy for "close to the tested low-DO condition," not a validated
# biological cutoff.
HSB_LOW_DO_MG_L_THRESHOLD = 3.5

# Penalties AT the anchor (29C, low DO vs high DO), digitized off
# Figure 1's estimated marginal means:
#   growth:      low DO ~0.0055 vs high DO ~0.0188  -> ~29% of high  -> ~71% reduction
#   consumption: low DO ~0.0185 vs high DO ~0.0370   -> ~50% of high -> ~50% reduction
# These are read off a plot, not copied from a table - expect several
# percentage points of digitization error.
HSB_GROWTH_PENALTY_AT_ANCHOR = 0.71
HSB_CONSUMPTION_PENALTY_AT_ANCHOR = 0.50


def hsb_low_do_growth_penalty(temp_c: float, dissolved_oxygen_mg_l: float) -> float:
    """
    Growth-risk penalty fraction (0.0-1.0) for hybrid striped bass under
    low dissolved oxygen at a given water temperature. 0.0 = no modeled
    penalty, 1.0 = full penalty as observed at the 29C anchor.

    Returns 0.0 if:
      - DO is above HSB_LOW_DO_MG_L_THRESHOLD (no penalty modeled for
        DO outside the tested "low" range), OR
      - temp is at/below HSB_DO_INTERACTION_THRESHOLD_C (26C - the
        paper found no significant low-DO growth effect even at 22C).

    Between 26C and 29C, the penalty ramps LINEARLY from 0 to
    HSB_GROWTH_PENALTY_AT_ANCHOR. This ramp is an interpolation choice
    I'm making, NOT measured data - the paper has zero data points in
    23-28C, so this is a straight-line guess at the shape of the
    transition, not a finding. The real DO x temp relationship in the
    underlying biology is understood to be step-like (categorical DO
    levels, a sharp jump in effect at high temp), not linear - this is
    a KNOWN, accepted simplification, kept as-is by decision rather
    than fixed, so don't mistake the smooth ramp for a claim about how
    the fish actually respond.

    Above 29C, the penalty is held FLAT at the anchor value rather than
    extrapolated further - there's no evidence for what happens past
    the highest tested temperature, and continuing the ramp upward
    would be pure fabrication.
    """
    if dissolved_oxygen_mg_l > HSB_LOW_DO_MG_L_THRESHOLD:
        return 0.0
    if temp_c <= HSB_DO_INTERACTION_THRESHOLD_C:
        return 0.0
    if temp_c >= HSB_DO_INTERACTION_ANCHOR_C:
        return HSB_GROWTH_PENALTY_AT_ANCHOR
    span = HSB_DO_INTERACTION_ANCHOR_C - HSB_DO_INTERACTION_THRESHOLD_C
    progress = (temp_c - HSB_DO_INTERACTION_THRESHOLD_C) / span
    return round(HSB_GROWTH_PENALTY_AT_ANCHOR * progress, 4)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class HourlyReading:
    timestamp: datetime
    ambient_temp_c: float


@dataclass
class RiskAssessment:
    tier: RiskTier
    degree_hours: float
    current_temp_c: float
    hours_above_watch: int
    depth_category: str
    construction_type: str = "earthen_unlined"
    has_cover: bool = False
    species: str = "catfish"
    dissolved_oxygen_mg_l: float | None = None
    # None = not applicable (not an HSB pond) OR not measured (HSB pond,
    # but no dissolved_oxygen_mg_l reading available). A real float
    # (including 0.0) means the penalty was actually computed from a
    # real DO reading - 0.0 there means "checked, no penalty", which is
    # a different fact than "never checked". Don't collapse these.
    hsb_do_growth_penalty: float | None = None


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def calculate_degree_hours(readings: list[HourlyReading], depth_category: str, construction_type: str = "earthen_unlined", has_cover: bool = False) -> float:
    """
    Sum how many 'degree-hours' of heat have accumulated over the
    trailing window, weighted by pond depth, construction type, AND
    whether the pond has a shade cover.

    Each reading above WATCH_THRESHOLD_C contributes
    (temp - threshold) * READING_INTERVAL_HOURS to the total - so a
    30-min reading at 5 degrees excess adds 2.5 degree-hours, not 5.
    Getting this interval weighting right matters: without it,
    switching from hourly to 30-min data would silently double every
    degree-hour total and push everything into a false "danger" tier.

    A pond that's been climbing for hours racks up more than one that
    just spiked to the same peak - this is the whole point of using
    accumulation instead of a single instantaneous reading.

    Depth, construction, and cover multipliers combine
    multiplicatively - treated as a first-order approximation, not a
    validated physical law (a shallow above-ground tank gets both
    penalties stacked; a covered version of that same tank gets the
    cover discount on top of both).
    """
    depth_mult = DEPTH_MULTIPLIER.get(depth_category, 1.0)
    construction_mult = CONSTRUCTION_MULTIPLIER.get(construction_type, 1.0)
    cover_mult = COVER_MULTIPLIER if has_cover else 1.0
    combined_mult = depth_mult * construction_mult * cover_mult

    # number of readings that fit in the trailing window, given the
    # actual interval between readings
    window_size = round(ACCUMULATION_WINDOW_HOURS / READING_INTERVAL_HOURS)
    window = readings[-window_size:]

    total = 0.0
    for reading in window:
        excess = reading.ambient_temp_c - WATCH_THRESHOLD_C
        if excess > 0:
            total += excess * READING_INTERVAL_HOURS

    return round(total * combined_mult, 2)


def classify_risk(degree_hours: float) -> RiskTier:
    """Map accumulated degree-hours to a risk tier."""
    for tier, threshold in TIER_THRESHOLDS:
        if degree_hours >= threshold:
            return tier
    return RiskTier.SAFE


def assess_pond(
    readings: list[HourlyReading],
    depth_category: str,
    construction_type: str = "earthen_unlined",
    has_cover: bool = False,
    species: str = "catfish",
    dissolved_oxygen_mg_l: float | None = None,
) -> RiskAssessment:
    """
    Full pipeline: readings -> degree-hours -> risk tier.

    species / dissolved_oxygen_mg_l are optional and backward-compatible -
    omitting them behaves exactly as before. Passing
    species="hybrid_striped_bass" plus a dissolved_oxygen_mg_l reading
    additionally computes hsb_do_growth_penalty via
    hsb_low_do_growth_penalty().

    NOTE: hsb_do_growth_penalty is reported as a SEPARATE advisory field,
    not folded into `tier`. `tier` is purely the ambient-temperature
    degree-hour model (depth/construction/cover), which is a physically
    grounded heating-rate calculation. The DO x temp penalty is a
    biological growth-suppression estimate from a different paper, on a
    different response variable (feed conversion/growth), for a
    different species than the pond is currently stocked with per your
    last message (channel catfish, not hybrid striped bass). Combining
    the two into one tier would require inventing a weighting between
    "pond is physically heating up" and "this species' growth is
    suppressed" that neither source supports - so it's surfaced
    separately instead of silently baked in.

    hsb_do_growth_penalty is None when it wasn't actually computed -
    either the pond isn't HSB, or it is but dissolved_oxygen_mg_l
    wasn't supplied. A real float (0.0 included) means a DO reading
    was actually checked against hsb_low_do_growth_penalty(). Do not
    treat None the same as 0.0 - one means "confirmed no penalty",
    the other means "never measured", and collapsing them hides the
    difference from anyone reading the assessment.
    """
    degree_hours = calculate_degree_hours(readings, depth_category, construction_type, has_cover)
    tier = classify_risk(degree_hours)

    window_size = round(ACCUMULATION_WINDOW_HOURS / READING_INTERVAL_HOURS)
    window = readings[-window_size:]
    hours_above_watch = sum(
        1 for r in window if r.ambient_temp_c > WATCH_THRESHOLD_C
    ) * READING_INTERVAL_HOURS
    current_temp = readings[-1].ambient_temp_c if readings else 0.0

    hsb_penalty = None  # not applicable (not HSB) or not measured (no DO reading)
    if species == "hybrid_striped_bass" and dissolved_oxygen_mg_l is not None:
        hsb_penalty = hsb_low_do_growth_penalty(current_temp, dissolved_oxygen_mg_l)

    return RiskAssessment(
        tier=tier,
        degree_hours=degree_hours,
        current_temp_c=current_temp,
        hours_above_watch=hours_above_watch,
        depth_category=depth_category,
        construction_type=construction_type,
        has_cover=has_cover,
        species=species,
        dissolved_oxygen_mg_l=dissolved_oxygen_mg_l,
        hsb_do_growth_penalty=hsb_penalty,
    )


# ---------------------------------------------------------------------------
# MOCK DATA - use this until FortyGuard API access comes through
# ---------------------------------------------------------------------------

def get_mock_readings(scenario: str = "hot_afternoon") -> list[HourlyReading]:
    """
    Simulates a realistic daily heat curve so you can test the engine
    before you have real API access. (Left generic - not modeled on any
    one location's actual climate data; swap for real FortyGuard
    readings once the Gluckstadt AOI is confirmed working.)

    Scenarios:
      - "normal_day": mild, never crosses watch threshold
      - "hot_afternoon": realistic slow climb through midday, peaks ~2pm
      - "sudden_spike": rapid jump (tests that accumulation still
        correctly shows LOWER risk than a slow climb to the same peak)
    """
    base_date = datetime(2026, 8, 8, 6, 0)  # start at 6am

    curves = {
        "normal_day":    [24, 25, 26, 27, 28, 28, 27, 26, 25, 24, 23, 22],
        "hot_afternoon": [25, 26, 28, 30, 32, 34, 35, 34, 32, 30, 28, 26],
        "sudden_spike":  [25, 25, 26, 26, 27, 35, 36, 30, 28, 27, 26, 25],
    }

    temps = curves.get(scenario, curves["hot_afternoon"])

    return [
        HourlyReading(timestamp=base_date + timedelta(hours=i), ambient_temp_c=t)
        for i, t in enumerate(temps)
    ]


# ---------------------------------------------------------------------------
# Quick test when run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for scenario in ["normal_day", "hot_afternoon", "sudden_spike"]:
        print(f"\n--- Scenario: {scenario} ---")
        readings = get_mock_readings(scenario)
        for depth in ["shallow", "medium", "deep"]:
            for construction in ["earthen_unlined", "earthen_lined", "concrete", "above_ground"]:
                for has_cover in [False, True]:
                    result = assess_pond(readings, depth, construction, has_cover)
                    cover_label = "covered" if has_cover else "uncovered"
                    print(
                        f"  depth={depth:8s} | construction={construction:15s} | "
                        f"{cover_label:9s} | tier={result.tier.value:7s} | "
                        f"degree_hours={result.degree_hours:5.2f}"
                    )

    # Gluckstadt, MS demo pond: earthen/lined, ~5ft (~1.52m) deep ->
    # "deep" bucket (within calibrated range, see module docstring),
    # channel catfish - no HSB penalty applies.
    print("\n--- Gluckstadt, MS demo pond (channel catfish, earthen_lined, deep) ---")
    gluckstadt_readings = get_mock_readings("hot_afternoon")
    gluckstadt_result = assess_pond(
        gluckstadt_readings,
        depth_category="deep",
        construction_type="earthen_lined",
        has_cover=False,
    )
    print(
        f"  tier={gluckstadt_result.tier.value:7s} | "
        f"degree_hours={gluckstadt_result.degree_hours:5.2f} | "
        f"current_temp_c={gluckstadt_result.current_temp_c}"
    )

    # HSB DO x temp penalty demo - same readings, hypothetically stocked
    # with hybrid striped bass and a low-DO reading, to show the
    # separate advisory field in action.
    print("\n--- Same pond, hypothetical HSB stock, DO=2.8 mg/L (demo only) ---")
    hsb_result = assess_pond(
        gluckstadt_readings,
        depth_category="deep",
        construction_type="earthen_lined",
        has_cover=False,
        species="hybrid_striped_bass",
        dissolved_oxygen_mg_l=2.8,
    )
    penalty_display = (
        f"{hsb_result.hsb_do_growth_penalty:.2f}"
        if hsb_result.hsb_do_growth_penalty is not None
        else "None (not measured)"
    )
    print(
        f"  tier={hsb_result.tier.value:7s} (ambient-heat model, unchanged) | "
        f"hsb_do_growth_penalty={penalty_display} "
        f"(separate advisory - see assess_pond docstring)"
    )

    # Same HSB pond, but no DO reading available - this is the actual
    # state every real HSB pond is in right now (orchestrator.py never
    # supplies dissolved_oxygen_mg_l, since nothing collects it yet).
    # Confirms None ("not measured") is reported, not a silent 0.0.
    print("\n--- Same pond, HSB stock, NO DO reading (current real-world state) ---")
    hsb_no_do_result = assess_pond(
        gluckstadt_readings,
        depth_category="deep",
        construction_type="earthen_lined",
        has_cover=False,
        species="hybrid_striped_bass",
    )
    print(
        f"  tier={hsb_no_do_result.tier.value:7s} (ambient-heat model, unchanged) | "
        f"hsb_do_growth_penalty={hsb_no_do_result.hsb_do_growth_penalty!r} "
        f"(None, not 0.0 - honestly reflects 'never measured')"
    )
