"""
Alpaca API client singleton.
Provides thin wrappers around alpaca-py for use by all strategy scripts.
"""
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

try:
    from alpaca.trading.requests import OptionOrderRequest as _OptionOrderRequest
    _HAS_OPTION_REQUEST = True
except ImportError:
    _HAS_OPTION_REQUEST = False
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
import config

_trading_client = None
_data_client = None


def trading_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(
            api_key=config.ALPACA_KEY,
            secret_key=config.ALPACA_SECRET,
            paper=config.PAPER_TRADING,
        )
    return _trading_client


def data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(
            api_key=config.ALPACA_KEY,
            secret_key=config.ALPACA_SECRET,
        )
    return _data_client


def get_account():
    return trading_client().get_account()


def get_position(symbol: str):
    """Returns position or None if not held."""
    try:
        return trading_client().get_open_position(symbol)
    except Exception:
        return None


def get_open_orders(symbol: str = None):
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbol=symbol)
    return trading_client().get_orders(req)


def get_latest_price(symbol: str) -> float:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = data_client().get_stock_latest_quote(req)
    q = quote[symbol]
    # Use midpoint of bid/ask
    return (q.bid_price + q.ask_price) / 2


def submit_market_order(symbol: str, qty: int, side: OrderSide, reason: str = "") -> object:
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    order = trading_client().submit_order(req)
    return order


def submit_option_order(
    symbol: str,
    qty: int,
    side: OrderSide,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float = None,
) -> object:
    if _HAS_OPTION_REQUEST:
        req = _OptionOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            type=order_type,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )
    else:
        # Older alpaca-py versions: use MarketOrderRequest/LimitOrderRequest with option symbol
        if order_type == OrderType.LIMIT and limit_price is not None:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
    return trading_client().submit_order(req)


def close_position(symbol: str) -> object:
    return trading_client().close_position(symbol)


def get_market_clock():
    return trading_client().get_clock()
