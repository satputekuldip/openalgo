"""Running without a broker: public-data mode, NSE symbols and the Postgres engine.

None of these touch the network or a PostgreSQL server. The engine tests build
engines without connecting, and the symbol tests use a hand-made bhavcopy frame
with the same columns NSE publishes.
"""

import pandas as pd
import pytest

from database import engine_factory
from services.public_data import nse_symbols
from utils import data_mode

# ---------------------------------------------------------------------------
# utils.data_mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected", [("public", True), ("PUBLIC ", True), ("", False), ("broker", False)]
)
def test_public_data_mode_reads_the_env_key(monkeypatch, value, expected):
    monkeypatch.setenv("OPENALGO_DATA_MODE", value)
    assert data_mode.public_data_mode() is expected


def test_an_install_without_the_key_is_not_in_public_mode(monkeypatch):
    monkeypatch.delenv("OPENALGO_DATA_MODE", raising=False)
    assert data_mode.public_data_mode() is False


def test_live_mode_is_refused_without_a_broker(monkeypatch):
    from database import settings_db

    monkeypatch.setenv("OPENALGO_DATA_MODE", "public")
    with pytest.raises(data_mode.LiveModeUnavailable) as raised:
        settings_db.set_analyze_mode(False)
    assert "Connect a broker" in str(raised.value)
    assert settings_db.get_analyze_mode() is True


def test_analyze_mode_is_unchanged_for_a_broker_install(monkeypatch):
    from database import settings_db

    monkeypatch.delenv("OPENALGO_DATA_MODE", raising=False)
    settings_db.init_db()
    original = settings_db.get_analyze_mode()
    try:
        settings_db.set_analyze_mode(False)
        assert settings_db.get_analyze_mode() is False
        settings_db.set_analyze_mode(True)
        assert settings_db.get_analyze_mode() is True
    finally:
        settings_db.set_analyze_mode(original)


# ---------------------------------------------------------------------------
# database.engine_factory: PostgreSQL with one schema per former SQLite file
# ---------------------------------------------------------------------------

PG = "postgresql://u:p@127.0.0.1:5433/openalgo"


@pytest.fixture
def fresh_engine_cache(monkeypatch):
    monkeypatch.setattr(engine_factory, "_PG_ENGINES", {})


def test_postgres_engines_are_shared_per_url(fresh_engine_cache):
    first = engine_factory.create_db_engine(f"{PG}?schema=main")
    again = engine_factory.create_db_engine(f"{PG}?schema=main")
    other = engine_factory.create_db_engine(f"{PG}?schema=logs")
    assert first is again
    assert first is not other


def test_schema_is_removed_from_the_url_and_psycopg_is_chosen(fresh_engine_cache):
    engine = engine_factory.create_db_engine(f"{PG}?schema=sandbox")
    assert engine.url.drivername == "postgresql+psycopg"
    assert "schema" not in engine.url.query
    assert engine.pool.size() == engine_factory._PG_POOL_SIZE


@pytest.mark.parametrize("schema", ["main; DROP TABLE x", "Main", "1logs", "a-b"])
def test_an_unsafe_schema_name_is_refused(fresh_engine_cache, schema):
    with pytest.raises(ValueError):
        engine_factory.create_db_engine(f"{PG}?schema={schema}")


def test_sqlite_still_gets_nullpool():
    from sqlalchemy.pool import NullPool

    engine = engine_factory.create_db_engine("sqlite:///db/openalgo-test.db")
    assert isinstance(engine.pool, NullPool)


# ---------------------------------------------------------------------------
# services.public_data.nse_symbols
# ---------------------------------------------------------------------------

_COLUMNS = [
    "FinInstrmTp", "FinInstrmId", "TckrSymb", "SctySrs", "XpryDt", "StrkPric", "OptnTp",
    "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric", "UndrlygPric",
    "OpnIntrst", "TtlTradgVol", "NewBrdLotQty",
]  # fmt: skip


def _fo():
    rows = [
        ["IDF", 1, "NIFTY", None, "2026-11-24", None, None, "NIFTY26NOVFUT",
         100, 110, 90, 105, 101, 104.5, 5000, 10, 65],
        ["IDO", 2, "NIFTY", None, "2026-11-24", 23000.0, "CE", "NIFTY26NOV23000CE",
         1, 2, 0.5, 1.5, 1.2, 104.5, 700, 20, 65],
        ["STO", 3, "VEDL", None, "2026-11-24", 292.5, "PE", "VEDL26NOV292.5PE",
         3, 4, 2, 3.5, 3.1, 300, 900, 30, 1150],
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=_COLUMNS)


def _cm():
    rows = [
        ["STK", 10, "INFY", "BE", None, None, None, "INFOSYS", 1, 1, 1, 1, 1, None, None, 1, 1],
        ["STK", 11, "INFY", "EQ", None, None, None, "INFOSYS", 2, 3, 1, 2.5, 2, None, None, 9, 1],
        ["STK", 12, "SGBJUN28", "GB", None, None, None, "GOLD", 5, 5, 5, 5, 5, None, None, 1, 1],
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=_COLUMNS)


def test_fo_symbols_follow_the_platform_format():
    table = nse_symbols.fo_symbols(_fo()).set_index("symbol")
    assert list(table.index) == ["NIFTY24NOV26FUT", "NIFTY24NOV2623000CE", "VEDL24NOV26292.5PE"]
    assert table.loc["NIFTY24NOV26FUT", "expiry"] == "24-NOV-26"
    assert table.loc["NIFTY24NOV26FUT", "instrumenttype"] == "FUT"
    assert table.loc["NIFTY24NOV26FUT", "strike"] == 0
    assert table.loc["VEDL24NOV26292.5PE", "lotsize"] == 1150
    assert set(table["exchange"]) == {"NFO"}


def test_equity_prefers_the_eq_series_and_skips_bonds():
    table = nse_symbols.equity_symbols(_cm())
    assert list(table["symbol"]) == ["INFY"]
    assert table.iloc[0]["brsymbol"] == "INFY-EQ"


def test_symbol_table_has_indices_and_unique_symbols():
    table = nse_symbols.build_symbol_table(_cm(), _fo())
    assert not table.duplicated(["symbol", "exchange"]).any()
    assert set(nse_symbols.INDEX_UNDERLYINGS) <= set(
        table[table["exchange"] == "NSE_INDEX"]["symbol"]
    )
    assert list(table.columns) == nse_symbols.SYMTOKEN_COLUMNS


@pytest.mark.parametrize("strike, text", [(20800.0, "20800"), (292.5, "292.5"), (0.25, "0.25")])
def test_format_strike(strike, text):
    assert nse_symbols.format_strike(strike) == text


# ---------------------------------------------------------------------------
# broker/publicdata streaming adapter: one republish thread per connection
# ---------------------------------------------------------------------------


def test_a_thread_stuck_across_a_reconnect_still_stops(monkeypatch):
    """disconnect() gives up on a thread stuck in a slow publish after 5s. A
    reconnect must not revive it: with one shared stop signal, connect() cleared
    it and the old thread kept republishing beside the new one forever."""
    import threading

    from broker.publicdata.streaming import publicdata_adapter as module

    monkeypatch.setattr(module, "FOLLOW_UP_SECONDS", 0)
    adapter = module.PublicdataWebSocketAdapter()
    stuck, release = threading.Event(), threading.Event()
    calls = []

    def slow_publish(symbol, exchange, modes):
        calls.append(threading.current_thread())
        if len(calls) == 2:  # the old thread's follow-up send: hang here
            stuck.set()
            release.wait(30)

    monkeypatch.setattr(adapter, "_publish", slow_publish)
    adapter.connect()
    first = adapter._thread
    adapter.subscribe("RELIANCE", "NSE", 1)
    assert stuck.wait(5)

    adapter.disconnect()  # join times out: the old thread is still in _publish
    assert first.is_alive()
    adapter.connect()
    second = adapter._thread

    release.set()
    first.join(5)
    try:
        assert not first.is_alive(), "the old republish thread outlived its connection"
        assert second.is_alive()
    finally:
        adapter.disconnect()
    assert not second.is_alive()


# ---------------------------------------------------------------------------
# Moving from public data to a broker
# ---------------------------------------------------------------------------


def _session_app():
    from flask import Flask

    app = Flask(__name__)
    app.secret_key = "test"
    return app


@pytest.mark.parametrize("mode, valid", [("public", True), ("", False)])
def test_a_no_broker_session_ends_once_a_broker_is_configured(monkeypatch, mode, valid):
    from datetime import datetime

    from flask import session

    from utils import session as session_utils

    monkeypatch.setenv("OPENALGO_DATA_MODE", mode)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    with _session_app().test_request_context():
        session.update(logged_in=True, login_time=datetime.now().isoformat(), broker="publicdata")
        assert session_utils.is_session_valid() is valid


def test_a_broker_session_is_unaffected(monkeypatch):
    from datetime import datetime

    from flask import session

    from utils import session as session_utils

    monkeypatch.delenv("OPENALGO_DATA_MODE", raising=False)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    with _session_app().test_request_context():
        session.update(logged_in=True, login_time=datetime.now().isoformat(), broker="angel")
        assert session_utils.is_session_valid() is True


def test_login_does_not_resume_a_no_broker_session_once_a_broker_is_set(monkeypatch):
    from types import SimpleNamespace

    import database.auth_db as auth_db
    from blueprints import auth
    from utils import auth_utils

    monkeypatch.delenv("OPENALGO_DATA_MODE", raising=False)
    stored = SimpleNamespace(
        is_revoked=False, broker="publicdata", auth="x", feed_token=None, user_id=None
    )
    monkeypatch.setattr(auth_db, "get_auth_token_dbquery", lambda name: stored)
    monkeypatch.setattr(auth_db, "decrypt_token", lambda value: "public-token")
    # Observe the resume itself: without the guard, the never-expiring token
    # passes the funds check and the stored session is logged straight back in.
    resumed = []
    monkeypatch.setattr(auth_utils, "handle_auth_success", lambda **kw: resumed.append(kw))
    with _session_app().test_request_context():
        assert auth._try_resume_broker_session("devtrader") is None
    assert resumed == []
