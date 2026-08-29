"""
PondSense - Mock Temperature API
-----------------------------------
Stands in for FortyGuard's real API during testing/demo so you're
not waiting on actual weather to hit DANGER tier. Same rough shape
(temp_c keyed by zip) - point fetch_readings() in orchestrator.py at
this instead of get_mock_readings() when you want to test against a
live endpoint rather than a hardcoded scenario.

Run:
  pip install flask --break-system-packages
  python mock_temp_api.py

Test:
  curl http://localhost:5001/temp?zip=38773
  curl -X POST http://localhost:5001/set -d "temp_c=36.5"
"""

from flask import Flask, request, jsonify
import time

app = Flask(__name__)

# In-memory override - lets you force a temperature during a live demo
# without touching code. Defaults to a moderate/safe reading.
_state = {"temp_c": 27.0}


@app.route("/temp", methods=["GET"])
def get_temp():
    zip_code = request.args.get("zip", "unknown")
    return jsonify({
        "zip": zip_code,
        "temp_c": _state["temp_c"],
        "timestamp": int(time.time()),
    })


@app.route("/set", methods=["POST"])
def set_temp():
    try:
        raw = request.form.get("temp_c")
        if raw is None and request.is_json:
            raw = request.json.get("temp_c")
        temp_c = float(raw)
    except (TypeError, ValueError):
        return jsonify({"error": "temp_c must be a number"}), 400
    _state["temp_c"] = temp_c
    print(f"[MOCK API] temp_c set to {temp_c}")
    return jsonify({"ok": True, "temp_c": temp_c})


if __name__ == "__main__":
    print("Mock temperature API running on port 5001.")
    app.run(port=5001)
