# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is **pre-1.0**. There are no releases yet; changes land on `main`.

## [Unreleased]

### Documentation

- The README's "Results and settlement" section now states the two rules that
  decide what a result is — a non-runner never settles or voids a market, and
  void is read from Betfair's own `VOIDED` status, never inferred — and the
  seven-day window for which a resolved market stays readable in both views.
  Both are behaviour this release changed; a README that still described the old
  rules would have been describing the bug.


### Fixed

Everything below was found by running the collector against real Betfair and a
real PostgreSQL. The collector had never completed a full run before, so these
are the bugs that made it impossible rather than merely wrong.

- **`betfair` — password mode called the certificate endpoint.**
  `betfairlightweight`'s `Login.url` is hard-wired to
  `identity_cert_uri + "certlogin"`, with no password endpoint on that class, so
  password login POSTed to the certificate endpoint and Betfair answered
  `CERT_AUTH_REQUIRED` — an error that reads like "this account needs 2FA".
  Password mode now uses `login_interactive()`, and raises if no session token
  comes back rather than marking itself logged in.

- **`betfair` — password login died without `/certs`.**
  The library resolves a certificate directory on every login POST. Passing
  `cert_files=""` skips the lookup.

- **`betfair` — certificate mode could not construct a client.**
  `APIClient` requires `username` positionally, and the password is required
  even for cert auth: betfairlightweight always POSTs to `certlogin` and always
  sends `{username, password}`.

- **`schema` — `selection` was keyed on `selection_id` alone.**
  Betfair reuses selection ids across markets; `selection_id 1` is `0 - 0` in the
  Correct Score market of every football match. Each market's selection 1
  overwrote the previous market's, leaving `market_id` pointing at whichever
  market wrote last — so joins returned the **wrong runner name**. Measured
  before the fix: 11,096 markets produced only 3,034 markets' worth of selection
  rows, and 419 Correct Score markets produced 10. Now keyed
  `(market_id, selection_id)`, with a migration for existing databases.

- **`db` — partitions could not be created.**
  `FOR VALUES FROM (%s) TO (%s)` raised `IndeterminateDatatype`. A partition
  bound will not take a bind parameter, and `%s::date` does not help either.
  Bounds are now clock-derived literals.

- **`odds` — `BOOK_CHUNK_SIZE` of 100 exceeded a Delay key's limit.**
  Every call returned `ANGX-0001` / `TOO_MUCH_DATA`: 9,462 errors, 0 books.
  Measured: 40 is accepted, 100 is not. Default lowered to 40.

- **`odds` — JSONB columns were written without a `Jsonb()` wrapper.**
  psycopg3 does not adapt a `dict` for a `%s` placeholder.

- **`odds` — Betfair's infinite-size lay offer corrupted every row.**
  `json.dumps` writes it as the bare token `Infinity`, which Python accepts but
  is not valid JSON, so Postgres rejected the whole poll on any market carrying a
  1.01 lay. Non-finite floats are now stored as `null`.

- **`config` — the catalogue projection asked for Live-key-only fields.**
  `VENUE` and `EVENT_RUNNER_DESCRIPTION` fail a Delay key's request with
  `DSC-0018`. Dropping them also removed `market_type_code`, which the
  collector uses to decide what to poll — so it silently discarded every market
  (763 events → 0 markets). The projection is now configurable and validated.

- **Dependency:** `betfairlightweight` 2.20.4 → **2.24.0**. 2.20.4's
  `MarketCatalogueDescription` rejects `betDelayModels`, a field Betfair now
  returns, so every catalogue call raised `InvalidResponse`.

### Added

- **`betfeed validate`** — checks credentials, delay-key limits, database
  reachability and a real login, and exits non-zero when not ready, so a host
  application or CI step can gate on it.

- **Embeddable configuration.** The package boots from environment variables
  with **no `.env` file at all**, which is what lets OpenBettingExchange supply
  credentials without writing one. Previously four settings read `raw` instead of
  the merged defaults, so a fresh checkout with no `.env` could not boot.

- **Delay-key-safe defaults.** `CATALOGUE_MAX_RESULTS` defaulted to 1000, which
  a Delay key rejects. Betfair issues a Delay key by default, so a default that
  only works on a paid key breaks the first person to try it.

- **`app_key_delay` in `betfeed health`.** A Delay key withholds `publish_time`
  entirely, so data age is not measurable. `odds_age_seconds` measures when *we*
  polled, not when Betfair priced the market, and is now labelled so the number
  is not mistaken for freshness.

- Documentation: README, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, CHANGELOG,
  issue and PR templates, and CI.

### CI

- Tests on 3.13. **3.14 is experimental and non-blocking**: `pydantic-core` ships
  no 3.14 wheels, so `uv` tries to compile it from Rust and the runner cannot
  build it. Nothing in this package is at fault, and claiming 3.14 support would
  be false. The job turns green by itself when upstream ships the wheel.
- A real PostgreSQL, because several bugs here only appear against one.
- `.env.example` must match `DEFAULTS` — it is the file users copy.
- The package must boot with **no `.env`**, which is the embedding contract.
- No credential, private key or machine-absolute path may be committed.

### Known limitations

- **Data is 5 minutes behind.** This is a Delay key. Betfair withholds
  `publish_time`, so the delay cannot be measured from a timestamp, and polling
  faster does not reduce it. Only a Live key does.
- **Scores API** is not authorised on every app key; `SCORES_ENABLED` degrades
  quietly when it is not.
- **Feed 1 settles slowly** while `FIXTURES_INTERVAL_MINUTES` is 720, because
  the results sweep rides inside the fixtures job.
