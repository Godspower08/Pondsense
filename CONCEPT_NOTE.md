# PondSense
### An automated heat-risk early-warning system for U.S. warm-water pond aquaculture
**FortyGuard Hackathon '26**

---

## 1. The Problem

Catfish and hybrid striped bass are farmed in outdoor ponds across the U.S. Southeast — most heavily in the Mississippi Delta. Warm water holds less dissolved oxygen than cool water, so on sustained hot days, fish can slowly suffocate long before anyone notices anything is wrong. Farmers currently have no automated way to know their pond has been quietly accumulating heat stress over the preceding hours.

## 2. Who This Is For

Independent and small-commercial pond operators farming channel catfish or hybrid striped bass (HSB) in the U.S., who have email access but not necessarily a data-monitoring budget. Longer-term, the more durable buyer is a cooperative, feed supplier, or aquaculture insurer — organizations that lose money when farmers lose fish, and who could subsidize or fully cover alerts for farmers under them (B2B2C).

## 3. The Core Idea

Don't just check "is it hot right now" — track how much heat has *accumulated* over a trailing window, the same way sunburn builds from cumulative exposure rather than the temperature at any single instant. Combine that with pond-specific physical factors (depth, construction material, shade cover) that change how fast a given pond actually heats up, and turn the result into a plain-language risk tier a farmer can act on.

## 4. System Architecture

```
Farmer's email  ──▶  email_reply_handler.py  ──▶  farmer_data.py (Supabase)
                            │                              │
                            ▼                              │
                    location.html + location_routes.py ◀────┘
                    (pond lat/lng + width capture)

Scheduled hourly  ──▶  orchestrator.py
                            │
                            ▼
                    api_adapter.py ──▶ FortyGuard Temperature API
                            │
                            ▼
                    risk_engine.py (degree-hour risk model)
                            │
                            ▼
                    email_alerts.py ──▶ farmer's inbox
```

**Registration:** A farmer emails `JOIN <species> <depth> <construction>` (e.g. `JOIN catfish deep concrete`) to a dedicated address. `email_reply_handler.py` polls that inbox, parses the command against species (`catfish` / `hybrid_striped_bass`), depth (`shallow` / `medium` / `deep`), and construction (`earthen_unlined` / `earthen_lined` / `concrete` / `above_ground`). A deterministic farmer ID is derived from the email address so re-registering updates the same farmer rather than duplicating them. Pond numbers auto-increment per farmer (Pond 1, Pond 2, ...) — nobody invents an ID. The confirmation reply includes a unique tokenized link to the location-pinning page.

Before matching, the incoming email body has quoted reply history stripped out (anything after an `On ... wrote:` header, an Outlook-style separator, or a `>`-quoted line) — otherwise a threaded reply containing PondSense's own prior instructions gets misread as a fresh command, since those instructions themselves contain the word "join."

**Location capture:** The tokenized link opens a hosted page with three ways to supply a pond's coordinates: (1) browser GPS, held until accuracy is ≤20m or 60 seconds elapse; (2) pasting a Google Maps share link, resolved server-side (including shortened links, by following redirects) and parsed for coordinates; (3) manual entry — either dragging a pin on a map or typing exact latitude/longitude into two fields that recenter the map at a usable zoom. The farmer also enters the pond's approximate width in meters on the same page. Both a location and a width are required before the page will submit.

**Risk assessment (scheduled, hourly):** For every registered pond:
1. If location data is missing, send a one-time "please pin your pond" nudge email (suppressed after the first send until resolved).
2. Fetch temperature readings for a trailing 6-hour window ending at the last fully-completed hour — a genuine "now-relative" window, not a fixed clock time.
3. Run the readings through the degree-hour risk model.
4. Send an alert on a tier *change* (except DANGER, which re-notifies every cycle, since silence during genuine crisis is worse than a repeat message).

## 5. FortyGuard Integration

Two endpoints, called directly over HTTP:

- **`POST /v1/heatmap`** — submits one job per hour requested (`filter_type: 1`, a single-hour window; a multi-hour request returns one aggregate stat for the whole span, not a per-hour series, so N hours of history means N separate jobs). The payload requires a GeoJSON `polygon_aoi`, not a point — a farmer's lat/lng and reported pond width are converted into a small bounding-box polygon, sized with a safety margin so a rough width estimate doesn't crop the pond out of frame. `granularity` must be exactly 60, 80, or 100 (meters).
- **`GET /v1/status/{activity_id}`** — polled with backoff until the job reports `Completed` or `Failed`. The temperature value is read from `stats_data.temperature_stats.mean`.

FortyGuard's coverage has real per-day gaps with no fixed pattern, so the adapter walks backward across dates (holding the same time-of-day window) until it finds one with full coverage, up to a set number of days back. A plain network error retries the same window; a confirmed empty result moves on immediately, since those are different signals.

FortyGuard's API does not support forecasting — a `start_date` later than today is rejected outright. Any predictive layer in PondSense is built by extrapolating its own accumulated degree-hour trend, not by requesting future data from FortyGuard.

## 6. The Risk Model

For each hourly reading in the trailing window, excess above a 30°C baseline is summed into "degree-hours." That raw total is adjusted by three multipliers, applied together:

- **Depth:** shallow ×1.7 / medium ×1.0 / deep ×0.7 — shallower water heats faster for a given surface area.
- **Construction:** earthen unlined ×1.00 (baseline) / earthen lined ×1.05 / concrete ×1.10 / above-ground ×1.20.
- **Cover:** halves the accumulated total if the pond has shade cover.

The adjusted total is classified: **SAFE** (0–3) / **WATCH** (3–8) / **ALERT** (8–15) / **DANGER** (15+). Both catfish and hybrid striped bass are assessed through this same model.

## 7. Alerts

Email is the live channel for this submission. Every alert has two parts, deliberately kept separate: a short formal-tone paragraph generated by an LLM for framing only, and a deterministic data block (tier, temperature, degree-hours, depth, construction, timestamp) assembled directly from the risk assessment in code — the LLM never sees or supplies these numbers, so it cannot alter what actually gets recorded.

An SMS channel was designed alongside email (Twilio-based, including an inbound webhook for manual readings and free-text Q&A), sharing the same underlying farmer/pond data. It is not the active channel in this submission — email is the one running live.

## 8. Data Model (Supabase / Postgres)

- **`farmers`**: farmer_id (PK), phone_number, email, sms_opted_in, email_opted_in
- **`ponds`**: pond_id (PK), farmer_id (FK), species, depth_category, construction_type, last_tier, lat, lng, accuracy_m, location_method, location_token (unique), pond_width_m, location_gap_notified
- **`alerts`**: pond_id, tier, degree_hours, sent_via — one row per notification sent
- **`manual_readings`**: pond_id, reading_type, value — for farmer-submitted readings when no automated data exists

## 9. Deployment & Automation

- **The location-pinning page** runs on a persistent hosted server (not a developer's machine).
- **Registration replies** run on a schedule (~every 10 minutes) via a single-shot email-check process — not a continuously-running loop.
- **The risk cycle** runs hourly on its own schedule, matching FortyGuard's own per-hour data granularity.
- Required configuration: Supabase URL/key, an LLM API key, a Gmail address and app password, a FortyGuard API key, and the hosted app's public URL (for building location links).

---
*Prepared for FortyGuard Hackathon '26 — Build Sprint Aug 18–30, 2026*
