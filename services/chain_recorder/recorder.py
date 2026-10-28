"""Option-chain recorder: Phase 0 of the ClickHouse plan, on DuckDB.

Subscribes to a fixed set of option contracts plus their underlyings, keeps
the latest tick of each in memory, and every ``interval_seconds`` writes one
snapshot of all of them to ``option_chain_snapshots`` in the Historify DuckDB
file. The recorded week is the workload the plan's benchmark is run on.

Off unless ``CHAIN_RECORDER_ENABLED = 'true'``. It starts after the master
contract loads (database/master_contract_cache_hook.py), records only on
weekdays during market hours, and stops at shutdown (utils/shutdown.py).

Threads: the tick callback runs where MarketDataService delivers it (a green
thread under eventlet, a real one elsewhere) and the snapshot loop is a plain
threading.Thread, so both sit on the same side of the eventlet boundary and
share an ordinary lock. Neither does I/O while holding it.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from services.chain_recorder import store
from services.chain_recorder.config import RecorderConfig, underlying_exchange
from services.chain_recorder.selection import Contract, select_contracts
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

#: Subscribe in batches so one request never carries thousands of symbols.
SUBSCRIBE_BATCH = 200


def _in_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return MARKET_OPEN[0] * 60 + MARKET_OPEN[1] <= minutes <= MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]


class ChainRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {}  # "EXCHANGE:SYMBOL" -> (received_monotonic, tick data dict)
        self._contracts: list[Contract] = []
        self._underlying_keys = {}  # underlying -> "EXCHANGE:SYMBOL"
        self._subscriber_id = None
        self._stop = threading.Event()
        self._thread = None
        self._username = None
        self.rows_written = 0
        self.snapshots_written = 0
        self.last_error = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, username: str, config: RecorderConfig | None = None) -> bool:
        config = config or RecorderConfig.from_env()
        if not config.enabled:
            return False
        with self._lock:
            if self._thread is not None:
                return True

        contracts, underlying_keys = self._resolve(username, config)
        if not contracts:
            logger.warning("Chain recorder: no option contracts found to record; not started")
            return False

        store.ensure_table()
        self._subscribe(username, contracts, underlying_keys)

        stop = threading.Event()
        thread = threading.Thread(
            target=self._loop,
            args=(stop, config.interval_seconds),
            name="chain-recorder",
            daemon=True,
        )
        with self._lock:
            self._contracts = contracts
            self._underlying_keys = underlying_keys
            self._username = username
            self._stop = stop
            self._thread = thread
        thread.start()
        logger.info(
            f"Chain recorder started: {len(contracts)} contracts across "
            f"{len(underlying_keys)} underlyings, every {config.interval_seconds}s"
        )
        return True

    def stop(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            stop = self._stop
            subscriber_id, self._subscriber_id = self._subscriber_id, None
            self._latest.clear()
        stop.set()
        if subscriber_id is not None:
            from services.market_data_service import get_market_data_service

            get_market_data_service().unsubscribe_priority(subscriber_id)
        if thread is not None:
            thread.join(timeout=10)
            logger.info("Chain recorder stopped")

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._thread is not None,
                "contracts": len(self._contracts),
                "symbols_with_ticks": len(self._latest),
                "snapshots_written": self.snapshots_written,
                "rows_written": self.rows_written,
                "last_error": self.last_error,
            }

    # -- setup ---------------------------------------------------------------

    def _resolve(self, username: str, config: RecorderConfig):
        from database.auth_db import get_api_key_for_tradingview
        from database.symbol import SymToken
        from services.quotes_service import get_quotes

        api_key = get_api_key_for_tradingview(username)
        today = datetime.now(IST).date()
        contracts, underlying_keys = [], {}

        for underlying in config.underlyings:
            exchange = underlying_exchange(underlying)
            ok, response, _ = get_quotes(underlying, exchange, api_key=api_key)
            spot = (response.get("data") or {}).get("ltp") if ok else None
            if not spot:
                logger.warning(f"Chain recorder: no price for {exchange}:{underlying}; skipped")
                continue

            rows = [
                {
                    "symbol": r.symbol,
                    "exchange": r.exchange,
                    "expiry": r.expiry or "",
                    "strike": r.strike or 0,
                    "instrumenttype": r.instrumenttype,
                }
                for r in SymToken.query.filter(
                    SymToken.name == underlying,
                    SymToken.exchange == "NFO",
                    SymToken.instrumenttype.in_(["CE", "PE"]),
                ).all()
            ]
            chosen = select_contracts(
                rows, underlying, float(spot), today, config.expiries, config.strikes_each_side
            )
            if chosen:
                contracts.extend(chosen)
                underlying_keys[underlying] = f"{exchange}:{underlying}"
        return contracts, underlying_keys

    def _subscribe(self, username: str, contracts, underlying_keys) -> None:
        from database.auth_db import get_api_key_for_tradingview, get_broker_name
        from services.market_data_service import SubscriberPriority, get_market_data_service
        from services.websocket_service import subscribe_to_symbols

        broker = get_broker_name(get_api_key_for_tradingview(username)) or "unknown"
        payload = [{"symbol": c.symbol, "exchange": c.exchange} for c in contracts]
        payload += [
            {"symbol": key.split(":", 1)[1], "exchange": key.split(":", 1)[0]}
            for key in underlying_keys.values()
        ]
        for start in range(0, len(payload), SUBSCRIBE_BATCH):
            ok, response, _ = subscribe_to_symbols(
                username=username,
                broker=broker,
                symbols=payload[start : start + SUBSCRIBE_BATCH],
                mode="Quote",
            )
            if not ok:
                logger.warning(f"Chain recorder: subscribe failed: {response.get('message')}")

        wanted = {f"{c.exchange}:{c.symbol}" for c in contracts} | set(underlying_keys.values())
        subscriber_id = get_market_data_service().subscribe_with_priority(
            priority=SubscriberPriority.LOW,
            event_type="all",
            callback=self._on_tick,
            filter_symbols=wanted,
            name="chain_recorder",
        )
        with self._lock:
            self._subscriber_id = subscriber_id

    # -- ticks and snapshots ---------------------------------------------------

    def _on_tick(self, message: dict) -> None:
        symbol, exchange = message.get("symbol"), message.get("exchange")
        if not symbol or not exchange:
            return
        data = message.get("data") or {}
        with self._lock:
            # Keyed by the subscribed set only (the subscription filters to it),
            # so this map never grows past the number of contracts recorded.
            previous = self._latest.get(f"{exchange}:{symbol}", (0.0, {}))[1]
            self._latest[f"{exchange}:{symbol}"] = (time.monotonic(), {**previous, **data})

    def _loop(self, stop: threading.Event, interval: int) -> None:
        while not stop.wait(interval - (time.time() % interval)):
            now = datetime.now(IST)
            if not _in_market_hours(now):
                continue
            rows = self.build_rows(now.replace(tzinfo=None, microsecond=0))
            try:
                written = store.write_snapshot(rows)
                with self._lock:
                    self.rows_written += written
                    self.snapshots_written += 1 if written else 0
                    self.last_error = None
            except Exception as e:
                logger.exception(f"Chain recorder: snapshot write failed: {e}")
                with self._lock:
                    self.last_error = "The last snapshot could not be saved. See the logs."

    def build_rows(self, snapshot_time: datetime) -> list[dict]:
        """One row per contract that has had a tick. Copies under the lock, builds outside it."""
        with self._lock:
            latest = dict(self._latest)
            contracts = list(self._contracts)
            underlying_keys = dict(self._underlying_keys)
        now = time.monotonic()

        spots = {}
        for underlying, key in underlying_keys.items():
            entry = latest.get(key)
            spots[underlying] = entry[1].get("ltp") if entry else None

        rows = []
        for c in contracts:
            entry = latest.get(f"{c.exchange}:{c.symbol}")
            if entry is None:
                continue
            received, data = entry
            rows.append(
                {
                    "ts": snapshot_time,
                    "underlying": c.underlying,
                    "underlying_ltp": spots.get(c.underlying),
                    "expiry": c.expiry,
                    "strike": c.strike,
                    "option_type": c.option_type,
                    "symbol": c.symbol,
                    "exchange": c.exchange,
                    "ltp": data.get("ltp"),
                    "bid": data.get("bid"),
                    "ask": data.get("ask"),
                    "volume": data.get("volume"),
                    "oi": data.get("oi"),
                    "tick_age_ms": int((now - received) * 1000),
                }
            )
        return rows


_recorder = ChainRecorder()


def get_chain_recorder() -> ChainRecorder:
    return _recorder


def start_for_admin_user() -> bool:
    """Start recording for the platform's one user, if recording is enabled."""
    config = RecorderConfig.from_env()
    if not config.enabled:
        return False
    from database.user_db import find_user_by_username

    user = find_user_by_username()
    if user is None:
        return False
    try:
        return _recorder.start(user.username, config)
    except Exception as e:
        logger.exception(f"Chain recorder could not start: {e}")
        return False


def stop_chain_recorder() -> None:
    _recorder.stop()
