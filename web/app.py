"""
TorpTradingBot — Flask web UI (port 6060).

Routes:
  GET /                    — HTML dashboard
  GET /api/trades          — today's trade signals as JSON
  GET /api/state           — watchlist + open positions as JSON
  GET /api/suspicious      — suspicious watchlist + suspicious positions as JSON
  GET /api/log             — last 150 lines of cron.log
  GET /api/config          — config.yaml as JSON
  POST /api/config         — update config.yaml from JSON body
  POST /api/test-connection — test Ollama and Polymarket connectivity
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import yaml
from flask import Flask, jsonify, render_template, Response, request

app = Flask(__name__)
log = logging.getLogger(__name__)

BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
CRON_LOG = LOG_DIR / "cron.log"
WATCHLIST_PATH = STATE_DIR / "watchlist.json"
POSITIONS_PATH = STATE_DIR / "open_positions.json"
SUSPICIOUS_WATCHLIST_PATH = STATE_DIR / "suspicious_watchlist.json"
SUSPICIOUS_POSITIONS_PATH = STATE_DIR / "suspicious_positions.json"
PORTFOLIO_PATH            = STATE_DIR / "virtual_portfolio.json"
SUSP_PORTFOLIO_PATH       = STATE_DIR / "suspicious_virtual_portfolio.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _save_config(data: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _load_json(path: Path, default):
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _today_trades() -> list[dict]:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    csv_path = LOG_DIR / f"trades_{date_str}.csv"
    if not csv_path.exists():
        return []
    try:
        with open(csv_path, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


def _market_title_map() -> dict[str, str]:
    """
    Build a {market_id: market_title} lookup from both virtual portfolio files.
    Used to enrich the trades CSV (which only stores market_id) with human-readable titles.
    """
    title_map: dict[str, str] = {}
    for path in (PORTFOLIO_PATH, SUSP_PORTFOLIO_PATH):
        data = _load_json(path, {})
        for mid, pos in data.get("open_positions", {}).items():
            t = pos.get("market_title", "")
            if t:
                title_map[mid] = t
        for pos in data.get("closed_positions", []):
            mid = pos.get("_market_id") or pos.get("market_id", "")
            t = pos.get("market_title", "")
            if mid and t:
                title_map[mid] = t
    return title_map


@app.route("/api/trades")
def api_trades():
    """Today's trade signals (all columns from CSV), enriched with market titles."""
    trades = _today_trades()
    titles = _market_title_map()
    for t in trades:
        mid = t.get("market_id", "")
        if mid and mid in titles:
            t["market_title"] = titles[mid]
    return jsonify(
        {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "trades": trades,
        }
    )


@app.route("/api/state")
def api_state():
    """Current watchlist and open positions."""
    return jsonify(
        {
            "watchlist": _load_json(WATCHLIST_PATH, []),
            "open_positions": _load_json(POSITIONS_PATH, {}),
        }
    )


def _portfolio_summary(p: dict | None) -> dict | None:
    """
    Convert raw virtual_portfolio.json data into the shape the UI expects.
    Adds computed fields (total_pnl, unrealized_pnl, open_count) and converts
    open_positions from a dict to an array with _market_id embedded.
    """
    if not p:
        return None
    open_pos: dict = p.get("open_positions", {})
    unrealized = sum(float(pos.get("unrealized_pnl", 0)) for pos in open_pos.values())
    realized   = float(p.get("realized_pnl", 0))
    return {
        "starting_balance":  p.get("starting_balance", 500),
        "available_balance": round(float(p.get("available_balance", 0)), 4),
        "total_invested":    round(sum(float(pos.get("size_usdc", 0)) for pos in open_pos.values()), 4),
        "realized_pnl":      round(realized, 4),
        "unrealized_pnl":    round(unrealized, 4),
        "total_pnl":         round(realized + unrealized, 4),
        "wins":              p.get("wins", 0),
        "losses":            p.get("losses", 0),
        "open_count":        len(open_pos),
        "open_positions":    [{**pos, "_market_id": mid} for mid, pos in open_pos.items()],
        "closed_positions":  p.get("closed_positions", [])[:10],
        "last_updated":      p.get("last_updated"),
    }


@app.route("/api/portfolio")
def api_portfolio():
    """Virtual dry-run portfolio for both strategies."""
    return jsonify({
        "regular":    _portfolio_summary(_load_json(PORTFOLIO_PATH, None)),
        "suspicious": _portfolio_summary(_load_json(SUSP_PORTFOLIO_PATH, None)),
    })


@app.route("/api/suspicious")
def api_suspicious():
    """Suspicious watchlist and open positions from suspicious copies."""
    return jsonify(
        {
            "watchlist": _load_json(SUSPICIOUS_WATCHLIST_PATH, []),
            "positions": _load_json(SUSPICIOUS_POSITIONS_PATH, {}),
        }
    )


@app.route("/api/log")
def api_log():
    """Last 150 lines of cron.log."""
    if not CRON_LOG.exists():
        return Response("", mimetype="text/plain")
    try:
        with open(CRON_LOG) as f:
            lines = f.readlines()
        return Response("".join(lines[-150:]), mimetype="text/plain")
    except OSError:
        return Response("", mimetype="text/plain")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return config.yaml contents as JSON."""
    try:
        return jsonify(_load_config())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config", methods=["POST"])
def api_config_post():
    """Update config.yaml from a JSON body (partial updates supported)."""
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    allowed_keys = {"mode", "ollama", "polymarket", "suspicious"}
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        return jsonify({"error": f"Unknown config keys: {unknown}"}), 400

    if "mode" in data and data["mode"] not in ("dry_run", "live"):
        return jsonify({"error": "mode must be 'dry_run' or 'live'"}), 400

    try:
        current = _load_config()
        current.update(data)
        _save_config(current)
        return jsonify({"ok": True, "message": "Config saved. Cron jobs pick up changes on next run."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    """Test connectivity to Ollama and Polymarket data API."""
    results: dict = {}

    # Ollama
    try:
        cfg = _load_config()
        ollama_host = cfg.get("ollama", {}).get("host", "http://localhost:11434")
        resp = requests.get(f"{ollama_host}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])] if resp.ok else []
        results["ollama"] = {
            "ok": resp.status_code == 200,
            "message": f"Connected — {len(models)} model(s) available" if resp.ok else f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        results["ollama"] = {"ok": False, "message": str(exc)}

    # Polymarket gamma API (markets endpoint — reliable public endpoint)
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 1},
            timeout=10,
        )
        results["polymarket"] = {
            "ok": resp.status_code == 200,
            "message": "Connected" if resp.ok else f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        results["polymarket"] = {"ok": False, "message": str(exc)}

    return jsonify(results)


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=6060, debug=False)
