"""ClickHouse access for market data: one shared HTTP client and the table set.

Optional. Nothing here runs unless ``CLICKHOUSE_URL`` is set in ``.env``:

    CLICKHOUSE_URL = 'http://openalgo:openalgo@127.0.0.1:8124/openalgo'

Talking HTTP through httpx rather than a native driver keeps this safe under
the default eventlet worker: httpx waits on Python sockets, which eventlet
patches, so a slow insert yields to the hub instead of stalling it (the
problem a C-level driver would have, see docs/development/postgres.md).

Tables:

- ``ohlcv``: bars Historify downloads, mirrored here (ReplacingMergeTree, so a
  re-downloaded bar replaces the earlier one).
- ``ticks``: every tick the platform streams, kept ``CLICKHOUSE_TICK_TTL_DAYS``.
- ``tick_bars_1m``: 1-minute bars built from ticks by a materialized view;
  read them through the ``tick_bars_1m_view`` view.
- ``option_chain``: option-chain snapshots from services/chain_recorder.
"""

import json
import os
import re
from urllib.parse import urlsplit

import httpx

from utils.lazy import LazyInit
from utils.logging import get_logger

logger = get_logger(__name__)

REQUEST_TIMEOUT_SECONDS = 30.0
_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def clickhouse_url() -> str:
    return os.getenv("CLICKHOUSE_URL", "").strip()


def clickhouse_enabled() -> bool:
    return bool(clickhouse_url())


def tick_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("CLICKHOUSE_TICK_TTL_DAYS", "90")))
    except ValueError:
        return 90


def _json_value(value):
    """numpy numbers become plain numbers; dates and anything else become text."""
    if hasattr(value, "item"):
        return value.item()
    return str(value)


class ClickHouseError(Exception):
    """ClickHouse refused or could not be reached. Details go to the log."""


class ClickHouseClient:
    def __init__(self, url: str):
        parts = urlsplit(url)
        self.database = parts.path.strip("/") or "default"
        if not _DATABASE_NAME.match(self.database):
            raise ValueError(f"Invalid ClickHouse database name: {self.database!r}")
        self._http = httpx.Client(
            base_url=f"{parts.scheme or 'http'}://{parts.hostname}:{parts.port or 8123}",
            auth=(parts.username or "default", parts.password or ""),
            params={"database": self.database},
            timeout=REQUEST_TIMEOUT_SECONDS,
            http1=True,
            http2=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    def execute(self, sql: str, body: str | None = None) -> str:
        """Run one statement. With ``body``, ``sql`` goes in the query string."""
        try:
            if body is None:
                response = self._http.post("/", content=sql)
            else:
                response = self._http.post("/", params={"query": sql}, content=body)
        except httpx.HTTPError as e:
            raise ClickHouseError(f"ClickHouse unreachable: {e}") from e
        if response.status_code != 200:
            raise ClickHouseError(f"ClickHouse error {response.status_code}: {response.text[:300]}")
        return response.text

    def insert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        body = "\n".join(json.dumps(row, default=_json_value) for row in rows)
        self.execute(f"INSERT INTO {table} FORMAT JSONEachRow", body)
        return len(rows)

    def query(self, sql: str) -> list[dict]:
        text = self.execute(f"{sql} FORMAT JSONEachRow")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def close(self) -> None:
        self._http.close()


def _build_client() -> ClickHouseClient:
    return ClickHouseClient(clickhouse_url())


_client = LazyInit(_build_client, name="clickhouse-client")


def get_client() -> ClickHouseClient:
    """The one shared client. Only call when clickhouse_enabled() is True."""
    return _client.get()


def schema_statements(ttl_days: int) -> list[str]:
    return [
        """CREATE TABLE IF NOT EXISTS ohlcv (
            symbol LowCardinality(String),
            exchange LowCardinality(String),
            interval LowCardinality(String),
            timestamp Int64,
            open Float64, high Float64, low Float64, close Float64,
            volume Int64,
            oi Int64,
            version UInt64
        ) ENGINE = ReplacingMergeTree(version)
        PARTITION BY toYYYYMM(toDateTime(timestamp))
        ORDER BY (exchange, symbol, interval, timestamp)""",
        f"""CREATE TABLE IF NOT EXISTS ticks (
            ts DateTime64(3, 'Asia/Kolkata'),
            exchange LowCardinality(String),
            symbol LowCardinality(String),
            ltp Float64,
            volume Int64,
            oi Int64,
            bid Float64,
            ask Float64
        ) ENGINE = MergeTree
        PARTITION BY toYYYYMM(ts)
        ORDER BY (exchange, symbol, ts)
        TTL toDateTime(ts) + INTERVAL {int(ttl_days)} DAY""",
        """CREATE TABLE IF NOT EXISTS tick_bars_1m (
            exchange LowCardinality(String),
            symbol LowCardinality(String),
            minute DateTime('Asia/Kolkata'),
            open AggregateFunction(argMin, Float64, DateTime64(3, 'Asia/Kolkata')),
            high SimpleAggregateFunction(max, Float64),
            low SimpleAggregateFunction(min, Float64),
            close AggregateFunction(argMax, Float64, DateTime64(3, 'Asia/Kolkata')),
            volume_max SimpleAggregateFunction(max, Int64),
            volume_min SimpleAggregateFunction(min, Int64),
            oi AggregateFunction(argMax, Int64, DateTime64(3, 'Asia/Kolkata'))
        ) ENGINE = AggregatingMergeTree
        PARTITION BY toYYYYMM(minute)
        ORDER BY (exchange, symbol, minute)""",
        """CREATE MATERIALIZED VIEW IF NOT EXISTS tick_bars_1m_mv TO tick_bars_1m AS
        SELECT exchange, symbol, toStartOfMinute(ts) AS minute,
               argMinState(ltp, ts) AS open, max(ltp) AS high, min(ltp) AS low,
               argMaxState(ltp, ts) AS close,
               max(volume) AS volume_max,
               min(if(volume > 0, volume, toInt64(9223372036854775807))) AS volume_min,
               argMaxState(oi, ts) AS oi
        FROM ticks WHERE ltp > 0
        GROUP BY exchange, symbol, minute""",
        # Ticks carry the day's cumulative volume, so a minute's volume is its
        # rise across the minute. Price-only ticks report volume 0, which would
        # make the whole day's volume look like one minute's, so they are kept
        # out of the minimum (the sentinel above); a minute with no volume at
        # all reads 0.
        """CREATE VIEW IF NOT EXISTS tick_bars_1m_view AS
        SELECT exchange, symbol, minute,
               argMinMerge(open) AS open, max(high) AS high, min(low) AS low,
               argMaxMerge(close) AS close,
               if(min(volume_min) = 9223372036854775807, 0,
                  max(volume_max) - min(volume_min)) AS volume,
               argMaxMerge(oi) AS oi
        FROM tick_bars_1m GROUP BY exchange, symbol, minute""",
        """CREATE TABLE IF NOT EXISTS option_chain (
            ts DateTime('Asia/Kolkata'),
            underlying LowCardinality(String),
            underlying_ltp Nullable(Float64),
            expiry Date,
            strike Float64,
            option_type LowCardinality(String),
            symbol String,
            exchange LowCardinality(String),
            ltp Nullable(Float64),
            bid Nullable(Float64),
            ask Nullable(Float64),
            volume Nullable(Int64),
            oi Nullable(Int64),
            tick_age_ms Int32
        ) ENGINE = MergeTree
        PARTITION BY toYYYYMM(expiry)
        ORDER BY (underlying, expiry, ts, strike, option_type)""",
    ]


def ensure_schema(client: ClickHouseClient | None = None) -> None:
    client = client or get_client()
    for statement in schema_statements(tick_ttl_days()):
        client.execute(statement)
