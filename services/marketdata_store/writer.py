"""Batched, non-blocking writes to ClickHouse.

Callers (the tick callback, a Historify download, the chain recorder) only
append to an in-memory buffer and return at once. One background thread
flushes every ``FLUSH_SECONDS`` with one insert per table, never one per row.

Bounded: each table's buffer holds at most ``MAX_BUFFERED_ROWS``. If ClickHouse
is down long enough to fill it, the oldest rows are dropped and counted, so a
dead server costs recorded data, never memory or the trading path.

Threads: the callers and the flush thread are ordinary threads (green under
eventlet, real under gthread), so one plain lock serves both. The lock covers
only the buffer swap; the network work happens after it is released.
"""

import threading
from collections import deque

from services.marketdata_store.clickhouse import (
    ClickHouseError,
    clickhouse_enabled,
    ensure_schema,
    get_client,
)
from utils.logging import get_logger

logger = get_logger(__name__)

FLUSH_SECONDS = 1.0
MAX_BUFFERED_ROWS = 200_000
MAX_ROWS_PER_INSERT = 50_000
TABLES = ("ohlcv", "ticks", "option_chain")


class BatchWriter:
    def __init__(self, client_factory=get_client, schema=ensure_schema):
        self._client_factory = client_factory
        self._ensure_schema = schema
        self._lock = threading.Lock()
        self._buffers = {table: deque(maxlen=MAX_BUFFERED_ROWS) for table in TABLES}
        self._stop = threading.Event()
        self._thread = None
        self._schema_ready = False
        self.rows_written = dict.fromkeys(TABLES, 0)
        self.rows_dropped = dict.fromkeys(TABLES, 0)
        self.last_error = None

    def enqueue(self, table: str, rows: list[dict]) -> None:
        """Buffer rows for ``table``. Never blocks on the network."""
        if not rows:
            return
        with self._lock:
            buffer = self._buffers[table]
            overflow = max(0, len(buffer) + len(rows) - MAX_BUFFERED_ROWS)
            buffer.extend(rows)  # deque(maxlen) drops the oldest past the bound
            self.rows_dropped[table] += overflow
            if self._thread is None:
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._run, args=(self._stop,), name="clickhouse-writer", daemon=True
                )
                self._thread.start()

    def _take(self, table: str) -> list[dict]:
        with self._lock:
            buffer = self._buffers[table]
            count = min(len(buffer), MAX_ROWS_PER_INSERT)
            return [buffer.popleft() for _ in range(count)]

    def _put_back(self, table: str, rows: list[dict]) -> None:
        """Return unsent rows to the front, oldest first, within the bound."""
        with self._lock:
            buffer = self._buffers[table]
            room = MAX_BUFFERED_ROWS - len(buffer)
            keep = rows[-room:] if room > 0 else []
            self.rows_dropped[table] += len(rows) - len(keep)
            buffer.extendleft(reversed(keep))

    def flush(self) -> bool:
        """Send what is buffered. Returns False if anything failed to send."""
        ok = True
        client = None
        for table in TABLES:
            while True:
                rows = self._take(table)
                if not rows:
                    break
                try:
                    if client is None:
                        client = self._client_factory()
                    if not self._schema_ready:
                        self._ensure_schema(client)
                        self._schema_ready = True
                    client.insert(table, rows)
                    with self._lock:
                        self.rows_written[table] += len(rows)
                        self.last_error = None
                except (ClickHouseError, ValueError) as e:
                    self._put_back(table, rows)
                    logger.warning(f"ClickHouse write of {len(rows)} {table} rows deferred: {e}")
                    with self._lock:
                        self.last_error = "ClickHouse is not accepting writes. See the logs."
                    ok = False
                    break
        return ok

    def _run(self, stop: threading.Event) -> None:
        backoff = FLUSH_SECONDS
        while not stop.wait(backoff):
            try:
                backoff = FLUSH_SECONDS if self.flush() else min(backoff * 2, 30.0)
            except Exception as e:
                logger.exception(f"ClickHouse writer error: {e}")
                backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        """Stop the thread and make one last attempt to send the buffer."""
        with self._lock:
            thread, self._thread = self._thread, None
            stop = self._stop
        stop.set()
        if thread is not None:
            thread.join(timeout=10)
        try:
            self.flush()
        except Exception as e:
            logger.exception(f"ClickHouse final flush failed: {e}")

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._thread is not None,
                "buffered": {table: len(buffer) for table, buffer in self._buffers.items()},
                "rows_written": dict(self.rows_written),
                "rows_dropped": dict(self.rows_dropped),
                "last_error": self.last_error,
            }


_writer = BatchWriter()


def get_writer() -> BatchWriter:
    return _writer


def write(table: str, rows: list[dict]) -> bool:
    """Buffer rows when ClickHouse is configured. Returns False when it is not."""
    if not clickhouse_enabled():
        return False
    _writer.enqueue(table, rows)
    return True


def stop_writer() -> None:
    _writer.stop()
