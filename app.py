import datetime
import logging
import os
import threading
import time
import traceback

from flask import Flask, jsonify, render_template, request

from master_query import collect_working_servers, query_all_masters, probe_candidates

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)


# Surface the real exception instead of Flask's generic "Internal Server
# Error" page. Set SHOW_ERROR_DETAILS=0 to hide the traceback from users
# once things are stable (it'll still show up in the logs either way).
SHOW_ERROR_DETAILS = os.environ.get("SHOW_ERROR_DETAILS", "1") != "0"


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    logging.exception("unhandled exception in request")
    if not SHOW_ERROR_DETAILS:
        return "Something went wrong - check the server logs.", 500
    return (
        "<h1>Unhandled exception</h1><pre>{}</pre>".format(
            traceback.format_exc()
        ),
        500,
    )

# How long a result set is reused before we hit the masters again.
# Keep this reasonably high - these are small, volunteer-run master
# servers and hammering them on every page load is not a good idea.
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "90"))

_cache_lock = threading.Lock()
_cache = {}  # filter_str -> {"working", "total", "fetched_at", "error"}


def refresh_cache(filter_str, force=False):
    with _cache_lock:
        entry = _cache.get(filter_str)
        if entry and not force and (time.time() - entry["fetched_at"]) < CACHE_SECONDS:
            return entry

    try:
        working, total = collect_working_servers(filter_str=filter_str)
        error = None
    except Exception as exc:  # keep serving stale data if a refresh blows up
        logging.exception("refresh failed")
        prev = _cache.get(filter_str, {"working": [], "total": 0})
        working, total, error = prev["working"], prev["total"], str(exc)

    entry = {"working": working, "total": total, "fetched_at": time.time(), "error": error}
    with _cache_lock:
        _cache[filter_str] = entry
    return entry


def _filter_from_gamedir(gamedir):
    return f"\\gamedir\\{gamedir}" if gamedir else "\\gamedir\\*"


@app.route("/")
def index():
    gamedir = request.args.get("gamedir", "").strip()
    force = request.args.get("refresh") == "1"
    data = refresh_cache(_filter_from_gamedir(gamedir), force=force)

    fetched_at = data["fetched_at"]
    fetched_at_str = (
        datetime.datetime.fromtimestamp(fetched_at).strftime("%H:%M:%S UTC")
        if fetched_at else "never"
    )
    age = int(time.time() - fetched_at) if fetched_at else None

    return render_template(
        "index.html",
        servers=data["working"],
        total=data["total"],
        fetched_at_str=fetched_at_str,
        age=age,
        error=data["error"],
        gamedir=gamedir,
    )


@app.route("/api/servers")
def api_servers():
    gamedir = request.args.get("gamedir", "").strip()
    force = request.args.get("refresh") == "1"
    data = refresh_cache(_filter_from_gamedir(gamedir), force=force)

    return jsonify(
        {
            "fetched_at": data["fetched_at"],
            "working_count": len(data["working"]),
            "candidate_count": data["total"],
            "servers": data["working"],
            "error": data["error"],
        }
    )


@app.route("/api/debug")
def api_debug():
    """
    Bypasses the cache entirely: queries the masters live, then shows
    the alive/dead status of EVERY candidate it got back, not just the
    ones that answered. Use this to tell apart "nobody's online right
    now" from "the probe itself is broken" - paste the output back if
    you need help reading it.
    """
    gamedir = request.args.get("gamedir", "").strip()
    candidates = query_all_masters(filter_str=_filter_from_gamedir(gamedir))
    results = probe_candidates(candidates)
    alive = sum(1 for r in results if r["alive"])
    return jsonify(
        {
            "candidate_count": len(candidates),
            "alive_count": alive,
            "results": results,
        }
    )


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
