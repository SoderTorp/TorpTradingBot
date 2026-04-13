"""
market_data.py
==============
Fetches stock context for the Ollama advisor.

Collects:
  - Current price, 52-week range, daily/weekly/monthly returns
  - Technicals: RSI-14, SMA-20, SMA-50, historical volatility
  - Volume analysis (today vs 20d average)
  - Company info: sector, beta, P/E, market cap
  - Upcoming earnings date + days-to-earnings
  - Recent news headlines (last 5)

All errors are swallowed — returns partial data so the advisor
can still reason even when some data sources are unavailable.

Dependencies: yfinance
"""

import math
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("wheel.market_data")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rsi(closes: list, period: int = 14) -> Optional[float]:
    """Calculate RSI-14 from a list of closing prices."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d if d > 0 else 0.0 for d in recent]
    losses = [-d if d < 0 else 0.0 for d in recent]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _hist_volatility(closes: list, period: int = 20) -> Optional[float]:
    """Annualised historical volatility (%) from the last `period` closes."""
    if len(closes) < period + 1:
        return None
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - period, len(closes))
    ]
    mean = sum(log_returns) / period
    variance = sum((r - mean) ** 2 for r in log_returns) / (period - 1)
    return round(math.sqrt(variance) * math.sqrt(252) * 100, 1)


def _pct_change(closes: list, lookback: int) -> Optional[float]:
    if len(closes) < lookback + 1:
        return None
    return round((closes[-1] - closes[-(lookback + 1)]) / closes[-(lookback + 1)] * 100, 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_stock_context(symbol: str) -> dict:
    """
    Return a dict with all market context needed for Ollama prompts.

    Never raises — on any failure returns {"symbol": symbol, "error": "..."}.
    Partial failures are logged but execution continues.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {
            "symbol": symbol,
            "error": "yfinance not installed — run: pip install yfinance",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    ctx: dict = {
        "symbol": symbol,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        ticker = yf.Ticker(symbol)

        # ----------------------------------------------------------------
        # Price history (90 days covers all technicals we need)
        # ----------------------------------------------------------------
        hist = ticker.history(period="90d")
        if hist.empty:
            ctx["error"] = f"No price history returned for {symbol}"
            return ctx

        closes = hist["Close"].tolist()
        volumes = hist["Volume"].tolist()
        current_price = round(float(closes[-1]), 2)

        ctx["price"] = current_price
        ctx["volume_today"] = int(volumes[-1])
        ctx["avg_volume_20d"] = int(sum(volumes[-20:]) / min(20, len(volumes)))
        ctx["volume_ratio"] = (
            round(ctx["volume_today"] / ctx["avg_volume_20d"], 2)
            if ctx["avg_volume_20d"] > 0 else 1.0
        )

        # ----------------------------------------------------------------
        # 52-week range
        # ----------------------------------------------------------------
        try:
            hist_1y = ticker.history(period="1y")
            if not hist_1y.empty:
                w52_high = round(float(hist_1y["High"].max()), 2)
                w52_low = round(float(hist_1y["Low"].min()), 2)
                ctx["week52_high"] = w52_high
                ctx["week52_low"] = w52_low
                ctx["pct_from_52w_high"] = round(
                    (current_price - w52_high) / w52_high * 100, 1
                )
                ctx["pct_from_52w_low"] = round(
                    (current_price - w52_low) / w52_low * 100, 1
                )
        except Exception as e:
            log.warning("52w range fetch failed: %s", e)

        # ----------------------------------------------------------------
        # Technicals
        # ----------------------------------------------------------------
        ctx["rsi_14"] = _rsi(closes)
        ctx["hist_volatility_20d"] = _hist_volatility(closes, 20)

        ctx["sma_20"] = (
            round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else None
        )
        ctx["sma_50"] = (
            round(sum(closes[-50:]) / 50, 2) if len(closes) >= 50 else None
        )

        if ctx["sma_20"] and ctx["sma_50"]:
            ctx["trend"] = "BULLISH" if ctx["sma_20"] > ctx["sma_50"] else "BEARISH"
        else:
            ctx["trend"] = "UNKNOWN"

        ctx["change_1d_pct"] = _pct_change(closes, 1)
        ctx["change_5d_pct"] = _pct_change(closes, 5)
        ctx["change_20d_pct"] = _pct_change(closes, 20)

        # ----------------------------------------------------------------
        # Company info
        # ----------------------------------------------------------------
        try:
            info = ticker.info
            ctx["company_name"] = info.get("longName", symbol)
            ctx["sector"] = info.get("sector", "Unknown")
            ctx["industry"] = info.get("industry", "Unknown")
            ctx["market_cap_b"] = (
                round(info.get("marketCap", 0) / 1e9, 1)
                if info.get("marketCap") else None
            )
            ctx["pe_ratio"] = info.get("trailingPE")
            ctx["beta"] = info.get("beta")
            ctx["short_float_pct"] = info.get("shortPercentOfFloat")
            # yfinance sometimes exposes IV on the info dict
            ctx["implied_volatility"] = info.get("impliedVolatility")
        except Exception as e:
            log.warning("Company info fetch failed: %s", e)
            ctx["company_name"] = symbol

        # ----------------------------------------------------------------
        # Earnings date
        # ----------------------------------------------------------------
        try:
            cal = ticker.calendar
            earnings_date = None

            if cal is not None:
                if isinstance(cal, dict):
                    # Older yfinance returns dict
                    raw = cal.get("Earnings Date")
                    if raw:
                        earnings_date = raw[0] if isinstance(raw, list) else raw
                elif hasattr(cal, "empty") and not cal.empty:
                    # Newer yfinance returns DataFrame
                    if "Earnings Date" in cal.index:
                        raw = cal.loc["Earnings Date"].iloc[0]
                        earnings_date = raw

            if earnings_date is not None:
                ed_str = str(earnings_date)[:10]
                ctx["earnings_date"] = ed_str
                try:
                    ed = datetime.strptime(ed_str, "%Y-%m-%d")
                    ctx["days_to_earnings"] = (ed - datetime.now()).days
                except ValueError:
                    pass
        except Exception as e:
            log.warning("Earnings date fetch failed: %s", e)

        # ----------------------------------------------------------------
        # Recent news headlines
        # ----------------------------------------------------------------
        try:
            raw_news = ticker.news or []
            headlines = []
            for n in raw_news[:5]:
                # yfinance v0.2+ nests content differently
                content = n.get("content", {})
                title = (
                    content.get("title")
                    or n.get("title", "")
                )
                publisher = (
                    content.get("provider", {}).get("displayName", "")
                    or n.get("publisher", "")
                )
                pub_date = (
                    (content.get("pubDate") or "")[:10]
                    or n.get("providerPublishTime", "")
                )
                if isinstance(pub_date, int):
                    # Unix timestamp
                    pub_date = datetime.utcfromtimestamp(pub_date).strftime("%Y-%m-%d")

                if title:
                    headlines.append({
                        "title": title,
                        "publisher": publisher,
                        "published": str(pub_date)[:10],
                    })
            ctx["recent_news"] = headlines
        except Exception as e:
            log.warning("News fetch failed: %s", e)
            ctx["recent_news"] = []

    except Exception as e:
        ctx["error"] = str(e)
        log.error("fetch_stock_context failed for %s: %s", symbol, e)

    return ctx


def format_context_summary(ctx: dict) -> str:
    """
    One-line human-readable summary for print statements.
    Used in wheel.py console output.
    """
    parts = []
    if ctx.get("price"):
        parts.append(f"${ctx['price']:.2f}")
    if ctx.get("rsi_14") is not None:
        parts.append(f"RSI={ctx['rsi_14']}")
    if ctx.get("trend"):
        parts.append(f"trend={ctx['trend']}")
    if ctx.get("hist_volatility_20d") is not None:
        parts.append(f"HV={ctx['hist_volatility_20d']}%")
    if ctx.get("days_to_earnings") is not None:
        parts.append(f"earnings in {ctx['days_to_earnings']}d")
    return "  ".join(parts) if parts else "no data"
