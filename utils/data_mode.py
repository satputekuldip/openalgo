"""Whether this install runs without a broker, on public market data.

An install chooses this with one ``.env`` key::

    OPENALGO_DATA_MODE = 'public'

Every install that has not set the key behaves exactly as before: a broker is
required, and nothing in this module changes any code path for it.

In public mode the broker keys in ``.env`` are optional, the OpenAlgo login
completes without a broker login, and the account is held in Sandbox mode
because there is no broker to send a live order to. Market data comes from the
``publicdata`` broker plugin (``broker/publicdata/``).

See docs/plans/2026-10-28-start-without-broker-plan.md.
"""

import os

#: The broker plugin that serves public market data.
PUBLIC_BROKER = "publicdata"

#: What a trader reads when something tries to switch a public-data install to Live.
LIVE_UNAVAILABLE_MESSAGE = (
    "No broker is connected, so this install trades in Sandbox mode only. "
    "Connect a broker to trade live."
)


class LiveModeUnavailable(Exception):
    """Raised when Live mode is requested on an install with no broker."""

    def __init__(self, message: str = LIVE_UNAVAILABLE_MESSAGE):
        super().__init__(message)


def public_data_mode() -> bool:
    """True when the install runs without a broker, on public market data."""
    return os.getenv("OPENALGO_DATA_MODE", "").strip().lower() == "public"
