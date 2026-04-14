"""
VirtualPortfolio — dry-run simulated trading account.

Tracks a $500 (configurable) virtual bankroll across both the regular copy
strategy and the suspicious-activity strategy.  Each strategy instance gets
its own JSON file.

Lifecycle per run:
  1. resolve_positions(client) — check each open position against current market
     data; mark-to-market open ones, close resolved ones and credit/debit balance
  2. can_open(size) — balance guard before recording a new position
  3. open_position(signal) — deduct from balance and record the simulated bet

JSON schema  (state/virtual_portfolio.json):
{
  "starting_balance": 500.0,
  "available_balance": 423.50,
  "realized_pnl": 12.30,
  "open_positions": {
    "<conditionId>": {
      "wallet_copied":   "0xabc…",
      "outcome":         "Yes",
      "side":            "BUY",
      "size_usdc":       50.0,
      "entry_price":     0.45,
      "shares":          111.11,
      "strategy":        "polymarket_copy",
      "opened_at":       "2026-04-14T…",
      "current_price":   0.52,
      "unrealized_pnl":  7.78
    }
  },
  "closed_positions": [          # newest first, capped at 50
    {
      "<all open fields>",
      "final_price":   1.0,
      "payout":        111.11,
      "pnl":           61.11,
      "resolution":    "WIN",
      "closed_at":     "2026-04-15T…"
    }
  ],
  "wins":         3,
  "losses":       1,
  "last_updated": "2026-04-14T…"
}
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strategies.polymarket_copy import PolymarketClient, TradeSignal

log = logging.getLogger(__name__)

_CLOSED_CAP = 50   # keep at most this many closed-position records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# VirtualPortfolio
# ---------------------------------------------------------------------------

class VirtualPortfolio:
    """
    A simulated trading account for dry-run mode.

    Thread-safety: not thread-safe; each cron run is a single process, so
    this is fine.
    """

    def __init__(self, path: str, starting_balance: float = 500.0):
        self.path = path
        self._data: dict = _load_json(path, {})
        if not self._data:
            self._data = {
                "starting_balance": starting_balance,
                "available_balance": starting_balance,
                "realized_pnl":     0.0,
                "open_positions":   {},
                "closed_positions": [],
                "wins":             0,
                "losses":           0,
                "last_updated":     _now_iso(),
            }
            self._save()
            log.info(
                "Virtual portfolio initialised at $%.2f (%s)",
                starting_balance, path,
            )
        else:
            log.debug(
                "Virtual portfolio loaded: $%.2f available, %d open positions (%s)",
                self._data.get("available_balance", 0),
                len(self._data.get("open_positions", {})),
                path,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_open(self, size_usdc: float) -> bool:
        """Return True if the virtual balance can cover this bet."""
        return self._data["available_balance"] >= size_usdc

    def open_position(self, signal: "TradeSignal", strategy_name: str) -> None:
        """
        Record a new simulated position and deduct from available balance.

        Silently skips if:
        - price is zero (can't calculate shares)
        - a position for this market is already open (avoid double-deducting)
        """
        if signal.price <= 0:
            log.warning(
                "Virtual portfolio: skipping position with zero price for %s",
                signal.market_id,
            )
            return

        if signal.market_id in self._data["open_positions"]:
            log.debug(
                "Virtual portfolio: already open position for %s — skipping duplicate",
                signal.market_id,
            )
            return

        self._data["available_balance"] -= signal.size_usdc
        shares = signal.size_usdc / signal.price

        self._data["open_positions"][signal.market_id] = {
            "wallet_copied":  signal.wallet,
            "outcome":        signal.outcome,
            "side":           signal.side,
            "size_usdc":      signal.size_usdc,
            "entry_price":    signal.price,
            "shares":         round(shares, 6),
            "strategy":       strategy_name,
            "opened_at":      _now_iso(),
            "current_price":  signal.price,
            "unrealized_pnl": 0.0,
        }
        self._data["last_updated"] = _now_iso()
        self._save()

        log.info(
            "Virtual portfolio: opened %s %s/%s  size=$%.2f  entry=%.4f  balance=$%.2f",
            signal.side, signal.market_id, signal.outcome,
            signal.size_usdc, signal.price,
            self._data["available_balance"],
        )

    def resolve_positions(self, client: "PolymarketClient") -> None:
        """
        For each open position:
        - Fetch the market from gamma-api
        - If still active: update mark-to-market price + unrealized P&L
        - If resolved (closed=true): calculate payout, update balance, archive

        Skips any market that can't be fetched (API error) and tries again
        next run.
        """
        open_positions = self._data.get("open_positions", {})
        if not open_positions:
            return

        log.info("Resolving %d open virtual position(s)…", len(open_positions))
        to_close: list[tuple[str, dict]] = []

        for market_id, pos in list(open_positions.items()):
            market = client.get_market_by_id(market_id)
            if not market:
                log.debug("Could not fetch market %s — will retry next run", market_id)
                continue

            outcome_name = pos["outcome"]
            outcome_idx, current_price = self._resolve_outcome_price(
                market, outcome_name
            )
            if outcome_idx is None:
                log.debug(
                    "Could not map outcome '%s' for market %s", outcome_name, market_id
                )
                continue

            # Capture market metadata once (title + resolution date).
            if not pos.get("market_title"):
                title = market.get("question") or market.get("description") or ""
                if title:
                    pos["market_title"] = title
            if not pos.get("end_date"):
                end_date = (
                    market.get("end_date_iso")
                    or market.get("endDate")
                    or market.get("resolutionTime")
                    or ""
                )
                if end_date:
                    pos["end_date"] = end_date

            if not market.get("closed"):
                # Mark-to-market update
                old_price = pos.get("current_price", pos["entry_price"])
                pos["current_price"]  = current_price
                pos["unrealized_pnl"] = round(
                    pos["shares"] * current_price - pos["size_usdc"], 4
                )
                if abs(current_price - old_price) > 0.001:
                    log.debug(
                        "Mark-to-market %s: price %.4f → %.4f  unrealised=$%.2f",
                        market_id, old_price, current_price, pos["unrealized_pnl"],
                    )
            else:
                # Market has resolved — final payout
                final_price = current_price  # will be 0.0 or 1.0 after resolution
                payout = round(pos["shares"] * final_price, 4)
                pnl    = round(payout - pos["size_usdc"], 4)
                resolution = "WIN" if final_price >= 0.5 else "LOSS"

                self._data["available_balance"] += payout
                self._data["realized_pnl"]      += pnl
                if resolution == "WIN":
                    self._data["wins"] += 1
                else:
                    self._data["losses"] += 1

                closed_entry = {
                    **pos,
                    "final_price": final_price,
                    "payout":      payout,
                    "pnl":         pnl,
                    "resolution":  resolution,
                    "closed_at":   _now_iso(),
                }
                to_close.append((market_id, closed_entry))

                log.info(
                    "Virtual portfolio: %s %s/%s  pnl=$%+.2f  balance=$%.2f",
                    resolution, market_id, outcome_name, pnl,
                    self._data["available_balance"],
                )

        # Apply closures
        for market_id, closed_entry in to_close:
            del self._data["open_positions"][market_id]
            self._data["closed_positions"].insert(0, closed_entry)

        # Cap closed positions list
        self._data["closed_positions"] = \
            self._data["closed_positions"][:_CLOSED_CAP]

        self._data["last_updated"] = _now_iso()
        self._save()

    def enrich_position(self, market_id: str, client: "PolymarketClient") -> None:
        """
        Immediately fetch market metadata (title + end_date) for a freshly opened
        position so the dashboard can show "Closes in" without waiting for the
        next resolve_positions() cycle.

        No-ops silently if the position doesn't exist, is already enriched, or
        the API call fails.
        """
        pos = self._data["open_positions"].get(market_id)
        if not pos:
            return
        if pos.get("end_date") and pos.get("market_title"):
            return  # already complete

        market = client.get_market_by_id(market_id)
        if not market:
            log.debug("enrich_position: could not fetch market %s", market_id)
            return

        changed = False
        if not pos.get("market_title"):
            title = market.get("question") or market.get("description") or ""
            if title:
                pos["market_title"] = title
                changed = True
        if not pos.get("end_date"):
            end_date = (
                market.get("end_date_iso")
                or market.get("endDate")
                or market.get("resolutionTime")
                or ""
            )
            if end_date:
                pos["end_date"] = end_date
                changed = True

        if changed:
            self._data["last_updated"] = _now_iso()
            self._save()
            log.debug(
                "enrich_position: stored title=%r end_date=%r for %s",
                pos.get("market_title"), pos.get("end_date"), market_id,
            )

    def summary(self) -> dict:
        """Return a flat summary dict suitable for the /api/portfolio endpoint."""
        open_pos = self._data.get("open_positions", {})
        unrealized = sum(
            p.get("unrealized_pnl", 0.0) for p in open_pos.values()
        )
        realized = self._data.get("realized_pnl", 0.0)
        return {
            "starting_balance":  self._data["starting_balance"],
            "available_balance": round(self._data["available_balance"], 4),
            "total_invested":    round(
                sum(p["size_usdc"] for p in open_pos.values()), 4
            ),
            "realized_pnl":      round(realized, 4),
            "unrealized_pnl":    round(unrealized, 4),
            "total_pnl":         round(realized + unrealized, 4),
            "wins":              self._data["wins"],
            "losses":            self._data["losses"],
            "open_count":        len(open_pos),
            "open_positions":    list(
                {**p, "_market_id": mid} for mid, p in open_pos.items()
            ),
            "closed_positions":  self._data.get("closed_positions", [])[:10],
            "last_updated":      self._data.get("last_updated"),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_outcome_price(
        self, market: dict, outcome_name: str
    ) -> tuple[int | None, float]:
        """
        Extract the current price for a given outcome from a market dict.

        Supports two formats:
        - CLOB API: market["tokens"] = [{"outcome": "Up", "price": 0.95, "winner": True}, ...]
        - Gamma API: market["outcomes"] = JSON string, market["outcomePrices"] = JSON string

        Returns (outcome_index, price).  Returns (None, 0.0) on any parse failure.
        """
        try:
            target = outcome_name.lower()

            # --- CLOB format (preferred) ---
            tokens = market.get("tokens")
            if tokens and isinstance(tokens, list):
                idx = next(
                    (i for i, t in enumerate(tokens) if t.get("outcome", "").lower() == target),
                    None,
                )
                if idx is not None:
                    return idx, float(tokens[idx].get("price", 0))

            # --- Gamma-API fallback ---
            outcomes_raw = market.get("outcomes", "[]")
            prices_raw   = market.get("outcomePrices", "[]")
            outcomes: list[str] = json.loads(outcomes_raw) \
                if isinstance(outcomes_raw, str) else outcomes_raw
            prices: list[str]   = json.loads(prices_raw) \
                if isinstance(prices_raw, str) else prices_raw

            if outcomes and prices:
                idx = next(
                    (i for i, o in enumerate(outcomes) if o.lower() == target),
                    None,
                )
                if idx is not None and idx < len(prices):
                    return idx, float(prices[idx])

            return None, 0.0
        except Exception as exc:
            log.debug("_resolve_outcome_price error: %s", exc)
            return None, 0.0

    def _save(self) -> None:
        _save_json(self.path, self._data)
