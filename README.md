# OpenBettingExchangeDataFeed

[![CI](https://github.com/wippa-studios/OpenBettingExchangeDataFeed/actions/workflows/ci.yml/badge.svg)](https://github.com/wippa-studios/OpenBettingExchangeDataFeed/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-%E2%89%A53.13-3776AB.svg)](pyproject.toml)
[![Betfair Delay key](https://img.shields.io/badge/app%20key-Delay%20ready-8A2BE2)](#delay-key-limits)

Two Betfair feeds on PostgreSQL, from one collector.

| | **Feed 1** | **Feed 2** |
|---|---|---|
| Contents | fixtures, markets, runners | feed 1 **+ prices, ladders, results, scores** |
| Odds | never | yes, freshest available |
| Default cadence | every **12 h** | every **5 m** |
| Prices | structurally impossible — no price column exists in `v_feed1` | `market_odds_current`, joined in `v_feed2` |

Both feeds are **the same contract document**. The only difference is whether
`price` is populated, and the document says which via `priced`. One
implementation, so the two cannot drift apart.

Built to be embedded by [OpenBettingExchange](https://github.com/wippa-studios/OpenBettingExchange),
but it is a standalone package: it runs on its own, over a CLI or MCP, and needs
nothing from the exchange to work.

---

## Contents

- [Quickstart](#quickstart)
- [Getting a Betfair API key](#getting-a-betfair-api-key)
- [Running with OpenBettingExchange](#running-with-openbettingexchange)
- [Running standalone](#running-standalone)
- [The contract](#the-contract)
- [Configuration](#configuration)
- [Delay-key limits](#delay-key-limits)
- [Storage](#storage)
- [Results and settlement](#results-and-settlement)
- [Operating it](#operating-it)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

---

## Quickstart

```bash
git clone https://github.com/wippa-studios/OpenBettingExchangeDataFeed
cd OpenBettingExchangeDataFeed
uv sync

# 1. PostgreSQL — installs the server, creates a `betfair` role and a `betfeed`
#    database, and writes DATABASE_URL into .env. Safe to re-run.
sudo ./scripts/install_postgres.sh

# 2. Betfair credentials — see the next section
$EDITOR .env

# 3. Check everything at once
uv run betfeed validate

# 4. Collect
uv run betfeed migrate
uv run betfeed run fixtures
uv run betfeed run odds

# 5. Keep it running
uv run betfeed collect
```

`betfeed validate` is the step worth keeping. It checks credentials, delay-key
limits, database reachability and a real login, and exits non-zero if anything is
wrong — so you find out in seconds rather than from a stack trace ten minutes
into a collection.

---

## Getting a Betfair API key

Betfair issues **two** application keys when you register: a **Delay** key
(active immediately, for development) and a **Live** key (inactive until you
upgrade your subscription). This package is built for the **Delay** key, which
is what you get by default.

### 1. Create the key

From Betfair's own documentation:

1. Log in to your Betfair account at [betfair.com](https://www.betfair.com)
2. In a **separate browser window**, open the **Accounts API Demo Tool**
   (Developer Tools → Accounts API Demo Tool)
3. Select the **`createDeveloperAppKeys`** operation
4. Your session token (`ssoid`) populates automatically
   - For `.com.au`, `.it`, `.es` and `.ro` jurisdictions, see the Exchange API
     FAQ: *How do I retrieve a session token for use with the Demo Tool?*
5. Enter a unique **Application Name** — do not use your Betfair username
6. Press **Execute**

Your 16-character key appears in the Demo Tool's right-hand panel under
**Application Key**. Expand the column to see it in full.

### 2. Put it in `.env`

```bash
BETFAIR_LOGIN_MODE=password
BETFAIR_APP_KEY=your16characterkey
BETFAIR_USERNAME=your-betfair-username
BETFAIR_PASSWORD=your-betfair-password
```

```bash
chmod 600 .env
```

### 3. Prove it

```bash
uv run betfeed validate
```

### What "Delay" means, precisely

Measured on a Delay key, and worth stating plainly because it is not obvious:

| | |
|---|---|
| Betfair marks every book | `is_market_data_delayed: true` |
| `publish_time` | **withheld entirely — always `null`** |
| Prices | 5 minutes behind the real exchange |

Because `publish_time` is withheld, **the age of the data cannot be measured.**
`betfeed health` reports `odds_age_seconds`, which measures *when we polled* —
not when Betfair priced the market. It is labelled `app_key_delay: true` so the
number is never mistaken for freshness.

**Polling faster does not reduce the delay.** Only a Live key does, and that is a
commercial upgrade with Betfair, not a configuration change.

### Two things that are Live-key-only

Requesting these on a Delay key fails the whole call with `DSC-0018`:

- the `VENUE` catalogue projection
- the `EVENT_RUNNER_DESCRIPTION` projection

The defaults exclude both. `betfeed validate` fails if you add either back.

---

## Running with OpenBettingExchange

[OpenBettingExchange](https://github.com/wippa-studios/OpenBettingExchange) is
the venue this feed was built for. It embeds this package as a plug-in: an
administrator pastes their Betfair credentials into the **admin dashboard** and
the exchange runs the collector on their behalf.

**No `.env` file is involved.** The exchange passes everything as environment
variables, which is why this package boots from the environment alone — a
property the exchange's test suite pins, because a config that cannot boot
without a file it is not given is not embeddable.

```
  admin dashboard
        │  app key · username · password
        ▼
  OpenBettingExchange  ──►  betfeed (env vars only)
        │                          │
        │                    PostgreSQL
        │                          │
        └──── reads ────► v_feed1 · v_feed2 ◄──┘
```

If you are running the exchange, you do not need this README's setup steps —
start at [openbettingexchange → admin → feeds](https://github.com/wippa-studios/OpenBettingExchange).

---

## Running standalone

### CLI

```bash
uv run betfeed migrate                      # create/refresh the schema (idempotent)
uv run betfeed run fixtures                 # one fixtures pass
uv run betfeed run odds                     # one odds pass
uv run betfeed collect                      # both jobs, until SIGTERM
uv run betfeed collect --jobs fixtures      # one job, continuously
uv run betfeed validate                     # check config + credentials + login
uv run betfeed health                       # freshness and job status

uv run betfeed feed1 --sport soccer --n 5 --compact
uv run betfeed feed2 --n 5 --compact
```

```
feed 2  generated=2026-10-13T12:05:02+00:00  oldest_price=41s
  3001  Test Utd v Arsenal  (upcoming)
      1.100        Match Odds                      OPEN +41s
          11         Test Utd                   2.30       ACTIVE    b3/l3
          12         Draw                       3.40       ACTIVE    b3/l3
          13         Arsenal                    2.00       ACTIVE    b3/l3
```

### Over MCP

```json
{
  "mcpServers": {
    "betfair": {
      "command": ["uv", "run", "--directory", "/path/to/OpenBettingExchangeDataFeed", "betfeed-mcp"]
    }
  }
}
```

| Tool | Returns |
|---|---|
| `get_feed1_fixtures` | feed 1 — fixtures, no prices |
| `get_feed2_odds` | feed 2 — prices, ladders, results, scores |
| `get_market_history` | stored price history for one market |
| `feed_health` | freshness and job status |
| `collector_config` | what the collector is configured to do |
| `list_event_types`, `list_events`, `list_market_catalogue`, `list_market_book`, `get_account_funds` | live Betfair queries |

The feed tools read PostgreSQL, so a consumer asking *"what is on today?"* costs
no Betfair requests and no market-data weight. The Betfair tools are there for
questions the feed cannot answer.

---

## The contract

```jsonc
{
  "version": 1,
  "priced": true,                  // false for feed 1
  "generated_at": "2026-10-13T12:05:02+00:00",
  "stale": false,
  "max_price_age_seconds": 41,
  "events": [{
    "event_key": "3001", "name": "Test Utd v Arsenal", "sport": "soccer",
    "competition": "Premier League", "venue": "Old Trafford",
    "starts_at": "...", "state": "upcoming|live|finished",
    "score": {"home_score": "2", "away_score": "1"},
    "markets": [{
      "market_key": "1.100", "name": "Match Odds",
      "status": "OPEN",            // Betfair's own vocabulary, verbatim
      "state": "upcoming",         // three-state summary
      "in_play": false,
      "price_age_seconds": 41,
      "result": {"status": "CLOSED", "voided": false,
                 "winners": ["11"], "observed_at": "..."},
      "selections": [{
        "selection_key": "11", "name": "Test Utd", "handicap": 0.0,
        "price": 2.3, "size": 100.0,
        "back": [{"price": 2.3, "size": 100.0}],
        "lay":  [{"price": 2.26, "size": 80.0}],
        "status": "ACTIVE", "result": "WINNER"
      }]
    }]
  }]
}
```

Three decisions worth knowing:

- **`price` is the best available to back**, not the last traded price.
- **`status` is Betfair's own vocabulary** and is kept alongside `state`.
  Collapsing `SUSPENDED` into "upcoming" hides the fact that a market has
  stopped trading — exactly what a consumer needs to know before betting on it.
- **Every published price is on the [Betfair tick
  ladder](https://support.developer.betfair.com/hc/en-us/articles/360002266912-Price-Increment).**
  A price that is not on the ladder is refused at validation and dropped at
  ingest. A price that never existed on the exchange is worse than a missing
  rung.

---

## Configuration

Everything is an environment variable. A `.env` file is read if present, and
**environment variables take precedence over it** — which is what lets a host
application supply credentials without writing a file at all.

### Required

| Variable | Notes |
|---|---|
| `DATABASE_URL` | `postgresql://user:pass@host:5432/db`. No default is usable; the install script writes one for you. |
| `BETFAIR_APP_KEY` | The 16-character key from Betfair. |
| `BETFAIR_USERNAME` / `BETFAIR_PASSWORD` | Your Betfair login. Required for `login_mode=password`. |

### Optional — defaults shown

| Variable | Default | Notes |
|---|---|---|
| `BETFAIR_LOGIN_MODE` | `password` | `password` \| `certs` \| `session_token`. See below. |
| `BETFAIR_LOCALE` | `en` | Betfair locale. |
| `EVENT_TYPE_IDS` | `1,8` | Sports to walk. See the table below. |
| `MARKET_TYPE_CODES` | *(empty)* | Restrict market types. Empty means all. |
| `FIXTURES_INTERVAL_MINUTES` | `720` | Feed 1 cadence. **See the note below — 720 leaves results up to 12 h stale.** |
| `ODDS_INTERVAL_MINUTES` | `5` | Feed 2 cadence. |
| `ODDS_HORIZON_HOURS` | `168` | How far ahead odds are polled. |
| `SETTLE_GRACE_MINUTES` | `30` | How long a finished market is left unresolved. |
| `BOOK_CHUNK_SIZE` | `40` | Markets per `listMarketBook`. **Delay-key limit.** |
| `CATALOGUE_CHUNK_EVENTS` | `100` | Events per `listMarketCatalogue`. |
| `CATALOGUE_MAX_RESULTS` | `200` | Markets per catalogue response. **Delay-key limit.** |
| `CATALOGUE_PROJECTION` | see below | **Must be delay-safe.** |
| `LADDER_LEVELS` | `3` | Price levels stored per side. |
| `INCLUDE_TRADED_VOLUME` | `false` | Adds traded volume per selection. Costs weight. |
| `SNAPSHOT_RETENTION_DAYS` | `30` | Whole partitions are dropped. |
| `RETENTION_CHECK_EVERY_MINUTES` | `360` | |
| `RESULTS_BACKFILL_HOURS` | `48` | How far back the results sweep looks. |
| `RESULTS_BACKFILL_MAX_MARKETS` | `2000` | |
| `SCORES_ENABLED` | `true` | Scores API — not authorised on every key. |
| `LOG_LEVEL` | `INFO` | |

### `EVENT_TYPE_IDS`

| id | Sport | | id | Sport |
|---|---|---|---|---|
| `1` | Soccer | | `15` | Basketball |
| `4` | Cricket | | `17` | American Football |
| `7` | Horse Racing | | `20` | Boxing |
| `8` | Tennis | | `24` | Darts |
| `10` | Golf | | `26` | Snooker |
| `12` | Motor Racing | | `30` | Esports |
| `13` | Rugby | | `4339` | Racing (Aus) |

Horse racing is deliberately **opt-in**: it is by far the highest market count and
will dominate every table.

### `BETFAIR_LOGIN_MODE`

| Mode | Credentials | Use when |
|---|---|---|
| `password` | app key + username + password | **A Delay key.** This is the default and the right answer for most people. |
| `certs` | app key + `BETFAIR_CERTS_PATH` | The account has 2FA and a client certificate has been registered. |
| `session_token` | app key + a cached token | You logged in by hand once and want to skip the login call. |

> **On the `certs` row.** betfairlightweight's `Login` endpoint is hard-wired to
> the *certificate* login URL, so password mode calls `login_interactive()` to
> reach the real password endpoint. If you switch to `certs` and get
> `CERT_AUTH_REQUIRED` on a Delay key, that is expected — certificate auth is
> not available for Delay keys.

---

## Delay-key limits

A Delay key is rate- and size-limited more tightly than a Live key. These values
are measured, not guessed, and they are the defaults:

| Call | Limit | Above it |
|---|---|---|
| `listMarketCatalogue` | `maxResults ≤ 200` | `ANGX-0001` / `TOO_MUCH_DATA` |
| `listMarketBook` | `≤ 40` markets per call | `ANGX-0001` / `TOO_MUCH_DATA` |
| Catalogue projection | no `VENUE`, no `EVENT_RUNNER_DESCRIPTION` | `DSC-0018` |

`betfeed validate` checks all three.

### One more, that is not a limit but a data shape

Betfair reports an **infinite size** for the best-lay-at-minimum-odds offer. That
has no JSON representation, so it is stored as `null` — meaning "no finite size
offered". A consumer that treats `null` size as zero will under-report matched
volume at the 1.01 lay.

---

## Storage

Current state is upserted, so it is always the freshest view. History is
append-only.

| Table | Role |
|---|---|
| `fixture` | events — name, competition, start time, country |
| `market` | markets, keyed by `market_id` |
| `selection` | runners, keyed by **`(market_id, selection_id)`** |
| `market_odds_current` | the newest book per market — feed 2's live half |
| `market_snapshot` | every poll, partitioned daily by `captured_at` |
| `market_result` | settlement: winners, voided, runner statuses |
| `event_score` | match score, where the Scores API is authorised |
| `fixture_change` | what feed 1 changed, per run |
| `feed_run` | one row per job run — freshness alarms read this |

> **Why the composite key.** Betfair **reuses selection ids across markets**:
> `selection_id 1` is `0 - 0` in the Correct Score market of every football match
> on the exchange. Keyed on `selection_id` alone, each market's selection 1
> overwrites the previous market's. Measured on a real collection with the
> single-column key: 11,096 markets produced only 3,034 markets' worth of
> selection rows, and 419 Correct Score markets produced 10 — with `market_id`
> pointing at whichever market wrote last, so joins returned the **wrong runner
> name**. Always key a selection by its market.

```sql
-- what feed 2 is serving, right now
SELECT market_id, captured_at, now() - captured_at AS age, status
FROM market_odds_current ORDER BY age DESC LIMIT 10;

-- one market's price history
SELECT captured_at, ladder
FROM market_snapshot
WHERE market_id = '1.100' ORDER BY captured_at DESC LIMIT 100;

-- everything on today
SELECT * FROM v_feed2
WHERE market_start_time BETWEEN now() AND now() + interval '1 day';
```

Retention drops whole partitions — a DDL `DROP`, not a `DELETE` that bloats the
table.

---

## Results and settlement

Three layers, because they have different reliability:

1. **From the book — free.** Any market book whose status is terminal, or which
   has assigned a `WINNER`, gives the outcome with no extra API call. This is the
   normal path and it is why feed 2 settles faster than feed 1.
2. **Sweep.** The fixtures job re-checks markets that finished while nothing was
   watching, over `RESULTS_BACKFILL_HOURS`.
3. **Scores**, where the app key is authorised for the Scores API.

### What a result is, and is not

Two rules decide what counts, and both exist because the wrong version of each
was measured on real data:

**A market is settled only when Betfair says it is finished, or names a `WINNER`.**
A `REMOVED` runner — a non-runner, a late withdrawal, which happens in most
professional seasons — is **not** an outcome. The first implementation counted
it as one, and a routine withdrawal produced `voided: true` on a market that was
still `OPEN`; measured on a live poll, two "Match Specials" markets were reported
as cancelled while their fixtures had not been played. The runner's status still
travels through in `runners`, which is where a consumer applies its own
non-runner rule — it just never settles or voids a market by itself.

**Void is read, never inferred.** Only Betfair's own `VOIDED` status produces
`voided: true`. A terminal market with no winner — abandoned, postponed — is
settled with `voided: false` and an empty winner list, and the consumer's per-sport
rule decides what that means.

### A resolved market stays readable for seven days

The collector marks a settled market `is_resolved = TRUE, is_active = FALSE` so
the odds job stops polling it — and both views still publish it, for seven days
by `retired_at`. This is not a nicety: filtering on `is_active` alone removed
every settled market from both feeds, so the result was collected, stored, and
then made invisible to exactly the consumer it was collected for. Measured: six
rows in `market_result`, zero visible in `v_feed2`. A consumer settles within
days, not weeks, and the bound keeps a long-running collector from accumulating
every fixture it has ever seen.

> **Feed 1 settles slowly, by design of the cadence.** The results sweep rides
> inside the fixtures job, which defaults to every 12 h. A match finishing at
> 14:00 may not produce a result until 02:00. **If you want fresher settlement on
> feed 1, lower `FIXTURES_INTERVAL_MINUTES`** — the job asks Betfair for no price
> data, so polling it more often does not burn market-data weight.

---

## Operating it

As a systemd unit:

```ini
[Unit]
Description=OpenBettingExchangeDataFeed collector
After=network.target postgresql.service

[Service]
Type=simple
WorkingDirectory=/opt/OpenBettingExchangeDataFeed
ExecStart=/usr/local/bin/uv run betfeed collect
EnvironmentFile=/opt/OpenBettingExchangeDataFeed/.env
Restart=always
RestartSec=10
# The collector holds Betfair credentials in memory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/OpenBettingExchangeDataFeed

[Install]
WantedBy=multi-user.target
```

`betfeed health` is what an alarm should read. It reports per-job last-success,
failure counts in the last 24 hours, row counts and price age — plus
`app_key_delay`, so nobody reads `odds_age_seconds` as data freshness.

```jsonc
{
  "jobs": {"fixtures": {"last_ok": "...", "failed_recently": false}, ...},
  "active_fixtures": 768, "active_markets": 9976, "active_selections": 38201,
  "results": 6, "markets_with_odds": 9456,
  "odds_age_seconds": 171,
  "app_key_delay": true,
  "data_age_seconds": null,
  "note": "Betfair withholds publish_time on a DELAY key, so data age is not measurable..."
}
```

---

## Development

```bash
uv sync                 # dependencies
uv run pytest -q        # tests
uv run betfeed migrate  # schema
```

The package layout:

```
betfeed/
  betfair.py     Betfair API client — retrying, weight-aware
  config.py      validated configuration, defaults first
  db.py          connection, schema, migrations, partitions, upserts
  normalize.py   Betfair resources → feed rows; ladder tick validation
  contract.py    feed document assembly
  feeds.py       CLI + MCP readers
  odds.py        tick ladder
  scheduler.py   cadence
  cli.py         command line
  mcp_server.py  MCP server
  jobs/          fixtures.py · odds.py
```

### Tests

Tests run offline against fakes — no Betfair credentials and no database needed.
Several of them exist because of bugs that **only** appear against real Betfair
and a real Postgres, which is why they are pinned here:

- password mode must use the password endpoint, not the certificate one
- partition bounds must be literals, not bind parameters
- `selection` must key on `(market_id, selection_id)`
- JSONB columns need a `Jsonb()` wrapper
- non-finite floats (Betfair's infinite-size lay) must be coerced to `null`
- the configuration must boot with no `.env` at all

---

## Troubleshooting

**`ConfigError: EVENT_TYPE_IDS must list at least one Betfair event type id`**
You are running with no defaults. If you are on a recent version this is fixed;
otherwise set `EVENT_TYPE_IDS=1,8`.

**`CertsError: [Errno 2] No such file or directory: '/certs'`**
Old version: password mode demanded a certificate directory. Fixed — upgrade.

**`BetfairAuthError: … CERT_AUTH_REQUIRED`**
You are on `login_mode=certs` with a Delay key. Certificate auth is not
available for Delay keys. Set `BETFAIR_LOGIN_MODE=password`.

**`ANGX-0001` / `TOO_MUCH_DATA`**
`CATALOGUE_MAX_RESULTS` or `BOOK_CHUNK_SIZE` is too high for a Delay key. Run
`betfeed validate`; it names the offending setting.

**`DSC-0018` on a catalogue call**
`CATALOGUE_PROJECTION` includes `VENUE` or `EVENT_RUNNER_DESCRIPTION`, which are
Live-key-only.

**`cannot adapt type 'dict' using placeholder '%s'`**
Old version: JSONB columns were written without a `Jsonb()` wrapper. Upgrade.

**`invalid input syntax for type json … Token "Infinity" is invalid`**
Old version: Betfair's infinite-size lay offer. Upgrade — it is now stored as
`null`.

**`PermissionError: .env`**
The install script ran under `sudo` and left the file root-owned. Re-run it (it
now chowns to the invoking user) or `sudo chown "$USER" .env`.

**`excessive request rate`**
Too many calls. Raise `FIXTURES_INTERVAL_MINUTES` and `ODDS_INTERVAL_MINUTES`.
Betfair charges by *weight*, not request count.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bugs and feature requests via the
[issue tracker](https://github.com/wippa-studios/OpenBettingExchangeDataFeed/issues).

## Security

This package reads and stores Betfair credentials. See
[SECURITY.md](SECURITY.md) — do not open a public issue about a credential
problem.

## Licence

[MIT](LICENSE). Betfair is a trademark of the Betfair Group. This project is not
affiliated with or endorsed by Betfair.