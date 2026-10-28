# ClickHouse for Market Data Analytics: Plan

**Status:** Phase 0 tooling built: the option-chain recorder on DuckDB, the
benchmark with its fixed thresholds, and a ClickHouse dev profile to compare
against. The recorded week and the real benchmark run have not happened yet,
so there is no go/no-go decision. See section 8.
**Date:** 2026-10-28
**Related:**
[2026-10-28-trading-first-product-plan.md](2026-10-28-trading-first-product-plan.md) (section 6.2, historical options data),
[2026-10-28-start-without-broker-plan.md](2026-10-28-start-without-broker-plan.md),
[../development/postgres.md](../development/postgres.md)

---

## 1. Goal

Add ClickHouse as an optional store for high-volume, append-only market data:
recorded ticks, option-chain snapshots and long intraday histories. It fits
where DuckDB (Historify) reaches its limits: continuous writes while other code
reads, and scans over billions of rows.

### Non-goals

- Transactional data (orders, auth, settings, sandbox books, strategy state).
  That stays in Postgres or SQLite.
- Replacing DuckDB for small installs. ClickHouse stays opt-in, and an install
  without it behaves exactly as today.
- Making ClickHouse a requirement of the end-of-day public-data path. NSE daily
  files are small, and DuckDB handles them.

## 2. Phase 0: measure before building

Decide with numbers, not assumptions.

1. **Record a real workload.** Run an option-chain recorder on DuckDB first:
   NIFTY, BANKNIFTY and 3 stock underlyings, every expiry, every 5 seconds, for
   one week.
2. **Benchmark the same data in both stores.** Cover:
   - write throughput while a backtest reads at the same time;
   - a 1-year minute-bar scan across 200 symbols;
   - "IV smile at 10:15 every day for 6 months";
   - disk size after compression;
   - memory used (RSS).
3. **Go/no-go rule.** Build ClickHouse support only if DuckDB fails one of
   these, with the exact thresholds fixed before the benchmark runs:
   - the writer blocks readers for longer than a set number of milliseconds;
   - a target query is slower than a set number of seconds;
   - disk use grows unacceptably.

## 3. Design (only if Phase 0 says go)

### 3.1 Storage provider interface

`services/marketdata_store/` with one interface (`write_bars`, `write_ticks`,
`write_chain_snapshot`, `read_ohlcv`, `read_chain`). Two backends: `duckdb`
(default, the current Historify code moved behind the interface) and
`clickhouse`. One `.env` key chooses between them:

```
HISTORIFY_BACKEND = 'clickhouse'   # unset means duckdb
CLICKHOUSE_URL = 'http://openalgo:openalgo@127.0.0.1:8124/openalgo'
```

### 3.2 Tables (MergeTree)

| Table | Key | Notes |
| --- | --- | --- |
| `ohlcv` | `(exchange, symbol, interval, ts)` | ReplacingMergeTree, so a re-downloaded bar replaces the old one |
| `ticks` | `(exchange, symbol, ts)` | Partitioned by month, with a configurable TTL (for example 90 days) |
| `option_chain` | `(underlying, expiry, ts, strike, type)` | LTP, OI, volume, IV, greeks; partitioned by expiry month |
| Materialized views | | 1-minute bars from ticks, and daily OI per strike |

### 3.3 Writing without blocking

- **Batched writes.** A recorder buffers rows and writes every 1 to 2 seconds
  through one shared client, never one insert per tick.
- **Where the writer runs.** In the WebSocket proxy process, which already sees
  every tick and runs unpatched by eventlet under gunicorn. It takes ticks from
  the proxy's own receive loop (see review note 1); it never opens a socket on
  the ZeroMQ bus.
- **No asyncio in request code.** HTTP inserts go through a shared, long-lived
  client with an explicit timeout.

### 3.4 Reading

- **Through the interface only.** Historify's charts, backtests, scanners and
  option tools read through the provider, never ClickHouse directly.
- **Eventlet check comes first.** Before ClickHouse is allowed under the
  default eventlet worker, prove its client waits cooperatively, with a
  subprocess test in the style of `test/test_eventlet_cross_thread_locks.py`.
  Postgres needs the same open check.

### 3.5 Dev and deployment

- **Dev compose.** Add `clickhouse/clickhouse-server` to
  `docker-compose.dev.yaml` under a `clickhouse` profile, so `up -d` alone does
  not start it. Non-default ports (8124 for HTTP, 9001 for native) and a memory
  cap. See review note 4 on the bind address.
- **Production.** No change to install scripts until a later phase. A guide
  documents the minimum RAM (realistically 4 GB or more for the server).

## 4. Phases

| Phase | Delivers |
| --- | --- |
| 0 | DuckDB chain recorder plus the benchmark and the go/no-go decision |
| 1 | Provider interface; Historify moved behind it with no behaviour change |
| 2 | ClickHouse backend for `ohlcv`; a copy tool from DuckDB to ClickHouse; dev compose profile |
| 3 | Tick and option-chain recording into ClickHouse, with materialized views |
| 4 | Option tools and backtests read recorded chains (this answers the historical options data risk in the trading-first plan) |
| 5 | Production install option, backup and retention docs |

## 5. Testing

- **Contract tests.** The same tests run against both backends, so the two
  cannot drift apart.
- **Leak checks.** Run `fd-audit` on the recorder (client, buffer bounds,
  thread lifecycle).
- **gthread suite.** Add a case for concurrent recorder writes during backtest
  reads.

## 6. Risks and open questions

- **Memory on small VPS installs.** The main reason it stays opt-in.
- **Data licensing.** Recorded exchange data stays on the user's machine and is
  never shared.
- **Eventlet compatibility of the client** (3.4).
- **Overlap with DuckDB.** If Phase 0 shows DuckDB is enough, stop there. Keep
  only the recorder and the interface, and skip ClickHouse.

## 7. Review notes (checked against the code, 2026-10-28)

1. **The recorder cannot join the ZeroMQ bus as a second subscriber.** The bus
   has exactly one SUB, the proxy's, and it is the only binder
   (`websocket_proxy/server.py:117-131`); every publisher connects to it. A
   second SUB has nothing to connect to (SUB to SUB does not work), and making
   it bind would break the "SUB binds, PUBs connect" invariant in `CLAUDE.md`.
   The recorder therefore hooks into the proxy's `zmq_listener`
   (`server.py:1953`), where each message already arrives: a bounded in-memory
   queue fed from that loop, drained by the batching writer. Section 3.3 is
   written that way.
2. **No option-chain recorder exists yet**, so Phase 0 includes building one
   on DuckDB, not just running it. It needs a live broker feed: an install in
   public-data mode has end-of-day prices only and cannot produce the
   5-second workload.
3. **Historify already serialises its writes.** `database/historify_db.py`
   wraps writers in `_serialized_write` and opens a fresh `duckdb.connect()`
   per operation (`get_connection`, line 99). The Phase 0 "writer blocks
   readers" measurement should run against this real path, because it is the
   behaviour ClickHouse would be compared with.
4. **Bind address.** The plan asked for 127.0.0.1. The rest of
   `docker-compose.dev.yaml` now publishes on `${OPENALGO_BIND:-0.0.0.0}` so
   other machines on the LAN can reach it. ClickHouse should follow the same
   variable, so one setting decides for the whole stack.
5. **Ports 8124 and 9001 are free on the dev machine**, and the defaults 8123
   and 9000 are free too. The non-default choice still avoids collisions with
   other projects' ClickHouse containers.

## 8. Phase 0: what exists and how to run it

### The recorder

`services/chain_recorder/` records into `option_chain_snapshots` in the
Historify DuckDB file, through Historify's own connection helper and write
serialisation. Off unless enabled in `.env`:

```
CHAIN_RECORDER_ENABLED = 'true'
CHAIN_RECORDER_UNDERLYINGS = 'NIFTY,BANKNIFTY,RELIANCE,HDFCBANK,INFY'   # default
CHAIN_RECORDER_EXPIRIES = '2'                # nearest expiries per underlying
CHAIN_RECORDER_STRIKES_EACH_SIDE = '20'      # strikes either side of the money
CHAIN_RECORDER_INTERVAL_SECONDS = '5'
```

It starts after the master contract loads, records weekdays 09:15 to 15:30
IST, and stops on logout and at shutdown. It needs a live broker feed.

**A change from section 2:** "every expiry" is not recordable. A broker streams
about 3,000 symbols in all (`MAX_SYMBOLS_PER_WEBSOCKET` x
`MAX_WEBSOCKET_CONNECTIONS`), and NIFTY alone lists more than that across its
expiries. The defaults record 5 underlyings x 2 expiries x 41 strikes x CE/PE =
820 contracts, about 3.7 million rows a trading day.

**Strikes are fixed at start.** They are centred on the money when the
recorder starts and are not re-centred during the day. A large move leaves the
recorded band off-centre until the next login.

### The benchmark

```bash
docker compose -f docker-compose.dev.yaml --profile clickhouse up -d clickhouse
uv run python scripts/marketdata_benchmark.py \
    --clickhouse http://openalgo:openalgo@127.0.0.1:8124/openalgo
```

It generates the recorder's exact data shape inside each engine (20 days of
chain snapshots and a year of 1-minute bars for 200 symbols by default), runs
the plan's five measurements and prints the verdict against
`scripts/marketdata_benchmark_thresholds.json`. Results go to
`db/benchmarks/marketdata_phase0.json`. It never touches the platform's own
DuckDB file or tables.

**The thresholds are proposals and must be confirmed before the first real
run.** Changing them after reading results defeats the go/no-go rule.

Limits of the comparison, to keep in mind when reading results:

- Data is synthetic. The recorded week validates the shape and the row rate;
  the benchmark then scales that shape up.
- Both engines generate their data server-side (`INSERT ... SELECT`), so write
  times exclude the client sending rows, which the real recorder pays.
- DuckDB is measured in one process. Under gunicorn the app and the WebSocket
  proxy are separate processes, and DuckDB allows one writing process per
  file: a recorder in the proxy process could not write to the Historify file
  at all while the app holds it. That is not a benchmark number but it bears
  on the decision.
- A smoke run at 1 day and tiny scale (both engines, 2026-10-28) completed
  with no errors; its numbers mean nothing at that size.
