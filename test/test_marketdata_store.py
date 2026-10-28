"""Optional ClickHouse market-data store: off by default, batched, bounded.

Unit tests use a fake client, so they need no server. The last test runs
against a real ClickHouse when CLICKHOUSE_TEST_URL is set (the dev compose one:
http://openalgo:openalgo@127.0.0.1:8124/openalgo) and is skipped otherwise.
"""

import json
import os
from datetime import date, datetime

import httpx
import numpy as np
import pandas as pd
import pytest

from services.marketdata_store import clickhouse, writer
from services.marketdata_store.tick_recorder import tick_recording_enabled, tick_row


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.inserts = []

    def insert(self, table, rows):
        if self.fail:
            raise clickhouse.ClickHouseError("down")
        self.inserts.append((table, list(rows)))
        return len(rows)


def _writer(client, schema_calls=None):
    calls = schema_calls if schema_calls is not None else []
    return writer.BatchWriter(client_factory=lambda: client, schema=lambda c: calls.append(c))


# -- off by default ---------------------------------------------------------------


def test_everything_is_off_without_a_url(monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_URL", raising=False)
    assert clickhouse.clickhouse_enabled() is False
    assert tick_recording_enabled() is False
    assert writer.write("ticks", [{"a": 1}]) is False
    assert writer.get_writer().status()["running"] is False


def test_historify_does_not_mirror_without_a_url(monkeypatch):
    import database.historify_db as historify_db

    monkeypatch.delenv("CLICKHOUSE_URL", raising=False)
    sent = []
    monkeypatch.setattr(writer, "write", lambda *a: sent.append(a))
    historify_db._mirror_to_clickhouse(pd.DataFrame({"timestamp": [1], "close": [1.0]}))
    assert sent == []


def test_tick_recording_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_URL", "http://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("CLICKHOUSE_RECORD_TICKS", "false")
    assert tick_recording_enabled() is False


# -- the writer --------------------------------------------------------------------


def test_rows_are_sent_in_one_insert_per_table_and_schema_made_once():
    client, schema_calls = FakeClient(), []
    w = _writer(client, schema_calls)
    w._buffers["ticks"].extend({"n": i} for i in range(3))
    w._buffers["ohlcv"].extend({"n": i} for i in range(2))
    assert w.flush() is True
    assert w.flush() is True  # nothing left; no empty inserts
    assert [(t, len(r)) for t, r in client.inserts] == [("ohlcv", 2), ("ticks", 3)]
    assert len(schema_calls) == 1
    assert w.status()["rows_written"]["ticks"] == 3


def test_a_failed_insert_keeps_the_rows_in_order():
    client = FakeClient(fail=True)
    w = _writer(client)
    w._buffers["ticks"].extend({"n": i} for i in range(5))
    assert w.flush() is False
    assert [r["n"] for r in w._buffers["ticks"]] == [0, 1, 2, 3, 4]
    assert w.status()["last_error"]

    client.fail = False
    assert w.flush() is True
    assert [r["n"] for r in client.inserts[0][1]] == [0, 1, 2, 3, 4]


def test_the_buffer_is_bounded_and_drops_the_oldest(monkeypatch):
    monkeypatch.setattr(writer, "MAX_BUFFERED_ROWS", 4)
    w = _writer(FakeClient())
    w._buffers = {table: writer.deque(maxlen=4) for table in writer.TABLES}
    w._stop.set()  # keep the background thread from flushing during the test
    w._thread = object()
    w.enqueue("ticks", [{"n": i} for i in range(6)])
    assert [r["n"] for r in w._buffers["ticks"]] == [2, 3, 4, 5]
    assert w.status()["rows_dropped"]["ticks"] == 2


def test_enqueue_starts_one_thread_and_stop_flushes_what_is_left():
    client = FakeClient()
    w = _writer(client)
    w.enqueue("ticks", [{"n": 1}])
    w.enqueue("ticks", [{"n": 2}])
    thread = w._thread
    assert thread is not None and thread.is_alive()
    w.stop()
    assert not thread.is_alive()
    assert sum(len(rows) for _, rows in client.inserts) == 2


# -- the client --------------------------------------------------------------------


def test_insert_sends_json_rows_with_plain_numbers(monkeypatch):
    seen = {}

    def handler(request):
        seen["query"] = request.url.params["query"]
        seen["database"] = request.url.params["database"]
        seen["body"] = request.content.decode()
        return httpx.Response(200, text="")

    client = clickhouse.ClickHouseClient("http://u:p@ch.local:8124/openalgo")
    client._http = httpx.Client(
        base_url="http://ch.local:8124",
        transport=httpx.MockTransport(handler),
        params={"database": "openalgo"},
    )
    rows = [{"volume": np.int64(7), "close": np.float64(1.5), "day": date(2026, 10, 28)}]
    assert client.insert("ohlcv", rows) == 1
    assert seen["query"] == "INSERT INTO ohlcv FORMAT JSONEachRow"
    assert seen["database"] == "openalgo"
    assert json.loads(seen["body"]) == {"volume": 7, "close": 1.5, "day": "2026-10-28"}


def test_a_bad_database_name_is_refused():
    with pytest.raises(ValueError):
        clickhouse.ClickHouseClient("http://u:p@h:8124/open;algo")


# -- ticks -------------------------------------------------------------------------------


def test_tick_rows_use_the_ticks_own_time_in_ist():
    row = tick_row(
        {"symbol": "RELIANCE", "exchange": "NSE",
         "data": {"ltp": 1196.2, "volume": 10, "timestamp": 1793157156463}}
    )  # fmt: skip
    assert row["ts"] == "2026-10-28 08:42:36.463"
    assert (row["ltp"], row["volume"], row["oi"]) == (1196.2, 10, 0)
    assert tick_row({"symbol": "X", "exchange": "NSE", "data": {}}) is None


# -- against a real server ----------------------------------------------------------------

LIVE = os.getenv("CLICKHOUSE_TEST_URL")


@pytest.mark.skipif(not LIVE, reason="set CLICKHOUSE_TEST_URL to run against a ClickHouse server")
def test_round_trip_against_clickhouse(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_URL", LIVE)
    client = clickhouse.ClickHouseClient(LIVE)
    clickhouse.ensure_schema(client)
    clickhouse.ensure_schema(client)  # idempotent

    marker = f"TEST{datetime.now():%H%M%S%f}"
    try:
        # Ticks build 1-minute bars through the materialized view.
        ticks = [
            {"ts": f"2026-10-28 10:15:{s:02d}.000", "exchange": "NSE", "symbol": marker,
             "ltp": price, "volume": vol, "oi": 0, "bid": 0, "ask": 0}
            for s, price, vol in ((1, 100.0, 1000), (10, 102.0, 0), (20, 103.0, 1500), (59, 101.0, 1800))
        ]  # fmt: skip
        client.insert("ticks", ticks)
        bar = client.query(
            f"SELECT open, high, low, close, volume FROM tick_bars_1m_view WHERE symbol = '{marker}'"
        )
        assert len(bar) == 1
        assert (bar[0]["open"], bar[0]["high"], bar[0]["low"], bar[0]["close"]) == (
            100,
            103,
            100,
            101,
        )
        # 800, not 1800: the price-only tick (volume 0) must not count.
        assert int(bar[0]["volume"]) == 800  # Int64 arrives as a JSON string

        # A minute with only price-only ticks reports no volume.
        client.insert("ticks", [{"ts": "2026-10-28 10:16:05.000", "exchange": "NSE",
                                 "symbol": marker, "ltp": 101.5, "volume": 0, "oi": 0,
                                 "bid": 0, "ask": 0}])  # fmt: skip
        quiet = client.query(
            f"SELECT volume FROM tick_bars_1m_view WHERE symbol = '{marker}' "
            "AND minute = toDateTime('2026-10-28 10:16:00', 'Asia/Kolkata')"
        )
        assert int(quiet[0]["volume"]) == 0

        # A re-downloaded bar replaces the earlier one.
        bar_row = {"symbol": marker, "exchange": "NSE", "interval": "1m", "timestamp": 1793157000,
                   "open": 1, "high": 2, "low": 1, "close": 2, "volume": 5, "oi": 0}  # fmt: skip
        client.insert("ohlcv", [{**bar_row, "version": 1}])
        client.insert("ohlcv", [{**bar_row, "close": 9, "version": 2}])
        rows = client.query(f"SELECT close FROM ohlcv FINAL WHERE symbol = '{marker}'")
        assert rows == [{"close": 9}]
    finally:
        for table in ("ticks", "tick_bars_1m", "ohlcv"):
            client.execute(f"DELETE FROM {table} WHERE symbol = '{marker}'")
        client.close()


@pytest.mark.parametrize(
    "mode, starts", [("thread", True), ("subprocess", False), ("external", False)]
)
def test_ticks_are_recorded_only_where_the_proxy_runs(monkeypatch, mode, starts):
    """In subprocess or external mode the proxy process records, so the app must
    not: it would record the same ticks again from its own MarketDataService."""
    import websocket_proxy.app_integration as app_integration
    from services.marketdata_store import tick_recorder

    monkeypatch.setenv("CLICKHOUSE_URL", "http://u:p@127.0.0.1:1/db")
    monkeypatch.setattr(app_integration, "resolve_proxy_mode", lambda: mode)
    started = []
    monkeypatch.setattr(tick_recorder, "start_tick_recorder", lambda: started.append(True) or True)
    assert tick_recorder.start_tick_recorder_if_proxy_in_process() is starts
    assert started == ([True] if starts else [])


def test_bars_are_mirrored_when_clickhouse_is_set(monkeypatch):
    import database.historify_db as historify_db

    monkeypatch.setenv("CLICKHOUSE_URL", "http://u:p@127.0.0.1:1/db")
    sent = []
    monkeypatch.setattr(writer, "write", lambda table, rows: sent.append((table, rows)))
    historify_db._mirror_to_clickhouse(pd.DataFrame({"timestamp": [1, 2], "close": [1.0, 2.0]}))
    assert [t for t, _ in sent] == ["ohlcv"] and len(sent[0][1]) == 2
    assert all("version" in row for row in sent[0][1])
