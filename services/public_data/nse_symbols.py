"""OpenAlgo symbols and end-of-day prices built from the NSE bhavcopy.

Symbols follow the platform format (CLAUDE.md, Symbol Format):

    equity   INFY
    future   BANKNIFTY24APR24FUT
    option   NIFTY28MAR2420800CE, VEDL25APR24292.5CE
    index    NIFTY, BANKNIFTY (exchange NSE_INDEX)
"""

from datetime import date

import pandas as pd

from services.public_data.nse_archive import fetch_bhavcopy
from utils.thread_safe_cache import LockedTTLCache

#: Cash-market series that trade as ordinary equity. EQ wins when a ticker is
#: listed in more than one.
EQUITY_SERIES = ("EQ", "BE", "BZ", "SM", "ST")

#: F&O index underlyings, which are also the OpenAlgo NSE_INDEX symbol names.
INDEX_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")

#: Neither file carries a tick size. These are the common values and are only
#: used to round sandbox limit prices.
DEFAULT_TICK_SIZE = 0.05

SYMTOKEN_COLUMNS = [
    "symbol",
    "brsymbol",
    "name",
    "exchange",
    "brexchange",
    "token",
    "expiry",
    "strike",
    "lotsize",
    "instrumenttype",
    "tick_size",
]


def format_strike(strike: float) -> str:
    """20800.0 -> "20800", 292.5 -> "292.5"."""
    value = float(strike)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _expiry_label(series: pd.Series) -> pd.Series:
    """2026-11-23 -> 23-NOV-26, the expiry format every broker master uses."""
    return pd.to_datetime(series).dt.strftime("%d-%b-%y").str.upper()


def fo_symbols(fo: pd.DataFrame) -> pd.DataFrame:
    """NFO rows of the symbol table, with the close of each contract."""
    frame = fo[fo["FinInstrmTp"].isin(["IDF", "STF", "IDO", "STO"])].copy()
    frame["expiry"] = _expiry_label(frame["XpryDt"])
    compact = frame["expiry"].str.replace("-", "", regex=False)

    is_future = frame["FinInstrmTp"].isin(["IDF", "STF"])
    frame["instrumenttype"] = frame["OptnTp"].where(~is_future, "FUT")
    frame["strike"] = frame["StrkPric"].where(~is_future, 0).fillna(0).astype(float)

    strike_text = frame["strike"].map(format_strike)
    frame["symbol"] = frame["TckrSymb"] + compact + "FUT"
    options = ~is_future
    frame.loc[options, "symbol"] = (
        frame.loc[options, "TckrSymb"]
        + compact[options]
        + strike_text[options]
        + frame.loc[options, "OptnTp"]
    )

    return pd.DataFrame(
        {
            "symbol": frame["symbol"],
            "brsymbol": frame["FinInstrmNm"],
            "name": frame["TckrSymb"],
            "exchange": "NFO",
            "brexchange": "NFO",
            "token": frame["FinInstrmId"].astype(str),
            "expiry": frame["expiry"],
            "strike": frame["strike"],
            "lotsize": frame["NewBrdLotQty"].fillna(1).astype(int),
            "instrumenttype": frame["instrumenttype"],
            "tick_size": DEFAULT_TICK_SIZE,
        }
    )


def equity_symbols(cm: pd.DataFrame) -> pd.DataFrame:
    """NSE equity rows of the symbol table."""
    frame = cm[cm["SctySrs"].isin(EQUITY_SERIES)].copy()
    rank = {series: i for i, series in enumerate(EQUITY_SERIES)}
    frame["_rank"] = frame["SctySrs"].map(rank)
    frame = frame.sort_values("_rank").drop_duplicates("TckrSymb", keep="first")

    return pd.DataFrame(
        {
            "symbol": frame["TckrSymb"],
            "brsymbol": frame["TckrSymb"] + "-" + frame["SctySrs"],
            "name": frame["FinInstrmNm"],
            "exchange": "NSE",
            "brexchange": "NSE",
            "token": frame["FinInstrmId"].astype(str),
            "expiry": "",
            "strike": 0.0,
            "lotsize": 1,
            "instrumenttype": "EQ",
            "tick_size": DEFAULT_TICK_SIZE,
        }
    )


def index_symbols() -> pd.DataFrame:
    """The F&O index underlyings, which trade as NSE_INDEX symbols."""
    return pd.DataFrame(
        {
            "symbol": list(INDEX_UNDERLYINGS),
            "brsymbol": list(INDEX_UNDERLYINGS),
            "name": list(INDEX_UNDERLYINGS),
            "exchange": "NSE_INDEX",
            "brexchange": "NSE_INDEX",
            "token": list(INDEX_UNDERLYINGS),
            "expiry": "",
            "strike": 0.0,
            "lotsize": 1,
            "instrumenttype": "INDEX",
            "tick_size": DEFAULT_TICK_SIZE,
        }
    )


def build_symbol_table(cm: pd.DataFrame | None, fo: pd.DataFrame | None) -> pd.DataFrame:
    parts = [index_symbols()]
    if cm is not None:
        parts.append(equity_symbols(cm))
    if fo is not None:
        parts.append(fo_symbols(fo))
    table = pd.concat(parts, ignore_index=True)
    return table.drop_duplicates(["symbol", "exchange"], keep="first")[SYMTOKEN_COLUMNS]


def _bar(row: pd.Series) -> dict:
    return {
        "open": float(row["OpnPric"]),
        "high": float(row["HghPric"]),
        "low": float(row["LwPric"]),
        "close": float(row["ClsPric"]),
        "prev_close": float(row["PrvsClsgPric"]),
        "volume": int(row["TtlTradgVol"]),
        "oi": int(row["OpnIntrst"]) if pd.notna(row.get("OpnIntrst")) else 0,
    }


# OpenAlgo NFO symbol -> row label in that day's F&O frame, per trading day.
_fo_index = LockedTTLCache(maxsize=16, ttl=6 * 3600)


def _fo_row_index(day: date, fo: pd.DataFrame) -> dict:
    index = _fo_index.get(day)
    if index is None:
        table = fo_symbols(fo)
        index = dict(zip(table["symbol"], table.index, strict=True))
        _fo_index[day] = index
    return index


def lookup_bar(symbol: str, exchange: str, day: date) -> dict | None:
    """One day's OHLC for an OpenAlgo symbol, or None if that day has no row for it.

    Raises:
        ArchiveUnavailable: The day's file had to be fetched and could not be.
    """
    if exchange == "NSE":
        cm = fetch_bhavcopy("CM", day)
        if cm is None:
            return None
        rows = cm[(cm["TckrSymb"] == symbol) & cm["SctySrs"].isin(EQUITY_SERIES)]
        if rows.empty:
            return None
        rank = {series: i for i, series in enumerate(EQUITY_SERIES)}
        row = rows.assign(_rank=rows["SctySrs"].map(rank)).sort_values("_rank").iloc[0]
        return _bar(row)

    if exchange not in ("NFO", "NSE_INDEX"):
        return None
    fo = fetch_bhavcopy("FO", day)
    if fo is None:
        return None

    if exchange == "NSE_INDEX":
        # The bhavcopy carries an index only as the underlying of its contracts.
        rows = fo[fo["TckrSymb"] == symbol]
        if rows.empty:
            return None
        close = float(rows["UndrlygPric"].iloc[0])
        return {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "prev_close": 0.0,
            "volume": 0,
            "oi": 0,
        }

    label = _fo_row_index(day, fo).get(symbol)
    if label is None:
        return None
    return _bar(fo.loc[label])
