"""
Polymarket copy-trade strategy.

Three public classes:
  PolymarketClient  — HTTP wrappers for Polymarket's public APIs (no auth required for reads)
  WalletScorer      — Scores a wallet across recency, win-rate, entry timing, diversity
  WalletDiscovery   — Three-source pipeline that refreshes state/watchlist.json
  CopyTrader        — Reads watchlist, detects new trades, logs or executes them
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from core.logger import TradeLogger

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Polymarket public API base URLs
# ---------------------------------------------------------------------------
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# State file paths
WATCHLIST_PATH = "state/watchlist.json"
POSITIONS_PATH = "state/open_positions.json"

# How many seconds to wait between API calls to be polite
_REQUEST_DELAY = 0.25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None, timeout: int = 15) -> Any:
    """GET with basic retry logic (2 retries on transient errors)."""
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == 2:
                log.warning("GET %s failed after 3 attempts: %s", url, exc)
                return None
            time.sleep(1.5 ** attempt)
    return None


def _load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# PolymarketClient
# ---------------------------------------------------------------------------

class PolymarketClient:
    """Thin wrapper around Polymarket's public read endpoints."""

    def get_wallet_activity(
        self,
        wallet: str,
        limit: int = 50,
        start: int | None = None,
        end: int | None = None,
        sort_by: str | None = None,       # "TIMESTAMP", "TOKENS", or "CASH"
        sort_direction: str | None = None, # "ASC" or "DESC"
    ) -> list[dict]:
        params: dict = {"user": wallet, "limit": limit}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if sort_by:
            params["sortBy"] = sort_by
        if sort_direction:
            params["sortDirection"] = sort_direction
        data = _get(f"{DATA_API}/activity", params=params)
        time.sleep(_REQUEST_DELAY)
        return data or []

    def get_wallet_positions(self, wallet: str) -> list[dict]:
        data = _get(f"{DATA_API}/positions", params={"user": wallet})
        time.sleep(_REQUEST_DELAY)
        return data or []

    def get_markets(self, limit: int = 100, active: bool = True) -> list[dict]:
        params: dict = {"limit": limit}
        if active:
            params["active"] = "true"
        data = _get(f"{GAMMA_API}/markets", params=params)
        time.sleep(_REQUEST_DELAY)
        return data or []

    def get_order_book(self, token_id: str) -> dict | None:
        data = _get(f"{CLOB_API}/book", params={"token_id": token_id})
        time.sleep(_REQUEST_DELAY)
        return data

    def get_leaderboard(self, limit: int = 100) -> list[dict]:
        # NOTE: leaderboard lives under /v1/ and accepts max 50 per page;
        # we fetch two pages to approximate top-100.
        page1 = _get(f"{DATA_API}/v1/leaderboard", params={"limit": 50, "offset": 0, "timePeriod": "ALL", "orderBy": "PNL"})
        page2 = _get(f"{DATA_API}/v1/leaderboard", params={"limit": 50, "offset": 50, "timePeriod": "ALL", "orderBy": "PNL"})
        time.sleep(_REQUEST_DELAY)
        return (page1 or []) + (page2 or [])

    def get_market_by_id(self, condition_id: str) -> dict | None:
        """Fetch a single market by conditionId from the CLOB API.

        The CLOB endpoint returns a single dict with `closed`, `end_date_iso`,
        and a `tokens` list (each with `outcome`, `price`, `winner`) — enough
        for both mark-to-market and resolution calculations.
        Returns None if the market is not found or the request fails.
        """
        data = _get(f"{CLOB_API}/markets/{condition_id}")
        time.sleep(_REQUEST_DELAY)
        if isinstance(data, dict) and data.get("condition_id"):
            return data
        return None

    def get_recent_large_trades(
        self,
        min_size_usdc: float = 500,
        limit: int = 100,
    ) -> list[dict]:
        """Return recent trades above a USDC threshold across all wallets.

        Uses the /trades endpoint (not /activity which requires a user param).
        filterType=CASH + filterAmount=N filters to trades >= N USDC.
        """
        data = _get(
            f"{DATA_API}/trades",
            params={"filterType": "CASH", "filterAmount": min_size_usdc, "limit": limit},
        )
        time.sleep(_REQUEST_DELAY)
        return data or []


# ---------------------------------------------------------------------------
# WalletScorer
# ---------------------------------------------------------------------------

@dataclass
class ScoreBreakdown:
    recency: float = 0.0
    win_rate: float = 0.0
    entry_timing: float = 0.0
    diversity: float = 0.0
    final: float = 0.0
    disqualified: bool = False
    reason: str = ""
    name: str = ""        # display name from Polymarket profile (may be empty)


class WalletScorer:
    """Scores a wallet on a 0–1 scale using a 30-day rolling window."""

    RECENCY_WEIGHT = 0.30
    WIN_RATE_WEIGHT = 0.35
    ENTRY_TIMING_WEIGHT = 0.25
    DIVERSITY_WEIGHT = 0.10

    WINDOW_DAYS = 30
    MIN_TRADES_IN_7D = 5

    def __init__(self, config: dict, client: PolymarketClient | None = None):
        self.client = client or PolymarketClient()
        scoring_cfg = config.get("polymarket", {}).get("scoring", {})
        self.recency_w = scoring_cfg.get("recency_weight", self.RECENCY_WEIGHT)
        self.win_rate_w = scoring_cfg.get("win_rate_weight", self.WIN_RATE_WEIGHT)
        self.entry_timing_w = scoring_cfg.get("entry_timing_weight", self.ENTRY_TIMING_WEIGHT)
        self.diversity_w = scoring_cfg.get("diversity_weight", self.DIVERSITY_WEIGHT)

    def score(self, wallet: str) -> ScoreBreakdown:
        now = _now_ts()
        window_start = int(now - self.WINDOW_DAYS * 86400)
        activity = self.client.get_wallet_activity(
            wallet, limit=200, start=window_start
        )
        # Activity type field is uppercase "TRADE" (not "trade")
        trades = [a for a in activity if a.get("type", "").upper() == "TRADE"]

        # Extract display name from any activity record (all records carry the same profile fields)
        wallet_name = ""
        if activity:
            first = activity[0]
            wallet_name = first.get("name") or first.get("pseudonym") or ""

        # Disqualify: fewer than MIN_TRADES_IN_7D in the last 7 days
        cutoff_7d = now - 7 * 86400
        recent_trades = [t for t in trades if self._ts(t) >= cutoff_7d]
        if len(recent_trades) < self.MIN_TRADES_IN_7D:
            return ScoreBreakdown(
                disqualified=True,
                reason=f"only {len(recent_trades)} trades in last 7 days (min {self.MIN_TRADES_IN_7D})",
            )

        # Positions used for win-rate (activity records have no resolved/profit fields)
        positions = self.client.get_wallet_positions(wallet)

        recency_score = self._calc_recency(trades, now)
        win_rate_score = self._calc_win_rate(positions)
        entry_timing_score = self._calc_entry_timing(trades)
        diversity_score = self._calc_diversity(trades)

        final = (
            recency_score * self.recency_w
            + win_rate_score * self.win_rate_w
            + entry_timing_score * self.entry_timing_w
            + diversity_score * self.diversity_w
        )

        return ScoreBreakdown(
            recency=recency_score,
            win_rate=win_rate_score,
            entry_timing=entry_timing_score,
            diversity=diversity_score,
            final=round(final, 4),
            name=wallet_name,
        )

    # ------------------------------------------------------------------
    # Scoring sub-calculations
    # ------------------------------------------------------------------

    def _ts(self, trade: dict) -> float:
        """Extract a UNIX timestamp from a trade record."""
        for key in ("timestamp", "createdAt", "created_at", "time"):
            val = trade.get(key)
            if val is None:
                continue
            if isinstance(val, (int, float)):
                # Milliseconds vs seconds heuristic
                return val / 1000 if val > 1e12 else val
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
        return 0.0

    def _calc_recency(self, trades: list[dict], now: float) -> float:
        """1.0 if traded in the last 24h, decays linearly to 0.5 at 7 days."""
        if not trades:
            return 0.0
        latest = max(self._ts(t) for t in trades)
        hours_ago = (now - latest) / 3600
        if hours_ago <= 24:
            return 1.0
        if hours_ago <= 168:  # 7 days
            return 1.0 - 0.5 * (hours_ago - 24) / (168 - 24)
        return 0.5

    def _calc_win_rate(self, positions: list[dict]) -> float:
        """
        Fraction of current positions that are profitable.
        Uses the /positions endpoint: cashPnl > 0 = winning, redeemable = True = resolved win.
        Activity records don't include resolved/profit fields, so positions are the
        only available data source for P&L.
        """
        if not positions:
            return 0.5  # neutral — no position history available

        wins = sum(
            1 for p in positions
            if float(p.get("cashPnl", 0) or 0) > 0 or p.get("redeemable") is True
        )
        return wins / len(positions)

    def _calc_entry_timing(self, trades: list[dict]) -> float:
        """
        Reward wallets that consistently buy at low prices (early / contrarian entry).

        Strategy: compute the average entry price across all BUY trades.
        Lower average price = buying before consensus forms = higher quality signal.

        Scoring curve:
          avg_price <= 0.15  → 1.0  (consistently early)
          avg_price == 0.50  → 0.5  (neutral)
          avg_price >= 0.85  → 0.0  (betting on heavy favourites)
        """
        buy_prices = [
            float(t.get("price") or 0)
            for t in trades
            if t.get("side", "").upper() in ("BUY",) and float(t.get("price") or 0) > 0
        ]
        if not buy_prices:
            return 0.5  # neutral

        avg_price = sum(buy_prices) / len(buy_prices)
        # Linear interpolation: 0.15 → 1.0, 0.85 → 0.0
        return max(0.0, min(1.0, (0.85 - avg_price) / 0.70))

    def _calc_diversity(self, trades: list[dict]) -> float:
        """Score based on number of distinct markets traded (caps at 10 = 1.0).
        Activity records use 'conditionId' as the market identifier."""
        market_ids = {t.get("conditionId") for t in trades} - {None}
        return min(len(market_ids) / 10, 1.0)


# ---------------------------------------------------------------------------
# WalletDiscovery
# ---------------------------------------------------------------------------

class WalletDiscovery:
    """
    Runs the three-source discovery pipeline and writes state/watchlist.json.

    Sources:
    1. Mine recent large trades (> discovery_min_trade_size_usdc)
    2. Polymarket leaderboard as seed — re-score and filter
    3. Market co-entry graph — find wallets that entered same markets earlier
       than known-good wallets
    """

    def __init__(self, config: dict, client: PolymarketClient | None = None):
        self.config = config
        self.pm_config = config.get("polymarket", {})
        self.client = client or PolymarketClient()
        self.scorer = WalletScorer(config, self.client)

    def run(self) -> list[dict]:
        min_score = self.pm_config.get("min_score_threshold", 0.65)
        max_wallets = self.pm_config.get("max_wallets_tracked", 20)
        min_trade_size = self.pm_config.get("discovery_min_trade_size_usdc", 500)

        candidates: set[str] = set()

        # Source 1: recent large trades
        log.info("Source 1: mining recent large trades (>$%s)", min_trade_size)
        large_trades = self.client.get_recent_large_trades(min_size_usdc=min_trade_size)
        for trade in large_trades:
            # /trades response uses "proxyWallet"; fall back to other common field names
            wallet = (
                trade.get("proxyWallet")
                or trade.get("maker")
                or trade.get("user")
                or trade.get("address")
            )
            if wallet:
                candidates.add(wallet.lower())
        log.info("Source 1 yielded %d candidates", len(candidates))

        # Source 2: leaderboard seed
        log.info("Source 2: leaderboard seed (top 100)")
        leaderboard = self.client.get_leaderboard(limit=100)
        for entry in leaderboard:
            # /v1/leaderboard response uses "proxyWallet"
            wallet = (
                entry.get("proxyWallet")
                or entry.get("proxyWalletAddress")
                or entry.get("address")
                or entry.get("user")
            )
            if wallet:
                candidates.add(wallet.lower())
        log.info("Source 2 added candidates; total now %d", len(candidates))

        # Source 3: co-entry graph — find wallets entering the same markets earlier
        # than already-qualified candidates. We seed from the current watchlist.
        existing_watchlist = _load_json(WATCHLIST_PATH, [])
        known_good = [e["wallet"] for e in existing_watchlist if e.get("score", 0) >= min_score]
        if known_good:
            log.info("Source 3: co-entry graph from %d known-good wallets", len(known_good))
            co_entry_candidates = self._co_entry_candidates(known_good[:5])
            candidates.update(co_entry_candidates)
            log.info("Source 3 added candidates; total now %d", len(candidates))

        # Score all candidates
        log.info("Scoring %d candidates…", len(candidates))
        scored: list[dict] = []
        for wallet in candidates:
            breakdown = self.scorer.score(wallet)
            if breakdown.disqualified:
                log.debug("Disqualified %s: %s", wallet, breakdown.reason)
                continue
            if breakdown.final >= min_score:
                entry: dict = {
                    "wallet": wallet,
                    "score": breakdown.final,
                    "score_breakdown": {
                        "recency": breakdown.recency,
                        "win_rate": breakdown.win_rate,
                        "entry_timing": breakdown.entry_timing,
                        "diversity": breakdown.diversity,
                    },
                    "last_scored_at": datetime.now(timezone.utc).isoformat(),
                }
                if breakdown.name:
                    entry["name"] = breakdown.name
                scored.append(entry)

        scored.sort(key=lambda x: x["score"], reverse=True)
        watchlist = scored[:max_wallets]

        _save_json(WATCHLIST_PATH, watchlist)
        log.info(
            "Watchlist updated: %d wallets qualify (min_score=%.2f)",
            len(watchlist),
            min_score,
        )
        return watchlist

    def _co_entry_candidates(self, known_wallets: list[str]) -> set[str]:
        """
        For each known-good wallet, find markets they are in, then find other
        wallets that entered those markets at an *earlier* timestamp.
        """
        candidates: set[str] = set()
        for wallet in known_wallets:
            positions = self.client.get_wallet_positions(wallet)
            market_ids = [
                p.get("market") or p.get("conditionId") or p.get("marketId")
                for p in positions
            ]
            market_ids = [m for m in market_ids if m][:5]  # cap per wallet

            for market_id in market_ids:
                # Find when this known-good wallet entered this market
                wallet_activity = self.client.get_wallet_activity(wallet, limit=50)
                wallet_entry_ts = None
                for a in wallet_activity:
                    if a.get("conditionId") == market_id and a.get("type", "").upper() == "TRADE":
                        wallet_entry_ts = self.scorer._ts(a)
                        break

                if not wallet_entry_ts:
                    continue

                # Use the /trades endpoint to find other participants in this market.
                # Filter to trades on this conditionId that happened earlier.
                market_activity = _get(
                    f"{DATA_API}/trades",
                    params={"market": market_id, "limit": 100},
                )
                if not market_activity:
                    continue
                for trade in market_activity:
                    trade_ts = self.scorer._ts(trade)
                    if trade_ts < wallet_entry_ts:
                        addr = (
                            trade.get("proxyWallet")
                            or trade.get("maker")
                            or trade.get("user")
                            or trade.get("address")
                        )
                        if addr and addr.lower() != wallet.lower():
                            candidates.add(addr.lower())
        return candidates


# ---------------------------------------------------------------------------
# CopyTrader
# ---------------------------------------------------------------------------

@dataclass
class TradeSignal:
    wallet: str
    market_id: str
    outcome: str
    side: str  # "BUY" or "SELL"
    size_usdc: float
    price: float
    token_id: str
    reason: str = ""
    ai_rationale: str = ""


class CopyTrader:
    """
    Reads a watchlist JSON, fetches recent activity for each tracked wallet,
    identifies new trades since the last run, and either logs (dry_run) or
    submits (live) orders.

    Parameterised so it can serve both the regular copy strategy and the
    suspicious-activity strategy without code duplication.
    """

    def __init__(
        self,
        config: dict,
        client: PolymarketClient | None = None,
        watchlist_path: str = WATCHLIST_PATH,
        positions_path: str = POSITIONS_PATH,
        strategy_name: str = "polymarket_copy",
        portfolio_path: str | None = None,
    ):
        self.config = config
        self.pm_config = config.get("polymarket", {})
        self.mode = config.get("mode", "dry_run")
        self.client = client or PolymarketClient()
        self.trade_logger = TradeLogger()
        self.watchlist_path = watchlist_path
        self.positions_path = positions_path
        self.strategy_name = strategy_name
        self.portfolio_path = portfolio_path

    def run(self) -> None:
        # Initialise virtual portfolio (dry_run only)
        portfolio = None
        if self.mode == "dry_run" and self.portfolio_path:
            from strategies.portfolio import VirtualPortfolio
            starting_bal = self.pm_config.get("dry_run_starting_balance", 500.0)
            portfolio = VirtualPortfolio(self.portfolio_path, starting_balance=starting_bal)
            portfolio.resolve_positions(self.client)

        watchlist = _load_json(self.watchlist_path, [])
        if not watchlist:
            log.info(
                "Watchlist is empty (%s) — run discovery first", self.watchlist_path
            )
            return

        positions = _load_json(self.positions_path, {})
        signals: list[TradeSignal] = []

        for entry in watchlist:
            wallet = entry["wallet"]
            new_signals = self._check_wallet(wallet, positions)
            signals.extend(new_signals)

        log.info("Found %d trade signal(s) across %d wallets", len(signals), len(watchlist))

        for signal in signals:
            self._handle_signal(signal, positions, portfolio)

        _save_json(self.positions_path, positions)

    def _check_wallet(self, wallet: str, positions: dict) -> list[TradeSignal]:
        """Return new trade signals for a wallet since we last checked."""
        cutoff = positions.get(f"_last_checked_{wallet}", _now_ts() - 900)  # 15 min default
        activity = self.client.get_wallet_activity(wallet, limit=50)

        new_signals: list[TradeSignal] = []
        for trade in activity:
            trade_ts = _extract_ts(trade)
            if trade_ts <= cutoff:
                continue
            # activity endpoint type values: TRADE, SPLIT, MERGE, REDEEM, REWARD, etc.
            trade_type = (trade.get("type") or "").upper()
            if trade_type and trade_type not in ("TRADE", "ORDER_FILLED", ""):
                continue

            # Activity records: usdcSize is the USDC notional value of the trade
            size_usdc = float(trade.get("usdcSize") or trade.get("size") or 0)
            if size_usdc < self.pm_config.get("min_bet_usdc", 20):
                continue

            market_id = trade.get("conditionId") or ""
            token_id = trade.get("asset") or market_id  # asset = ERC-1155 token id
            outcome = trade.get("outcome") or "YES"
            side = (trade.get("side") or "BUY").upper()
            price = float(trade.get("price") or 0)

            if not self._passes_filters(market_id, size_usdc, price):
                continue

            new_signals.append(
                TradeSignal(
                    wallet=wallet,
                    market_id=market_id,
                    outcome=str(outcome),
                    side=side,
                    size_usdc=min(size_usdc, self.pm_config.get("max_bet_usdc", 100)),
                    price=price,
                    token_id=token_id,
                    reason=f"Copy from {wallet[:8]}… (score signal)",
                )
            )

        positions[f"_last_checked_{wallet}"] = _now_ts()
        return new_signals

    def _passes_filters(self, market_id: str, size_usdc: float, price: float) -> bool:
        """Apply configured trade-execution filters."""
        if size_usdc < self.pm_config.get("min_bet_usdc", 20):
            log.debug("Filtered: size %.2f below min", size_usdc)
            return False

        # Fetch market metadata to check volume and days-to-resolution
        markets = self.client.get_markets()
        market_meta = next(
            (m for m in markets if m.get("conditionId") == market_id or m.get("id") == market_id),
            None,
        )
        if market_meta:
            volume = float(market_meta.get("volume", market_meta.get("usdcLiquidity", 0) or 0))
            if volume < self.pm_config.get("min_market_volume", 10000):
                log.debug("Filtered: market volume %.0f below min", volume)
                return False

            end_date_str = market_meta.get("endDate") or market_meta.get("resolutionTime")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    days_left = (end_date - datetime.now(timezone.utc)).days
                    if days_left > self.pm_config.get("max_days_to_resolution", 30):
                        log.debug("Filtered: %d days to resolution exceeds max", days_left)
                        return False
                except ValueError:
                    pass

        return True

    def _handle_signal(
        self,
        signal: TradeSignal,
        positions: dict,
        portfolio: Any | None = None,
    ) -> None:
        """Log (dry_run) or execute (live) a trade signal."""
        # Optional AI rationale — non-blocking
        try:
            from ai.ollama_client import OllamaClient

            ai = OllamaClient(self.config)
            result = ai.generate_trade_rationale(
                {
                    "wallet": signal.wallet,
                    "market_id": signal.market_id,
                    "outcome": signal.outcome,
                    "side": signal.side,
                    "price": signal.price,
                    "size_usdc": signal.size_usdc,
                }
            )
            signal.ai_rationale = result.get("text", "")
        except Exception:
            signal.ai_rationale = ""

        full_reason = signal.reason
        if signal.ai_rationale:
            full_reason += f" | AI: {signal.ai_rationale}"

        if self.mode == "dry_run":
            # Virtual balance guard — pause new trades when balance is exhausted
            if portfolio is not None and not portfolio.can_open(signal.size_usdc):
                log.info(
                    "[DRY RUN] SKIP — insufficient virtual balance "
                    "(need $%.2f, have $%.2f)",
                    signal.size_usdc,
                    portfolio._data["available_balance"],
                )
                return

            log.info(
                "[DRY RUN] Would %s %.2f USDC on %s/%s @ %.4f | %s",
                signal.side,
                signal.size_usdc,
                signal.market_id,
                signal.outcome,
                signal.price,
                full_reason,
            )
            self.trade_logger.log(
                strategy=self.strategy_name,
                market_id=signal.market_id,
                outcome=signal.outcome,
                side=signal.side,
                size_usdc=signal.size_usdc,
                price=signal.price,
                dry_run=True,
                reason=full_reason,
                order_id=None,
            )
            if portfolio is not None:
                portfolio.open_position(signal, self.strategy_name)
                # Immediately enrich with market title + end_date so the
                # dashboard shows "Closes in" without waiting for the next run.
                portfolio.enrich_position(signal.market_id, self.client)
        else:
            order_id = self._submit_order(signal)
            self.trade_logger.log(
                strategy=self.strategy_name,
                market_id=signal.market_id,
                outcome=signal.outcome,
                side=signal.side,
                size_usdc=signal.size_usdc,
                price=signal.price,
                dry_run=False,
                reason=full_reason,
                order_id=order_id,
            )
            # Track open position
            positions[signal.market_id] = {
                "wallet_copied": signal.wallet,
                "outcome": signal.outcome,
                "side": signal.side,
                "size_usdc": signal.size_usdc,
                "entry_price": signal.price,
                "order_id": order_id,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }

    def _submit_order(self, signal: TradeSignal) -> str | None:
        """Submit a live order via py-clob-client. Returns order ID or None."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.order_builder.constants import BUY, SELL

            private_key = os.getenv("POLYGON_PRIVATE_KEY")
            api_key = os.getenv("POLYMARKET_API_KEY")
            if not private_key or not api_key:
                raise EnvironmentError("Missing POLYGON_PRIVATE_KEY or POLYMARKET_API_KEY")

            client = ClobClient(
                host=CLOB_API,
                key=private_key,
                chain_id=137,  # Polygon mainnet
            )
            client.set_api_creds(client.create_or_derive_api_creds())

            side = BUY if signal.side == "BUY" else SELL
            order = client.create_market_order(
                token_id=signal.token_id,
                side=side,
                amount=signal.size_usdc,
            )
            resp = client.post_order(order)
            order_id = resp.get("orderID") or resp.get("id")
            log.info("Live order placed: %s", order_id)
            return order_id
        except ImportError:
            log.error("py-clob-client not installed — cannot place live orders")
            return None
        except Exception as exc:
            log.error("Order submission failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_ts(trade: dict) -> float:
    """Best-effort timestamp extraction from a trade dict."""
    for key in ("timestamp", "createdAt", "created_at", "time"):
        val = trade.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return val / 1000 if val > 1e12 else float(val)
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return 0.0
