"""Which option contracts the chain recorder subscribes to.

Pure functions over symbol-table rows, so the choice can be tested without a
broker or a database.

The ClickHouse plan's Phase 0 asked for "every expiry" every 5 seconds. A feed
cannot carry that: a broker allows about 3,000 streamed symbols in all
(MAX_SYMBOLS_PER_WEBSOCKET x MAX_WEBSOCKET_CONNECTIONS), and NIFTY alone lists
well over that across its expiries. So the recorder takes the nearest
``expiries`` expiries and ``strikes_each_side`` strikes either side of the
money, and the plan's workload is described in those terms.
"""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class Contract:
    symbol: str
    exchange: str
    underlying: str
    expiry: date
    strike: float
    option_type: str  # "CE" or "PE"


def parse_expiry(text: str) -> date | None:
    """'24-NOV-26' (the symbol table's format) -> date(2026, 11, 24)."""
    try:
        return datetime.strptime(text.strip().title(), "%d-%b-%y").date()
    except (ValueError, AttributeError):
        return None


def nearest_expiries(rows: list[dict], today: date, count: int) -> list[date]:
    """The ``count`` nearest expiries on or after ``today``."""
    expiries = {parse_expiry(row["expiry"]) for row in rows}
    upcoming = sorted(e for e in expiries if e is not None and e >= today)
    return upcoming[:count]


def strikes_around(strikes: list[float], spot: float, each_side: int) -> list[float]:
    """The strike nearest ``spot`` and ``each_side`` strikes above and below it."""
    ordered = sorted(set(strikes))
    if not ordered:
        return []
    atm = min(range(len(ordered)), key=lambda i: (abs(ordered[i] - spot), ordered[i]))
    return ordered[max(0, atm - each_side) : atm + each_side + 1]


def select_contracts(
    rows: list[dict],
    underlying: str,
    spot: float,
    today: date,
    expiries: int,
    strikes_each_side: int,
) -> list[Contract]:
    """CE and PE contracts for the nearest expiries, around the money for each.

    ``rows`` are symbol-table rows for one underlying's options: dicts with
    symbol, exchange, expiry, strike and instrumenttype.
    """
    options = [r for r in rows if r.get("instrumenttype") in ("CE", "PE")]
    chosen = []
    for expiry in nearest_expiries(options, today, expiries):
        of_expiry = [r for r in options if parse_expiry(r["expiry"]) == expiry]
        keep = set(strikes_around([float(r["strike"]) for r in of_expiry], spot, strikes_each_side))
        for r in of_expiry:
            if float(r["strike"]) in keep:
                chosen.append(
                    Contract(
                        symbol=r["symbol"],
                        exchange=r["exchange"],
                        underlying=underlying,
                        expiry=expiry,
                        strike=float(r["strike"]),
                        option_type=r["instrumenttype"],
                    )
                )
    return sorted(chosen, key=lambda c: (c.expiry, c.strike, c.option_type))
