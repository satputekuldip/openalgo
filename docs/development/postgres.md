# PostgreSQL for Development

OpenAlgo runs on SQLite by default, and every existing install keeps doing so.
PostgreSQL is an alternative chosen entirely by the database URLs in `.env`.
This page covers running it locally for development.

## What changes with PostgreSQL

| SQLite | PostgreSQL |
| --- | --- |
| Five files: `openalgo.db`, `logs.db`, `latency.db`, `health.db`, `sandbox.db` | One database, five schemas: `main`, `logs`, `latency`, `health`, `sandbox` |
| `NullPool`, a fresh connection per operation | One pooled engine per URL, shared by every module that uses it (10 connections, up to 30 more under load) |
| Historify in `db/historify.duckdb` | Unchanged: Historify stays on DuckDB |

A URL picks its schema with a `schema` query parameter:

```
postgresql+psycopg://openalgo:openalgo@127.0.0.1:5433/openalgo?schema=logs
```

`database/engine_factory.py` removes that parameter, creates the schema if it
is missing, and sets it as each connection's `search_path`, so every table
lands in its own schema and no module needs to know which backend it is on.
A plain `postgresql://` URL is upgraded to the `psycopg` (version 3) driver,
which is the one installed.

## Start the services

`docker-compose.dev.yaml` runs the services only, never the app:

```bash
docker compose -f docker-compose.dev.yaml up -d
```

| Service | Host address | Purpose |
| --- | --- | --- |
| PostgreSQL 17 | `127.0.0.1:5433` | Database `openalgo`, user and password `openalgo` |
| Redis 7 | `127.0.0.1:6380` | Reserved for caching and pub/sub work; nothing uses it yet |
| DbGate | `http://<host>:8082` | Browse, query and edit all five schemas |

The host ports avoid the standard 5432 and 6379 so they do not collide with
other projects. Override them with `OPENALGO_PG_PORT`, `OPENALGO_REDIS_PORT`
and `OPENALGO_DBGATE_PORT`.

**All three are published on every host interface**, so other machines on the
network can reach them, with the development password `openalgo`. DbGate has
no login of its own. Never run this file on a machine reachable from the
internet. To keep them on this machine only, start with
`OPENALGO_BIND=127.0.0.1`.

### ClickHouse (optional)

For the market-data benchmark only, and not started by `up -d`:

```bash
docker compose -f docker-compose.dev.yaml --profile clickhouse up -d clickhouse
```

HTTP on port 8124, native on 9001, user and password `openalgo`, capped at
4 GB of memory (`OPENALGO_CH_MEMORY`). See
`docs/plans/2026-10-28-clickhouse-market-data-plan.md`, section 8.

### DbGate

The connection to the dev database is preconfigured through environment
variables in `docker-compose.dev.yaml`, so DbGate opens on "OpenAlgo (dev)";
expand it to see the `main`, `logs`, `latency`, `health` and `sandbox` schemas.
Saved queries and settings live in the `openalgo_dev_dbgate` volume.

`down` keeps the data; `down -v` deletes it.

## Point the app at it

Uncomment the PostgreSQL block in `.env` (see `.sample.env`) so all five URLs
use the server, then run the app as usual:

```bash
uv run app.py
```

Tables are created on first start, in their schemas. Check with:

```bash
docker exec openalgo-dev-postgres psql -U openalgo -d openalgo \
  -c "select table_schema, count(*) from information_schema.tables where table_schema in ('main','logs','latency','health','sandbox') group by 1"
```

## Known limits

- **Verified on the development server only.** Startup, setup, login, master
  contract, quotes, history, sandbox orders and the WebSocket stream were run
  against PostgreSQL with `uv run app.py`. The gunicorn workers were not:
  `psycopg` waits for the server inside its own code, and whether that yields
  to the eventlet hub has not been measured. Do not run production on
  PostgreSQL under eventlet until a test in `test/test_eventlet_cross_thread_locks.py`
  style proves the wait is cooperative.
- **Migrations in `upgrade/` are written for SQLite** (`PRAGMA table_info`,
  table rebuilds). A fresh PostgreSQL database needs none of them, because
  `create_all` builds the current schema, but none of them has been run
  against PostgreSQL.
- **No data migration** from an existing SQLite install to PostgreSQL yet.
- The `indmoney` and `deltaexchange` master-contract modules try SQLite
  `PRAGMA` tuning on import. On PostgreSQL that fails inside a `try` and logs
  one warning; nothing else is affected.
