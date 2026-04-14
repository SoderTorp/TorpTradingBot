"""
SuspiciousActivityDetector strategy.

Hunts for *new* wallets (< 30 days old) that are suspiciously profitable
on politically-sensitive prediction markets: oil, gold, defense, pharma,
trade policy, and financial policy markets.

The hypothesis: fresh accounts hitting high win rates on policy-impacted
markets shortly before announcements may have advance information.

Public class:
  SuspiciousActivityDetector  — scan pipeline that writes
                                state/suspicious_watchlist.json

Copying is handled by the existing CopyTrader (polymarket_copy.py)
instantiated with the suspicious watchlist/positions paths.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from strategies.polymarket_copy import (
    DATA_API,
    PolymarketClient,
    _get,
    _load_json,
    _now_ts,
    _REQUEST_DELAY,
    _save_json,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State file paths (imported by main.py so CopyTrader can be wired up)
# ---------------------------------------------------------------------------
SUSPICIOUS_WATCHLIST_PATH = "state/suspicious_watchlist.json"
SUSPICIOUS_POSITIONS_PATH = "state/suspicious_positions.json"

# ---------------------------------------------------------------------------
# Political sensitivity keyword map
# Keys are shown as category labels in flags / web UI.
# Keywords are matched case-insensitively against market titles.
# ---------------------------------------------------------------------------
POLITICAL_CATEGORIES: dict[str, list[str]] = {
    "OIL": [
        "crude oil", "oil price", "wti", "brent", "opec",
        "petroleum", "barrel", "oil production",
    ],
    "GOLD": [
        "gold price", "gold futures", "precious metal",
        "bullion", "xau", "gold above", "gold below",
    ],
    "ENERGY": [
        "natural gas", "lng", "pipeline", "nuclear energy",
        "energy sector", "power grid", "electricity price",
    ],
    "TECH_REGULATION": [
        "nvidia", "meta ", "google", "alphabet", "microsoft",
        "antitrust", "ai regulation", "chip export", "semiconductor",
        "tech regulation", "big tech",
    ],
    "DEFENSE": [
        "defense contract", "military", "lockheed", "raytheon", "boeing",
        "weapons", "nato", "arms deal", "pentagon", "war ends",
        "ceasefire", "troops",
    ],
    "PHARMA": [
        "fda approval", "fda", "drug approval", "vaccine",
        "pfizer", "moderna", "clinical trial", "pharma",
        "drug trial",
    ],
    "TRADE_POLICY": [
        "tariff", "trade war", "trade deal", "sanctions",
        "import duty", "export ban", "customs", "wto",
        "trade agreement",
    ],
    "FINANCIAL_POLICY": [
        "federal reserve", "fed rate", "interest rate",
        "inflation", "cpi", "treasury yield", "jerome powell",
        "rate cut", "rate hike", "fomc",
    ],
}

# ---------------------------------------------------------------------------
# Scoring weights
#
# Weight rationale for insider detection:
#   - New account + political market focus are the primary signals
#   - Win rate is unreliable for brand-new wallets (few resolved positions)
#     so it is kept as a low-weight bonus rather than a gate
# ---------------------------------------------------------------------------
WEIGHTS = {
    "account_age":   0.35,  # primary signal: brand-new account
    "concentration": 0.30,  # primary signal: all-in on political markets
    "velocity":      0.20,  # secondary: burst trading behaviour
    "win_rate":      0.10,  # bonus: high win rate is suspicious but often unmeasurable for new accounts
    "roi":           0.05,  # bonus: extreme profit rate
}

# Win-rate bonus only kicks in above this threshold
MIN_SUSPICIOUS_WIN_RATE = 0.70

# Velocity normaliser: 20 trades/day → score 1.0
VELOCITY_NORM = 20.0

# Concentration: any political trading above 5% starts scoring; 70%+ → 1.0
# Set deliberately low because Polymarket traders naturally spread across many
# categories — even 20-30% political concentration in a new wallet is notable.
CONCENTRATION_BASE = 0.05

# Daily P&L normaliser: $2 000/day → score 1.0
DAILY_PNL_NORM = 2000.0

# Extreme ROI flags
EXTREME_DAILY_PNL_USD = 3000.0
EXTREME_TOTAL_PNL_USD = 100_000.0

# Account age thresholds (days)
AGE_TIER_1 = 7     # score 1.0
AGE_TIER_2 = 14    # score 0.8
AGE_TIER_3 = 30    # score 0.5
AGE_MAX = 30       # disqualify ≥ this


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class SuspiciousnessScore:
    account_age_days: float = 0.0
    account_age_score: float = 0.0
    win_rate_political: float = 0.0
    win_rate_score: float = 0.0
    velocity_trades_per_day: float = 0.0
    velocity_score: float = 0.0
    concentration_pct: float = 0.0
    concentration_score: float = 0.0
    daily_pnl_usdc: float = 0.0
    roi_score: float = 0.0
    final: float = 0.0
    flags: list[str] = field(default_factory=list)
    political_categories: list[str] = field(default_factory=list)
    disqualified: bool = False
    reason: str = ""
    name: str = ""   # display name from Polymarket profile (may be empty)


# ---------------------------------------------------------------------------
# SuspiciousActivityDetector
# ---------------------------------------------------------------------------

class SuspiciousActivityDetector:
    """
    Scans recent Polymarket traders, identifies accounts < 30 days old with
    anomalous profitability on politically-sensitive markets, and writes
    state/suspicious_watchlist.json.
    """

    WATCHLIST_TTL_HOURS = 48  # keep entries for 48h even if not re-detected

    def __init__(self, config: dict, client: PolymarketClient | None = None):
        self.config = config
        self.client = client or PolymarketClient()
        cfg = config.get("suspicious", {})
        self.min_score = float(cfg.get("min_score", 0.60))
        self.max_age_days = float(cfg.get("max_account_age_days", AGE_MAX))
        self.scan_limit = int(cfg.get("scan_limit", 200))
        self.max_wallets = int(cfg.get("max_wallets_tracked", 10))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def scan(self) -> list[dict]:
        """
        Full scan pipeline. Returns the updated suspicious watchlist.
        """
        candidates = self._gather_candidates()
        log.info("Evaluating %d candidate wallets for suspicious activity", len(candidates))

        new_detections: list[dict] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for wallet in candidates:
            score = self._score_wallet(wallet)
            if score.disqualified:
                log.debug("Disqualified %s: %s", wallet[:10], score.reason)
                continue
            if score.final < self.min_score:
                log.debug(
                    "Below threshold %s: score=%.3f", wallet[:10], score.final
                )
                continue
            if not score.political_categories:
                log.debug("No political markets detected for %s", wallet[:10])
                continue

            log.info(
                "SUSPICIOUS WALLET %s: score=%.3f age=%.1fd flags=%s",
                wallet[:10],
                score.final,
                score.account_age_days,
                score.flags,
            )
            detection: dict = {
                "wallet": wallet,
                "score": round(score.final, 4),
                "score_breakdown": {
                    "account_age":   round(score.account_age_score, 3),
                    "concentration": round(score.concentration_score, 3),
                    "velocity":      round(score.velocity_score, 3),
                    "win_rate":      round(score.win_rate_score, 3),
                    "roi":           round(score.roi_score, 3),
                },
                "account_age_days": round(score.account_age_days, 1),
                "political_categories": score.political_categories,
                "flags": score.flags,
                "last_detected_at": now_iso,
                "last_scored_at": now_iso,
            }
            if score.name:
                detection["name"] = score.name
            new_detections.append(detection)

        # Merge with existing watchlist (respect 48h TTL)
        existing = _load_json(SUSPICIOUS_WATCHLIST_PATH, [])
        cutoff = _now_ts() - self.WATCHLIST_TTL_HOURS * 3600
        active_existing = [
            e for e in existing
            if _parse_iso_ts(e.get("last_detected_at", "")) > cutoff
        ]
        new_wallets = {e["wallet"] for e in new_detections}
        merged = new_detections + [
            e for e in active_existing if e["wallet"] not in new_wallets
        ]
        merged.sort(key=lambda x: x["score"], reverse=True)
        watchlist = merged[: self.max_wallets]

        _save_json(SUSPICIOUS_WATCHLIST_PATH, watchlist)
        log.info(
            "Suspicious watchlist updated: %d wallets "
            "(%d new detections, %d carried over from TTL)",
            len(watchlist),
            len(new_detections),
            len(watchlist) - len(new_detections),
        )
        return watchlist

    # ------------------------------------------------------------------
    # Candidate gathering
    # ------------------------------------------------------------------

    def _gather_candidates(self) -> set[str]:
        """
        Collect candidate wallet addresses from recent large trades.
        We deliberately avoid the leaderboard here — those are established
        traders, not the new accounts we're hunting for.
        """
        candidates: set[str] = set()
        trades = self.client.get_recent_large_trades(
            min_size_usdc=self.config.get("polymarket", {}).get(
                "discovery_min_trade_size_usdc", 500
            ),
            limit=self.scan_limit,
        )
        for t in trades:
            wallet = (
                t.get("proxyWallet")
                or t.get("maker")
                or t.get("user")
                or t.get("address")
            )
            if wallet:
                candidates.add(wallet.lower())
        log.info("Gathered %d candidate wallets from recent large trades", len(candidates))
        return candidates

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_wallet(self, wallet: str) -> SuspiciousnessScore:
        score = SuspiciousnessScore()

        # ── Step 1: account age (proxy = oldest recorded trade) ───────
        age_days = self._get_account_age_days(wallet)
        if age_days is None:
            return SuspiciousnessScore(
                disqualified=True, reason="could not determine account age"
            )
        if age_days >= self.max_age_days:
            return SuspiciousnessScore(
                disqualified=True,
                reason=f"account age {age_days:.0f}d >= max {self.max_age_days:.0f}d",
            )

        score.account_age_days = age_days
        score.account_age_score = self._calc_age_score(age_days)

        # ── Step 2: fetch 30-day activity and current positions ────────
        window_start = int(_now_ts() - 30 * 86400)
        activity = self.client.get_wallet_activity(
            wallet, limit=200, start=window_start
        )
        trades = [a for a in activity if a.get("type", "").upper() == "TRADE"]
        positions = self.client.get_wallet_positions(wallet)

        # Capture display name from first activity record
        if activity:
            score.name = activity[0].get("name") or activity[0].get("pseudonym") or ""

        # ── Step 3: political market classification ────────────────────
        political_trades = []
        political_categories_seen: set[str] = set()
        for t in trades:
            title = t.get("title", "").lower()
            matched_cats = _political_match(title)
            if matched_cats:
                political_trades.append(t)
                political_categories_seen.update(matched_cats)

        # Also classify current positions (supplementary)
        political_positions = [
            p for p in positions
            if _political_match(p.get("title", "").lower())
        ]

        score.political_categories = sorted(political_categories_seen)

        # ── Step 4: concentration from trading ACTIVITY (not positions)
        # Using USDC volume ratio so large political bets count more.
        # Positions-based concentration fails for new wallets because most
        # of their positions have already resolved and are no longer visible.
        score.concentration_pct = _calc_concentration_from_activity(
            trades, political_trades
        )
        score.concentration_score = max(
            0.0,
            (score.concentration_pct - CONCENTRATION_BASE) / (1.0 - CONCENTRATION_BASE),
        )

        # ── Step 5: win rate — positions first, activity as fallback ───
        # New wallets often have 0 open positions (all resolved already),
        # so win rate is treated as a low-weight bonus rather than a gate.
        # We derive it from open positions when available.
        score.win_rate_political = _calc_position_win_rate(political_positions) if political_positions else 0.0
        score.win_rate_score = max(
            0.0,
            (score.win_rate_political - MIN_SUSPICIOUS_WIN_RATE) / (1.0 - MIN_SUSPICIOUS_WIN_RATE),
        )

        # ── Step 6: trade velocity ─────────────────────────────────────
        effective_age = max(age_days, 1.0)
        tpd = len(trades) / effective_age
        score.velocity_trades_per_day = tpd
        score.velocity_score = min(tpd / VELOCITY_NORM, 1.0)

        # ── Step 7: daily P&L rate (from open positions only) ──────────
        total_pnl = _calc_total_pnl(positions)
        daily_pnl = total_pnl / effective_age
        score.daily_pnl_usdc = daily_pnl
        score.roi_score = min(max(daily_pnl, 0.0) / DAILY_PNL_NORM, 1.0)

        # ── Step 8: final score ────────────────────────────────────────
        score.final = round(
            score.account_age_score  * WEIGHTS["account_age"]
            + score.concentration_score * WEIGHTS["concentration"]
            + score.velocity_score   * WEIGHTS["velocity"]
            + score.win_rate_score   * WEIGHTS["win_rate"]
            + score.roi_score        * WEIGHTS["roi"],
            4,
        )

        # ── Step 9: human-readable flags ──────────────────────────────
        flags: list[str] = []
        flags.append(f"new_account_{age_days:.0f}d")
        if score.win_rate_political >= MIN_SUSPICIOUS_WIN_RATE:
            flags.append(f"political_win_rate_{score.win_rate_political*100:.0f}%")
        if tpd >= 5:
            flags.append(f"trade_velocity_{tpd:.1f}_per_day")
        if score.concentration_pct >= 0.80:
            flags.append(f"concentration_{score.concentration_pct*100:.0f}%_political")
        if daily_pnl >= 1000:
            flags.append(f"daily_pnl_${daily_pnl:.0f}")
        if daily_pnl >= EXTREME_DAILY_PNL_USD or total_pnl >= EXTREME_TOTAL_PNL_USD:
            flags.append("extreme_roi")
        if score.political_categories:
            flags.append(f"categories:[{','.join(score.political_categories)}]")
        score.flags = flags

        return score

    # ------------------------------------------------------------------
    # Account age helper
    # ------------------------------------------------------------------

    def _get_account_age_days(self, wallet: str) -> float | None:
        """
        Fetch the oldest recorded trade for this wallet to estimate account age.
        Uses sortBy=TIMESTAMP sortDirection=ASC limit=1.
        Returns age in days, or None if no activity found.
        """
        oldest = self.client.get_wallet_activity(
            wallet,
            limit=1,
            sort_by="TIMESTAMP",
            sort_direction="ASC",
        )
        if not oldest:
            return None
        first_ts = _extract_ts(oldest[0])
        if first_ts <= 0:
            return None
        age_seconds = _now_ts() - first_ts
        return age_seconds / 86400.0

    # ------------------------------------------------------------------
    # Age score
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_age_score(age_days: float) -> float:
        if age_days < AGE_TIER_1:
            return 1.0
        if age_days < AGE_TIER_2:
            return 0.8
        return 0.5  # < AGE_MAX (already disqualified if >= AGE_MAX)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _political_match(title_lower: str) -> list[str]:
    """Return list of matched political category names for a given market title."""
    matched = []
    for category, keywords in POLITICAL_CATEGORIES.items():
        if any(kw in title_lower for kw in keywords):
            matched.append(category)
    return matched


def _calc_position_win_rate(positions: list[dict]) -> float:
    """Win rate on a list of positions (cashPnl > 0 or redeemable)."""
    if not positions:
        return 0.0
    wins = sum(
        1 for p in positions
        if float(p.get("cashPnl", 0) or 0) > 0 or p.get("redeemable") is True
    )
    return wins / len(positions)


def _calc_concentration_from_activity(
    all_trades: list[dict], political_trades: list[dict]
) -> float:
    """
    Fraction of total USDC trading volume in political markets.
    Uses activity (trade history) rather than open positions, so it works
    correctly for new wallets that have already closed most of their positions.
    """
    def _usdc(t: dict) -> float:
        return float(t.get("usdcSize") or t.get("size") or 0)

    total_vol = sum(_usdc(t) for t in all_trades)
    political_vol = sum(_usdc(t) for t in political_trades)
    if total_vol <= 0:
        return 0.0
    return political_vol / total_vol


def _calc_total_pnl(positions: list[dict]) -> float:
    """Sum unrealized + realized P&L across all positions."""
    total = 0.0
    for p in positions:
        total += float(p.get("cashPnl", 0) or 0)
        total += float(p.get("realizedPnl", 0) or 0)
    return total


def _extract_ts(record: dict) -> float:
    """Best-effort Unix timestamp from an activity record."""
    for key in ("timestamp", "createdAt", "created_at", "time"):
        val = record.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return float(val) / 1000 if val > 1e12 else float(val)
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return 0.0


def _parse_iso_ts(iso_str: str) -> float:
    """Parse ISO 8601 string to Unix timestamp; returns 0.0 on failure."""
    if not iso_str:
        return 0.0
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
