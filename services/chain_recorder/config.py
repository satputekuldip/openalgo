"""Chain recorder settings, read from ``.env``. All optional; unset means off."""

import os
from dataclasses import dataclass

#: F&O index underlyings. Their price comes from NSE_INDEX; every other
#: underlying is an NSE equity.
INDEX_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})

DEFAULT_UNDERLYINGS = "NIFTY,BANKNIFTY,RELIANCE,HDFCBANK,INFY"


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        value = default
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    enabled: bool
    underlyings: tuple[str, ...]
    expiries: int
    strikes_each_side: int
    interval_seconds: int

    @classmethod
    def from_env(cls) -> "RecorderConfig":
        names = os.getenv("CHAIN_RECORDER_UNDERLYINGS", DEFAULT_UNDERLYINGS)
        return cls(
            enabled=os.getenv("CHAIN_RECORDER_ENABLED", "").strip().lower() == "true",
            underlyings=tuple(n.strip().upper() for n in names.split(",") if n.strip()),
            expiries=_int("CHAIN_RECORDER_EXPIRIES", 2, 1, 6),
            strikes_each_side=_int("CHAIN_RECORDER_STRIKES_EACH_SIDE", 20, 1, 60),
            interval_seconds=_int("CHAIN_RECORDER_INTERVAL_SECONDS", 5, 1, 300),
        )


def underlying_exchange(underlying: str) -> str:
    return "NSE_INDEX" if underlying in INDEX_UNDERLYINGS else "NSE"
