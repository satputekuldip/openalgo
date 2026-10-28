# Starting Without a Broker Account: Sandbox on Public Market Data

**Status:** Phases 1 and 2 built (NSE only), plus a streaming adapter that
republishes the end-of-day close. Phases 3 to 6, the BSE files and the sandbox
fill-honesty rules (section 6) are not built yet. See "Implementation notes" at
the end.
**Date:** 2026-10-28
**Related:**
[2026-10-28-trading-first-product-plan.md](2026-10-28-trading-first-product-plan.md),
[2026-10-28-multi-broker-accounts-plan.md](2026-10-28-multi-broker-accounts-plan.md)

---

## 1. Goal

A trader can install OpenAlgo and use it **the same day without any broker
account**: browse markets, download history, backtest, and trade in
**Sandbox mode** on market data from public sources. A broker can be connected
later without losing anything they built.

Features will be limited, and the product says so plainly, on screen, at the
point where the limit bites.

Vocabulary: this is **Sandbox mode** (the platform's term), never "paper
trading" or "virtual trading" on any screen.

### Non-goals

- Live orders without a broker. Impossible and not attempted.
- Tick-accurate real-time data from public sources. Public data is end of day,
  delayed, or unofficial, and is labelled as such.
- Redistributing exchange data. Everything is fetched to the user's own
  machine for their own use.

## 2. What blocks this today

| Where | Today | Change needed |
| --- | --- | --- |
| `utils/env_check.py` | `BROKER_API_KEY`, `BROKER_API_SECRET`, `REDIRECT_URL` are required; the app exits until `<broker>` in `REDIRECT_URL` is replaced | Make these optional when the install is in public-data mode |
| Login flow (`blueprints/auth.py`, `brlogin.py`) | After the OpenAlgo login, the user is sent to a broker login | Skip it in public-data mode |
| Services (`services/*`) | Load `broker.<name>.api.*` by broker name from the auth row | Keep this: a virtual plugin plugs in through the same door |
| Sandbox engine (`sandbox/execution_engine.py`, `position_manager.py`) | Fills and MTM come from `services.quotes_service.get_quotes` | Works unchanged once quotes come from the public plugin |
| Master contract (`SymToken`) | Downloaded from the broker | Built from public files |
| Analyzer toggle | User can switch to Live | Locked to Sandbox while no broker is connected |
| `yfinance` | In a dev-only dependency group (`pyproject.toml:263`) | Move to main deps only if a source below needs it |

## 3. Design: a virtual "public" broker plugin

The least invasive route is a plugin, not a mode flag threaded through every
service. Add `broker/publicdata/` with the standard plugin layout
(`plugin.json`, `api/`, `database/master_contract_db.py`, `mapping/`,
`streaming/`), so every existing surface (option chain, charts, Flow, Python
host, `/strategy`, scalping, tools, `/api/v1/`) keeps calling the same functions.

| Plugin part | Behaviour |
| --- | --- |
| `api/auth_api.py` | No external login. Issues a local placeholder token so the auth row and `/api/v1/` keys work as usual |
| `api/data.py` | Quotes, multi-quotes, history and a level-1-only depth from the source chain in section 4 |
| `api/order_api.py`, `funds.py`, `margin_api.py` | Never called for live: the account is locked to Sandbox. If anything reaches them, refuse with a trader message ("No broker is connected, so this order went nowhere. Connect a broker under Accounts to trade live.") |
| `database/master_contract_db.py` | Builds `SymToken` from public files (4.2) |
| `streaming/` adapter | Polls the quote sources on a schedule and publishes normalised ticks to the ZMQ bus by **connecting** to the proxy SUB (never binding), like every other adapter |
| `plugin.json` | `"broker_type": "public_data"`, `"live_orders": false`, plus a capability map read by the UI (section 5) |

`plugin.json` gains a capability map for every plugin (default: all true), so
the UI hides or labels what a data source cannot do instead of hard-coding the
public plugin's name.

### 3.1 Setup and login

- `/setup` asks one new question: **Connect a broker now** or **Start without a
  broker (Sandbox only)**. The second writes `OPENALGO_DATA_MODE = 'public'`
  to `.env` and skips the broker keys.
- `env_check` treats the broker keys as optional only when that key is set.
  Every existing install has no such key and sees no change.
- The OpenAlgo login lands straight on the dashboard. A persistent, quiet
  banner reads: "Sandbox only. Prices are from public sources and may be
  delayed. Connect a broker to trade live."
- Install scripts gain a `--no-broker` option; Docker reads the same `.env` key.

### 3.2 Moving to a real broker later

- Connecting a broker switches the plugin and downloads the broker's master
  contract. Historify data, strategies, Flow graphs and Python scripts are
  untouched.
- Sandbox positions opened on public prices stay in the sandbox book; their
  symbols already use the OpenAlgo format, so they resolve against the new
  master.
- With the multi-account plan, `publicdata` becomes just another row in
  `broker_accounts`, usable as the **data account** beside any broker.

## 4. Public data sources

Sources are organised as a chain per data type. Each is a small provider class
behind one interface (`services/public_data/`), with its own rate limit,
cache (`LockedTTLCache`) and health status. The order below is the default
priority; the user can turn any unofficial source off.

**Trust levels** shown in the UI beside every price:

- **Official**: files the exchange publishes for download.
- **Public vendor**: a documented public endpoint of a company (no login).
- **Unofficial**: website endpoints or libraries that scrape them. They can
  break or be blocked without notice and their terms are unclear. **Off by
  default**, enabled with an explicit opt-in.

### 4.1 End-of-day prices (Official)

- **NSE bhavcopy** for cash (CM) and F&O, in the UDiFF common format NSE moved
  to (the old multi-file F&O download was discontinued on 2024-07-08). The F&O
  file lists every contract with OHLC, settle price, open interest and change
  in OI.
- **BSE bhavcopy** for BSE equities and BFO.
- **NSE index closing values** for `NSE_INDEX`.
- A nightly job (after the exchange publishes, typically evening IST) fetches
  the day's files into Historify as daily bars. A backfill tool fetches a date
  range, politely rate limited, resuming where it stopped.
- **Open task:** pin the exact current file URLs and name patterns from NSE's
  reports pages before building. NSE circulars in 2026 renamed files and are
  retiring older formats (some through 2026-12-07), so the fetcher must read
  the format from a small config, not from hard-coded strings.

### 4.2 Symbol master

- **Derived from the F&O bhavcopy** (Official): it lists every live
  underlying, expiry, strike and option type, which is enough to build the
  OpenAlgo symbols (`NIFTY28MAR2420800CE` style). Lot sizes come from NSE's
  published market-lot file.
- **Cash symbols** from the CM bhavcopy and NSE's equity list.
- **Fallback (Public vendor):** brokers publish full instrument files on public
  CDNs: Upstox (`complete.json.gz`), Angel One (`OpenAPIScripMaster.json`),
  Dhan (scrip master CSV), Zerodha (instruments dump). These are richer but
  belong to those brokers; some block automated downloads (Upstox users
  report 403 responses). Use only as an optional fallback, never as the
  default.

### 4.3 Intraday and delayed quotes

| Source | Trust | Covers | Notes |
| --- | --- | --- | --- |
| Yahoo Finance via `yfinance` (`.NS`, `.BO`, `^NSEI`) | Unofficial | NSE/BSE equities, main indices, global indices, intraday bars for recent days | Delay not documented; treat as delayed. No F&O. Breaks occasionally |
| NSE website JSON (quotes, option chain) | Unofficial | Equities, indices, **live option chain with OI** | Needs cookies, blocked when abused, about 3 requests per second at most per community libraries. Terms unclear. Off by default |
| Delta Exchange India public REST (`/v2/products`, `/v2/tickers/{symbol}`, `/v2/history/candles`, up to 2000 candles per call) | Public vendor | Crypto futures and options | No login needed for market data. Public WebSocket channels to be confirmed from official docs |

Quote polling runs on the shared executor (no per-call threads), every 5 to 15
seconds for subscribed symbols only, with a hard cap on symbols per source.
Every tick carries the source's timestamp, not the fetch time.

### 4.4 Options analytics without a live chain

With the NSE website source off, option tools run on the **F&O bhavcopy**: last
close, settle price and OI per strike. Greeks and IV are computed locally with
`opengreeks` from settle prices. Option Chain, OI Tracker, Max Pain, IV Smile,
GEX, Strike Profile and the strategy payoff builder all work **as of the last
close**, and say so in their header.

### 4.5 User-supplied data

Historify CSV import stays available for any vendor the user already pays for.

## 5. Capability matrix

| Area | No broker, official sources only | Plus unofficial sources (opt-in) | With a broker |
| --- | --- | --- | --- |
| Historify, charts on daily bars | Yes | Yes, plus recent intraday bars | Yes |
| Backtests (Portfolio, SIP, OpenScript, strategy backtests) | Daily bars | Daily plus limited intraday | Full |
| Option analytics tools | As of last close | Live chain (delayed) | Live |
| Sandbox orders: equity | Fills at last close outside hours; during hours only if a quote source is on | Fills at delayed quote | Live quotes |
| Sandbox orders: F&O | Last settle price, marked stale | Delayed chain prices | Live |
| WebSocket stream, `/trading`, scalping | Static (no ticks) | Polled ticks, seconds apart | Live ticks |
| Market depth | No | Level 1 only | Full |
| Flow, Python, `/strategy` in Sandbox | Yes, on the above prices | Yes | Yes |
| Live orders, margin API, broker order-update stream | No | No | Yes |
| Crypto (Delta Exchange) | Public data, sandbox orders | Same | Live |

## 6. Sandbox fill honesty

Delayed prices make fills look better than reality if left alone:

- A sandbox fill records the **price source, its trust level and quote age**.
- During market hours, a MARKET order whose freshest quote is older than a set
  age (default 2 minutes) is held as pending with a clear reason rather than
  filled at a stale price.
- Sandbox P&L and strategy reports carry a label when any fill came from a
  delayed or end-of-day price, so a trader does not take a result from public
  data as proof that a strategy works live.
- Auto square-off times are unchanged (exchange-aligned).

## 7. Runtime and safety rules

- HTTP through `utils/httpx_client.get_httpx_client()` with explicit timeouts;
  no `asyncio` in request code; pollers are shared module-level singletons.
- The streaming adapter runs in the proxy process like other adapters and
  connects to the fixed `ZMQ_PORT`.
- Per-source back-off on errors, and an automatic switch-off with an Action
  Center note when a source is blocked, never a retry loop that hammers it.
- A real browser User-Agent string is not spoofed to evade blocking; if a
  source refuses automated access, it is reported as unavailable.
- Run `fd-audit` after the pollers and nightly fetchers land.

## 8. Phases

| Phase | Delivers |
| --- | --- |
| 1. Boot without a broker | `OPENALGO_DATA_MODE`, optional broker keys in `env_check`, setup choice, login skip, Sandbox lock and banner, `publicdata` plugin skeleton, capability map in `plugin.json` |
| 2. Official end-of-day data | NSE and BSE bhavcopy fetchers and backfill into Historify, symbol master from bhavcopy plus lot file, last-close quotes, option tools on last close |
| 3. Delayed intraday | Yahoo provider, polled streaming adapter, staleness rules in the sandbox engine, trust labels in the UI |
| 4. Crypto | Delta Exchange public market data through the same plugin |
| 5. Opt-in unofficial sources | NSE website quotes and option chain, behind an explicit opt-in with a plain warning |
| 6. Upgrade path | Connect a broker without data loss; with the multi-account plan, public data as a data account |

Phases 1 and 2 alone give a useful product: research, backtests, option
analytics and end-of-day sandbox trading, all from official files.

## 9. Testing

- `env_check` tests: existing `.env` files (no `OPENALGO_DATA_MODE`) behave
  exactly as before; public mode boots with broker keys absent.
- Bhavcopy parser tests against saved sample files, including a holiday (no
  file), a half day and a format change.
- Sandbox engine tests for stale-quote holding and fill labelling.
- A `test_gthread_*` case for concurrent polling and order placement.
- Migration not needed for phase 1 (no schema change); any later column
  (for example fill source on sandbox trades) ships as an `upgrade/` script.

## 10. Risks and open questions

1. **NSE access.** Even official archive downloads can be blocked for
   automated clients. Measure with a polite fetcher before committing to the
   nightly design.
2. **Exchange data terms.** Personal use on one's own machine is the intended
   use; confirm nothing in the product redistributes it (for example shared
   strategy reports that embed price series).
3. **Yahoo stability.** Unofficial and occasionally broken; it is a
   convenience, not a dependency.
4. **Which plugin name?** `publicdata` is proposed; it appears in logs and
   `/api/v1/` responses, so pick it once.
5. **Docs.** The install guide, `.sample.env`, the docs site diagrams and
   CLAUDE.md (which assumes a broker session) need updating with phase 1.

## Implementation notes (2026-10-28)

What exists now:

- `OPENALGO_DATA_MODE = 'public'` (`utils/data_mode.py`). `utils/env_check.py`
  drops the broker keys from the required list and skips the REDIRECT_URL
  checks only when it is set.
- Login completes without a broker (`blueprints/auth.py`,
  `_connect_public_data`, also `/auth/public-connect` and the 2FA path). The
  placeholder token is reused across logins so `upsert_auth` never tears the
  shared feed down (#1591).
- Live mode is refused in `database/settings_db.set_analyze_mode` with
  `LiveModeUnavailable`, and every caller (web toggle, settings route, REST
  analyzer toggle, Telegram) answers with the trader message.
- `broker/publicdata/`: symbols from the latest NSE cash and F&O bhavcopy
  (equities, every live NFO future and option with lot size, five F&O
  indices), quotes and daily history from the same files, depth with no book,
  orders refused, and a streaming adapter that republishes the close.
- `services/public_data/nse_archive.py` downloads each file once into
  `db/public_data/nse/`, one request at a time, at least a second apart, with
  its own HTTP/1.1 client because the archive resets HTTP/2 streams.
- Frontend: the broker page completes the connection by itself, and a banner
  states that prices are end-of-day closes and orders go to the sandbox.

Found while building:

- The bhavcopy carries `NewBrdLotQty` per contract, so NSE's market-lot file
  is not needed.
- The current URLs are
  `nsearchives.nseindia.com/content/{cm,fo}/BhavCopy_NSE_{CM,FO}_0_0_0_YYYYMMDD_F_0000.csv.zip`.
- After a few quick requests in a row the archive stopped answering this
  machine for a while (timeouts). Spacing requests is necessary, not a
  courtesy.

## Sources

- [NSE reports and formats for members (UDiFF bhavcopy)](https://www.nseindia.com/static/resources/forms-formats-members)
- [NSE derivatives reports archive](https://nseindia.com/all-reports-derivatives)
- [NSE circular MSD76877 (bhavcopy file changes)](https://nsearchives.nseindia.com/content/circulars/MSD76877.pdf)
- [Upstox community: instrument files](https://community.upstox.com/t/how-to-access-full-nse-equity-instruments-json-file-with-trading-symbol-and-instrument-key/9179)
- [Upstox community: 403 on automated download](https://community.upstox.com/t/official-api-endpoint-for-instrument-master-list-python-sdk/11013)
- [Delta Exchange India API guide](https://www.delta.exchange/support/solutions/articles/80001174969-kickstarting-your-trading-journey-with-delta-india-apis)
- [nse-js (NSE rate limits)](https://www.npmjs.com/package/nse-js)
- [nsepython](https://pypi.org/project/nsepython/0.0.958/)
- [nsefin](https://pypi.org/project/nsefin/)
