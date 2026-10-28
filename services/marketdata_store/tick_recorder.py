"""Record every streamed tick into ClickHouse.

Runs only when ClickHouse is configured, and ``CLICKHOUSE_RECORD_TICKS`` is not
'false'. It subscribes to MarketDataService at the lowest priority for every
symbol, so it records exactly what the platform already streams (charts,
watchlists, strategies, the sandbox) and adds no subscription of its own. Each
tick is appended to the writer's buffer and the callback returns at once.

It must run in the process that runs the WebSocket proxy, because that is the
MarketDataService every tick passes through. On the development server the
proxy is a thread of the app, so the app starts it
(``start_tick_recorder_if_proxy_in_process``). Under gunicorn and Docker the
proxy is its own process and starts it there (websocket_proxy/server.py main),
and the app does not, so each tick is recorded once.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from services.marketdata_store.clickhouse import clickhouse_enabled
from services.marketdata_store.writer import write
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_lock = threading.Lock()
_subscriber_id = None


def tick_recording_enabled() -> bool:
    return (
        clickhouse_enabled()
        and os.getenv("CLICKHOUSE_RECORD_TICKS", "true").strip().lower() != "false"
    )


def _tick_time(data: dict) -> str:
    """The tick's own time when it carries one (epoch ms or s), else now. IST wall clock."""
    raw = data.get("timestamp")
    try:
        value = float(raw)
        seconds = value / 1000 if value > 1e11 else value
    except (TypeError, ValueError):
        seconds = time.time()
    return datetime.fromtimestamp(seconds, IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def tick_row(message: dict) -> dict | None:
    symbol, exchange = message.get("symbol"), message.get("exchange")
    data = message.get("data") or {}
    if not symbol or not exchange or data.get("ltp") is None:
        return None
    return {
        "ts": _tick_time(data),
        "exchange": exchange,
        "symbol": symbol,
        "ltp": float(data.get("ltp") or 0),
        "volume": int(data.get("volume") or 0),
        "oi": int(data.get("oi") or 0),
        "bid": float(data.get("bid") or 0),
        "ask": float(data.get("ask") or 0),
    }


def _on_tick(message: dict) -> None:
    row = tick_row(message)
    if row is not None:
        write("ticks", [row])


def start_tick_recorder() -> bool:
    global _subscriber_id
    if not tick_recording_enabled():
        return False
    from services.market_data_service import SubscriberPriority, get_market_data_service

    with _lock:
        if _subscriber_id is not None:
            return True
        _subscriber_id = get_market_data_service().subscribe_with_priority(
            priority=SubscriberPriority.LOW,
            event_type="all",
            callback=_on_tick,
            filter_symbols=None,
            name="clickhouse_tick_recorder",
        )
    logger.info("Recording streamed ticks to ClickHouse")
    return True


def start_tick_recorder_if_proxy_in_process() -> bool:
    """Start in the app process only when the proxy runs inside it (thread mode)."""
    if not tick_recording_enabled():
        return False
    from websocket_proxy.app_integration import resolve_proxy_mode

    if resolve_proxy_mode() != "thread":
        return False  # the proxy process records instead
    return start_tick_recorder()


def stop_tick_recorder() -> None:
    global _subscriber_id
    with _lock:
        subscriber_id, _subscriber_id = _subscriber_id, None
    if subscriber_id is not None:
        from services.market_data_service import get_market_data_service

        get_market_data_service().unsubscribe_priority(subscriber_id)
