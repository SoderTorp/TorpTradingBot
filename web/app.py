"""
TradingBot Web UI
=================
Minimal Flask app serving a live trade log viewer on port 6060.

Routes:
  GET /              — HTML dashboard
  GET /api/trades    — today's trades as JSON (polled every 5s by the UI)
  GET /api/state     — current state of all three strategies as JSON
  GET /api/account   — live Alpaca account balance + P&L vs starting budget
  GET /api/log       — last N lines of cron.log as plain text
  GET /api/config    — current bot configuration (from config module)
  POST /api/config   — save new configuration to .env + reload in-process
  POST /api/test-connection — test Alpaca and Ollama connectivity
"""
import csv
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template, Response, request
import config

app = Flask(__name__)

LOG_DIR = Path(config.LOG_DIR)
STATE_DIR = Path(config.STATE_DIR)
CRON_LOG = Path(config.LOG_DIR) / "cron.log"

BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = BASE_DIR / ".env"


# ── .env helpers ─────────────────────────────────────────────────────────────

def _update_env_file(updates: dict) -> None:
    """Update specific keys in .env file, preserving comments and blank lines."""
    lines = ENV_PATH.read_text().splitlines(keepends=True) if ENV_PATH.exists() else []

    updated: set = set()
    result = []
    for line in lines:
        bare = line.lstrip()
        if bare and not bare.startswith('#') and '=' in bare:
            key = bare.split('=', 1)[0].strip()
            if key in updates:
                # Preserve any inline comment after the value
                rest = bare.split('=', 1)[1]
                comment = ''
                q = None
                for i, c in enumerate(rest):
                    if c in ('"', "'") and q is None:
                        q = c
                    elif c == q:
                        q = None
                    elif c == '#' and q is None:
                        comment = '  ' + rest[i:].rstrip('\n')
                        break
                result.append(f'{key}={updates[key]}{comment}\n')
                updated.add(key)
                continue
        result.append(line)

    # Append any keys that didn't exist in the file yet
    for k, v in updates.items():
        if k not in updated:
            result.append(f'{k}={v}\n')

    ENV_PATH.write_text(''.join(result))


# ── Trade helpers ─────────────────────────────────────────────────────────────

def _today_csv() -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return LOG_DIR / f"trades_{date_str}.csv"


def _read_trades() -> list[dict]:
    path = _today_csv()
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _read_state(name: str) -> dict:
    path = STATE_DIR / f"{name}.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _tail_log(n: int = 100) -> str:
    if not CRON_LOG.exists():
        return ""
    with open(CRON_LOG) as f:
        lines = f.readlines()
    return "".join(lines[-n:])


# ── Dashboard routes ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/trades")
def api_trades():
    trades = _read_trades()
    return jsonify({"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "trades": trades})


@app.route("/api/state")
def api_state():
    return jsonify({
        "wheel": _read_state("wheel"),
        "copy_trading": _read_state("copy_trading"),
    })


@app.route("/api/account")
def api_account():
    try:
        from core import alpaca_client
        account = alpaca_client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        portfolio_value = float(account.portfolio_value)
        starting = config.TOTAL_BUDGET
        pnl = equity - starting
        pct = (pnl / starting) * 100 if starting else 0
        return jsonify({
            "equity": equity,
            "cash": cash,
            "portfolio_value": portfolio_value,
            "starting_budget": starting,
            "pnl": pnl,
            "pct_change": pct,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log")
def api_log():
    return Response(_tail_log(150), mimetype="text/plain")


# ── Config routes ─────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return current configuration values."""
    return jsonify({
        "KEY": config.ALPACA_KEY,
        "SECRET": config.ALPACA_SECRET,
        "PAPER_TRADING": "true" if config.PAPER_TRADING else "false",
        "TOTAL_BUDGET": config.TOTAL_BUDGET,
        "TRADING_PCT": config.TRADING_PCT,
        "RESERVE_PCT": config.RESERVE_PCT,
        "WHEEL_ALLOC": config.WHEEL_ALLOC,
        "WHEEL_STOCK": config.WHEEL_STOCK,
        "COPY_ALLOC": config.COPY_ALLOC,
        "COPY_TRADE_MAX_POSITION_PCT": config.COPY_TRADE_MAX_POSITION_PCT,
        "COPY_TRADE_MAX_ALLOCATION_PCT": config.COPY_TRADE_MAX_ALLOCATION_PCT,
        "COPY_TRADE_MAX_CONCURRENT": config.COPY_TRADE_MAX_CONCURRENT,
        "COPY_TRADE_DISCLOSURE_AGE_MAX": config.COPY_TRADE_DISCLOSURE_AGE_MAX,
        "COPY_TRADE_STOP_LOSS_PCT": config.COPY_TRADE_STOP_LOSS_PCT,
        "COPY_TRADE_TRAILING_TRIGGER_PCT": config.COPY_TRADE_TRAILING_TRIGGER_PCT,
        "COPY_TRADE_TRAILING_DISTANCE_PCT": config.COPY_TRADE_TRAILING_DISTANCE_PCT,
        "COPY_TRADE_KELLY_FRACTION": config.COPY_TRADE_KELLY_FRACTION,
        "COPY_TRADE_LADDER_TARGETS": json.dumps(config.COPY_TRADE_LADDER_TARGETS),
        "OLLAMA_URL": config.OLLAMA_URL,
        "OLLAMA_MODEL": config.OLLAMA_MODEL,
        "OLLAMA_TIMEOUT": config.OLLAMA_TIMEOUT,
    })


@app.route("/api/config", methods=["POST"])
def api_config_save():
    """Save configuration to .env and reload the in-process config module."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    try:
        _update_env_file(data)

        # Reload env vars into the running process
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(ENV_PATH), override=True)
        importlib.reload(config)

        # Reset Alpaca singletons so next call uses the updated credentials
        from core import alpaca_client
        alpaca_client._trading_client = None
        alpaca_client._data_client = None

        return jsonify({
            "ok": True,
            "message": "Settings saved and reloaded. Strategy cron jobs will pick up changes on next run."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    """
    Test Alpaca and Ollama connectivity using the credentials from the request body.
    Uses request values so the user can test before saving.
    """
    data = request.get_json() or {}
    results = {}

    # ── Alpaca ────────────────────────────────────────────────────────────────
    try:
        from alpaca.trading.client import TradingClient
        key = data.get("KEY", "").strip()
        secret = data.get("SECRET", "").strip()
        paper = str(data.get("PAPER_TRADING", "true")).lower() == "true"

        if not key or not secret:
            results["alpaca"] = {"ok": False, "message": "API Key or Secret is empty"}
        else:
            tc = TradingClient(api_key=key, secret_key=secret, paper=paper)
            acct = tc.get_account()
            equity = float(acct.equity)
            mode = "paper" if paper else "LIVE"
            results["alpaca"] = {
                "ok": True,
                "message": f"Connected — equity ${equity:,.2f} ({mode})"
            }
    except Exception as e:
        results["alpaca"] = {"ok": False, "message": str(e)}

    # ── Ollama ────────────────────────────────────────────────────────────────
    try:
        import requests as req
        url = data.get("OLLAMA_URL", "http://localhost:11434/api/generate").strip()
        model = data.get("OLLAMA_MODEL", "llama3.2").strip()
        timeout = min(int(data.get("OLLAMA_TIMEOUT", 60)), 30)  # cap test at 30s

        resp = req.post(
            url,
            json={"model": model, "prompt": "Reply with one word: pong", "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        results["ollama"] = {
            "ok": True,
            "message": f"Connected — model '{model}' is responding"
        }
    except Exception as e:
        results["ollama"] = {"ok": False, "message": str(e)}

    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6060, debug=False)
