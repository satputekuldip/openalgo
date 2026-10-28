"""Option-chain recorder (Phase 0 of the ClickHouse plan): selection, rows, store.

No broker and no network: contracts come from hand-made symbol rows, ticks are
fed straight to the callback, and the store writes to a DuckDB file in a
temporary directory.
"""

from datetime import date, datetime

import pytest

from services.chain_recorder import store
from services.chain_recorder.config import RecorderConfig, underlying_exchange
from services.chain_recorder.recorder import ChainRecorder, _in_market_hours
from services.chain_recorder.selection import (
    Contract,
    nearest_expiries,
    parse_expiry,
    select_contracts,
    strikes_around,
)


def _rows():
    rows = []
    for expiry in ("27-OCT-26", "03-NOV-26", "10-NOV-26", "24-NOV-26"):
        for strike in range(22000, 23600, 100):
            for kind in ("CE", "PE"):
                rows.append(
                    {
                        "symbol": f"NIFTY{expiry.replace('-', '')}{strike}{kind}",
                        "exchange": "NFO",
                        "expiry": expiry,
                        "strike": float(strike),
                        "instrumenttype": kind,
                    }
                )
    rows.append({"symbol": "NIFTY24NOV26FUT", "exchange": "NFO", "expiry": "24-NOV-26",
                 "strike": 0.0, "instrumenttype": "FUT"})  # fmt: skip
    return rows


# -- selection -----------------------------------------------------------------


def test_parse_expiry_reads_the_symbol_table_format():
    assert parse_expiry("24-NOV-26") == date(2026, 11, 24)
    assert parse_expiry("") is None


def test_expired_contracts_are_skipped():
    assert nearest_expiries(_rows(), date(2026, 10, 28), 2) == [
        date(2026, 11, 3),
        date(2026, 11, 10),
    ]


def test_strikes_are_centred_on_the_money():
    strikes = [float(s) for s in range(22000, 23600, 100)]
    assert strikes_around(strikes, 22749.75, 2) == [22500.0, 22600.0, 22700.0, 22800.0, 22900.0]
    # Near the edge of the list it keeps what exists rather than failing.
    assert strikes_around(strikes, 21000, 2) == [22000.0, 22100.0, 22200.0]


def test_select_contracts_takes_both_sides_and_skips_futures():
    chosen = select_contracts(_rows(), "NIFTY", 22749.75, date(2026, 10, 28), 2, 3)
    assert len(chosen) == 2 * 7 * 2  # expiries x strikes x (CE, PE)
    assert {c.option_type for c in chosen} == {"CE", "PE"}
    assert all(isinstance(c, Contract) and c.underlying == "NIFTY" for c in chosen)
    assert min(c.strike for c in chosen) == 22400.0
    assert max(c.strike for c in chosen) == 23000.0


# -- config --------------------------------------------------------------------


def test_recording_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("CHAIN_RECORDER_ENABLED", raising=False)
    assert RecorderConfig.from_env().enabled is False
    assert ChainRecorder().start("anyone") is False


def test_config_bounds_bad_values(monkeypatch):
    monkeypatch.setenv("CHAIN_RECORDER_ENABLED", "true")
    monkeypatch.setenv("CHAIN_RECORDER_EXPIRIES", "99")
    monkeypatch.setenv("CHAIN_RECORDER_STRIKES_EACH_SIDE", "not a number")
    monkeypatch.setenv("CHAIN_RECORDER_UNDERLYINGS", " nifty , ,infy")
    config = RecorderConfig.from_env()
    assert config.enabled and config.expiries == 6 and config.strikes_each_side == 20
    assert config.underlyings == ("NIFTY", "INFY")
    assert underlying_exchange("NIFTY") == "NSE_INDEX"
    assert underlying_exchange("INFY") == "NSE"


@pytest.mark.parametrize(
    "moment, open_",
    [
        (datetime(2026, 10, 28, 9, 14), False),
        (datetime(2026, 10, 28, 9, 15), True),
        (datetime(2026, 10, 28, 15, 30), True),
        (datetime(2026, 10, 28, 15, 31), False),
        (datetime(2026, 10, 31, 11, 0), False),  # Saturday
    ],
)
def test_market_hours(moment, open_):
    assert _in_market_hours(moment) is open_


# -- ticks to rows ---------------------------------------------------------------


def _recorder_with(contracts):
    recorder = ChainRecorder()
    recorder._contracts = contracts
    recorder._underlying_keys = {"NIFTY": "NSE_INDEX:NIFTY"}
    return recorder


def test_rows_carry_the_latest_tick_and_the_underlying_price():
    contracts = select_contracts(_rows(), "NIFTY", 22749.75, date(2026, 10, 28), 1, 0)
    recorder = _recorder_with(contracts)
    ce = next(c for c in contracts if c.option_type == "CE")

    recorder._on_tick({"symbol": "NIFTY", "exchange": "NSE_INDEX", "data": {"ltp": 22750.5}})
    recorder._on_tick({"symbol": ce.symbol, "exchange": "NFO", "data": {"ltp": 101.0, "oi": 500}})
    # A later tick that carries only the price keeps the OI already known.
    recorder._on_tick({"symbol": ce.symbol, "exchange": "NFO", "data": {"ltp": 102.5}})

    rows = recorder.build_rows(datetime(2026, 10, 28, 10, 15))
    assert len(rows) == 1  # the PE has had no tick yet, so it is left out
    row = rows[0]
    assert (row["ltp"], row["oi"], row["underlying_ltp"]) == (102.5, 500, 22750.5)
    assert row["expiry"] == date(2026, 11, 3) and row["option_type"] == "CE"
    assert row["tick_age_ms"] >= 0


# -- store -----------------------------------------------------------------------


def test_snapshots_land_in_duckdb(monkeypatch, tmp_path):
    import database.historify_db as historify_db

    monkeypatch.setattr(historify_db, "HISTORIFY_DB_PATH", str(tmp_path / "historify.duckdb"))
    contracts = select_contracts(_rows(), "NIFTY", 22749.75, date(2026, 10, 28), 1, 1)
    recorder = _recorder_with(contracts)
    for c in contracts:
        recorder._on_tick({"symbol": c.symbol, "exchange": "NFO", "data": {"ltp": 50.0, "oi": 10}})

    store.ensure_table()
    store.ensure_table()  # idempotent
    first = store.write_snapshot(recorder.build_rows(datetime(2026, 10, 28, 10, 15, 0)))
    second = store.write_snapshot(recorder.build_rows(datetime(2026, 10, 28, 10, 15, 5)))
    assert first == second == len(contracts) == 6
    assert store.write_snapshot([]) == 0

    with historify_db.get_connection() as conn:
        count, snapshots = conn.execute(
            f"SELECT count(*), count(DISTINCT ts) FROM {store.TABLE}"
        ).fetchone()
    assert (count, snapshots) == (12, 2)
