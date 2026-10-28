# Multiple Broker Accounts Logged In at Once: Plan

**Status:** Planning only. Nothing in this document is built.
**Date:** 2026-10-28
**Related:** [2026-10-28-trading-first-product-plan.md](2026-10-28-trading-first-product-plan.md) (Phase 6)

---

## 1. Goal

Let the one OpenAlgo user connect **several broker accounts at the same time**
(different brokers, or several accounts at the same broker, for example their
own and a family member's) and:

1. see funds, positions, orders and P&L per account and combined;
2. place an order on a chosen account, or on a group of accounts with a
   quantity multiplier per account (fan-out, master-to-child copy);
3. run any strategy against a chosen account or group;
4. pick one account as the **market data account** that feeds the shared
   WebSocket stream for everybody;
5. keep each account's daily login, token expiry and static IP status visible.

### Non-goals

- Multiple OpenAlgo **users**. The deployment stays single-user; this is one
  person operating several accounts. No roles, no per-account permissions.
- Offering copy trading to third parties as a service. That has regulatory
  implications (SEBI: platforms offering it must operate through registered
  brokers and empanelled algo providers) and is out of scope.
- Cross-account margin netting. Each broker computes its own margin.

## 2. How the code works today (why this is not a small change)

Findings from the current tree:

| Area | Today | Consequence |
| --- | --- | --- |
| `database/auth_db.py` `Auth` | One row per **username** (`name` unique), one `broker`, one `auth`, one `feed_token` | Only one broker session can exist |
| Broker credentials | `BROKER_API_KEY`, `BROKER_API_SECRET`, `BROKER_API_KEY_MARKET`, `REDIRECT_URL` in `.env`; read with `os.getenv` at call time in about 100 files under `broker/` (for example `broker/zerodha/api/order_api.py:249`) | Two accounts at the same broker cannot have different keys without threading credentials through every plugin |
| Flask session | `session["broker"]` read in about 16 blueprints | UI pages assume one broker |
| API key resolution | `get_auth_token_broker(api_key)` returns one token and one broker | `/api/v1/` cannot address an account |
| `SymToken` (master contract) | One table, no broker column | Two brokers' symbol masters would overwrite each other |
| Feed pool | `websocket_proxy/broker_factory.py:_POOLED_ADAPTERS` keyed `{broker}_{user_id}` | Already close to account-keyed; needs the account id in the key |
| `upsert_auth` teardown | Publishes `CACHE_INVALIDATE_ALL` and `cleanup_pools_for_user` when the token changes | Must be scoped to the one account whose token changed, or logging into account B tears down account A's feed |
| Sandbox | `sandbox.db`, one book per user | Needs a book per account to mirror live |

## 3. Design

### 3.1 Data model

New table `broker_accounts` (in `openalgo.db`, migration in `upgrade/`):

| Column | Notes |
| --- | --- |
| `id` | Integer PK; the account id used everywhere |
| `label` | Trader-chosen name ("Own Zerodha", "Spouse Dhan") |
| `broker` | Plugin name |
| `client_id` | Broker client code, display and de-duplication |
| `api_key_enc`, `api_secret_enc`, `market_key_enc`, `market_secret_enc` | Fernet-encrypted credentials, same KDF as `auth_db` |
| `aux_params_enc` | TOTP secret or other broker-specific values, encrypted |
| `redirect_url` | Per account (OAuth brokers register one per app) |
| `is_default` | Exactly one; receives requests that name no account |
| `is_data_account` | Exactly one; feeds the WebSocket stream |
| `enabled` | Disabled accounts are never logged in or traded |
| `created_at`, `updated_at` | |

`Auth` gains `account_id` (FK, nullable during migration). The unique
constraint moves from `name` to `(name, account_id)`, which SQLite cannot alter
in place: rebuild the table per `migrate_sandbox_trigger_pending.py`.

**Migration backfill:** create one `broker_accounts` row from the current
`.env` credentials and the existing `Auth.broker`, mark it default and data
account, and point the existing `Auth` row at it. An install that never adds a
second account behaves exactly as today. `.env` keys remain the fallback source
for the default account so existing installs and install scripts keep working.

`SymToken` gains a `broker` column with a composite index
`(broker, symbol, exchange)`. Master contract download writes only its own
broker's rows. Backfill existing rows with the current broker. Lookups pass the
broker of the account in use.

Sandbox tables gain `account_id`; each live account gets a mirrored sandbox
book. Backfill to the default account.

### 3.2 The account context

One small object carries everything a broker call needs:

```python
@dataclass(frozen=True, slots=True)
class AccountContext:
    account_id: int
    broker: str
    auth_token: str
    feed_token: str | None
    credentials: BrokerCredentials  # decrypted, never logged
```

Resolved once per request or per job by `services/accounts/resolve.py`:

- `/api/v1/`: optional `account` (id or label) in the JSON body, beside
  `apikey`; absent means default account. Group orders use `accounts`
  (list) or `account_group`.
- UI: the active account selected in the navbar account switcher, stored in
  the Flask session as `account_id` (replacing reads of `session["broker"]`
  through a helper, not one-by-one edits).
- Strategies, Flow, Python host, scheduler jobs: the account id stored on
  the run.

### 3.3 Getting credentials to broker plugins

This is the largest piece of work. Two options:

- **A. Explicit parameter (recommended).** Each plugin function that reads
  `os.getenv("BROKER_*")` takes the credentials from the `AccountContext` passed
  by the service layer. Done broker by broker; a plugin is marked
  `MULTI_ACCOUNT_READY = True` in its `plugin.json` when converted. Accounts at
  an unconverted broker are limited to one per broker (still useful: Zerodha
  plus Dhan works on day one, because each broker reads its own env keys and
  the per-account values for it can be exported as the default).
- **B. Context variable** set around each broker call. Less churn, but the
  value must survive eventlet green threads, gthread pool threads, executors,
  APScheduler jobs and the real threads named in CLAUDE.md. A missed hop sends
  an order with the wrong account's keys. Rejected for order paths; acceptable
  only as a transitional shim behind a test that fails if the context is unset.

The `broker-integration` skill and its docs are updated so new plugins are
written against the context from the start.

### 3.4 Login and session lifecycle

- An **Accounts** page (new route, three registrations) lists accounts with
  status: Connected, Login needed, Expired, Disabled; token expiry time;
  static IP check result.
- Each account logs in independently through its broker's existing auth flow,
  with the callback carrying the account id in the OAuth `state` parameter.
- Automated daily TOTP login, where the broker allows it, runs per account
  after the ~3:00 AM IST rollover, staggered to avoid rate limits.
- `upsert_auth(..., account_id)` keeps the existing gate (compare decrypted
  plaintext; tear down only on a real change) and scopes the teardown to that
  account: the ZMQ message carries the account id, and only that account's
  pool is cleaned. Logging into account B must never disturb A's feed
  (extends the issue #1591 invariant).
- Logging out of OpenAlgo does not revoke broker tokens; revoking an account
  is an explicit action on that account.

### 3.5 Market data

- One **data account** feeds the shared WebSocket stream and quote endpoints.
  Data does not depend on which account trades, so there is no reason to open
  several feeds by default.
- Pool key becomes `{broker}_{account_id}`. ZMQ topology is unchanged (proxy
  SUB binds, every publisher connects; fixed `ZMQ_PORT`).
- Optional later: a second account as feed failover when the data account's
  session expires.
- Symbol resolution for an order uses the **trading** account's broker
  `SymToken` rows; resolution for a subscription uses the data account's.

### 3.6 Orders across accounts

- Single-account order: identical to today, routed by `AccountContext`.
- **Account groups** (new table): a named set of accounts with a quantity
  rule per member (fixed lots, multiplier, or percentage of available funds,
  always rounded to whole lots and capped by freeze quantity).
- Fan-out places each member order independently, in parallel on the shared
  executor, bounded by `utils/broker_backpressure.py`. One member failing does
  not cancel the others; the response lists each member's result.
- Exits are matched per account and per order id, never by symbol across
  accounts (same rule as "match a fill to the order it belongs to").
- `place_order_with_auth` gains `account_id`; the analyzer toggle and
  `force_live` semantics apply per call, unchanged.
- Rate: 10 orders per second is per account at the broker; fan-out across
  accounts sharing one static IP must still respect each broker's IP-level
  limits. Throttle per broker and per account.

### 3.7 Risk

- Positions are keyed `(account_id, symbol, exchange, product)`.
- `services/risk/` stays I/O-free; consumers pass positions for one account or
  for a group, and the same evaluators run on either set.
- New aggregate rules (daily loss across all accounts) are added to the core
  with cases in `test/risk/vectors.json`.
- Kill switch: per account and global.

### 3.8 UI

- Navbar account switcher beside the broker badge (inside
  `components/layout/AccountControls.tsx`, so /trading and the navbar stay in
  sync), with an "All accounts" view for read-only screens.
- Positions, Orderbook, Tradebook, Holdings and Funds gain an account column
  and filter; the dashboard shows per-account and combined P&L.
- Order tickets (trading terminal, scalping, option chain) show the target
  account or group prominently. Changing it mid-session requires a deliberate
  action, never a default that silently changes.

### 3.9 Security

- Credentials move from `.env` to the database, encrypted. The admin page
  shows masked values only. Back-up and restore docs updated.
- Every order log row and Telegram alert names the account.
- A family member's account is still operated by this single user; the
  Accounts page states that the owner of each account must have authorised it.

### 3.10 Static IP

Under the SEBI framework each broker app is tied to whitelisted IPs. Several
accounts can register the same server IP, but some brokers allow an IP on only
one app or one client. The Accounts page shows, per account, whether the
broker accepted an authenticated non-order call from this server, and explains
in trader language when an order was refused for IP reasons.

## 4. Phases

| Phase | Delivers | Behaviour for single-account installs |
| --- | --- | --- |
| A. Schema and context | `broker_accounts`, `Auth.account_id`, `SymToken.broker`, sandbox `account_id`, migrations with `--status`, `AccountContext` resolver, default account backfill | Unchanged |
| B. Different brokers at once | Accounts page, per-account login, scoped `upsert_auth` teardown, data account, account switcher, per-account books | Unchanged |
| C. Same broker, many accounts | Credential threading in plugins (start with Zerodha, Dhan, Angel, Fyers, Upstox, Shoonya/Flattrade), `MULTI_ACCOUNT_READY` flag | Unchanged |
| D. Groups and fan-out | Account groups, multipliers, group orders in API, Flow, `/strategy` and terminals | Unchanged |
| E. Combined risk and review | Cross-account RMS rules, combined P&L, journal by account | Unchanged |

## 5. Testing

- Migration tested on a copy of a populated real database forced back to the
  old schema, and re-run to prove idempotence.
- `test_gthread_*` cases: two accounts logging in concurrently; login of B
  while A streams (A's ticks keep arriving); fan-out to five accounts with one
  broker failing; exit claim per account under a barrier.
- Eventlet subprocess tests for any new cross-thread hand-off.
- `fd-audit` after phases B and D (new sessions, pools, executor use).
- Vectors added for every new risk rule.

## 6. Risks and open questions

1. **Plugin churn.** About 100 files read broker env keys directly. Phase C is
   broker by broker and can stop at the brokers people actually use.
2. **SymToken size.** Several brokers' masters multiply the table. Measure
   disk and lookup time with three masters loaded before committing to one
   table versus one per broker.
3. **Single-active-session brokers.** Finvasia/Noren style brokers drop a token
   on duplicate sessions; two OpenAlgo accounts must never share one broker
   login.
4. **Documentation.** CLAUDE.md's "one broker session per instance" line and
   the multi-session invariant must be updated when Phase B lands, along with
   the architecture diagrams in `openalgo-docs`.
5. **Should the data account be allowed to differ from every trading
   account?** Recommended yes; symbol mapping handles the difference.

## Sources

- [New SEBI framework for retail algo trading, Fyers](https://fyers.in/notice-board/new-sebi-framework-for-retail-algo-trading-from-april-01-2026/)
- [SEBI rules for retail algo trading from April 1 2026, Fyers support](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026)
- [SEBI algo trading rules 2026, Sahi](https://www.sahi.com/blogs/sebi-algo-trading-rules-2026-what-every-retail-trader-must-know-before-april)
- [What is copy trading, share.market](https://www.share.market/buzz/learn/what-is-copy-trading/)
