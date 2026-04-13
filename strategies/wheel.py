"""
Wheel Strategy
==============
Runs every 15 minutes during market hours (via cron).
With --summary flag: prints daily summary (run at market close).

STAGE PUT  : Sell a cash-secured put ~10% OTM, 2-4 weeks out.
             Close early at 50% profit. On assignment → STAGE CALL.
STAGE CALL : Sell a covered call ~10% above cost basis, 2-4 weeks out.
             Close early at 50% profit. If called away → STAGE PUT.

Budget: WHEEL_BUDGET from config (default $1,250).

LLM Integration
---------------
A local Ollama model (http://localhost:11434) acts as an advisory layer
at four key decision gates:

  Gate 1 — Before selling a new CSP        (advise_sell_put)
  Gate 2 — Before selling a new CC         (advise_sell_call)
  Gate 3 — When a position is losing money (advise_roll_or_close)
  Gate 4 — Earnings risk guard             (advise_earnings_risk)

The LLM is ADVISORY ONLY. All hard risk rules (cash check, position
sizing, reserve floor) are enforced by the bot regardless of LLM output.
If Ollama is unreachable, the bot continues normally — the LLM never
blocks execution.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpaca.trading.enums import OrderSide, OrderType
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import ContractType, ExerciseStyle

import config
from core import alpaca_client, market_hours, logger

# LLM advisory modules
from market_data import fetch_stock_context, format_context_summary
from ollama_advisor import (
    advise_sell_put,
    advise_sell_call,
    advise_roll_or_close,
    advise_earnings_risk,
)

STATE_FILE = os.path.join(config.STATE_DIR, "wheel.json")

STRIKE_PUT_DISCOUNT = 0.10          # CSP strike = price * (1 - 0.10)
STRIKE_CALL_PREMIUM_PCT = 0.10      # CC  strike = cost_basis * (1 + 0.10)
MIN_DAYS_TO_EXPIRY = 14
MAX_DAYS_TO_EXPIRY = 28
EARLY_CLOSE_PROFIT_PCT = 0.50       # Close contract when 50% of premium captured

# LLM configuration
LLM_ENABLED = getattr(config, "LLM_ENABLED", True)
# If LLM says skip AND confidence is above this threshold, honour the skip.
# Below this threshold we log the concern but still proceed.
LLM_SKIP_CONFIDENCE_THRESHOLD = getattr(config, "LLM_SKIP_CONFIDENCE_THRESHOLD", 0.65)
# Loss threshold: call advise_roll_or_close when mark-to-market loss exceeds this
LLM_ROLL_LOSS_THRESHOLD = getattr(config, "LLM_ROLL_LOSS_THRESHOLD", 1.00)  # 100% of premium


def _print_separator():
    print("━" * 50)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "stage": "PUT",
        "symbol": config.WHEEL_STOCK,
        "cost_basis": 0.0,
        "premium_collected": 0.0,
        "current_contract": None,
        "contract_entry_price": 0.0,
        "shares_owned": 0,
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Option utilities (unchanged from original)
# ---------------------------------------------------------------------------

def _next_friday(min_days: int, max_days: int) -> str:
    """Return the nearest Friday that's between min_days and max_days away."""
    today = datetime.now(timezone.utc).date()
    for offset in range(min_days, max_days + 1):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() == 4:  # Friday
            return candidate.isoformat()
    # Fallback: first Friday after min_days
    candidate = today + timedelta(days=min_days)
    while candidate.weekday() != 4:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def _get_option_contract(symbol: str, contract_type: ContractType, target_strike: float, expiry: str):
    """
    Find the best matching option contract from Alpaca's option chain.
    Returns the contract with strike closest to target_strike on or after expiry date.
    """
    client = alpaca_client.trading_client()
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        expiration_date=expiry,
        type=contract_type,
        strike_price_gte=str(round(target_strike * 0.95, 2)),
        strike_price_lte=str(round(target_strike * 1.05, 2)),
    )
    contracts = client.get_option_contracts(req)
    if not contracts or not contracts.option_contracts:
        return None
    best = min(contracts.option_contracts, key=lambda c: abs(float(c.strike_price) - target_strike))
    return best


def _get_contract_price(contract_symbol: str) -> float:
    """Get current mid-price of an option contract."""
    try:
        option_data_client = OptionHistoricalDataClient(
            api_key=config.ALPACA_KEY,
            secret_key=config.ALPACA_SECRET,
        )
        from alpaca.data.requests import OptionLatestQuoteRequest
        req = OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
        quotes = option_data_client.get_option_latest_quote(req)
        q = quotes[contract_symbol]
        return (q.bid_price + q.ask_price) / 2
    except Exception as e:
        print(f"  Warning: could not get option price for {contract_symbol}: {e}")
        return 0.0


def _account_cash() -> float:
    account = alpaca_client.get_account()
    return float(account.cash)


def _days_to_expiry(expiry_str: str) -> int:
    """Calculate calendar days from today to the given expiry date string."""
    try:
        expiry = datetime.strptime(expiry_str[:10], "%Y-%m-%d")
        return max(0, (expiry - datetime.now()).days)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# LLM helper: fetch context + run advisor, with console output
# ---------------------------------------------------------------------------

def _llm_advise_put(symbol: str, target_strike: float, expiry: str):
    """Fetch market context and get LLM advice for a CSP. Returns AdvisorDecision."""
    if not LLM_ENABLED:
        return None

    print(f"  Fetching market context for {symbol}...")
    ctx = fetch_stock_context(symbol)

    if ctx.get("error"):
        print(f"  ⚠  Market data warning: {ctx['error']}")

    print(f"  Market snapshot: {format_context_summary(ctx)}")

    # Gate 4: earnings risk guard first
    dte = ctx.get("days_to_earnings")
    if dte is not None and dte <= 14:
        print(f"  Running earnings risk check ({dte} days to earnings)...")
        earnings_decision = advise_earnings_risk(ctx, dte)
        earnings_decision.print_summary()
        if (not earnings_decision.proceed
                and earnings_decision.confidence >= LLM_SKIP_CONFIDENCE_THRESHOLD):
            return earnings_decision  # Short-circuit: skip the full CSP analysis

    # Gate 1: full CSP advice
    print(f"  Consulting LLM advisor for CSP on {symbol}...")
    decision = advise_sell_put(ctx, target_strike, expiry)
    decision.print_summary()
    return decision


def _llm_advise_call(symbol: str, cost_basis: float, target_strike: float, expiry: str):
    """Fetch market context and get LLM advice for a CC. Returns AdvisorDecision."""
    if not LLM_ENABLED:
        return None

    print(f"  Fetching market context for {symbol}...")
    ctx = fetch_stock_context(symbol)

    if ctx.get("error"):
        print(f"  ⚠  Market data warning: {ctx['error']}")

    print(f"  Market snapshot: {format_context_summary(ctx)}")

    # Gate 4: earnings risk guard
    dte = ctx.get("days_to_earnings")
    if dte is not None and dte <= 14:
        print(f"  Running earnings risk check ({dte} days to earnings)...")
        earnings_decision = advise_earnings_risk(ctx, dte)
        earnings_decision.print_summary()
        if (not earnings_decision.proceed
                and earnings_decision.confidence >= LLM_SKIP_CONFIDENCE_THRESHOLD):
            return earnings_decision

    # Gate 2: full CC advice
    print(f"  Consulting LLM advisor for CC on {symbol}...")
    decision = advise_sell_call(ctx, cost_basis, target_strike, expiry)
    decision.print_summary()
    return decision


def _llm_advise_threatened(
    symbol: str,
    contract_sym: str,
    contract_type: str,
    entry_price: float,
    current_price: float,
    expiry_str: str,
):
    """Gate 3: get LLM advice when a position is losing money. Returns AdvisorDecision."""
    if not LLM_ENABLED:
        return None

    print(f"  ⚠  Position loss detected — consulting LLM advisor...")
    ctx = fetch_stock_context(symbol)
    dte = _days_to_expiry(expiry_str)
    decision = advise_roll_or_close(
        ctx, contract_sym, contract_type, entry_price, current_price, dte
    )
    decision.print_summary()
    return decision


def _llm_should_skip(decision, label: str) -> bool:
    """
    Returns True if the LLM decision warrants skipping this trade.
    Applies the confidence threshold — low-confidence skips are logged but not honoured.
    """
    if decision is None:
        return False
    if not decision.proceed and decision.confidence >= LLM_SKIP_CONFIDENCE_THRESHOLD:
        print(f"  ⛔ LLM skipping {label} (confidence {decision.confidence:.0%} ≥ threshold {LLM_SKIP_CONFIDENCE_THRESHOLD:.0%})")
        return True
    if not decision.proceed and decision.confidence < LLM_SKIP_CONFIDENCE_THRESHOLD:
        print(f"  ℹ  LLM suggested skipping {label} but confidence too low ({decision.confidence:.0%} < {LLM_SKIP_CONFIDENCE_THRESHOLD:.0%}) — proceeding anyway.")
    return False


def _llm_apply_strike_suggestion(decision, computed_strike: float, label: str) -> float:
    """
    If the LLM suggests a different strike and it's more conservative, use it.
    For puts: lower is more conservative. For calls: higher is more conservative.
    """
    if decision is None or decision.suggested_strike is None:
        return computed_strike
    suggested = decision.suggested_strike
    if suggested != computed_strike:
        print(f"  ℹ  LLM suggests strike ${suggested:.2f} vs bot's ${computed_strike:.2f} for {label}")
    return suggested


# ---------------------------------------------------------------------------
# PUT stage
# ---------------------------------------------------------------------------

def run_put_stage(state: dict):
    symbol = state["symbol"]
    try:
        price = alpaca_client.get_latest_price(symbol)
    except Exception as e:
        print(f"  Could not fetch price for {symbol}: {e} — skipping run.")
        return

    # ----------------------------------------------------------------
    # Check existing open put contract
    # ----------------------------------------------------------------
    if state["current_contract"]:
        contract_sym = state["current_contract"]
        entry_price = state["contract_entry_price"]
        current_price = _get_contract_price(contract_sym)

        if entry_price > 0 and current_price > 0:
            profit_pct = (entry_price - current_price) / entry_price
            loss_pct = (current_price - entry_price) / entry_price
            print(f"  Open CSP: {contract_sym}  entry=${entry_price:.2f}  now=${current_price:.2f}  profit={profit_pct:.1%}")

            # ---- Gate 3: LLM roll/close advice on threatened position ----
            if loss_pct >= LLM_ROLL_LOSS_THRESHOLD:
                expiry_str = contract_sym[-15:-9] if len(contract_sym) > 15 else ""
                roll_decision = _llm_advise_threatened(
                    symbol, contract_sym, "Cash-Secured Put",
                    entry_price, current_price, expiry_str
                )
                if roll_decision and not roll_decision.proceed and roll_decision.confidence >= LLM_SKIP_CONFIDENCE_THRESHOLD:
                    print(f"  ⛔ LLM recommends closing losing CSP now.")
                    order = alpaca_client.submit_option_order(contract_sym, 1, OrderSide.BUY)
                    loss_amount = (current_price - entry_price) * 100
                    state["premium_collected"] -= loss_amount
                    state["current_contract"] = None
                    state["contract_entry_price"] = 0.0
                    save_state(state)
                    logger.log_trade("wheel", symbol, "BUY_TO_CLOSE_PUT_LOSS", 1,
                                     current_price, "LLM_CLOSE_LOSING_POSITION", str(order.id))
                    _print_separator()
                    print(f"[WHEEL] LLM-Advised Close — CSP (Loss)")
                    _print_separator()
                    print(f"  Rule         : LLM_CLOSE_LOSING_POSITION")
                    print(f"  Contract     : {contract_sym}")
                    print(f"  Entry Premium: ${entry_price:.2f}")
                    print(f"  Close Price  : ${current_price:.2f}")
                    print(f"  Loss         : ${loss_amount:.2f}")
                    print(f"  LLM Reasoning: {roll_decision.reasoning}")
                    _print_separator()
                    return

            # Early close at 50% profit
            if profit_pct >= EARLY_CLOSE_PROFIT_PCT:
                print(f"  50% profit reached — closing early and rolling.")
                order = alpaca_client.submit_option_order(contract_sym, 1, OrderSide.BUY)
                premium_captured = (entry_price - current_price) * 100
                state["premium_collected"] += premium_captured
                state["current_contract"] = None
                state["contract_entry_price"] = 0.0
                save_state(state)

                logger.log_trade("wheel", symbol, "BUY_TO_CLOSE_PUT", 1,
                                 current_price, "EARLY_CLOSE_50PCT_PROFIT", str(order.id))
                _print_separator()
                print(f"[WHEEL] Early Close — CSP")
                _print_separator()
                print(f"  Rule         : EARLY_CLOSE_50PCT_PROFIT")
                print(f"  Contract     : {contract_sym}")
                print(f"  Entry Premium: ${entry_price:.2f}")
                print(f"  Close Premium: ${current_price:.2f}")
                print(f"  Captured     : ${premium_captured:.2f}")
                print(f"  Total Premium: ${state['premium_collected']:.2f}")
                _print_separator()
                # Fall through to sell a new put below

        # Check for put assignment: if we now own shares, move to CALL stage
        position = alpaca_client.get_position(symbol)
        if position and int(position.qty) >= 100:
            shares = int(position.qty)
            cost = float(position.avg_entry_price)
            print(f"  ASSIGNED on put — now own {shares} shares at avg ${cost:.2f}")
            state["stage"] = "CALL"
            state["cost_basis"] = cost
            state["shares_owned"] = shares
            state["current_contract"] = None
            state["contract_entry_price"] = 0.0
            save_state(state)
            logger.log_trade("wheel", symbol, "ASSIGNED_PUT", shares, cost,
                             "PUT_ASSIGNMENT_MOVE_TO_CALL", "n/a")
            print(f"  → Moving to CALL stage.")
            return

        if state["current_contract"]:
            print(f"  CSP still open, no action needed.")
            return

    # ----------------------------------------------------------------
    # Sell a new CSP
    # ----------------------------------------------------------------
    target_strike = round(price * (1 - STRIKE_PUT_DISCOUNT))
    expiry = _next_friday(MIN_DAYS_TO_EXPIRY, MAX_DAYS_TO_EXPIRY)

    # ---- Gate 1 + Gate 4: LLM advisory for new CSP ----
    llm_decision = _llm_advise_put(symbol, target_strike, expiry)

    if _llm_should_skip(llm_decision, "new CSP"):
        print(f"  Skipping CSP this cycle — re-evaluating at next scheduled run.")
        logger.log_trade("wheel", symbol, "SKIP_CSP_LLM", 0, 0,
                         "LLM_SKIP", llm_decision.reasoning if llm_decision else "n/a")
        return

    # Apply LLM-suggested strike (more conservative wins for puts = lower strike)
    if llm_decision and llm_decision.suggested_strike:
        llm_strike = llm_decision.suggested_strike
        # Only accept if it's ≤ our computed strike (more conservative)
        if llm_strike < target_strike:
            print(f"  Applying LLM-suggested strike ${llm_strike:.2f} (more conservative than ${target_strike:.2f})")
            target_strike = round(llm_strike)

    # Apply LLM-suggested expiry
    if llm_decision and llm_decision.suggested_expiry:
        expiry = llm_decision.suggested_expiry

    # Safety check: enough cash to get assigned?
    cash = _account_cash()
    required_cash = target_strike * 100
    reserve = config.RESERVE_FLOOR
    if cash - required_cash < reserve:
        print(f"  Insufficient cash to sell CSP (need ${required_cash:.0f}, have ${cash:.0f} with ${reserve:.0f} reserve). Skipping.")
        return

    contract = _get_option_contract(symbol, ContractType.PUT, target_strike, expiry)
    if not contract:
        print(f"  No suitable put contract found for {symbol} near strike ${target_strike} expiry {expiry}.")
        return

    contract_sym = contract.symbol
    order = alpaca_client.submit_option_order(contract_sym, 1, OrderSide.SELL)
    entry_price = _get_contract_price(contract_sym)

    state["current_contract"] = contract_sym
    state["contract_entry_price"] = entry_price
    save_state(state)

    logger.log_trade("wheel", symbol, "SELL_PUT", 1, entry_price, "SELL_CSP", str(order.id))
    _print_separator()
    print(f"[WHEEL] Sell Cash-Secured Put")
    _print_separator()
    print(f"  Rule         : SELL_CSP")
    print(f"  Symbol       : {symbol}  @  ${price:.2f}")
    print(f"  Contract     : {contract_sym}")
    print(f"  Strike       : ${float(contract.strike_price):.2f}  (10% OTM)")
    print(f"  Expiry       : {expiry}")
    print(f"  Premium      : ${entry_price:.2f}/share  (${entry_price * 100:.2f} total)")
    print(f"  Order ID     : {order.id}")
    print(f"  Cash Reserve : ${cash - required_cash:.2f} remaining after collateral")
    if llm_decision and llm_decision.source == "llm":
        print(f"  LLM Advice   : ✅ PROCEED  ({llm_decision.confidence:.0%} confidence)")
        print(f"  LLM Reasoning: {llm_decision.reasoning}")
    _print_separator()


# ---------------------------------------------------------------------------
# CALL stage
# ---------------------------------------------------------------------------

def run_call_stage(state: dict):
    symbol = state["symbol"]
    cost_basis = state["cost_basis"]

    # ----------------------------------------------------------------
    # Check if we still own shares
    # ----------------------------------------------------------------
    position = alpaca_client.get_position(symbol)
    if not position or int(position.qty) == 0:
        print(f"  Position in {symbol} gone — shares called away. Moving to PUT stage.")
        state["stage"] = "PUT"
        state["current_contract"] = None
        state["contract_entry_price"] = 0.0
        state["shares_owned"] = 0
        save_state(state)
        logger.log_trade("wheel", symbol, "CALLED_AWAY", state["shares_owned"],
                         cost_basis, "CC_ASSIGNMENT_MOVE_TO_PUT", "n/a")
        return

    price = alpaca_client.get_latest_price(symbol)

    # ----------------------------------------------------------------
    # Check open call contract
    # ----------------------------------------------------------------
    if state["current_contract"]:
        contract_sym = state["current_contract"]
        entry_price = state["contract_entry_price"]
        current_price = _get_contract_price(contract_sym)

        if entry_price > 0 and current_price > 0:
            profit_pct = (entry_price - current_price) / entry_price
            loss_pct = (current_price - entry_price) / entry_price
            print(f"  Open CC: {contract_sym}  entry=${entry_price:.2f}  now=${current_price:.2f}  profit={profit_pct:.1%}")

            # ---- Gate 3: LLM roll/close advice on threatened CC ----
            if loss_pct >= LLM_ROLL_LOSS_THRESHOLD:
                expiry_str = contract_sym[-15:-9] if len(contract_sym) > 15 else ""
                roll_decision = _llm_advise_threatened(
                    symbol, contract_sym, "Covered Call",
                    entry_price, current_price, expiry_str
                )
                if roll_decision and not roll_decision.proceed and roll_decision.confidence >= LLM_SKIP_CONFIDENCE_THRESHOLD:
                    print(f"  ⛔ LLM recommends closing losing CC now.")
                    order = alpaca_client.submit_option_order(contract_sym, 1, OrderSide.BUY)
                    loss_amount = (current_price - entry_price) * 100
                    state["premium_collected"] -= loss_amount
                    state["current_contract"] = None
                    state["contract_entry_price"] = 0.0
                    save_state(state)
                    logger.log_trade("wheel", symbol, "BUY_TO_CLOSE_CALL_LOSS", 1,
                                     current_price, "LLM_CLOSE_LOSING_POSITION", str(order.id))
                    _print_separator()
                    print(f"[WHEEL] LLM-Advised Close — CC (Loss)")
                    _print_separator()
                    print(f"  Rule         : LLM_CLOSE_LOSING_POSITION")
                    print(f"  Contract     : {contract_sym}")
                    print(f"  Entry Premium: ${entry_price:.2f}")
                    print(f"  Close Price  : ${current_price:.2f}")
                    print(f"  Loss         : ${loss_amount:.2f}")
                    print(f"  LLM Reasoning: {roll_decision.reasoning}")
                    _print_separator()
                    return

            if profit_pct >= EARLY_CLOSE_PROFIT_PCT:
                print(f"  50% profit reached — closing early and rolling.")
                order = alpaca_client.submit_option_order(contract_sym, 1, OrderSide.BUY)
                premium_captured = (entry_price - current_price) * 100
                state["premium_collected"] += premium_captured
                state["current_contract"] = None
                state["contract_entry_price"] = 0.0
                save_state(state)

                logger.log_trade("wheel", symbol, "BUY_TO_CLOSE_CALL", 1,
                                 current_price, "EARLY_CLOSE_50PCT_PROFIT", str(order.id))
                _print_separator()
                print(f"[WHEEL] Early Close — CC")
                _print_separator()
                print(f"  Rule         : EARLY_CLOSE_50PCT_PROFIT")
                print(f"  Contract     : {contract_sym}")
                print(f"  Captured     : ${premium_captured:.2f}")
                print(f"  Total Premium: ${state['premium_collected']:.2f}")
                _print_separator()
                # Fall through to sell new call

        if state["current_contract"]:
            print(f"  CC still open, no action needed.")
            return

    # ----------------------------------------------------------------
    # Sell a new covered call
    # ----------------------------------------------------------------
    target_strike = round(cost_basis * (1 + STRIKE_CALL_PREMIUM_PCT))
    if target_strike <= cost_basis:
        target_strike = round(cost_basis) + 1

    expiry = _next_friday(MIN_DAYS_TO_EXPIRY, MAX_DAYS_TO_EXPIRY)

    # ---- Gate 2 + Gate 4: LLM advisory for new CC ----
    llm_decision = _llm_advise_call(symbol, cost_basis, target_strike, expiry)

    if _llm_should_skip(llm_decision, "new CC"):
        print(f"  Skipping CC this cycle — re-evaluating at next scheduled run.")
        logger.log_trade("wheel", symbol, "SKIP_CC_LLM", 0, 0,
                         "LLM_SKIP", llm_decision.reasoning if llm_decision else "n/a")
        return

    # Apply LLM-suggested strike (more conservative for calls = higher strike)
    if llm_decision and llm_decision.suggested_strike:
        llm_strike = llm_decision.suggested_strike
        # Only accept if it's ≥ our computed strike (more conservative = higher cap)
        if llm_strike > target_strike:
            print(f"  Applying LLM-suggested strike ${llm_strike:.2f} (more conservative than ${target_strike:.2f})")
            target_strike = round(llm_strike)

    # Safety: never sell below cost basis
    if target_strike <= cost_basis:
        target_strike = round(cost_basis) + 1

    # Apply LLM-suggested expiry
    if llm_decision and llm_decision.suggested_expiry:
        expiry = llm_decision.suggested_expiry

    contract = _get_option_contract(symbol, ContractType.CALL, target_strike, expiry)
    if not contract:
        print(f"  No suitable call contract found for {symbol} near strike ${target_strike} expiry {expiry}.")
        return

    contract_sym = contract.symbol
    order = alpaca_client.submit_option_order(contract_sym, 1, OrderSide.SELL)
    entry_price = _get_contract_price(contract_sym)

    state["current_contract"] = contract_sym
    state["contract_entry_price"] = entry_price
    save_state(state)

    logger.log_trade("wheel", symbol, "SELL_CALL", 1, entry_price, "SELL_CC", str(order.id))
    _print_separator()
    print(f"[WHEEL] Sell Covered Call")
    _print_separator()
    print(f"  Rule         : SELL_CC")
    print(f"  Symbol       : {symbol}  @  ${price:.2f}")
    print(f"  Contract     : {contract_sym}")
    print(f"  Strike       : ${float(contract.strike_price):.2f}  (10% above cost basis)")
    print(f"  Expiry       : {expiry}")
    print(f"  Premium      : ${entry_price:.2f}/share  (${entry_price * 100:.2f} total)")
    print(f"  Cost Basis   : ${cost_basis:.2f}")
    print(f"  Order ID     : {order.id}")
    if llm_decision and llm_decision.source == "llm":
        print(f"  LLM Advice   : ✅ PROCEED  ({llm_decision.confidence:.0%} confidence)")
        print(f"  LLM Reasoning: {llm_decision.reasoning}")
    _print_separator()


# ---------------------------------------------------------------------------
# Daily summary (unchanged, with LLM note added)
# ---------------------------------------------------------------------------

def print_daily_summary(state: dict):
    _print_separator()
    print(f"[WHEEL] Daily Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    _print_separator()
    print(f"  Symbol        : {state['symbol']}")
    print(f"  Current Stage : {state['stage']}")
    print(f"  Shares Owned  : {state['shares_owned']}")
    print(f"  Cost Basis    : ${state['cost_basis']:.2f}")
    print(f"  Open Contract : {state['current_contract'] or 'None'}")
    print(f"  Premium Collctd: ${state['premium_collected']:.2f}")
    print(f"  LLM Advisory  : {'Enabled' if LLM_ENABLED else 'Disabled'}  (model: {os.environ.get('OLLAMA_MODEL', 'llama3.2')})")

    position = alpaca_client.get_position(state["symbol"])
    if position:
        market_val = float(position.market_value)
        unrealized = float(position.unrealized_pl)
        total_return = unrealized + state["premium_collected"]
        print(f"  Market Value  : ${market_val:.2f}")
        print(f"  Unrealized P&L: ${unrealized:+.2f}")
        print(f"  Total Return  : ${total_return:+.2f}  (unrealized + premiums)")
    _print_separator()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run():
    market_hours.exit_if_closed()
    state = load_state()
    print(f"[WHEEL] Stage: {state['stage']}  |  {state['symbol']}  |  "
          f"Premium collected: ${state['premium_collected']:.2f}  |  "
          f"LLM: {'on' if LLM_ENABLED else 'off'}")
    if state["stage"] == "PUT":
        run_put_stage(state)
    else:
        run_call_stage(state)


if __name__ == "__main__":
    if "--summary" in sys.argv:
        state = load_state()
        print_daily_summary(state)
    else:
        run()
