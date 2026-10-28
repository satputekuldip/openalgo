"""Where the chain recorder writes.

With ClickHouse configured (``CLICKHOUSE_URL``), snapshots go to its
``option_chain`` table through the batched writer (services/marketdata_store).
Otherwise they go to a table in the Historify DuckDB file, through Historify's
own connection helper and write serialisation (``database/historify_db.py``).

The DuckDB table is created when the recorder first starts, not at app start,
so an install that never enables recording never gets it.
"""

import pandas as pd

from database.historify_db import _retry_duckdb_conflict, _serialized_write, get_connection
from services.marketdata_store.clickhouse import clickhouse_enabled
from services.marketdata_store.writer import write
from utils.logging import get_logger

logger = get_logger(__name__)

TABLE = "option_chain_snapshots"

COLUMNS = [
    "ts",
    "underlying",
    "underlying_ltp",
    "expiry",
    "strike",
    "option_type",
    "symbol",
    "exchange",
    "ltp",
    "bid",
    "ask",
    "volume",
    "oi",
    "tick_age_ms",
]

_CREATE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    ts TIMESTAMP NOT NULL,          -- snapshot time, IST wall clock
    underlying VARCHAR NOT NULL,
    underlying_ltp DOUBLE,
    expiry DATE NOT NULL,
    strike DOUBLE NOT NULL,
    option_type VARCHAR NOT NULL,   -- CE or PE
    symbol VARCHAR NOT NULL,
    exchange VARCHAR NOT NULL,
    ltp DOUBLE,
    bid DOUBLE,
    ask DOUBLE,
    volume BIGINT,
    oi BIGINT,
    tick_age_ms INTEGER             -- how old the last tick was at snapshot time
)
"""


def ensure_table() -> None:
    """Create the DuckDB table, unless snapshots go to ClickHouse."""
    if clickhouse_enabled():
        return  # the writer creates ClickHouse's option_chain table itself
    _ensure_duckdb_table()


@_serialized_write
@_retry_duckdb_conflict
def _ensure_duckdb_table() -> None:
    with get_connection() as conn:
        conn.execute(_CREATE)


def write_snapshot(rows: list[dict]) -> int:
    """Store one snapshot. ClickHouse when configured, else DuckDB. Returns the row count."""
    if not rows:
        return 0
    if clickhouse_enabled():
        write("option_chain", [_for_clickhouse(row) for row in rows])
        return len(rows)
    return _write_duckdb(rows)


def _for_clickhouse(row: dict) -> dict:
    out = dict(row)
    out["ts"] = row["ts"].strftime("%Y-%m-%d %H:%M:%S")
    out["expiry"] = row["expiry"].isoformat()
    return out


@_serialized_write
@_retry_duckdb_conflict
def _write_duckdb(rows: list[dict]) -> int:
    frame = pd.DataFrame(rows, columns=COLUMNS)  # noqa: F841 (read by DuckDB by name)
    with get_connection() as conn:
        conn.execute(f"INSERT INTO {TABLE} SELECT * FROM frame")
    return len(rows)
