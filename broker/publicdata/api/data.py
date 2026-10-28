"""Market data for an install without a broker, from official NSE end-of-day files.

Every price here is the close of the latest published trading day, not a live
price. Quotes say so with ``source`` and ``as_of``, so a screen or a sandbox
fill can show the age of what it used. Intraday intervals are not offered:
the official files are daily.
"""

from datetime import date, datetime, timedelta

import pandas as pd

from services.public_data.nse_archive import latest_trading_day, trading_days
from services.public_data.nse_symbols import lookup_bar
from utils.logging import get_logger

logger = get_logger(__name__)

HISTORY_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]

#: Longest range one history request may walk. Each day is one file, fetched once.
MAX_HISTORY_DAYS = 400


class PublicDataError(Exception):
    """A request the official files cannot answer. The message is for a trader."""


def _no_data(symbol: str, exchange: str) -> PublicDataError:
    return PublicDataError(
        f"{exchange}:{symbol} is not in the latest NSE end-of-day file. Without a broker, "
        "prices cover NSE equities, NSE F&O contracts and the F&O indices."
    )


class BrokerData:
    #: Daily only. The official files have one bar per trading day.
    timeframe_map = {"D": "D"}

    def __init__(self, auth_token=None):
        self.auth_token = auth_token

    def _latest_day(self) -> date:
        day = latest_trading_day()
        if day is None:
            raise PublicDataError(
                "No NSE end-of-day file is available yet. Prices appear once NSE "
                "publishes the day's files, usually after 7 PM IST."
            )
        return day

    def get_quotes(self, symbol: str, exchange: str) -> dict:
        day = self._latest_day()
        bar = lookup_bar(symbol, exchange, day)
        if bar is None:
            raise _no_data(symbol, exchange)
        return {
            "ask": 0,
            "bid": 0,
            "ask_qty": 0,
            "bid_qty": 0,
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "ltp": bar["close"],
            "prev_close": bar["prev_close"],
            "volume": bar["volume"],
            "oi": bar["oi"],
            "source": "nse_bhavcopy",
            "as_of": day.isoformat(),
        }

    def get_multiquotes(self, symbols: list) -> list:
        results = []
        for item in symbols:
            symbol, exchange = item.get("symbol"), item.get("exchange")
            try:
                results.append(
                    {
                        "symbol": symbol,
                        "exchange": exchange,
                        "data": self.get_quotes(symbol, exchange),
                    }
                )
            except PublicDataError as e:
                results.append({"symbol": symbol, "exchange": exchange, "error": str(e)})
        return results

    def get_depth(self, symbol: str, exchange: str) -> dict:
        """End-of-day files carry no order book: one level with the close and no size."""
        quote = self.get_quotes(symbol, exchange)
        empty = [{"price": 0, "quantity": 0} for _ in range(5)]
        return {
            "bids": empty,
            "asks": empty,
            "ltp": quote["ltp"],
            "ltq": 0,
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "prev_close": quote["prev_close"],
            "volume": quote["volume"],
            "oi": quote["oi"],
            "totalbuyqty": 0,
            "totalsellqty": 0,
        }

    get_market_depth = get_depth

    def get_history(
        self, symbol: str, exchange: str, timeframe: str, from_date: str, to_date: str
    ) -> pd.DataFrame:
        if timeframe not in self.timeframe_map:
            raise PublicDataError(
                "Without a broker only daily candles are available, from NSE end-of-day "
                "files. Choose the daily interval."
            )

        start = datetime.strptime(str(from_date)[:10], "%Y-%m-%d").date()
        end = datetime.strptime(str(to_date)[:10], "%Y-%m-%d").date()
        if (end - start) > timedelta(days=MAX_HISTORY_DAYS):
            raise PublicDataError(
                f"Ask for at most {MAX_HISTORY_DAYS} days at a time. Each day is one NSE file, "
                "and longer ranges are downloaded in parts."
            )

        rows = []
        for day in trading_days(start, end):
            bar = lookup_bar(symbol, exchange, day)
            if bar is None:
                continue
            # Midnight of the trading day, the convention the broker plugins use
            # for daily bars.
            rows.append(
                {
                    "timestamp": int(pd.Timestamp(day).timestamp()),
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "oi": bar["oi"],
                }
            )
        return pd.DataFrame(rows, columns=HISTORY_COLUMNS)
