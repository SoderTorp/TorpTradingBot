"""
ollama_advisor.py
=================
Sends structured prompts to a local Ollama model and parses JSON decisions.
Provides an advisory layer over the wheel strategy's four key decision gates:

  1. advise_sell_put()       — before opening a new CSP
  2. advise_sell_call()      — before opening a new CC
  3. advise_roll_or_close()  — when a position is moving against us
  4. advise_earnings_risk()  — quick guard before any new open

Design principles:
  - LLM is ADVISOR only. Code retains final veto via hard risk rules.
  - All methods return AdvisorDecision — never raise, never block.
  - On Ollama failure, defaults to proceed=True so the strategy continues.
  - Temperature=0.1 for consistent, structured output.

Configuration (override in config.py or environment):
  OLLAMA_MODEL   — default "llama3.2"
  OLLAMA_URL     — default "http://localhost:11434/api/generate"
  OLLAMA_TIMEOUT — default 300 seconds (5 minutes due to slow local LLM)

Dependencies: requests
"""

import json
import logging
import re
import requests
from dataclasses import dataclass, field
from typing import Optional

import config

log = logging.getLogger("wheel.ollama_advisor")

# ---------------------------------------------------------------------------
# Configuration — sourced from config.py / .env (edit via the web UI)
# ---------------------------------------------------------------------------

OLLAMA_URL = config.OLLAMA_URL
OLLAMA_MODEL = config.OLLAMA_MODEL
OLLAMA_TIMEOUT = config.OLLAMA_TIMEOUT

# Confidence threshold below which we log a warning regardless of proceed value
LOW_CONFIDENCE_THRESHOLD = 0.40

# JSON schema injected at the end of every prompt
_JSON_SCHEMA = """
You MUST respond with ONLY a single valid JSON object — no preamble, no markdown fences, no explanation outside the JSON.

Required schema:
{
  "proceed": true or false,
  "confidence": 0.0 to 1.0,
  "suggested_strike": number or null,
  "suggested_expiry": "YYYY-MM-DD" or null,
  "warnings": ["list of specific concerns, empty array if none"],
  "reasoning": "2-3 sentences explaining your decision"
}
"""


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class AdvisorDecision:
    """
    Structured result from every Ollama advisor call.

    Fields
    ------
    proceed           : LLM recommendation — True = open/hold, False = skip/close
    confidence        : 0.0–1.0 certainty of the recommendation
    reasoning         : Plain-English explanation (logged alongside every trade)
    suggested_strike  : LLM-recommended strike (may differ from bot's target)
    suggested_expiry  : LLM-recommended expiry date as YYYY-MM-DD string
    warnings          : List of flagged concerns (always logged, never block by themselves)
    raw_response      : Raw LLM text for debugging
    source            : "llm" | "fallback" — whether we got a real LLM response
    """
    proceed: bool = True
    confidence: float = 0.5
    reasoning: str = "No analysis available"
    suggested_strike: Optional[float] = None
    suggested_expiry: Optional[str] = None
    warnings: list = field(default_factory=list)
    raw_response: str = ""
    source: str = "llm"

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.70

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_THRESHOLD

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    # ------------------------------------------------------------------
    # Formatted output for console logs
    # ------------------------------------------------------------------

    def print_summary(self, indent: str = "  ") -> None:
        """Print formatted decision to stdout, matching wheel.py style."""
        icon = "✅" if self.proceed else "⛔"
        action = "PROCEED" if self.proceed else "SKIP"
        conf_bar = "█" * int(self.confidence * 10) + "░" * (10 - int(self.confidence * 10))
        print(f"{indent}╔══ LLM Advisor ({'Ollama/' + OLLAMA_MODEL}) ══")
        print(f"{indent}║  Decision   : {icon} {action}  (confidence {self.confidence:.0%}  [{conf_bar}])")
        print(f"{indent}║  Reasoning  : {self.reasoning}")
        if self.suggested_strike:
            print(f"{indent}║  Suggested Strike : ${self.suggested_strike:.2f}")
        if self.suggested_expiry:
            print(f"{indent}║  Suggested Expiry : {self.suggested_expiry}")
        if self.warnings:
            print(f"{indent}║  ⚠  Warnings:")
            for w in self.warnings:
                print(f"{indent}║     • {w}")
        if self.source == "fallback":
            print(f"{indent}║  ℹ  [fallback — Ollama unavailable, defaulting to proceed]")
        print(f"{indent}╚{'═' * 40}")


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> str:
    """POST to Ollama /api/generate and return the raw response text."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "top_p": 0.9,
            "num_predict": 1024,
        },
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("response", "")


def _parse_json(raw: str) -> dict:
    """
    Extract and parse JSON from LLM output.
    Handles markdown fences, leading/trailing text, and minor formatting issues.
    """
    # 1. Try direct parse
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass

    # 2. Extract first {...} block (handles leading/trailing prose)
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # 3. Strip markdown fences and retry
    cleaned = re.sub(r'```(?:json)?', '', raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    log.warning("Could not parse JSON from LLM response: %s", raw[:200])
    return {}


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fallback(reason: str, warning: str = "") -> AdvisorDecision:
    """Return a safe default when Ollama is unavailable."""
    return AdvisorDecision(
        proceed=True,
        confidence=0.5,
        reasoning=reason,
        warnings=[warning] if warning else [],
        source="fallback",
    )


def _build_context_block(ctx: dict) -> str:
    """Format the market data dict into a readable prompt section."""
    lines = [
        f"Symbol       : {ctx.get('symbol', 'N/A')}",
        f"Company      : {ctx.get('company_name', 'N/A')}",
        f"Sector       : {ctx.get('sector', 'N/A')}  |  Industry: {ctx.get('industry', 'N/A')}",
        f"Current Price: ${ctx.get('price', 'N/A')}",
    ]

    if ctx.get("week52_high") and ctx.get("week52_low"):
        lines.append(
            f"52w Range    : ${ctx['week52_low']} – ${ctx['week52_high']}"
            f"  (currently {ctx.get('pct_from_52w_high', 'N/A')}% from high)"
        )

    lines += [
        f"Returns      : 1d={ctx.get('change_1d_pct', 'N/A')}%  "
        f"5d={ctx.get('change_5d_pct', 'N/A')}%  "
        f"20d={ctx.get('change_20d_pct', 'N/A')}%",
        f"RSI-14       : {ctx.get('rsi_14', 'N/A')}",
        f"SMA-20       : ${ctx.get('sma_20', 'N/A')}  |  "
        f"SMA-50: ${ctx.get('sma_50', 'N/A')}  |  "
        f"Trend: {ctx.get('trend', 'N/A')}",
        f"Hist Vol 20d : {ctx.get('hist_volatility_20d', 'N/A')}%",
        f"Volume Ratio : {ctx.get('volume_ratio', 'N/A')}x  (today vs 20d avg)",
        f"Beta         : {ctx.get('beta', 'N/A')}  |  "
        f"P/E: {ctx.get('pe_ratio', 'N/A')}  |  "
        f"Mkt Cap: ${ctx.get('market_cap_b', 'N/A')}B",
    ]

    if ctx.get("short_float_pct") is not None:
        lines.append(f"Short Float  : {round(ctx['short_float_pct'] * 100, 1)}%")

    # Earnings — highlighted if soon
    dte = ctx.get("days_to_earnings")
    if dte is not None:
        flag = "⚠️  " if dte <= 14 else ""
        lines.append(f"Earnings     : {flag}{dte} days away  ({ctx.get('earnings_date', 'N/A')})")
    else:
        lines.append("Earnings     : Not found or >90 days away")

    # News
    news = ctx.get("recent_news", [])
    if news:
        lines.append("")
        lines.append("Recent News:")
        for n in news:
            lines.append(f"  [{n.get('published', '?')}] {n.get('title', '')}  — {n.get('publisher', '')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decision gate 1: Sell Cash-Secured Put
# ---------------------------------------------------------------------------

def advise_sell_put(ctx: dict, target_strike: float, expiry: str) -> AdvisorDecision:
    """
    Should we sell a Cash-Secured Put right now?

    Called immediately before opening a new CSP position.
    A proceed=False will cause the bot to skip this cycle and re-evaluate
    at the next scheduled run.

    Parameters
    ----------
    ctx           : market context dict from market_data.fetch_stock_context()
    target_strike : the bot's computed strike (price * 0.90)
    expiry        : the bot's computed expiry date (YYYY-MM-DD)
    """
    context_block = _build_context_block(ctx)

    prompt = f"""You are a conservative options analyst specialising in the Wheel Strategy (sell puts, hold if assigned, sell calls).

Evaluate whether selling a Cash-Secured Put (CSP) on this stock is appropriate RIGHT NOW.

── MARKET DATA ──────────────────────────────────────────────
{context_block}

── PROPOSED TRADE ───────────────────────────────────────────
  Type          : Sell Cash-Secured Put (CSP)
  Target Strike : ${target_strike:.2f}  (~10% OTM from current price)
  Target Expiry : {expiry}  (2–4 weeks out)

── DECISION CRITERIA ────────────────────────────────────────
Flag these as warnings and reduce confidence or set proceed=false if they apply:
  1. Earnings within 14 days  → high IV crush / gap-down risk
  2. RSI below 35             → oversold, potential further downside
  3. Stock down >5% in 5 days → momentum working against us
  4. SMA-20 below SMA-50      → bearish trend, underlying deteriorating
  5. Beta above 2.5           → excessive volatility for a CSP
  6. Significant negative news (not just volatility — structural risk)

If concerns exist but don't outright disqualify the trade, suggest:
  - A lower strike (more buffer, e.g., 15% OTM)
  - A later expiry (more time for sentiment to resolve)

Your role is advisory only. Hard risk rules are enforced separately.

{_JSON_SCHEMA}"""

    return _execute(prompt, default_strike=target_strike, default_expiry=expiry)


# ---------------------------------------------------------------------------
# Decision gate 2: Sell Covered Call
# ---------------------------------------------------------------------------

def advise_sell_call(
    ctx: dict,
    cost_basis: float,
    target_strike: float,
    expiry: str
) -> AdvisorDecision:
    """
    Should we sell a Covered Call right now?

    Called when the bot holds 100 shares and is about to write a CC.

    Parameters
    ----------
    ctx           : market context dict
    cost_basis    : average cost per share from put assignment
    target_strike : the bot's computed strike (cost_basis * 1.10)
    expiry        : the bot's computed expiry date (YYYY-MM-DD)
    """
    context_block = _build_context_block(ctx)
    current_price = ctx.get("price", 0)
    gain_from_basis = (
        (current_price - cost_basis) / cost_basis * 100
        if cost_basis > 0 else 0
    )

    prompt = f"""You are a conservative options analyst specialising in the Wheel Strategy.
We were assigned 100 shares when a put expired ITM. We now need to decide whether to sell a Covered Call.

── MARKET DATA ──────────────────────────────────────────────
{context_block}

── POSITION CONTEXT ─────────────────────────────────────────
  Cost Basis     : ${cost_basis:.2f}  (assigned price)
  Current Price  : ${current_price:.2f}
  Gain/Loss      : {gain_from_basis:+.1f}% from basis

── PROPOSED TRADE ───────────────────────────────────────────
  Type           : Sell Covered Call (CC)
  Target Strike  : ${target_strike:.2f}  (~10% above cost basis)
  Target Expiry  : {expiry}  (2–4 weeks)

── DECISION CRITERIA ────────────────────────────────────────
Flag these as warnings and reduce confidence or set proceed=false:
  1. Earnings within 14 days       → stock may gap up through strike, capping gains at exactly the wrong time
  2. RSI above 70                  → overbought, may want higher strike to avoid capping a continued run
  3. Price already >20% above basis → low theta income vs. opportunity cost of capping upside
  4. Significant negative news      → stock may keep falling; consider waiting before writing CC
  5. Price below or near cost basis → writing CC at a loss if called away

If concerns exist, suggest:
  - A higher strike (reduce assignment risk / cap better gains)
  - A later expiry (wait for better conditions)

{_JSON_SCHEMA}"""

    return _execute(prompt, default_strike=target_strike, default_expiry=expiry)


# ---------------------------------------------------------------------------
# Decision gate 3: Roll or Close a threatened position
# ---------------------------------------------------------------------------

def advise_roll_or_close(
    ctx: dict,
    contract_sym: str,
    contract_type: str,
    entry_price: float,
    current_price: float,
    days_to_expiry: int,
) -> AdvisorDecision:
    """
    An open position is moving against us (not at 50% profit — at a loss).
    Should we hold (let theta decay work), or close now to cut losses?

    Called when an open contract is showing a loss above a configurable threshold.

    proceed=True  → Hold — theta decay or mean reversion expected
    proceed=False → Close for a loss now — structural risk detected
    """
    context_block = _build_context_block(ctx)
    loss_pct = (
        (current_price - entry_price) / entry_price * 100
        if entry_price > 0 else 0
    )

    prompt = f"""You are a conservative options analyst specialising in the Wheel Strategy.
An open {contract_type} position is moving AGAINST us. Advise whether to hold or close early.

── MARKET DATA ──────────────────────────────────────────────
{context_block}

── CONTRACT STATUS ──────────────────────────────────────────
  Contract      : {contract_sym}
  Type          : {contract_type}
  Entry Premium : ${entry_price:.2f}  (what we received when selling)
  Current Price : ${current_price:.2f}  (what it would cost to close now)
  Mark-to-Market: {loss_pct:+.1f}%  (positive = loss, we'd pay more to close)
  Days to Expiry: {days_to_expiry}

── DECISION ─────────────────────────────────────────────────
  proceed = true   → HOLD — theta decay + mean reversion will likely help us
  proceed = false  → CLOSE NOW — structural / directional risk warrants cutting losses

Key factors:
  • With >10 days to expiry, theta decay accelerates — holding has merit unless
    there is a genuine structural reason the stock won't recover.
  • If loss_pct is > 200% and there are still many days left, closing now
    prevents further bleed.
  • Negative news about the fundamental business (not just macro) → close
  • Earnings within expiry window → high uncertainty → may close or roll out

{_JSON_SCHEMA}"""

    return _execute(prompt, default_strike=None, default_expiry=None)


# ---------------------------------------------------------------------------
# Decision gate 4: Earnings risk guard
# ---------------------------------------------------------------------------

def advise_earnings_risk(ctx: dict, days_to_earnings: int) -> AdvisorDecision:
    """
    Quick binary check: is it safe to open ANY new position given upcoming earnings?

    Called at the top of both run_put_stage and run_call_stage before any
    new position is opened.

    proceed=True  → Safe to open
    proceed=False → Earnings too close, skip this cycle
    """
    prompt = f"""You are a conservative options analyst.
A wheel strategy bot is about to open a new options position. Earnings are approaching.

── MARKET DATA ──────────────────────────────────────────────
{_build_context_block(ctx)}

── EARNINGS RISK CHECK ──────────────────────────────────────
  Days to Earnings: {days_to_earnings}

RULE:
  - proceed = false if earnings <= 10 days away  (high risk: IV crush, gap moves)
  - proceed = true  if earnings >  10 days away
  - Exception: if historical volatility is very high (>80%) the premium may
    justify the risk → consider proceed=true with low confidence and warnings

{_JSON_SCHEMA}"""

    return _execute(prompt, default_strike=None, default_expiry=None)


# ---------------------------------------------------------------------------
# Core execution helper
# ---------------------------------------------------------------------------

def _execute(
    prompt: str,
    default_strike: Optional[float],
    default_expiry: Optional[str],
) -> AdvisorDecision:
    """
    Call Ollama, parse the JSON, and return an AdvisorDecision.
    Never raises — on any failure returns a safe fallback.
    """
    try:
        raw = _call_ollama(prompt)
        log.debug("Ollama raw response: %s", raw[:500])
        data = _parse_json(raw)

        if not data:
            return AdvisorDecision(
                proceed=True,
                confidence=0.5,
                reasoning="LLM returned unparseable response — defaulting to proceed.",
                warnings=["Could not parse LLM JSON response"],
                raw_response=raw,
                source="fallback",
            )

        return AdvisorDecision(
            proceed=bool(data.get("proceed", True)),
            confidence=min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
            reasoning=str(data.get("reasoning", "No reasoning provided")),
            suggested_strike=(
                _safe_float(data.get("suggested_strike")) or default_strike
            ),
            suggested_expiry=(
                data.get("suggested_expiry")
                or (str(default_expiry) if default_expiry else None)
            ),
            warnings=data.get("warnings", []),
            raw_response=raw,
            source="llm",
        )

    except requests.exceptions.ConnectionError:
        log.warning("Ollama not reachable at %s — proceeding without LLM advice.", OLLAMA_URL)
        return _fallback(
            "Ollama not reachable — proceeding without LLM advice.",
            "Ollama connection failed"
        )

    except requests.exceptions.Timeout:
        log.warning("Ollama timed out after %ds — proceeding without LLM advice.", OLLAMA_TIMEOUT)
        return _fallback(
            f"Ollama timed out ({OLLAMA_TIMEOUT}s) — proceeding without LLM advice.",
            "Ollama timeout"
        )

    except Exception as e:
        log.error("Unexpected LLM error: %s", e, exc_info=True)
        return _fallback(
            f"LLM error ({type(e).__name__}: {e}) — proceeding without advice.",
            f"LLM error: {str(e)}"
        )
