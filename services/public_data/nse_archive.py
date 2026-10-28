"""Official NSE end-of-day files: the cash and F&O bhavcopy.

NSE publishes one UDiFF "common bhavcopy" zip per segment per trading day on
its public archive, usually in the evening after the close:

    https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
    https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip

The cash file has OHLC, previous close and volume for every listed security.
The F&O file has the same for every live contract plus settle price, open
interest, the underlying's close and the lot size (``NewBrdLotQty``), which is
enough to build the whole NFO symbol list without any broker.

Each file is downloaded once and kept under ``db/public_data/nse/``, so a
backfill or a restart never fetches the same day twice. Requests are spaced
out, at least a second apart: these are files for a person's own use, fetched to
their own machine, and the archive should never see more than a trickle.

The URL patterns live in ``URL_TEMPLATES`` because NSE renames these files from
time to time (its 2026 circulars retire older formats through December 2026).
A change of name is then a one-line edit.
"""

import io
import os
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import httpx
import pandas as pd

from utils.lazy import LazyInit
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache
from utils.version import get_version

logger = get_logger(__name__)

URL_TEMPLATES = {
    "CM": "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip",
    "FO": "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
}

#: Sent with every request so the archive can see who is asking.
USER_AGENT = os.getenv(
    "PUBLIC_DATA_USER_AGENT",
    f"Mozilla/5.0 (compatible; OpenAlgo/{get_version()}; +https://openalgo.in)",
)

#: Minimum seconds between two requests to the archive.
REQUEST_SPACING_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30.0

#: NSE publishes the day's files in the evening; before this IST hour the
#: latest file is assumed to be the previous trading day's.
PUBLISH_HOUR_IST = 19

IST = timezone(timedelta(hours=5, minutes=30))

_CACHE_DIR = os.path.join("db", "public_data", "nse")


def _build_archive_client() -> httpx.Client:
    # Its own client rather than utils.httpx_client's: that one negotiates
    # HTTP/2 outside Docker, and the NSE archive resets HTTP/2 streams. One
    # client for the life of the process, so the FD count stays flat.
    return httpx.Client(
        http1=True,
        http2=False,
        timeout=REQUEST_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


_archive_client = LazyInit(_build_archive_client, name="nse-archive-client")

# Requests start at least REQUEST_SPACING_SECONDS apart. The lock guards only
# the slot bookkeeping; the wait and the request happen outside it.
_request_lock = threading.Lock()
_next_request_at = 0.0  # monotonic time of the next free request slot

# Parsed files, by (segment, day). A day's file never changes once published.
# One F&O day parses to about 33 MB, so only the latest day's two files and a
# couple more stay in memory; a history request walking many days reads the
# older ones back from disk.
_parsed = LockedTTLCache(maxsize=4, ttl=6 * 3600)


class ArchiveUnavailable(Exception):
    """The archive refused or could not be reached. The message is for a trader."""


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _cache_path(segment: str, day: date) -> str:
    return os.path.join(_CACHE_DIR, segment.lower(), f"{_ymd(day)}.csv.zip")


def _missing_marker(segment: str, day: date) -> str:
    return os.path.join(_CACHE_DIR, segment.lower(), f"{_ymd(day)}.missing")


def today_ist() -> date:
    return datetime.now(IST).date()


def _published_by_now(day: date) -> bool:
    now = datetime.now(IST)
    return day < now.date() or (day == now.date() and now.hour >= PUBLISH_HOUR_IST)


def _download(segment: str, day: date) -> bytes | None:
    """Fetch one file. Returns None when NSE has no file for that day (a holiday)."""
    global _next_request_at

    url = URL_TEMPLATES[segment].format(ymd=_ymd(day))
    # Claim the next free slot under the lock, then wait for it outside: a
    # sleep under the lock would hold every other caller behind it.
    with _request_lock:
        slot = max(time.monotonic(), _next_request_at)
        _next_request_at = slot + REQUEST_SPACING_SECONDS
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)

    try:
        response = _archive_client.get().get(url)
    except Exception as e:
        logger.exception(f"NSE archive request failed for {url}: {e}")
        raise ArchiveUnavailable(
            "Could not reach the NSE archive. Check this computer's internet connection "
            "and try again in a few minutes."
        ) from e

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.error(f"NSE archive returned {response.status_code} for {url}")
        raise ArchiveUnavailable(
            "The NSE archive did not hand over the day's file. It sometimes refuses "
            "requests for a while; try again later."
        )
    return response.content


def fetch_bhavcopy(segment: str, day: date) -> pd.DataFrame | None:
    """Return the bhavcopy for ``segment`` ("CM" or "FO") on ``day``.

    Returns None when there is no file for that day (a holiday or a weekend), or
    when the day's file is not published yet.

    Raises:
        ArchiveUnavailable: The archive could not be reached or refused.
    """
    segment = segment.upper()
    key = (segment, day)
    cached = _parsed.get(key)
    if cached is not None:
        return cached

    path = _cache_path(segment, day)
    if os.path.exists(_missing_marker(segment, day)):
        return None

    if not os.path.exists(path):
        if not _published_by_now(day):
            return None
        content = _download(segment, day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if content is None:
            # Only a past day is final. Today's file may simply be late.
            if day < today_ist():
                with open(_missing_marker(segment, day), "w"):
                    pass
            return None
        tmp = f"{path}.part"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, path)

    try:
        with zipfile.ZipFile(path) as archive:
            name = archive.namelist()[0]
            with archive.open(name) as member:
                frame = pd.read_csv(io.TextIOWrapper(member, encoding="utf-8"), low_memory=False)
    except (zipfile.BadZipFile, IndexError, pd.errors.ParserError) as e:
        # A cut-off or replaced file: drop it so the next call fetches it again.
        logger.exception(f"Discarding unreadable bhavcopy {path}: {e}")
        os.remove(path)
        return None

    frame.columns = [c.strip() for c in frame.columns]
    _parsed[key] = frame
    return frame


def latest_trading_day(max_days_back: int = 10) -> date | None:
    """The most recent day with a published F&O bhavcopy, or None."""
    day = today_ist()
    for _ in range(max_days_back):
        if day.weekday() < 5 and fetch_bhavcopy("FO", day) is not None:
            return day
        day -= timedelta(days=1)
    return None


def trading_days(start: date, end: date) -> list[date]:
    """Weekdays from ``start`` to ``end`` inclusive. Holidays drop out on fetch."""
    days = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days
