"""
Market hours check via Alpaca's clock API.
"""
import sys
from core import alpaca_client


def is_market_open() -> bool:
    clock = alpaca_client.get_market_clock()
    return clock.is_open


def exit_if_closed():
    """Call at the top of every strategy script."""
    if not is_market_open():
        print("Market is closed. Exiting.")
        sys.exit(0)
