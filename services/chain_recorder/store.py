"""Where the chain recorder writes: a table in the Historify DuckDB file.

It goes through Historify's own connection helper and write serialisation
(``database/historify_db.py``), so the ClickHouse plan's Phase 0 measures the
write path the platform really has, not a friendlier copy of it.

The table is created when the recorder first starts, not at app start, so an
install that never enables recording never gets it.
"""

import pandas as pd

from database.historify_db import _retry_duckdb_conflict, _serialized_write, get_connection
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


@_serialized_write
@_retry_duckdb_conflict
def ensure_table() -> None:
    with get_connection() as conn:
        conn.execute(_CREATE)


@_serialized_write
@_retry_duckdb_conflict
def write_snapshot(rows: list[dict]) -> int:
    """Append one snapshot's rows in a single insert. Returns the row count."""
    if not rows:
        return 0
    frame = pd.DataFrame(rows, columns=COLUMNS)  # noqa: F841 (read by DuckDB by name)
    with get_connection() as conn:
        conn.execute(f"INSERT INTO {TABLE} SELECT * FROM frame")
    return len(rows)
