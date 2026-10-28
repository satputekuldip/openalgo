"""Market data stream for an install without a broker.

There is no live feed. Each subscribed symbol is sent the latest official NSE
end-of-day price when it is subscribed, and again every ``REPUBLISH_SECONDS``
so a client that joins later receives it within seconds. Every tick carries
``source`` and ``as_of`` so a screen can say the price is a close, not live.

Publishing goes through the base adapter, which connects to the proxy's SUB on
the fixed ZMQ_PORT like every other adapter (never binds).
"""

import time

from services.public_data.nse_archive import ArchiveUnavailable, latest_trading_day
from services.public_data.nse_symbols import lookup_bar
from utils import runtime as _runtime
from utils.logging import get_logger
from websocket_proxy.base_adapter import BaseBrokerWebSocketAdapter

# Real threads: this adapter runs in the proxy, which is unpatched under
# gunicorn, and on the dev server's ordinary threads. See the Zerodha adapter.
_real_threading = _runtime.original("threading")

# The proxy subscribes the adapter once per symbol and fans out to every client,
# so a client that joins later only sees the next republish. Each one is an
# in-memory lookup, which keeps a short period cheap.
REPUBLISH_SECONDS = 5

#: A new subscription is sent again after this delay. The first send can be
#: dropped while a freshly connected ZMQ publisher is still joining the bus.
FOLLOW_UP_SECONDS = 1.0

_MODE_NAMES = {1: "LTP", 2: "QUOTE", 3: "DEPTH"}


class PublicdataWebSocketAdapter(BaseBrokerWebSocketAdapter):
    def __init__(self):
        super().__init__()
        self.logger = get_logger("publicdata_websocket")
        self.broker_name = "publicdata"
        self.user_id = None
        self.connected = False
        self.lock = _real_threading.Lock()
        self.subscriptions = {}  # {(symbol, exchange): set(modes)}
        # Each republish thread gets its own pair, made in connect(). A thread
        # that outlives disconnect()'s join (stuck in a slow download) then
        # still sees its own stop, instead of one a later connect() cleared.
        self._stop = _real_threading.Event()
        self._wake = _real_threading.Event()
        self._fresh = set()  # subscriptions still owed their follow-up send
        self._thread = None

    def initialize(self, broker_name, user_id, auth_data=None):
        if broker_name != self.broker_name:
            return self._create_error_response(
                "INVALID_BROKER", f"Invalid broker name: {broker_name}"
            )
        self.user_id = user_id
        return self._create_success_response("Adapter initialized")

    def connect(self):
        with self.lock:
            if self.connected:
                return self._create_success_response("Already connected")
            self.connected = True
            self._stop = _real_threading.Event()
            self._wake = _real_threading.Event()
            self._thread = _real_threading.Thread(
                target=self._republish_loop,
                args=(self._stop, self._wake),
                name="publicdata-republish",
                daemon=True,
            )
            self._thread.start()
        return self._create_success_response("Connected to public end-of-day data")

    def disconnect(self):
        with self.lock:
            self.connected = False
            self.subscriptions.clear()
            thread, self._thread = self._thread, None
            stop, wake = self._stop, self._wake
        stop.set()
        wake.set()
        if thread is not None:
            thread.join(timeout=5)
        self.cleanup_zmq()
        return self._create_success_response("Disconnected")

    def subscribe(self, symbol, exchange, mode=2, depth_level=5):
        if mode not in _MODE_NAMES:
            return self._create_error_response("INVALID_MODE", f"Invalid mode {mode}")
        with self.lock:
            self.subscriptions.setdefault((symbol, exchange), set()).add(mode)
            self._fresh.add((symbol, exchange))
            wake = self._wake
        self._publish(symbol, exchange, {mode})
        wake.set()
        return self._create_success_response(
            f"Subscribed to {symbol}", symbol=symbol, exchange=exchange, mode=mode
        )

    def unsubscribe(self, symbol, exchange, mode=2):
        with self.lock:
            modes = self.subscriptions.get((symbol, exchange))
            if modes is not None:
                modes.discard(mode)
                if not modes:
                    del self.subscriptions[(symbol, exchange)]
        return self._create_success_response(f"Unsubscribed from {symbol}")

    def _republish_loop(self, stop, wake):
        while not stop.is_set():
            woken = wake.wait(REPUBLISH_SECONDS)
            wake.clear()
            if stop.is_set():
                return
            if woken:
                # Only the new subscriptions, once, after the bus has settled.
                time.sleep(FOLLOW_UP_SECONDS)
            with self.lock:
                keys = self._fresh if woken else self.subscriptions.keys()
                snapshot = [
                    (key, set(self.subscriptions[key])) for key in keys if key in self.subscriptions
                ]
                if woken:
                    self._fresh = set()
            for (symbol, exchange), modes in snapshot:
                if stop.is_set():
                    return
                self._publish(symbol, exchange, modes)

    def _publish(self, symbol, exchange, modes):
        try:
            day = latest_trading_day()
            bar = lookup_bar(symbol, exchange, day) if day else None
        except ArchiveUnavailable as e:
            self.logger.warning(f"No end-of-day price for {exchange}:{symbol}: {e}")
            return
        except Exception as e:
            self.logger.exception(f"Error reading end-of-day price for {exchange}:{symbol}: {e}")
            return
        if bar is None:
            return

        timestamp = int(time.time() * 1000)
        for mode in modes:
            tick = {
                "symbol": symbol,
                "exchange": exchange,
                "mode": _MODE_NAMES[mode].lower() if mode != 3 else "full",
                "ltp": bar["close"],
                "timestamp": timestamp,
                "source": "nse_bhavcopy",
                "as_of": day.isoformat(),
            }
            if mode >= 2:
                tick.update(
                    {
                        "open": bar["open"],
                        "high": bar["high"],
                        "low": bar["low"],
                        "close": bar["prev_close"],
                        "volume": bar["volume"],
                        "oi": bar["oi"],
                    }
                )
            if mode == 3:
                empty = [{"price": 0, "quantity": 0, "orders": 0} for _ in range(5)]
                tick["depth"] = {"buy": empty, "sell": empty}
            self.publish_market_data(f"{exchange}_{symbol}_{_MODE_NAMES[mode]}", tick)
