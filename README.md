# PondSense

Automated heat-risk alerts for U.S. warm-water pond aquaculture (catfish and hybrid striped bass), built on FortyGuard's Temperature API.

**Live demo:** https://pondsense.onrender.com
**Demo video:** https://canva.link/demqshjv5o3o521
**Full design and architecture:** see [`CONCEPT_NOTE.md`](./CONCEPT_NOTE.md)
**Submission summary:** see [`SUBMISSION_SUMMARY.md`](./SUBMISSION_SUMMARY.md)

## What's in this repo

| File | Purpose |
|---|---|
| `app.py` | Flask entry point — hosts the location-pinning page |
| `location_routes.py` / `location.html` | Pond GPS/maps-link/manual location capture |
| `farmer_data.py` | Supabase data layer (farmers, ponds, alerts) |
| `email_reply_handler.py` | Parses inbound `JOIN` emails, registers ponds, replies |
| `run_email_check.py` | Single-shot entry point for the scheduled email check |
| `orchestrator.py` | Hourly risk-assessment cycle across every pond |
| `api_adapter.py` | FortyGuard `/v1/heatmap` + `/v1/status` integration |
| `risk_engine.py` | Degree-hour accumulation and tier classification |
| `email_alerts.py` | Builds and sends alert/nudge emails |
| `sms_alerts.py` / `sms_webhook.py` | SMS channel (designed, not active in this submission) |
| `mock_temp_api.py` | Local stand-in for FortyGuard, for offline testing |
| `location_schema_migration.sql` | Adds location columns to an existing `ponds` table |
| `.github/workflows/` | Scheduled automation (see below) |

## Environment variables

Create a `.env` file (never commit this) with:

```
SUPABASE_URL=
SUPABASE_KEY=
FORTYGUARD_API_KEY=
GEMINI_API_KEY=
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=
LOCATION_BASE_URL=https://pondsense.onrender.com
PONDSENSE_LIVE=1
```

`GMAIL_APP_PASSWORD` must be a Gmail **App Password** (Google Account → Security → 2-Step Verification → App Passwords), not the account's normal login password. `SUPABASE_KEY` must be the `service_role` key, not `anon`.

## Local setup

```bash
pip install -r requirements.txt
```

Run the Supabase schema first (SQL Editor, in order):
1. `supabase_schema.sql`
2. `location_schema_migration.sql`

**Run the location-pinning server:**
```bash
python app.py
```

**Check for and reply to registration emails once:**
```bash
python run_email_check.py
```

**Run one full risk-assessment cycle:**
```bash
python orchestrator.py
```

## Registering a pond (as a farmer would)

Email `JOIN <species> <depth> <construction>` to the configured Gmail address, e.g.:

```
JOIN catfish deep concrete
```

Valid species: `catfish`, `hybrid_striped_bass`
Valid depth: `shallow`, `medium`, `deep`
Valid construction: `earthen_unlined`, `earthen_lined`, `concrete`, `above_ground`

You'll get a reply with a link to pin your pond's exact location. Alerts arrive at the same email address once that's done.

## Deployment (how the live demo actually runs)

- **`app.py`** is deployed on Render (free tier), started via `gunicorn app:app`.
- **Two scheduled GitHub Actions workflows** replace any need for a continuously-running process:
  - `.github/workflows/email-poll.yml` — checks for new registration emails every ~10 minutes
  - `.github/workflows/orchestrator-cycle.yml` — runs the full risk cycle hourly

Both need the same secrets set under repo **Settings → Secrets and variables → Actions**: `SUPABASE_URL`, `SUPABASE_KEY`, `FORTYGUARD_API_KEY`, `GEMINI_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `LOCATION_BASE_URL`.

Render needs its **own** copy of `SUPABASE_URL`, `SUPABASE_KEY`, and `GEMINI_API_KEY` set under its own Environment tab — these are separate from GitHub's secrets and are not shared automatically between the two platforms.
