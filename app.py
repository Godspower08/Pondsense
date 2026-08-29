"""
PondSense - main Flask app.

This is the ONE process ngrok points at. Currently just hosts the
location-pinning routes (location_routes.py). If SMS ever comes back,
its webhook blueprint would register here too - same app, same
ngrok tunnel, no need for a second process or a second tunnel
(ngrok's free tier only allows one online at a time anyway).

RUNNING THIS FOR REAL:
  1. Run location_schema_migration.sql in Supabase first.
  2. pip install flask requests --break-system-packages
  3. In one terminal:  python3 app.py
  4. In another terminal:  ngrok http 5000
  5. Copy the https://....ngrok-free.app URL ngrok prints.

You do NOT need to hand-copy that URL into .env anymore - see
email_reply_handler.py, which now reads it live from ngrok's local
API (http://127.0.0.1:4040/api/tunnels) each time it builds a JOIN
reply. Just make sure ngrok is running before you test JOIN emails.
LOCATION_BASE_URL in .env still works as a manual override/fallback
if you ever get a fixed domain, or if ngrok's local API isn't
reachable for some reason.
"""

from flask import Flask

from location_routes import location_bp

app = Flask(__name__)
app.register_blueprint(location_bp)


@app.route("/")
def health_check():
    # Simple sanity-check route - if this loads in a browser via your
    # ngrok URL, the tunnel and app are both working.
    return "PondSense is running."


if __name__ == "__main__":
    # debug=True is fine for local dev/demo; turn off before anything
    # resembling a real deployment.
    app.run(host="0.0.0.0", port=5000, debug=True)
