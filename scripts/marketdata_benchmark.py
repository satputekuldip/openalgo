"""Phase 0 benchmark for docs/plans/2026-10-28-clickhouse-market-data-plan.md.

Measures DuckDB (and ClickHouse, when a URL is given) on the workload the
option-chain recorder produces, and prints a go/no-go verdict against
scripts/marketdata_benchmark_thresholds.json.

    uv run python scripts/marketdata_benchmark.py
    uv run python scripts/marketdata_benchmark.py \\
        --clickhouse http://openalgo:openalgo@127.0.0.1:8124/openalgo

Data is synthetic, generated inside each engine with the recorder's exact
shape (services/chain_recorder): by default 5 underlyings x 2 expiries x 41
strikes x CE/PE = 820 contracts every 5 seconds, plus 200 symbols of 1-minute
bars for a year. Both engines get identical data and identical queries.

Never touches the platform's own files: DuckDB runs in db/benchmarks/, and
ClickHouse in its own ``bench_*`` tables, dropped first.

Measured, per the plan:
  1. reader latency while a writer appends snapshots (the writer-blocks-readers check)
  2. a 1-year minute-bar scan across all symbols
  3. "IV smile at 10:15 every day", including computing IV for the rows returned
  4. disk used, extrapolated to a year of recording
  5. memory: this process's peak RSS for DuckDB, the server's resident memory for ClickHouse
"""

import argparse
import json
import os
import resource
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THRESHOLDS = ROOT / "scripts" / "marketdata_benchmark_thresholds.json"

SNAPSHOTS_PER_DAY = 4500  # 09:15 to 15:30 every 5 seconds
TRADING_DAYS_PER_YEAR = 250
MINUTES_PER_DAY = 375
START = "2026-01-05 09:15:00"
#: Snapshots appended during the concurrency test land after the loaded data.
APPEND_START = "2027-01-04 09:15:00"

# Contract c of the synthetic set: underlying c // 164, expiry (c // 82) % 2,
# strike index (c // 2) % 41, CE when c is even. The same formulas in both SQL
# dialects, so the two engines hold the same rows.


def _chain_select_duckdb(days, contracts, snapshots=SNAPSHOTS_PER_DAY, start=START, offset=0):
    ts = f"TIMESTAMP '{start}' + to_days(d) + to_seconds((s + {offset}) * 5)"
    return f"""
        SELECT {ts} AS ts,
               'U' || (c // 164) AS underlying,
               22000.0 + (s % 300) AS underlying_ltp,
               DATE '2026-01-08' + to_days(7 * ((c // 82) % 2)) AS expiry,
               21000.0 + 50 * ((c // 2) % 41) AS strike,
               CASE WHEN c % 2 = 0 THEN 'CE' ELSE 'PE' END AS option_type,
               'SYM' || c AS symbol, 'NFO' AS exchange,
               10.0 + (c % 41) + (s % 97) / 10.0 AS ltp,
               9.9 AS bid, 10.1 AS ask,
               (s * 10)::BIGINT AS volume, (1000 + c)::BIGINT AS oi,
               (s % 1000)::INTEGER AS tick_age_ms
        FROM range({days}) t(d), range({snapshots}) u(s), range({contracts}) v(c)
    """


def _chain_select_clickhouse(days, contracts, snapshots=SNAPSHOTS_PER_DAY, start=START, offset=0):
    per_day = snapshots * contracts
    return f"""
        SELECT toDateTime('{start}') + toIntervalDay(intDiv(number, {per_day}))
                 + toIntervalSecond(5 * (intDiv(number % {per_day}, {contracts}) + {offset})) AS ts,
               concat('U', toString(intDiv(number % {contracts}, 164))) AS underlying,
               22000.0 + (intDiv(number % {per_day}, {contracts}) % 300) AS underlying_ltp,
               toDate('2026-01-08') + 7 * (intDiv(number % {contracts}, 82) % 2) AS expiry,
               21000.0 + 50 * (intDiv(number % {contracts}, 2) % 41) AS strike,
               if((number % {contracts}) % 2 = 0, 'CE', 'PE') AS option_type,
               concat('SYM', toString(number % {contracts})) AS symbol, 'NFO' AS exchange,
               10.0 + ((number % {contracts}) % 41)
                 + (intDiv(number % {per_day}, {contracts}) % 97) / 10.0 AS ltp,
               9.9 AS bid, 10.1 AS ask,
               toInt64(intDiv(number % {per_day}, {contracts}) * 10) AS volume,
               toInt64(1000 + number % {contracts}) AS oi,
               toInt32(intDiv(number % {per_day}, {contracts}) % 1000) AS tick_age_ms
        FROM numbers({days * per_day})
    """


QUERIES = {
    "duckdb": {
        "minute_scan": """SELECT symbol, count(*), avg(close), max(high) - min(low)
                          FROM bench_ohlcv GROUP BY symbol""",
        "iv_smile": """SELECT CAST(ts AS DATE) AS day, strike, option_type, ltp, underlying_ltp, expiry
                       FROM bench_chain
                       WHERE underlying = 'U0' AND CAST(ts AS TIME) = TIME '10:15:00'""",
        "latest": """SELECT strike, option_type, ltp FROM bench_chain
                     WHERE underlying = 'U0'
                       AND ts = (SELECT max(ts) FROM bench_chain WHERE underlying = 'U0')""",
    },
    "clickhouse": {
        "minute_scan": """SELECT symbol, count(), avg(close), max(high) - min(low)
                          FROM bench_ohlcv GROUP BY symbol FORMAT JSONCompact""",
        "iv_smile": """SELECT toDate(ts) AS day, strike, option_type, ltp, underlying_ltp, expiry
                       FROM bench_chain
                       WHERE underlying = 'U0' AND toHour(ts) = 10 AND toMinute(ts) = 15
                         AND toSecond(ts) = 0 FORMAT JSONCompact""",
        "latest": """SELECT strike, option_type, ltp FROM bench_chain
                     WHERE underlying = 'U0'
                       AND ts = (SELECT max(ts) FROM bench_chain WHERE underlying = 'U0')
                     FORMAT JSONCompact""",
    },
}


# -- engines -------------------------------------------------------------------


class DuckDBEngine:
    name = "duckdb"

    def __init__(self, path: Path):
        import duckdb

        self.duckdb = duckdb
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        for leftover in (path, Path(f"{path}.wal")):
            leftover.unlink(missing_ok=True)

    def run(self, sql):
        # A connection per call, as Historify's get_connection() does.
        with self.duckdb.connect(str(self.path)) as conn:
            return conn.execute(sql).fetchall()

    def load(self, days, contracts, symbols, bar_days):
        self.run(f"CREATE TABLE bench_chain AS {_chain_select_duckdb(days, contracts)}")
        self.run(f"""CREATE TABLE bench_ohlcv AS
            SELECT 'S' || sym AS symbol,
                   TIMESTAMP '{START}' + to_days(d) + to_minutes(m) AS ts,
                   100.0 + m % 7 AS open, 101.0 + m % 7 AS high, 99.0 + m % 7 AS low,
                   100.5 + m % 7 AS close, (m * 10)::BIGINT AS volume
            FROM range({symbols}) a(sym), range({bar_days}) b(d), range({MINUTES_PER_DAY}) c(m)""")
        self.run("CHECKPOINT")

    def append_snapshot(self, index, contracts):
        select = _chain_select_duckdb(1, contracts, snapshots=1, start=APPEND_START, offset=index)
        self.run(f"INSERT INTO bench_chain {select}")

    def disk_bytes(self):
        self.run("CHECKPOINT")
        return self.path.stat().st_size

    def memory_bytes(self):
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    def rows(self, table):
        return self.run(f"SELECT count(*) FROM {table}")[0][0]


class ClickHouseEngine:
    name = "clickhouse"

    def __init__(self, url: str):
        from urllib.parse import urlsplit

        import httpx

        parts = urlsplit(url)
        self.database = parts.path.strip("/") or "default"
        self.client = httpx.Client(
            base_url=f"{parts.scheme}://{parts.hostname}:{parts.port or 8123}",
            auth=(parts.username or "default", parts.password or ""),
            params={"database": self.database},
            timeout=600,
        )

    def run(self, sql):
        response = self.client.post("/", content=sql)
        if response.status_code != 200:
            raise RuntimeError(f"ClickHouse: {response.text[:500]}")
        text = response.text.strip()
        return json.loads(text)["data"] if text.startswith("{") else text

    def load(self, days, contracts, symbols, bar_days):
        for table in ("bench_chain", "bench_ohlcv"):
            self.run(f"DROP TABLE IF EXISTS {table}")
        self.run("""CREATE TABLE bench_chain (
                ts DateTime, underlying LowCardinality(String), underlying_ltp Float64,
                expiry Date, strike Float64, option_type LowCardinality(String),
                symbol String, exchange LowCardinality(String), ltp Float64, bid Float64,
                ask Float64, volume Int64, oi Int64, tick_age_ms Int32)
            ENGINE = MergeTree PARTITION BY toYYYYMM(expiry)
            ORDER BY (underlying, expiry, ts, strike, option_type)""")
        self.run(f"INSERT INTO bench_chain {_chain_select_clickhouse(days, contracts)}")
        self.run("""CREATE TABLE bench_ohlcv (
                symbol LowCardinality(String), ts DateTime, open Float64, high Float64,
                low Float64, close Float64, volume Int64)
            ENGINE = ReplacingMergeTree ORDER BY (symbol, ts)""")
        per_day = MINUTES_PER_DAY * symbols
        self.run(f"""INSERT INTO bench_ohlcv
            SELECT concat('S', toString(number % {symbols})),
                   toDateTime('{START}') + toIntervalDay(intDiv(number, {per_day}))
                     + toIntervalMinute(intDiv(number % {per_day}, {symbols})),
                   100.0 + intDiv(number % {per_day}, {symbols}) % 7, 101.0, 99.0, 100.5,
                   toInt64(number % 1000)
            FROM numbers({bar_days * per_day})""")
        self.run("OPTIMIZE TABLE bench_chain FINAL")
        self.run("OPTIMIZE TABLE bench_ohlcv FINAL")

    def append_snapshot(self, index, contracts):
        select = _chain_select_clickhouse(
            1, contracts, snapshots=1, start=APPEND_START, offset=index
        )
        self.run(f"INSERT INTO bench_chain {select}")

    def disk_bytes(self):
        result = self.run(f"""SELECT sum(bytes_on_disk) FROM system.parts
            WHERE database = '{self.database}' AND table = 'bench_chain' AND active
            FORMAT JSONCompact""")
        return int(result[0][0])

    def memory_bytes(self):
        result = self.run("""SELECT value FROM system.asynchronous_metrics
            WHERE metric = 'MemoryResident' FORMAT JSONCompact""")
        return int(result[0][0]) if result else 0

    def rows(self, table):
        return int(self.run(f"SELECT count() FROM {table} FORMAT JSONCompact")[0][0])


# -- measurements ----------------------------------------------------------------


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def implied_vols(rows):
    """IV for each returned row, as the option tools would compute it."""
    from opengreeks.black76 import implied_volatility

    count = 0
    for row in rows:
        day, strike, kind, price, spot, expiry = row[:6]
        day = datetime.fromisoformat(str(day)).date()
        expiry = datetime.fromisoformat(str(expiry)).date()
        years = max((expiry - day).days, 1) / 365.0
        try:
            implied_volatility(
                float(price), float(spot), float(strike), 0.0, years, "c" if kind == "CE" else "p"
            )
            count += 1
        except Exception:
            pass  # deep in or out of the money: no IV, as in the option chain
    return count


def concurrent_reads(engine, contracts, seconds, write_every):
    """Append a snapshot every ``write_every`` seconds while a reader loops."""
    stop = threading.Event()
    write_ms, read_ms, errors = [], [], []

    def writer():
        index = 0
        while not stop.is_set():
            started = time.perf_counter()
            try:
                engine.append_snapshot(index, contracts)
                write_ms.append((time.perf_counter() - started) * 1000)
            except Exception as e:  # a lock conflict is a result, not a crash
                errors.append(f"write: {e}")
            index += 1
            stop.wait(max(0.0, write_every - (time.perf_counter() - started)))

    def reader():
        query = QUERIES[engine.name]["latest"]
        while not stop.is_set():
            started = time.perf_counter()
            try:
                engine.run(query)
                read_ms.append((time.perf_counter() - started) * 1000)
            except Exception as e:
                errors.append(f"read: {e}")

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join()

    def pct(values, p):
        return round(statistics.quantiles(values, n=100)[p - 1], 1) if len(values) >= 2 else None

    return {
        "snapshots_written": len(write_ms),
        "write_p50_ms": pct(write_ms, 50),
        "write_p99_ms": pct(write_ms, 99),
        "reads": len(read_ms),
        "reader_p50_ms": pct(read_ms, 50),
        "reader_p99_ms": pct(read_ms, 99),
        "reader_max_ms": round(max(read_ms), 1) if read_ms else None,
        "errors": len(errors),
        "first_error": errors[0] if errors else None,
    }


def benchmark(engine, args):
    print(f"\n[{engine.name}] loading synthetic data ...", flush=True)
    load_seconds, _ = timed(
        lambda: engine.load(args.days, args.contracts, args.symbols, args.bar_days)
    )
    chain_rows = engine.rows("bench_chain")
    print(f"[{engine.name}] loaded {chain_rows:,} chain rows in {load_seconds:.0f}s", flush=True)

    disk = engine.disk_bytes()
    rows_per_year = args.contracts * SNAPSHOTS_PER_DAY * TRADING_DAYS_PER_YEAR
    result = {
        "chain_rows": chain_rows,
        "ohlcv_rows": engine.rows("bench_ohlcv"),
        "load_seconds": round(load_seconds, 1),
        "disk_bytes_chain": disk,
        "bytes_per_chain_row": round(disk / chain_rows, 2),
        "disk_gb_per_year_of_recording": round(disk / chain_rows * rows_per_year / 1e9, 2),
    }

    print(f"[{engine.name}] queries ...", flush=True)
    result["minute_bar_scan_seconds"] = round(
        timed(lambda: engine.run(QUERIES[engine.name]["minute_scan"]))[0], 3
    )
    query_s, rows = timed(lambda: engine.run(QUERIES[engine.name]["iv_smile"]))
    iv_s, solved = timed(lambda: implied_vols(rows))
    result.update(
        iv_smile_query_seconds=round(query_s, 3),
        iv_smile_rows=len(rows),
        iv_solved=solved,
        iv_smile_seconds=round(query_s + iv_s, 3),
    )

    print(f"[{engine.name}] writes during reads for {args.concurrency_seconds}s ...", flush=True)
    result["concurrency"] = concurrent_reads(
        engine, args.contracts, args.concurrency_seconds, args.write_every
    )
    result["memory_bytes"] = engine.memory_bytes()
    return result


def verdict(duck, limits):
    """The plan's rule: go only if DuckDB breaks a limit."""
    checks = {
        "reader_p99_ms_during_writes": duck["concurrency"]["reader_p99_ms"],
        "reader_max_ms_during_writes": duck["concurrency"]["reader_max_ms"],
        "minute_bar_scan_seconds": duck["minute_bar_scan_seconds"],
        "iv_smile_seconds": duck["iv_smile_seconds"],
        "disk_gb_per_year_of_recording": duck["disk_gb_per_year_of_recording"],
    }
    broken = {k: (v, limits[k]) for k, v in checks.items() if v is None or v > limits[k]}
    if duck["concurrency"]["errors"]:
        broken["writer_or_reader_errors"] = (duck["concurrency"]["errors"], 0)
    return ("GO: build ClickHouse support" if broken else "NO-GO: DuckDB is enough"), broken


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clickhouse", help="ClickHouse HTTP URL with user, password and database")
    parser.add_argument("--days", type=int, default=20, help="days of chain snapshots to generate")
    parser.add_argument("--contracts", type=int, default=820)
    parser.add_argument("--symbols", type=int, default=200)
    parser.add_argument("--bar-days", type=int, default=TRADING_DAYS_PER_YEAR)
    parser.add_argument("--concurrency-seconds", type=int, default=30)
    parser.add_argument("--write-every", type=float, default=0.2,
                        help="seconds between snapshots in the concurrency test (5 is real time)")  # fmt: skip
    parser.add_argument("--out", default=str(ROOT / "db" / "benchmarks" / "marketdata_phase0.json"))
    args = parser.parse_args()

    limits = json.loads(THRESHOLDS.read_text())
    results = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "args": vars(args),
        "limits": limits,
    }
    results["duckdb"] = benchmark(DuckDBEngine(ROOT / "db" / "benchmarks" / "phase0.duckdb"), args)
    if args.clickhouse:
        results["clickhouse"] = benchmark(ClickHouseEngine(args.clickhouse), args)

    decision, broken = verdict(results["duckdb"], limits)
    results["verdict"] = {"decision": decision, "limits_broken": broken}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(results, indent=2, default=str))
    print(f"\n{decision}")
    for name, (value, limit) in broken.items():
        print(f"  DuckDB {name} = {value} (limit {limit})")


if __name__ == "__main__":
    main()
