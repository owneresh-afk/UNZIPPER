from flask import Flask, jsonify
from threading import Thread
import logging
import os
import time
import shutil

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app = Flask(__name__)
_start_time = time.time()


@app.route("/")
def home():
    return (
        "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
        "<h1>⚡ UnZipper Pro</h1>"
        "<p style='color:green;font-size:1.2em'>✅ Bot is alive and running!</p>"
        "<p><a href='/health'>Health Check</a> · <a href='/status'>Status</a></p>"
        "</body></html>"
    ), 200


@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "UnZipper Pro"}), 200


@app.route("/status")
def status():
    uptime_sec = int(time.time() - _start_time)
    days, rem = divmod(uptime_sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    uptime_str = f"{days}d {hours}h {mins}m"

    try:
        disk = shutil.disk_usage("/")
        disk_info = {
            "total_gb": round(disk.total / 1e9, 1),
            "used_gb":  round(disk.used  / 1e9, 1),
            "free_gb":  round(disk.free  / 1e9, 1),
            "used_pct": round(disk.used  / disk.total * 100, 1),
        }
    except Exception:
        disk_info = {}

    return jsonify({
        "status":  "ok",
        "bot":     "UnZipper Pro",
        "uptime":  uptime_str,
        "uptime_seconds": uptime_sec,
        "disk":    disk_info,
    }), 200


def run_flask():
    # On Render RENDER=true is always set — use the assigned PORT.
    # In dev, fall back to BOT_PORT (default 5001) to avoid colliding with the API server.
    if os.environ.get("RENDER"):
        port = int(os.environ.get("PORT", 10000))
    else:
        port = int(os.environ.get("BOT_PORT", 5001))
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except OSError as e:
        logging.getLogger(__name__).warning(
            f"Flask keep-alive could not bind to port {port}: {e} — bot polling still runs fine."
        )


def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()
