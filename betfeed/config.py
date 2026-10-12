"""Configuration: read env, validate hard, never guess.

A misconfigured feed should die at boot with a message naming the variable, not
discover it three hours later when a customer reads stale prices. Every value
here is validated at import time; a bad value exits the process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Betfair event type ids we will not walk by accident.
# MarketProjection values Betfair accepts on listMarketCatalogue. The two
# Live-key-only ones are listed because they are valid syntax — asking for them
# on a Delay key fails the request, not the config.
KNOWN_MARKET_PROJECTIONS = (
    "RUNNER_DESCRIPTION,EVENT,EVENT_TYPE,COMPETITION,MARKET_START_TIME,"
    "MARKET_DESCRIPTION,VENUE"
)

KNOWN_EVENT_TYPES = {
    "1": "Soccer",
    "4": "Cricket",
    "7": "Horse Racing",
    "8": "Tennis",
    "10": "Golf",
    "12": "Motor Racing",
    "13": "Rugby",
    "15": "Basketball",
    "17": "American Football",
    "20": "Boxing",
    "24": "Darts",
    "26": "Snooker",
    "30": "Esports",
    "4339": "Racing (Aus)",
}

DEFAULTS: dict[str, str] = {
    "DATABASE_URL": "postgresql://betfair@127.0.0.1:5432/betfeed",
    # password = username/password non-interactive login, certs = client certs,
    # session_token = a cached token from a previous interactive login.
    "BETFAIR_LOGIN_MODE": "password",
    "BETFAIR_LOCALE": "en",
    # Which sports feed 1 walks. Horse racing is deliberately opt-in: it is by
    # far the highest market count and will dominate every table.
    "EVENT_TYPE_IDS": "1,8",
    # Empty = every market type for the sports above. Set e.g. "MATCH_ODDS" to
    # halve the odds job's request volume.
    "MARKET_TYPE_CODES": "",
    # The two cadences. 720m = the 12h feed-1 refresh, 5m = the feed-2 refresh.
    "FIXTURES_INTERVAL_MINUTES": "720",
    "ODDS_INTERVAL_MINUTES": "5",
    # Feed 2 stops polling markets that have started; this is the window after
    # start in which we still poll to capture the settle.
    "ODDS_HORIZON_HOURS": "168",
    "SETTLE_GRACE_MINUTES": "30",
    # Betfair caps market data at 200 weight points per request and 100 event ids
    # per catalogue filter. These are the chunk sizes we aim for; on
    # TOO_MUCH_DATA the client bisects instead of failing.
    "BOOK_CHUNK_SIZE": "40",
    "CATALOGUE_CHUNK_EVENTS": "100",
    # 200, not 1000. Measured on a DELAY app key: at 500 Betfair answers
    # ANGX-0001 / TOO_MUCH_DATA; 200 is accepted. Event-chunk size makes no
    # difference (measured at 100, 50 and 25), only the result count does.
    #
    # This has to be the DEFAULT, not a value someone put in their .env: an
    # embeddable package has to work on first boot with nothing configured, and
    # Betfair hands out DELAY keys by default (two keys per registration, the
    # Delay one active and the Live one inactive). A default that only works on a
    # paid key is a default that breaks the first person to try it.
    "CATALOGUE_MAX_RESULTS": "200",
    # DELAY-SAFE by default. VENUE and EVENT_RUNNER_DESCRIPTION are Live-key-only
    # and fail a Delay key's request outright with DSC-0018; everything else,
    # including MARKET_DESCRIPTION, works. Override only with a Live key.
    "CATALOGUE_PROJECTION": "RUNNER_DESCRIPTION,MARKET_DESCRIPTION,EVENT,EVENT_TYPE,COMPETITION,MARKET_START_TIME",
    "LADDER_LEVELS": "3",
    "INCLUDE_TRADED_VOLUME": "false",
    "SNAPSHOT_RETENTION_DAYS": "30",
    "RETENTION_CHECK_EVERY_MINUTES": "360",
    # Results. Settlement comes free with any market book; these govern the
    # backfill sweep that catches events finished while nothing was watching.
    "RESULTS_BACKFILL_HOURS": "48",
    "RESULTS_BACKFILL_MAX_MARKETS": "2000",
    # Match scores (2-1) need an app key authorised for Betfair's Scores API.
    # When it is not authorised this is a no-op that logs why, never a crash.
    "SCORES_ENABLED": "true",
    "LOG_LEVEL": "INFO",
}

_INT_RANGES = {
    "FIXTURES_INTERVAL_MINUTES": (1, 10080),
    "ODDS_INTERVAL_MINUTES": (1, 1440),
    "ODDS_HORIZON_HOURS": (1, 8760),
    "SETTLE_GRACE_MINUTES": (0, 720),
    "BOOK_CHUNK_SIZE": (1, 200),
    "CATALOGUE_CHUNK_EVENTS": (1, 100),
    "CATALOGUE_MAX_RESULTS": (1, 1000),
    "CATALOGUE_PROJECTION": (
        # What a Delay key accepts, then the Live-only ones. Verified field by
        # field against the live API: MARKET_DESCRIPTION is fine on a Delay key
        # (and is where market_type_code comes from), while VENUE and
        # EVENT_RUNNER_DESCRIPTION both fail the whole call with DSC-0018.
        "RUNNER_DESCRIPTION,MARKET_DESCRIPTION,EVENT,EVENT_TYPE,COMPETITION,"
        "MARKET_START_TIME,VENUE,EVENT_RUNNER_DESCRIPTION",
    ),
    "LADDER_LEVELS": (1, 10),
    "SNAPSHOT_RETENTION_DAYS": (1, 3650),
    "RETENTION_CHECK_EVERY_MINUTES": (5, 10080),
    "RESULTS_BACKFILL_HOURS": (1, 720),
    "RESULTS_BACKFILL_MAX_MARKETS": (1, 100000),
}


class ConfigError(Exception):
    """A configuration value is missing or nonsensical."""


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Minimal .env reader. No dependency, no export, no interpolation."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t or t.startswith("#") or "=" not in t:
            continue
        key, _, value = t.partition("=")
        key = key.strip()
        if not re_key(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def re_key(key: str) -> bool:
    return bool(key) and all(c.isalnum() or c == "_" for c in key)


def _int(name: str, raw: str, lo: int, hi: int) -> int:
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {n}")
    return n


def _bool(name: str, raw: str) -> bool:
    v = str(raw).strip().lower()
    if v in {"true", "1", "yes", "on"}:
        return True
    if v in {"false", "0", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _csv(name: str, raw: str) -> list[str]:
    return [p.strip() for p in str(raw).split(",") if p.strip()]


class Config:
    """Validated configuration. Construct via :func:`load_config`."""

    def __init__(self, raw: dict[str, str]):
        self.raw = dict(raw)

        # Merge the documented defaults FIRST, and read everything from `merged`.
        #
        # This used to happen ~30 lines further down, so the four settings above
        # it read `raw` and never saw a default. The visible consequence was that
        # a fresh checkout with NO .env could not boot at all:
        #     ConfigError: EVENT_TYPE_IDS must list at least one Betfair event type id
        # ...even though DEFAULTS says "1,8". It only ever worked because a .env
        # happened to exist with the key spelled out.
        #
        # That matters beyond tidiness: the whole point of this package being
        # embeddable is that a host application supplies the configuration —
        # OpenBettingExchange passes credentials in as environment variables and
        # never writes a .env at all. A config that cannot boot without a file it
        # is not given is not embeddable, however correct it looks on a
        # developer's machine where the file has always existed.
        merged = {**DEFAULTS, **raw}

        self.database_url = self._require_url("DATABASE_URL")
        self.locale = merged.get("BETFAIR_LOCALE", "en").strip() or "en"
        self.login_mode = merged.get("BETFAIR_LOGIN_MODE", "password").strip().lower()
        if self.login_mode not in {"password", "certs", "session_token"}:
            raise ConfigError(
                f"BETFAIR_LOGIN_MODE must be password|certs|session_token, got {self.login_mode!r}"
            )

        self.username = raw.get("BETFAIR_USERNAME", "").strip()
        self.password = raw.get("BETFAIR_PASSWORD", "").strip()
        self.app_key = raw.get("BETFAIR_APP_KEY", "").strip()
        self.certs_path = raw.get("BETFAIR_CERTS_PATH", "").strip()
        self.session_token = raw.get("BETFAIR_SESSION_TOKEN", "").strip()

        event_types = _csv("EVENT_TYPE_IDS", merged.get("EVENT_TYPE_IDS", ""))
        if not event_types:
            raise ConfigError("EVENT_TYPE_IDS must list at least one Betfair event type id")
        unknown = [e for e in event_types if e not in KNOWN_EVENT_TYPES]
        if unknown:
            known = ", ".join(f"{k}={v}" for k, v in sorted(KNOWN_EVENT_TYPES.items()))
            raise ConfigError(
                f"EVENT_TYPE_IDS has unknown id(s) {unknown}. Known ids: {known}"
            )
        self.event_type_ids = event_types
        self.event_type_names = {e: KNOWN_EVENT_TYPES[e] for e in event_types}
        self.market_type_codes = _csv("MARKET_TYPE_CODES", merged.get("MARKET_TYPE_CODES", ""))

        self.fixtures_interval_minutes = _int(
            "FIXTURES_INTERVAL_MINUTES",
            merged["FIXTURES_INTERVAL_MINUTES"],
            *_INT_RANGES["FIXTURES_INTERVAL_MINUTES"],
        )
        self.odds_interval_minutes = _int(
            "ODDS_INTERVAL_MINUTES",
            merged["ODDS_INTERVAL_MINUTES"],
            *_INT_RANGES["ODDS_INTERVAL_MINUTES"],
        )
        self.odds_horizon_hours = _int(
            "ODDS_HORIZON_HOURS", merged["ODDS_HORIZON_HOURS"], *_INT_RANGES["ODDS_HORIZON_HOURS"]
        )
        self.settle_grace_minutes = _int(
            "SETTLE_GRACE_MINUTES",
            merged["SETTLE_GRACE_MINUTES"],
            *_INT_RANGES["SETTLE_GRACE_MINUTES"],
        )
        self.book_chunk_size = _int(
            "BOOK_CHUNK_SIZE", merged["BOOK_CHUNK_SIZE"], *_INT_RANGES["BOOK_CHUNK_SIZE"]
        )
        # Comma-separated projection list, validated against what Betfair
        # actually accepts. An unknown field is rejected at boot with the
        # permitted set named, rather than becoming a DSC-0018 on the first
        # catalogue call an hour later.
        raw_projection = merged.get("CATALOGUE_PROJECTION", "").strip()
        if not raw_projection:
            raise ConfigError(
                "CATALOGUE_PROJECTION must name at least one Betfair MarketProjection"
            )
        known_projection = {p.strip() for p in KNOWN_MARKET_PROJECTIONS.split(",") if p.strip()}
        projection = [p.strip() for p in raw_projection.split(",") if p.strip()]
        unknown_projection = [p for p in projection if p not in known_projection]
        if unknown_projection:
            raise ConfigError(
                f"CATALOGUE_PROJECTION has unknown value(s) {unknown_projection}. "
                f"Known values: {', '.join(sorted(known_projection))}"
            )
        self.catalogue_projection = projection

        self.catalogue_chunk_events = _int(
            "CATALOGUE_CHUNK_EVENTS",
            merged["CATALOGUE_CHUNK_EVENTS"],
            *_INT_RANGES["CATALOGUE_CHUNK_EVENTS"],
        )
        self.catalogue_max_results = _int(
            "CATALOGUE_MAX_RESULTS",
            merged["CATALOGUE_MAX_RESULTS"],
            *_INT_RANGES["CATALOGUE_MAX_RESULTS"],
        )
        self.ladder_levels = _int(
            "LADDER_LEVELS", merged["LADDER_LEVELS"], *_INT_RANGES["LADDER_LEVELS"]
        )
        self.include_traded_volume = _bool(
            "INCLUDE_TRADED_VOLUME", merged["INCLUDE_TRADED_VOLUME"]
        )
        self.results_backfill_hours = _int(
            "RESULTS_BACKFILL_HOURS",
            merged["RESULTS_BACKFILL_HOURS"],
            *_INT_RANGES["RESULTS_BACKFILL_HOURS"],
        )
        self.results_backfill_max_markets = _int(
            "RESULTS_BACKFILL_MAX_MARKETS",
            merged["RESULTS_BACKFILL_MAX_MARKETS"],
            *_INT_RANGES["RESULTS_BACKFILL_MAX_MARKETS"],
        )
        self.scores_enabled = _bool("SCORES_ENABLED", merged["SCORES_ENABLED"])
        self.snapshot_retention_days = _int(
            "SNAPSHOT_RETENTION_DAYS",
            merged["SNAPSHOT_RETENTION_DAYS"],
            *_INT_RANGES["SNAPSHOT_RETENTION_DAYS"],
        )
        self.retention_check_every_minutes = _int(
            "RETENTION_CHECK_EVERY_MINUTES",
            merged["RETENTION_CHECK_EVERY_MINUTES"],
            *_INT_RANGES["RETENTION_CHECK_EVERY_MINUTES"],
        )
        self.log_level = merged["LOG_LEVEL"].strip().upper() or "INFO"

    def _require_url(self, name: str) -> str:
        value = (self.raw.get(name) or "").strip()
        if not value:
            raise ConfigError(f"{name} is required (e.g. postgresql://user:pw@127.0.0.1:5432/betfeed)")
        if not value.startswith(("postgres://", "postgresql://")):
            raise ConfigError(f"{name} must start with postgresql:// (got {value!r})")
        return value

    def describe(self) -> str:
        # Never prints credentials.
        return (
            f"sports={sorted(self.event_type_names.values())} "
            f"market_types={self.market_type_codes or 'all'} "
            f"feed1_every={self.fixtures_interval_minutes}m feed2_every={self.odds_interval_minutes}m "
            f"login={self.login_mode} ladder={self.ladder_levels}"
        )


def load_config(env_file: str | Path | None = None, overrides: dict | None = None) -> Config:
    here = Path(__file__).resolve().parent.parent
    path = Path(env_file) if env_file else here / ".env"
    raw = {**parse_env_file(path), **{k: v for k, v in os.environ.items() if v != ""}}
    if overrides:
        raw.update({k: v for k, v in overrides.items() if v is not None})
    return Config(raw)


def load_or_exit(env_file: str | Path | None = None) -> Config:
    """Load config, aborting the process with a readable reason on bad input."""
    try:
        cfg = load_config(env_file)
    except ConfigError as e:
        print(f"[config] aborting: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    missing = validate_credentials(cfg)
    if missing:
        print(f"[config] aborting: missing {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    return cfg


def validate_credentials(cfg: Config) -> list[str]:
    """Which credential pieces the chosen login mode still needs."""
    if not cfg.app_key:
        return ["BETFAIR_APP_KEY"]
    if cfg.login_mode == "password":
        return [n for n, v in (("BETFAIR_USERNAME", cfg.username), ("BETFAIR_PASSWORD", cfg.password)) if not v]
    if cfg.login_mode == "certs":
        return [] if cfg.certs_path else ["BETFAIR_CERTS_PATH"]
    return [] if cfg.session_token else ["BETFAIR_SESSION_TOKEN"]