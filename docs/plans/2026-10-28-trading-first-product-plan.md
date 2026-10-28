# Trading-First Product Plan

**Status:** Planning only. Nothing in this document is built.
**Date:** 2026-10-28
**Scope:** Product direction, information architecture, and a phased roadmap
that turns OpenAlgo from a showcase of capabilities into a product a trader
opens every morning to research, build, test, automate and review trades.

---

## 1. Problem statement

OpenAlgo already has more engine than most commercial platforms: a unified
broker API across 38 brokers, a sandbox engine, a no-code Flow builder, a hosted
Python strategy runner, OpenScript, a multi-leg strategy module with a shared
risk core, 21 analytics tools, a charting terminal and a scalping terminal.

What it does not have is a **trader's journey** through those parts. Today:

- The public home page sells integrations and technology ("Self-hosted algo
  trading", "Claude / Cursor / Windsurf / ChatGPT", "Runs on your computer")
  rather than outcomes (find a trade, test it, run it, review it).
- The top navigation leads with plumbing: Orderbook, Tradebook, Platforms,
  Logs. The building surfaces (Flow, Python, Scalping, Historify, Sandbox) are
  buried in the profile menu.
- There are **four separate places to build a strategy** (Flow, Python host,
  OpenScript, `/strategy` wizard) plus a fifth options-only Strategy Builder in
  `/tools`, each with its own idea of backtest, deploy and monitor. A trader has
  to know the architecture to pick one.
- Backtesting exists in pieces (Portfolio Backtester, SIP Backtester, OpenScript
  backtests) but there is no single "test this idea on history, then forward on
  sandbox, then live" path.
- Analytics tools are a flat grid of 21 cards, not workflows ("what is the
  market doing today", "where is the edge in this expiry").

The goal is not more features. It is to **re-centre the product on four
trader jobs** and route every existing engine into them.

## 2. Market scan (what traders compare us against)

Research done 2026-10-28. Vendor comparisons are mostly written by competitors,
so they are used for feature inventory only, not for verdicts.

| Product | What traders use it for | What it does well | Where OpenAlgo can win |
| --- | --- | --- | --- |
| AlgoTest | Options backtesting, forward testing, deploy | Visual leg builder, fast historical options backtests, simulator, 30+ brokers | Self-hosted, no per-backtest quota, code and no-code on one engine |
| Tradetron | Strategy marketplace, condition builder | Marketplace of deployable strategies, multi-broker, copy to many accounts | Transparent logic, no subscription per deployment, own data |
| Zerodha Streak | Scanners and simple equity algos | Very low friction, scanner to alert to order | Options-first, any broker, richer risk |
| uTrade Algos | Prebuilt options algos, backtests | Ready-made strategies, REST API | Open, scriptable, own RMS |
| Sensibull | Options strategy builder, payoff, virtual trading | Best-in-class payoff builder and learning UX | Same builder wired to real automation and RMS |
| Opstra (Definedge) | Options analytics, Greeks, vol surface, intraday sim | Analytics depth, 5-minute historical simulation | We already ship most of these tools; join them into workflows |
| TradingView | Charting, Pine strategies, alerts | Charting craft, strategy tester, alerts ecosystem | Alert to execution is already ours; add our own tester |
| Composer / QuantConnect | No-code (Composer) and code-first (QC) research | Clean research to live path, versioned strategies | Indian F&O focus, local data, single engine |

**Table stakes we are missing or have only in fragments:**

1. One strategy object that can be backtested, forward-tested in sandbox, and
   deployed live without being rebuilt.
2. A historical options backtester driven by the same leg builder traders use
   for payoff (strike by ATM offset, premium, or delta; per-leg SL/target;
   re-entry; time-based entry and exit).
3. A strategy library: templates (straddle, strangle, iron condor, ORB, EMA
   cross, supertrend) that open pre-filled in the builder.
4. A daily cockpit: market regime, my running strategies, my risk, my P&L.
5. Journal and analytics on my own trades (win rate, expectancy, drawdown by
   strategy, slippage versus signal).

**Regulatory constraint that shapes the product (SEBI retail algo framework,
in force from 2026-04-01):** orders through a registered app from a whitelisted
static IP, daily 2FA, 10 orders per second before an algo is treated as needing
exchange registration, and exchange-assigned Algo-ID tagging. A self-hosted
platform on the trader's own IP fits this model well, and it should be
presented as a strength (see section 6.4).

## 3. Product principles

1. **Jobs first, engines second.** Navigation names what the trader is doing,
   not which subsystem does it.
2. **One strategy, three stages.** Every strategy moves through Backtest, then
   Sandbox, then Live, with the same definition. The stage is a property of a
   run, not a separate tool.
3. **Every builder compiles to the same thing.** Flow, OpenScript, the options
   leg builder and Python remain, but they publish into one strategy registry
   with common run, monitor and review screens.
4. **Risk is visible before an order exists.** Payoff, max loss, margin and
   the RMS rules are shown while building, not discovered after a stop fires.
   Rules continue to come from `services/risk/` only.
5. **No regression for existing installs.** Old routes keep working and old
   strategies keep running. New screens are additions and redirects, under
   both eventlet and gthread.

## 4. Target information architecture

Top navigation is rebuilt around five jobs. Existing pages map into it; very
little is deleted.

| Job | New section | Contains (existing parts reused) |
| --- | --- | --- |
| Know the market | **Markets** | Daily cockpit (new), Option Chain, OI Tracker, Max Pain, GEX, IV Smile, Vol Surface, Straddle, Sector Rotation, Scanners (Chartink, new built-in scanner) |
| Build an idea | **Build** | Strategy library (new), Options leg builder (from `/tools` Strategy Builder), Flow, OpenScript, Python |
| Test it | **Test** | Unified backtest runner (new), Portfolio and SIP backtesters, Sandbox forward test |
| Run it | **Run** | Strategy runs (from `/strategy`), Trading terminal, Scalping terminal, alerts and webhooks (Platforms), kill switch |
| Review it | **Review** | P&L Tracker, Sandbox P&L, Journal (new), Positions, Orderbook, Tradebook, Logs |

Orderbook, Tradebook and Logs move out of the top bar into Review and Run. The
top bar keeps Dashboard, Positions and a global search.

## 5. Roadmap

Phases are ordered so each one ships something a trader can use on its own.

### Phase 0: Re-frame what exists (no new engines)

- Rewrite the home page around the five jobs with screenshots of real flows;
  move integrations and tech to a secondary "Connects to" section.
- New navigation per section 4; keep every old route, register new ones in
  `App.tsx`, `blueprints/react_app.py` and `config/navigation.ts` (the three
  registrations).
- Group `/tools` into Markets and Build/Test by the tool registry in
  `frontend/src/lib/tools.ts` (add a `job` field; the home page count keeps
  deriving from it).
- Dashboard becomes the **daily cockpit** v1: funds, day P&L, running
  strategies with stage and health, open risk, top movers in watched
  underlyings, today's expiry, kill switch.
- First-run onboarding: connect broker, pick a template, run it in sandbox.

### Phase 1: Strategy registry and library

- A **strategy registry** (new table, migration in `upgrade/`) that every
  builder publishes into: id, name, kind (`options_legs`, `flow`,
  `openscript`, `python`, `batch`/`signal` from `/strategy`), version,
  parameters, instruments, schedule, risk profile.
- Versioning: a run pins the version it started with; editing makes a new
  version.
- **Template library** of 15 to 20 strategies (short straddle, strangle, iron
  condor, iron fly, ratio spread, calendar, ORB breakout, EMA cross,
  supertrend, VWAP reversion, gap fade, expiry-day theta) that open pre-filled.
- Import and export as JSON so traders share strategies outside the platform.

### Phase 2: Unified backtest, then forward, then live

- One **Backtest** screen for any registry strategy. Backends:
  - options legs: a historical options backtester over Historify (DuckDB)
    data with ATM offset, premium-based and delta-based strike selection,
    per-leg and combined SL/target/trailing from `services/risk/`, re-entry,
    entry and exit times, slippage and brokerage model;
  - OpenScript: its existing backtest;
  - Flow: replay mode over historical bars (new);
  - Python: supported only where the script uses the SDK's history calls.
- Standard report: equity curve, drawdown, monthly heatmap, trade list,
  win rate, expectancy, profit factor, max consecutive losses, exposure.
- Parameter sweep and a simple walk-forward split (in-sample and out-of-sample)
  with a warning when results only hold in-sample.
- **Promote** buttons: Backtest to Sandbox (forward test on live data, sandbox
  engine) to Live. Live promotion shows the risk profile, margin estimate and
  the Algo-ID that will be tagged, and requires confirmation.
- Data honesty: the report states what data range and granularity was used
  and what is not modelled (for example liquidity, freeze limits, gaps).

### Phase 3: Options workbench

- Merge the `/tools` Strategy Builder and the `/strategy` wizard into one
  **options leg builder**: payoff at expiry and at a target date, Greeks,
  probability of profit, margin from the broker margin API, and live versus
  sandbox position import.
- What-if: shift spot, IV and days to expiry; adjustment suggestions (roll,
  hedge, convert to spread) as simulations, never automatic orders.
- One click from builder to Backtest, Sandbox or Live with the same legs.
- Expiry-day and event presets (weekly expiry, results, budget).

### Phase 4: Automation layer

- **Triggers** as first-class objects attachable to any registry strategy:
  schedule (IST times, market calendar aware), price/indicator condition,
  scanner hit, TradingView/Chartink webhook, order-update event.
- **Built-in scanner** over Historify and live quotes (indicator conditions
  from `openalgo.ta`), feeding triggers, alerts and watchlists.
- Strategy health supervision: stale feed, broker session expiry before 3 AM
  IST rollover, rejected orders, drift between expected and broker positions,
  each raising an Action Center item and Telegram alert.
- Portfolio-level RMS: daily loss limit, max open strategies, margin
  headroom, across all strategies (core rules in `services/risk/`).

### Phase 5: Review and learning loop

- **Trade journal**: every fill tagged with strategy, version, stage and
  trigger; notes and tags; screenshots of the chart at entry.
- Analytics per strategy and per tag: expectancy, slippage versus signal
  price, time-of-day performance, live versus backtest divergence.
- Weekly review summary on the dashboard and over Telegram.

### Phase 6: Multi-account

Covered by its own plan:
[2026-10-28-multi-broker-accounts-plan.md](2026-10-28-multi-broker-accounts-plan.md).
Phases 1 to 5 must model "account" as a field from the start (default: the
single existing account) so this phase does not need a second schema change.

## 6. Cross-cutting requirements

### 6.1 Runtime

Every new service must be correct under eventlet (default), gthread and the dev
server: no `asyncio` in request code, long backtests run as subprocesses or on
the shared executors with results written to disk/DB, streaming progress over
SocketIO via `extensions.emit_from_any_thread`. Long-lived responses go through
`utils/stream_registry.py`.

### 6.2 Data

Backtests read Historify (`historify.duckdb`). Options backtesting needs
historical option chains, which most brokers do not provide. Decide in Phase 2:
(a) record chains forward from the live feed into Historify (free, starts
empty), (b) import from a third-party vendor (cost, licensing), or both. This
is the largest open risk in the plan.

### 6.3 Vocabulary

Sandbox and Live only; never "paper trading", never "arm". Every error message
names the cause and the next action, for a trader.

### 6.4 Compliance

Show the static IP status, Algo-ID tagging and order rate against the 10 OPS
threshold in the Run section. Throttle automation below the threshold by
default.

## 7. Success measures

- A new user goes from install to a sandbox strategy running in under 10
  minutes without reading docs.
- Share of active strategies that were backtested before going live.
- Weekly active strategies per install; strategies promoted sandbox to live.
- Support questions of the form "which builder should I use" drop to near zero.

## 8. Open questions

1. Historical options data source (6.2).
2. Do Flow and Python strategies get backtest parity, or is that labelled as
   forward-test only?
3. Is the strategy library curated in the repo only, or is there a community
   share mechanism (and who moderates it)?
4. Does the home page redesign also change docs.openalgo.in diagrams (yes for
   any IA change, per CLAUDE.md)?

## Sources

- [8 Best Algo Trading Platforms in India (2026), AlgoTest](https://algotest.in/blog/8-best-algo-trading-platforms-in-india-2026.md)
- [AlgoTest vs Tradetron (2026)](https://algotest.in/blog/algotest-vs-tradetron-detailed-comparison/)
- [AlgoTest vs Streak](https://algotest.in/blog/algotest-vs-streak.md)
- [Best Algo Trading Software in India, Rupeezy](https://rupeezy.in/blog/best-algo-trading-software-india)
- [Sensibull Review 2026](https://algotest.in/blog/sensibull-review-2026-features-pricing-and-who-its-for/)
- [Opstra vs Sensibull (2026)](https://algotest.in/blog/opstra-vs-sensibull.md)
- [Best Options Simulator in India (2026)](https://algotest.in/blog/best-options-simulator-india.md)
- [Composer Review (2026)](https://walnutinvest.com/resources/composer-review)
- [New SEBI framework for retail algo trading, Fyers](https://fyers.in/notice-board/new-sebi-framework-for-retail-algo-trading-from-april-01-2026/)
- [SEBI algo trading rules 2026, Sahi](https://www.sahi.com/blogs/sebi-algo-trading-rules-2026-what-every-retail-trader-must-know-before-april)
- [SEBI framework explained, Liquide](https://blog.liquide.life/sebi-algo-trading-regulations-2026/)
