# ClickHouse for Market Data Analytics: Plan

**Status:** ClickHouse is built as an optional store (2026-10-28). Phase 0
(measure first) was skipped by decision: the benchmark script was removed
before it ran. See section 8 for what exists and how it differs from the
design above.
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

## 8. What is built (2026-10-28)

**Decision:** build ClickHouse support without the Phase 0 benchmark. The
benchmark script and its thresholds were removed unrun.

**Design as built, and where it differs from section 3:**

- **Off unless `CLICKHOUSE_URL` is set.** An install without it runs exactly as
  before. `.sample.env` documents the keys, commented out.
- **DuckDB stays Historify's store.** Section 3.1 proposed moving Historify
  behind a provider interface with a backend switch. Historify's bar table is
  used by about twenty functions (downloads, aggregation, catalog, exports), so
  instead every bar Historify commits is also copied to ClickHouse's `ohlcv`
  (`_mirror_to_clickhouse` in `database/historify_db.py`). Historify's screens
  and exports read DuckDB, unchanged. `scripts/historify_to_clickhouse.py`
  copies bars downloaded before ClickHouse was turned on.
- **Every streamed tick is recorded** into `ticks` (TTL
  `CLICKHOUSE_TICK_TTL_DAYS`, default 90), and a materialized view builds
  1-minute bars, read through `tick_bars_1m_view`. `CLICKHOUSE_RECORD_TICKS =
  'false'` turns that off. It records in the process that runs the WebSocket
  proxy, because every tick passes through that process's MarketDataService:
  the app on the development server, the proxy process under gunicorn and
  Docker. Each tick is recorded once.
- **The option-chain recorder** (`CHAIN_RECORDER_ENABLED`) writes to
  `option_chain` when ClickHouse is on, and to DuckDB otherwise.
- **Writes never wait on the network.** Callers append to a bounded buffer
  (200,000 rows per table); one thread inserts every second, one insert per
  table. If ClickHouse is down, rows wait and the oldest are dropped once the
  buffer is full, and the writer backs off. Nothing on the trading path waits.
- **HTTP, not a native driver.** The client is httpx over Python sockets,
  which eventlet patches, so it is cooperative under the default worker.
- **Bar volume:** ticks carry the day's cumulative volume, and price-only
  ticks carry 0. A minute's volume is the rise across it, counting only ticks
  that report volume; a minute with none reads 0. The first volume tick of a
  minute is the baseline, so trades between the previous minute's last tick
  and this minute's first are not counted.

**Not built:** reading ClickHouse from Historify's charts, backtests or option
tools (Phase 4), a daily-OI view, production install scripts and backup docs
(Phase 5).

**Configuration:**

```
CLICKHOUSE_URL = 'http://openalgo:openalgo@127.0.0.1:8124/openalgo'
CLICKHOUSE_RECORD_TICKS = 'true'
CLICKHOUSE_TICK_TTL_DAYS = '90'
CHAIN_RECORDER_ENABLED = 'true'              # optional; NIFTY, BANKNIFTY and 3 stocks by default
CHAIN_RECORDER_UNDERLYINGS = 'NIFTY,BANKNIFTY,RELIANCE,HDFCBANK,INFY'
CHAIN_RECORDER_EXPIRIES = '2'
CHAIN_RECORDER_STRIKES_EACH_SIDE = '20'
CHAIN_RECORDER_INTERVAL_SECONDS = '5'
```

"Every expiry" (section 2) is not recordable: a broker streams about 3,000
symbols in all, and NIFTY alone lists more than that. The chain recorder's
strikes are centred when it starts and not re-centred during the day.
