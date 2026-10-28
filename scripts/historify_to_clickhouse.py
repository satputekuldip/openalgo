"""Copy the bars already in Historify (DuckDB) into ClickHouse.

New downloads are mirrored to ClickHouse automatically once ``CLICKHOUSE_URL``
is set. This copies what was downloaded before that, once:

    uv run python scripts/historify_to_clickhouse.py
    uv run python scripts/historify_to_clickhouse.py --status   # counts only

Safe to re-run: ClickHouse's ``ohlcv`` replaces a bar with the same symbol,
exchange, interval and timestamp, so copying twice leaves one copy. Stop the
app first: DuckDB lets only one process open the file for writing.
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

BATCH_ROWS = 100_000


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--status", action="store_true", help="report counts, copy nothing")
    args = parser.parse_args()

    import duckdb

    from database.historify_db import get_db_path
    from services.marketdata_store.clickhouse import clickhouse_enabled, ensure_schema, get_client

    if not clickhouse_enabled():
        print("CLICKHOUSE_URL is not set in .env, so there is nowhere to copy to.")
        return 1

    client = get_client()
    ensure_schema(client)
    with duckdb.connect(get_db_path(), read_only=True) as conn:
        total = conn.execute("SELECT count(*) FROM market_data").fetchone()[0]
        in_clickhouse = int(client.query("SELECT count() AS n FROM ohlcv FINAL")[0]["n"])
        print(f"Historify (DuckDB): {total:,} bars. ClickHouse: {in_clickhouse:,} bars.")
        if args.status or total == 0:
            return 0

        version = time.time_ns() // 1_000_000
        copied = 0
        cursor = conn.execute(
            "SELECT symbol, exchange, interval, timestamp, open, high, low, close, volume, "
            "coalesce(oi, 0) AS oi FROM market_data"
        )
        columns = [d[0] for d in cursor.description]
        while rows := cursor.fetchmany(BATCH_ROWS):
            batch = [dict(zip(columns, row, strict=True), version=version) for row in rows]
            copied += client.insert("ohlcv", batch)
            print(f"  copied {copied:,} / {total:,}", flush=True)

    print(f"Done. {copied:,} bars copied to ClickHouse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
