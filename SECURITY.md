# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security problem**, especially anything that
involves credentials.

Report privately via [GitHub Security Advisories](https://github.com/wippa-studios/OpenBettingExchangeDataFeed/security/advisories/new)
("Report a vulnerability").

## What this package handles

This package authenticates to Betfair with your account credentials and holds a
database of market data. It does not handle money, player balances or personal
data. The assets worth protecting are:

| Asset | Why |
|---|---|
| `BETFAIR_APP_KEY`, `BETFAIR_USERNAME`, `BETFAIR_PASSWORD` | Full API access to your Betfair account |
| `BETFAIR_CERTS_PATH` contents | Your private key |
| `BETFAIR_SESSION_TOKEN` | A live session token for your account |
| `DATABASE_URL` | Usually embeds a database password |

## Handling credentials safely

**`.env` must be `600` and owned by you.**

```bash
chmod 600 .env
```

`scripts/install_postgres.sh` runs under `sudo`, so anything it creates is
root-owned. It now chowns `.env` to the invoking user and warns loudly if that
did not work.

**`.env` and `certs/` are gitignored.** Confirm before you commit anything:

```bash
git check-ignore -v .env certs/client.crt
```

**Prefer environment variables when embedding.** A host application can pass
credentials in the process environment and never write a file at all. Environment
variables take precedence over `.env`.

**Never log a credential.** If you add logging, do not log the config dict. There
is a `cfg.describe()` helper for exactly this reason — use it.

## Rotating a leaked credential

1. **Betfair password** — change it in your Betfair account settings, then update
   `.env` (or the host application's credential store).
2. **App key** — create a new key via the Accounts API Demo Tool
   (`createDeveloperAppKeys`), update the configuration, then delete the old key.
3. **Database password** — `ALTER ROLE betfair PASSWORD '...'` and update
   `DATABASE_URL`.
4. **Private key** — revoke and re-register the certificate with Betfair.

## Supported versions

| Version | Supported |
|---|---|
| `main` | ✅ |
| anything older | ❌ |

This package is pre-1.0 and has no release tags. Fixes land on `main`.
