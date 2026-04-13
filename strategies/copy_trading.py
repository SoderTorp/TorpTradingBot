"""
Politician Copy Trading
=======================
Two entry points (driven by cron):

  python copy_trading.py            — every 30 min: scan for new politician
                                      trades and mirror them with full risk gates.
  python copy_trading.py --monitor  — every 5 min: check open positions against
                                      stop loss, trailing stop, and ladder targets.

Risk controls applied to every new trade:
  Gate 1: Wheel strategy ticker overlap check
  Gate 2: Disclosure age ≤ COPY_TRADE_DISCLOSURE_AGE_MAX days
  Gate 3: Max concurrent open positions (COPY_TRADE_MAX_CONCURRENT)
  Gate 4: Total allocation cap (COPY_TRADE_MAX_ALLOCATION_PCT %)
  Gate 5: Minimum cash buffer (15% buying power)

Position management (monitor mode):
  Hard stop loss  : close at -COPY_TRADE_STOP_LOSS_PCT %
  Trailing stop   : activates at +COPY_TRADE_TRAILING_TRIGGER_PCT %,
                    trails by COPY_TRADE_TRAILING_DISTANCE_PCT % from high
  Profit ladder   : sell 1/3 of position at each COPY_TRADE_LADDER_TARGETS level
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, date, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from alpaca.trading.enums import OrderSide

import config
from core import alpaca_client, market_hours, logger

STATE_FILE = os.path.join(config.STATE_DIR, "copy_trading.json")

QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"
QUIVER_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_separator():
    print("━" * 45)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "followed_politician": None,
        "followed_politician_id": None,
        "copy_trades": [],
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _fetch_quiver_trades() -> list[dict]:
    """Fetch recent congressional trades from the QuiverQuant public API."""
    try:
        resp = requests.get(QUIVER_URL, headers=QUIVER_HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  HTTP error fetching congressional trades: {e}")
        return []


def _parse_date(date_str: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    # Try ISO with time component
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except Exception:
        return None


# ── Politician selection ──────────────────────────────────────────────────────

_TX_MAP = {
    "purchase": "BUY",
    "sale (full)": "SELL",
    "sale (partial)": "SELL",
    "sold": "SELL",
    "sell": "SELL",
}


def select_top_politician() -> tuple[str, str] | tuple[None, None]:
    """
    Pick the most actively trading politician from QuiverQuant congressional data.
    Returns (name, bioguide_id).
    """
    print("  Selecting top politician from QuiverQuant...")
    trades = _fetch_quiver_trades()
    if not trades:
        return None, None

    counts: Counter = Counter()
    ids: dict[str, str] = {}
    for t in trades:
        if (t.get("TickerType") or "Stock").upper() not in ("STOCK", ""):
            continue
        name = (t.get("Representative") or "").strip()
        bio_id = (t.get("BioGuideID") or "").strip()
        if name and bio_id:
            counts[bio_id] += 1
            ids[bio_id] = name

    if not counts:
        return None, None

    best_id, _ = counts.most_common(1)[0]
    best_name = ids[best_id]
    print(f"  Selected: {best_name} ({best_id})")
    return best_name, best_id


def fetch_recent_trades(bioguide_id: str) -> list[dict]:
    """Fetch recent stock trades for a politician from QuiverQuant and return normalised trade dicts."""
    all_trades = _fetch_quiver_trades()
    trades = []

    for t in all_trades:
        if (t.get("BioGuideID") or "").strip() != bioguide_id:
            continue
        if (t.get("TickerType") or "Stock").upper() not in ("STOCK", ""):
            continue
        ticker = (t.get("Ticker") or "").strip().upper()
        transaction = _TX_MAP.get((t.get("Transaction") or "").strip().lower())
        if not ticker or not transaction:
            continue

        disclosed = t.get("ReportDate") or t.get("TransactionDate") or ""
        amount_range = t.get("Range") or ""
        trade_id = f"{bioguide_id}_{ticker}_{transaction}_{disclosed}".replace(" ", "_")

        trades.append({
            "trade_id": trade_id,
            "ticker": ticker,
            "asset_type": "stock",
            "transaction": transaction,
            "amount_range": amount_range,
            "disclosed_date": disclosed,
        })

    return trades


# ── Risk gates ────────────────────────────────────────────────────────────────

def _wheel_symbol() -> str | None:
    wheel_state = os.path.join(config.STATE_DIR, "wheel.json")
    if not os.path.exists(wheel_state):
        return None
    with open(wheel_state) as f:
        return json.load(f).get("symbol")


def _current_allocation_pct(open_trades: list[dict], account_value: float) -> float:
    """Sum the current market value of all open copy trade positions as % of account."""
    total = 0.0
    for t in open_trades:
        if t["status"] != "OPEN":
            continue
        try:
            price = alpaca_client.get_latest_price(t["ticker"])
            total += price * t["shares"]
        except Exception:
            total += t["entry_price"] * t["shares"]  # fallback to entry
    return (total / account_value * 100) if account_value else 0.0


def check_all_gates(
    ticker: str,
    disclosed_date: str,
    open_trades: list[dict],
    account_value: float,
    cash: float,
) -> tuple[bool, str]:
    """Run all pre-execution gates. Returns (pass, reason)."""

    # Gate 1: Wheel ticker overlap
    wheel_sym = _wheel_symbol()
    if wheel_sym and ticker == wheel_sym:
        return False, f"Ticker {ticker} already in Wheel strategy"

    # Gate 2: Disclosure age
    d = _parse_date(disclosed_date)
    if d is None:
        return False, f"Could not parse disclosure date: {disclosed_date!r}"
    age_days = (date.today() - d).days
    if age_days > config.COPY_TRADE_DISCLOSURE_AGE_MAX:
        return False, f"Signal is {age_days}d old (max {config.COPY_TRADE_DISCLOSURE_AGE_MAX}d)"

    # Gate 3: Max concurrent positions
    num_open = sum(1 for t in open_trades if t["status"] == "OPEN")
    if num_open >= config.COPY_TRADE_MAX_CONCURRENT:
        return False, f"Max concurrent positions reached ({num_open}/{config.COPY_TRADE_MAX_CONCURRENT})"

    # Gate 4: Total allocation cap
    alloc_pct = _current_allocation_pct(open_trades, account_value)
    if alloc_pct >= config.COPY_TRADE_MAX_ALLOCATION_PCT:
        return False, f"Allocation cap reached ({alloc_pct:.1f}% / {config.COPY_TRADE_MAX_ALLOCATION_PCT}%)"

    # Gate 5: Minimum cash buffer (15% of account)
    if cash < account_value * 0.15:
        return False, "Insufficient cash buffer (need ≥15% buying power)"

    return True, "All gates passed"


def calculate_position_size(account_value: float, price: float, alloc_pct: float) -> int:
    """
    Conservative Kelly fraction sizing, capped at max_position_pct.
    Assumes politicians win ~52% of trades with 6% avg win / 4% avg loss.
    """
    win_rate = 0.52
    avg_win = 0.06
    avg_loss = 0.04
    kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
    kelly_conservative = max(0.01, kelly * config.COPY_TRADE_KELLY_FRACTION)

    remaining_alloc_pct = config.COPY_TRADE_MAX_ALLOCATION_PCT - alloc_pct
    size_pct = min(config.COPY_TRADE_MAX_POSITION_PCT, kelly_conservative * 100, remaining_alloc_pct)
    position_value = account_value * (size_pct / 100)
    return max(1, int(position_value / price))


def set_stops_and_targets(trade_record: dict, entry_price: float) -> dict:
    """Attach stop loss, trailing stop metadata, and ladder targets to a trade record."""
    trade_record["stop_loss_price"] = round(entry_price * (1 - config.COPY_TRADE_STOP_LOSS_PCT / 100), 4)
    trade_record["trailing_stop_activated"] = False
    trade_record["trailing_trigger_price"] = round(entry_price * (1 + config.COPY_TRADE_TRAILING_TRIGGER_PCT / 100), 4)
    trade_record["highest_price"] = entry_price
    trade_record["ladder_targets"] = [
        round(entry_price * (1 + pct / 100), 4)
        for pct in config.COPY_TRADE_LADDER_TARGETS
    ]
    trade_record["ladder_filled"] = [False] * len(config.COPY_TRADE_LADDER_TARGETS)
    return trade_record


# ── Trade execution ───────────────────────────────────────────────────────────

def mirror_trade(trade: dict, politician_name: str, state: dict) -> bool:
    """Mirror a politician trade with full risk controls. Returns True if placed."""
    ticker = trade["ticker"]
    transaction = trade["transaction"]
    open_trades = state["copy_trades"]

    account = alpaca_client.get_account()
    account_value = float(account.equity)
    cash = float(account.cash)

    # Run gates
    passed, reason = check_all_gates(
        ticker=ticker,
        disclosed_date=trade["disclosed_date"],
        open_trades=open_trades,
        account_value=account_value,
        cash=cash,
    )
    if not passed:
        print(f"  [GATE BLOCKED] {ticker}: {reason}")
        return False

    try:
        price = alpaca_client.get_latest_price(ticker)
    except Exception as e:
        print(f"  Could not get price for {ticker}: {e} — skipping.")
        return False

    if price <= 0:
        return False

    side = OrderSide.BUY if transaction == "BUY" else OrderSide.SELL

    if side == OrderSide.BUY:
        alloc_pct = _current_allocation_pct(open_trades, account_value)
        qty = calculate_position_size(account_value, price, alloc_pct)
        if cash - (qty * price) < config.RESERVE_FLOOR:
            print(f"  Reserve floor would be breached — skipping {ticker}.")
            return False
    else:
        position = alpaca_client.get_position(ticker)
        if not position:
            print(f"  SELL signal for {ticker} but no position held — skipping.")
            return False
        qty = int(position.qty)

    order = alpaca_client.submit_market_order(ticker, qty, side)
    filled_price = float(order.filled_avg_price) if order.filled_avg_price else price
    d = _parse_date(trade["disclosed_date"])
    age_days = (date.today() - d).days if d else 0
    rule = f"COPY_{politician_name.upper().replace(' ', '_')}"

    logger.log_trade("copy_trading", ticker, transaction, qty, filled_price, rule, str(order.id))

    if side == OrderSide.BUY:
        trade_record = {
            "trade_id": trade["trade_id"],
            "ticker": ticker,
            "action": transaction,
            "shares": qty,
            "entry_price": filled_price,
            "entry_timestamp": datetime.now(timezone.utc).isoformat(),
            "disclosure_date": trade["disclosed_date"],
            "disclosure_age_days": age_days,
            "signal_source": "CapitolTrades",
            "status": "OPEN",
            "realized_pnl": None,
        }
        trade_record = set_stops_and_targets(trade_record, filled_price)
        state["copy_trades"].append(trade_record)

    _print_separator()
    print(f"[COPY TRADE] Mirroring {politician_name}")
    _print_separator()
    print(f"  Rule            : {rule}")
    print(f"  Action          : {transaction} {qty} share(s) of {ticker}")
    print(f"  Entry Price     : ${filled_price:.2f}")
    if side == OrderSide.BUY:
        print(f"  Stop Loss       : ${trade_record['stop_loss_price']:.2f}  (-{config.COPY_TRADE_STOP_LOSS_PCT}%)")
        targets = [f"${t:.2f}" for t in trade_record["ladder_targets"]]
        print(f"  Ladder Targets  : {' / '.join(targets)}")
    print(f"  Disclosed       : {trade['disclosed_date']}  ({age_days}d ago)")
    print(f"  Order ID        : {order.id}")
    _print_separator()
    return True


# ── Position monitor (5-min cron) ─────────────────────────────────────────────

def monitor_open_trades():
    """
    Check every open copy trade against its stop loss, trailing stop,
    and profit ladder. Called every 5 minutes during market hours.
    """
    market_hours.exit_if_closed()
    state = load_state()
    open_trades = [t for t in state["copy_trades"] if t["status"] == "OPEN"]

    if not open_trades:
        print("[COPY MONITOR] No open positions.")
        return

    print(f"[COPY MONITOR] Checking {len(open_trades)} open position(s)...")
    changed = False

    for trade in open_trades:
        ticker = trade["ticker"]
        try:
            price = alpaca_client.get_latest_price(ticker)
        except Exception as e:
            print(f"  Could not get price for {ticker}: {e}")
            continue

        entry = trade["entry_price"]
        qty = trade["shares"]
        pct_chg = (price - entry) / entry * 100
        print(f"  {ticker}: ${price:.2f}  ({pct_chg:+.2f}%)  stop=${trade['stop_loss_price']:.2f}")

        # ---- Hard stop loss ----
        if price <= trade["stop_loss_price"]:
            order = alpaca_client.submit_market_order(ticker, qty, OrderSide.SELL)
            filled = float(order.filled_avg_price) if order.filled_avg_price else price
            trade["status"] = "CLOSED"
            trade["realized_pnl"] = round((filled - entry) * qty, 2)
            trade["close_timestamp"] = datetime.now(timezone.utc).isoformat()
            logger.log_trade("copy_trading", ticker, "SELL", qty, filled,
                             f"STOP_LOSS_{config.COPY_TRADE_STOP_LOSS_PCT}PCT", str(order.id))
            print(f"  ⛔ Stop loss triggered on {ticker}  P&L: ${trade['realized_pnl']:+.2f}")
            changed = True
            continue

        # ---- Trailing stop activation ----
        if not trade["trailing_stop_activated"] and price >= trade["trailing_trigger_price"]:
            trade["trailing_stop_activated"] = True
            print(f"  ↑ Trailing stop activated on {ticker} at ${price:.2f}")
            changed = True

        # ---- Update high-water mark ----
        if price > trade.get("highest_price", entry):
            trade["highest_price"] = price
            changed = True

        # ---- Trailing stop check ----
        if trade["trailing_stop_activated"]:
            trail_stop = trade["highest_price"] * (1 - config.COPY_TRADE_TRAILING_DISTANCE_PCT / 100)
            if price <= trail_stop:
                order = alpaca_client.submit_market_order(ticker, qty, OrderSide.SELL)
                filled = float(order.filled_avg_price) if order.filled_avg_price else price
                trade["status"] = "CLOSED"
                trade["realized_pnl"] = round((filled - entry) * qty, 2)
                trade["close_timestamp"] = datetime.now(timezone.utc).isoformat()
                logger.log_trade("copy_trading", ticker, "SELL", qty, filled,
                                 "TRAILING_STOP", str(order.id))
                print(f"  ↓ Trailing stop hit on {ticker}  P&L: ${trade['realized_pnl']:+.2f}")
                changed = True
                continue

        # ---- Profit ladder ----
        for i, target in enumerate(trade.get("ladder_targets", [])):
            if trade["ladder_filled"][i] or price < target:
                continue
            ladder_qty = max(1, qty // 3)
            order = alpaca_client.submit_market_order(ticker, ladder_qty, OrderSide.SELL)
            filled = float(order.filled_avg_price) if order.filled_avg_price else price
            pnl = round((filled - entry) * ladder_qty, 2)
            rule = f"LADDER_{i + 1}_+{config.COPY_TRADE_LADDER_TARGETS[i]}PCT"
            logger.log_trade("copy_trading", ticker, "SELL", ladder_qty, filled, rule, str(order.id))
            trade["ladder_filled"][i] = True
            trade["shares"] = qty - ladder_qty
            print(f"  🪜 Ladder {i + 1} hit on {ticker} @ ${filled:.2f}  P&L: ${pnl:+.2f}")
            changed = True

    if changed:
        save_state(state)


# ── Main scan (30-min cron) ───────────────────────────────────────────────────

def run():
    market_hours.exit_if_closed()
    state = load_state()

    # Auto-select politician
    if not state["followed_politician_id"]:
        name, slug = select_top_politician()
        if not slug:
            print("Could not determine top politician. Retrying next run.")
            return
        state["followed_politician"] = name
        state["followed_politician_id"] = slug
        save_state(state)

    politician_name = state["followed_politician"]
    politician_slug = state["followed_politician_id"]
    print(f"[COPY TRADE] Following: {politician_name}")

    trades = fetch_recent_trades(politician_slug)
    if not trades:
        print(f"  No trades found for {politician_name}.")
        return

    executed_ids = {t["trade_id"] for t in state["copy_trades"]}
    new_trades = [t for t in trades if t["trade_id"] not in executed_ids]
    print(f"  Found {len(trades)} total, {len(new_trades)} new.")

    if not new_trades:
        print("  Nothing new to mirror.")
        return

    for trade in new_trades:
        mirror_trade(trade, politician_name, state)
        # Mark seen even if gate-blocked (avoid infinite retry on old signals)
        if trade["trade_id"] not in {t["trade_id"] for t in state["copy_trades"]}:
            state["copy_trades"].append({
                "trade_id": trade["trade_id"],
                "ticker": trade["ticker"],
                "status": "SKIPPED",
            })

    save_state(state)


if __name__ == "__main__":
    if "--monitor" in sys.argv:
        monitor_open_trades()
    else:
        run()
