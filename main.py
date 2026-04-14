"""
TorpTradingBot — Polymarket copy trading bot entry point.

Usage:
    python main.py --task discover_wallets
    python main.py --task copy_trades
    python main.py --task scan_suspicious
    python main.py --task copy_suspicious
"""

import argparse
import logging
import os
import sys

import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/cron.log", mode="a"),
    ],
)
log = logging.getLogger("main")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _live_mode_guard(config: dict) -> None:
    """Exit early in live mode if required env vars are missing."""
    if config.get("mode", "dry_run") == "live":
        private_key = os.getenv("POLYGON_PRIVATE_KEY")
        api_key = os.getenv("POLYMARKET_API_KEY")
        if not private_key or not api_key:
            log.error(
                "Live mode requires POLYGON_PRIVATE_KEY and POLYMARKET_API_KEY env vars"
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# Task functions
# ---------------------------------------------------------------------------

def task_discover_wallets(config: dict) -> None:
    from strategies.polymarket_copy import WalletDiscovery

    log.info("Starting wallet discovery run")
    WalletDiscovery(config).run()
    log.info("Wallet discovery complete")


def task_copy_trades(config: dict) -> None:
    from strategies.polymarket_copy import CopyTrader

    _live_mode_guard(config)
    mode = config.get("mode", "dry_run")
    log.info("Starting copy trade run (mode=%s)", mode)
    CopyTrader(config, portfolio_path="state/virtual_portfolio.json").run()
    log.info("Copy trade run complete")


def task_scan_suspicious(config: dict) -> None:
    from strategies.suspicious_activity import SuspiciousActivityDetector

    if not config.get("suspicious", {}).get("enabled", True):
        log.info("Suspicious activity detection is disabled in config")
        return

    log.info("Starting suspicious activity scan")
    SuspiciousActivityDetector(config).scan()
    log.info("Suspicious activity scan complete")


def task_copy_suspicious(config: dict) -> None:
    from strategies.polymarket_copy import CopyTrader
    from strategies.suspicious_activity import (
        SUSPICIOUS_POSITIONS_PATH,
        SUSPICIOUS_WATCHLIST_PATH,
    )

    _live_mode_guard(config)
    mode = config.get("mode", "dry_run")
    log.info("Starting suspicious copy trade run (mode=%s)", mode)
    CopyTrader(
        config,
        watchlist_path=SUSPICIOUS_WATCHLIST_PATH,
        positions_path=SUSPICIOUS_POSITIONS_PATH,
        strategy_name="suspicious_copy",
        portfolio_path="state/suspicious_virtual_portfolio.json",
    ).run()
    log.info("Suspicious copy trade run complete")


# ---------------------------------------------------------------------------
# Task registry + CLI
# ---------------------------------------------------------------------------

TASKS = {
    "discover_wallets":  task_discover_wallets,
    "copy_trades":       task_copy_trades,
    "scan_suspicious":   task_scan_suspicious,
    "copy_suspicious":   task_copy_suspicious,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="TorpTradingBot")
    parser.add_argument(
        "--task",
        required=True,
        choices=list(TASKS.keys()),
        help="Task to run",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    TASKS[args.task](config)


if __name__ == "__main__":
    main()
