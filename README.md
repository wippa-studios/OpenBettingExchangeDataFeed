# OpenBettingExchangeDataFeed — two Betfair feeds on Postgres

Two feeds from one collector, one database, over MCP or the CLI.

| | Feed 1 | Feed 2 |
|---|---|---|
| Contents | fixtures, markets, runners | feed 1 **+ prices, ladders, results, scores** |
| Odds | never | yes, freshest available |
| Cadence | every **12 h** (`FIXTURES_INTERVAL_MINUTES`) | every **5 m** (`ODDS_INTERVAL_MINUTES`) |
| Prices | structurally impossible — no column in `v_feed1` | `market_odds_current`, joined in `v_feed2` |

Both are the same contract document. The only difference is whether `price` is
populated, and the document says which it is via `priced`. One implementation,
so the two cannot drift apart.

```
                    ┌─────────────────────────────────────┐
                    │ betfeed/mcp_server.py   (MCP)       │
                    │   Betfair tools ─┐                   │
                    │   feed tools   ──┼──► betfeed.*    │
                    └──────────────────┴──────────────────┘
                                       │
   Betfair API-NG ────► betfeed.betfair.BetfairClient
                                       │
                            ┌──────────┴───────────┐
                            │  collector process   │
                            │   every 12h: fixtures│
                            │   every  5m: odds    │
                            └──────────┬───────────┘
                                       ▼
   ┌────────────────────────────────────────────────────────────┐
   │ PostgreSQL                                                  │
   │   current  fixture · market · selection · market_odds_current│
   │            market_result · event_score                       │
   │   history  market_snapshot (partitioned) · fixture_change   │
   └────────────────────────────────────────────────────────────┘
```

---

## Setup

### 1. PostgreSQL, without Docker

```bash
sudo ./scripts/install_postgres.sh
```

Installs the server, creates a `betfair` role with a random password and a
`betfeed` database, and writes `DATABASE_URL` into `.env`. Re-running it is
safe and will not reset a working password.

> No sudo, no Docker? Betfair's own Linux binaries run fine unprivileged:
> download a `binaries-no-installation` tarball from enterprisedb.com,
> `./bin/initdb -D ~/pgdata`, `./bin/pg_ctl -D ~/pgdata start`, then point
> `DATABASE_URL` at it. The schema uses nothing beyond stock Postgres 12+.

### 2. Betfair credentials

```bash
cp .env.example .env      # if the install script did not already
$EDITOR .env              # BETFAIR_APP_KEY, and username/password
```

Login mode matters more than it looks:

| Mode | Credentials | Use when |
|---|---|---|
| `password` | app key + username + password | the account has no 2FA |
| `certs` | app key + `BETFAIR_CERTS_PATH` | **the collector, always** — works with 2FA |
| `session_token` | app key + a cached token | you logged in by hand once |

The original `login_interactive()` blocked on stdin for a one-time password.
That is fine at a terminal and fatal in cron, so the login modes above replace
it.

### 3. Schema and first run

```bash
uv run betfeed migrate            # idempotent
uv run betfeed run fixtures       # populate feed 1
uv run betfeed run odds           # capture first prices
uv run betfeed health
```

### 4. Keep it running

```bash
uv run betfeed collect            # both jobs, until Ctrl-C / SIGTERM
```

As a systemd unit, `After=network.target postgresql.service`, `Restart=always`.

---

## Reading the feeds

### CLI

```bash
uv run betfeed feed1 --sport soccer --n 5 --compact
uv run betfeed feed2 --n 5 --compact
uv run betfeed health
```

```
feed 2  generated=2026-10-13T12:05:02+00:00  oldest_price=41s
  3001  Test Utd v Arsenal  (upcoming)
      1.100        Match Odds                      OPEN +41s
          11         Test Utd                   2.30       ACTIVE    b3/l3
          12         Draw                       3.40       ACTIVE    b3/l3
          13         Arsenal                    3.00       ACTIVE    b3/l3
```

### Over MCP

Wire it into any MCP client:

```json
{
  "mcpServers": {
    "betfair": {
      "command": ["uv", "run", "--directory", "/home/Joel/Desktop/OpenBettingExchangeDataFeed", "betfeed-mcp"]
    }
  }
}
```

| Tool | Returns |
|---|---|
| `get_feed1_fixtures` | feed 1 — fixtures, no prices |
| `get_feed2_odds` | feed 2 — prices, ladders, results, scores |
| `get_market_history` | the stored price history of one market |
| `feed_health` | freshness and job status |
| `collector_config` | what the collector is configured to do |
| `list_event_types` / `list_events` / `list_market_catalogue` / `list_market_book` / `get_account_funds` | live Betfair queries |

The feed tools read Postgres, so a consumer asking "what is on today?" costs no
Betfair requests and no market-data weight. The Betfair tools are there for
questions the feed cannot answer.

---

## The contract

```jsonc
{
  "version": 1,
  "priced": true,                  // feed 1 is false
  "generated_at": "2026-10-13T12:05:02+00:00",
  "stale": false,                  // newest price older than 15 min
  "max_price_age_seconds": 41,
  "events": [{
    "event_key": "3001", "name": "Test Utd v Arsenal", "sport": "soccer",
    "competition": "Premier League", "venue": "Old Trafford",
    "starts_at": "...", "state": "upcoming|live|finished",
    "score": {"home_score": "2", "away_score": "1"},   // when captured
    "markets": [{
      "market_key": "1.100", "name": "Match Odds",
      "status": "OPEN",            // Betfair's own status, verbatim
      "state": "upcoming",         // the three-state summary
      "in_play": false,
      "price_age_seconds": 41,
      "result": {"status": "CLOSED_EVENT", "voided": false,
                 "winners": ["11"], "observed_at": "..."},
      "selections": [{
        "selection_key": "11", "name": "Test Utd", "handicap": 0.0,
        "price": 2.3, "size": 100.0,
        "back": [{"price": 2.3, "size": 100.0}, ...],
        "lay":  [{"price": 2.26, "size": 80.0}, ...],
        "status": "ACTIVE", "result": "WINNER"
      }]
    }]
  }]
}
```

`price` is the **best available to back**. `status` is Betfair's own vocabulary,
kept alongside `state` on purpose: collapsing `SUSPENDED` into "upcoming" hides
the fact that a market has stopped trading, which is exactly what a consumer
needs to know before betting on it.

Every published price must be on the [Betfair tick ladder](https://support.developer.betfair.com/hc/en-us/articles/360002266912-Price-Increment). A price
that is not on the ladder is refused at validation and dropped at ingest — a
price that never existed on the exchange is worse than a missing rung.

---

## Storage

Current state is upserted, so it is always the freshest view. History is
append-only.

| Table | Role |
|---|---|
| `fixture`, `market`, `selection` | feed 1's data, upserted by `market_id` / `selection_id` |
| `market_odds_current` | the newest book per market — feed 2's live half |
| `market_snapshot` | every poll, partitioned daily by `captured_at` |
| `market_result` | settlement: winners, voided, runner statuses |
| `event_score` | match score, where the Scores API is authorised |
| `fixture_change` | what feed 1 changed, per run |
| `feed_run` | one row per job run — freshness alarms read this |

```sql
-- what feed 2 is serving, right now
SELECT market_id, captured_at, now() - captured_at AS age, status
FROM market_odds_current ORDER BY age DESC LIMIT 10;

-- one market's price history
SELECT captured_at, ladder
FROM market_snapshot
WHERE market_id = '1.100' ORDER BY captured_at DESC LIMIT 100;
```

Retention drops whole partitions (`SNAPSHOT_RETENTION_DAYS`, default 30), so
purging history is a DDL `DROP` rather than a `DELETE` that bloats the table.

---

## Results

Three layers, because they have different reliability:

1. **Settlement from the book** — free. Any market book whose status is
   terminal, or that has assigned a `WINNER`, gives the outcome. Place markets
   keep every winner; voided runners keep their status.
2. **Backfill sweep** — feed 1 asks for the unresolved markets that have started
   (`RESULTS_BACKFILL_HOURS`, default 48). This is what catches an event that
   settled while the 5-minute job was down. Betfair will not return a closed
   market in a request that also contains open ones, so this needs its own call.
3. **Scores** — the match score (2-1) from Betfair's Scores API. It requires an
   app key authorised separately; without one, `SCORES_ENABLED=true` is a no-op
   that logs why. Settlement works either way.

`market_result.settled_at` is best-effort: Betfair's settlement timestamp is not
in the market book, so `observed_at` (when we saw it) is the reliable column.

---

## Betfair constraints this is built around

| Constraint | What the code does |
|---|---|
| `listMarketBook` capped at 200 **weight points**, not 200 markets | chunks at `BOOK_CHUNK_SIZE`, and **bisects and retries** on `TOO_MUCH_DATA` so the cap is discovered rather than assumed |
| `listMarketCatalogue` takes ≤ 100 event ids, ≤ 1000 markets | chunks events; a response at the cap splits the chunk and refetches, so a saturated neighbour cannot hide an event's markets |
| A single event cannot be split further | recorded in `truncated_events` and reported in the job stats, not silently dropped |
| Mixed OPEN+CLOSED requests return only OPEN markets | the backfill sweep requests the closed set explicitly |
| Sessions expire | login is lazy, re-checked before every call, and retried once on `INVALID_SESSION_TOKEN` |
| In-play prices are ~2 min delayed on a non-live key | expected; `price_age_seconds` is the honest clock |

Every one of these has a test in `tests/test_betfair.py`.

---

## Configuration

All in `.env`; see `.env.example` for the annotated list. The ones that matter:

| Variable | Default | Effect |
|---|---|---|
| `EVENT_TYPE_IDS` | `1,8` | sports to walk. Horse racing (7) is opt-in: it dwarfs everything else in market count |
| `MARKET_TYPE_CODES` | *(all)* | the single biggest lever on feed 2's request volume |
| `FIXTURES_INTERVAL_MINUTES` | `720` | feed 1 cadence |
| `ODDS_INTERVAL_MINUTES` | `5` | feed 2 cadence |
| `ODDS_HORIZON_HOURS` | `168` | only poll markets starting within this window |
| `LADDER_LEVELS` | `3` | depth per side |
| `BOOK_CHUNK_SIZE` | `100` | target markets per price request |
| `SNAPSHOT_RETENTION_DAYS` | `30` | how long price history is kept |

## Repo layout

```
betfeed/
  config.py       validated env; aborts naming the bad variable
  betfair.py      session mgmt, chunking, bisection, retries, catalogue paging
  normalize.py    Betfair resources -> dicts; settlement extraction
  contract.py     the wire contract (price optional = the feed1/feed2 split)
  odds.py         Betfair tick ladder
  db.py           connections, advisory locks, partitions, retention, run log
  feeds.py        rows -> feed document; health
  jobs/fixtures.py  feed 1 (12h) + result backfill
  jobs/odds.py      feed 2 (5m)
  scheduler.py    two loops, drift-corrected, lock-guarded
  cli.py          migrate / collect / run / feed1 / feed2 / health
  mcp_server.py   the MCP server (5 Betfair tools + 5 feed tools)
  schema.sql      the whole schema, idempotent
server.py         thin shim so old MCP configs still start the server
scripts/install_postgres.sh
tests/
```

---

## Tests

```bash
uv run pytest                                   # no database needed
TEST_DATABASE_URL=postgresql://...@127.0.0.1:5432/betfeed_test \
    uv run pytest tests/test_pipeline.py        # full pipeline, real Postgres
```

The pipeline tests **drop and recreate the schema they run in**. Point
`TEST_DATABASE_URL` at a scratch database, never at the live feed.

Coverage worth knowing about:

- `test_odds.py` — the ladder, including that midpoints round **up**. Python's
  `round()` is banker's rounding and disagreed with the JavaScript this was
  ported from on every half-step price; that bug was caught by a round-trip
  test, not by reading.
- `test_contract.py` — a price-less document is valid; an off-ladder price is
  refused; `SUSPENDED` is not collapsed.
- `test_betfair.py` — chunking, bisection under `TOO_MUCH_DATA`, retry limits,
  catalogue splitting, login failure messaging.
- `test_pipeline.py` — feed 1 has no prices; feed 2 does; history is retained
  while the current book is replaced; settlement retires a market; a failed job
  rolls back and is recorded; a second connection cannot take the job lock.

---

## Notes

- **Licensing.** A personal Betfair key is fine for your own use. Republishing
  these prices to anyone else is not permitted by the API's approved-use terms,
  and neither is jittering them and calling them your own. See
  `OpenBookmaker-Data/README.md` for the longer argument.
- **Costs.** With `BOOK_CHUNK_SIZE=100`, feed 2 costs roughly
  `markets / 100` price requests per poll — about 12 per poll for 1,200 soccer
  markets, or ~3,500 requests/day. Set `MARKET_TYPE_CODES=MATCH_ODDS` to cut
  that further.