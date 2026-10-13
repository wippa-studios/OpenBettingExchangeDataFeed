-- betfeed schema — current state + history.
--
-- Two layers, on purpose:
--   * CURRENT STATE  (fixture, market, selection, market_odds_current,
--     market_result, event_score) is upserted, so it always holds the freshest
--     view. This is feed 1, and feed 2 joined onto it.
--   * HISTORY        (market_snapshot, fixture_change) is append-only.
--     market_snapshot is RANGE-partitioned by captured_at so the 5-minute
--     odds history can be retained by dropping whole partitions.
--
-- Idempotent: safe to run on every boot.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── current state ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS betfair_event_type (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    market_count INT,
    seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fixture (
    fixture_id        TEXT PRIMARY KEY,           -- Betfair event id
    event_type_id     TEXT        NOT NULL,
    event_type_name   TEXT,
    sport_slug        TEXT        NOT NULL,       -- kebab: football, horse-racing
    competition_id    TEXT,
    competition_name  TEXT,
    event_name        TEXT        NOT NULL,
    country_code      TEXT,
    venue             TEXT,
    open_date         TIMESTAMPTZ,
    market_count      INT,
    is_active         BOOLEAN     NOT NULL DEFAULT TRUE,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fixture_active_idx ON fixture (is_active, open_date);
CREATE INDEX IF NOT EXISTS fixture_sport_idx  ON fixture (sport_slug);

CREATE TABLE IF NOT EXISTS market (
    market_id          TEXT PRIMARY KEY,
    fixture_id         TEXT REFERENCES fixture(fixture_id),
    market_type_code   TEXT        NOT NULL,
    market_name        TEXT        NOT NULL,
    market_start_time  TIMESTAMPTZ,
    market_time        TIMESTAMPTZ,              -- Betfair's own close time
    venue              TEXT,
    status             TEXT,                      -- OPEN / SUSPENDED / CLOSED / CLOSED_EVENT
    inplay             BOOLEAN,
    is_active          BOOLEAN     NOT NULL DEFAULT TRUE,
    is_resolved        BOOLEAN     NOT NULL DEFAULT FALSE,  -- we hold a result
    total_matched      NUMERIC(14,2),
    last_odds_at       TIMESTAMPTZ,              -- newest snapshot captured_at
    retired_at         TIMESTAMPTZ,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The odds job's scope query. Partial so retired markets never enter the plan.
CREATE INDEX IF NOT EXISTS market_poll_idx ON market (market_start_time)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS selection (
    -- selection_id is NOT unique on its own. Betfair REUSES selection ids
    -- across markets: selection_id 1 is "0 - 0" in the Correct Score market of
    -- every football match on the exchange. Keying on it alone made each
    -- market's selection 1 overwrite the previous market's.
    --
    -- Measured on a real collection with the single-column key: 11,096 markets
    -- produced only 3,034 markets' worth of selection rows, and 419 Correct
    -- Score markets produced 10. Not merely missing rows -- market_id was left
    -- pointing at whichever market wrote last, so the join to market returned
    -- the WRONG runner name for most selections.
    --
    -- A selection is identified by its market plus its id, which is exactly how
    -- the exchange addresses them.
    selection_id  BIGINT      NOT NULL,
    market_id     TEXT        NOT NULL REFERENCES market(market_id),
    runner_name   TEXT,
    sort_priority INT,
    handicap      NUMERIC(8,2),
    status        TEXT,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market_id, selection_id)
);
CREATE INDEX IF NOT EXISTS selection_market_idx ON selection (market_id, sort_priority);

-- Freshest odds per market. One row per market: the "live" half of feed 2.
CREATE TABLE IF NOT EXISTS market_odds_current (
    market_id     TEXT PRIMARY KEY REFERENCES market(market_id),
    captured_at   TIMESTAMPTZ NOT NULL,
    status        TEXT,
    inplay        BOOLEAN,
    bet_delay     INT,
    publish_time  TIMESTAMPTZ,
    total_matched NUMERIC(14,2),
    ladder        JSONB       NOT NULL
);

-- Settlement. Winner(s) typed for the common query, full runner detail in JSONB
-- (place markets have more than one winner; voided runners need their status).
CREATE TABLE IF NOT EXISTS market_result (
    market_id            TEXT PRIMARY KEY REFERENCES market(market_id),
    fixture_id           TEXT REFERENCES fixture(fixture_id),
    status               TEXT,                 -- SETTLED / CLOSED / CLOSED_EVENT
    voided               BOOLEAN     NOT NULL DEFAULT FALSE,
    winner_selection_ids BIGINT[]   NOT NULL DEFAULT '{}',
    market_start_time    TIMESTAMPTZ,
    observed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at           TIMESTAMPTZ,          -- BEST EFFORT, see docs
    source               TEXT        NOT NULL DEFAULT 'book',
    runners              JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS market_result_fixture_idx ON market_result (fixture_id);

-- Scores API: match score (2-1) where the app key is authorised for it.
-- values_raw is sport-specific and deliberately kept unparsed.
CREATE TABLE IF NOT EXISTS event_score (
    fixture_id   TEXT PRIMARY KEY REFERENCES fixture(fixture_id),
    event_status TEXT,
    home         TEXT,
    away         TEXT,
    home_score   TEXT,
    away_score   TEXT,
    values_raw   JSONB,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── history ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS market_snapshot (
    captured_at  TIMESTAMPTZ NOT NULL,
    market_id    TEXT        NOT NULL,
    fixture_id   TEXT,
    status       TEXT,
    inplay       BOOLEAN,
    publish_time TIMESTAMPTZ,
    total_matched NUMERIC(14,2),
    ladder       JSONB       NOT NULL,
    PRIMARY KEY (captured_at, market_id)
) PARTITION BY RANGE (captured_at);
CREATE INDEX IF NOT EXISTS market_snapshot_market_idx ON market_snapshot (market_id, captured_at DESC);

-- What feed 1 changed, and when. Cheap at a 12-hour cadence.
CREATE TABLE IF NOT EXISTS fixture_change (
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    job        TEXT        NOT NULL,
    entity     TEXT        NOT NULL,   -- fixture | market | selection
    entity_id  TEXT        NOT NULL,
    field      TEXT,
    old_value  TEXT,
    new_value  TEXT
);
CREATE INDEX IF NOT EXISTS fixture_change_entity_idx ON fixture_change (entity, entity_id, changed_at DESC);

-- One row per job run. The freshness alarm reads this table.
CREATE TABLE IF NOT EXISTS feed_run (
    run_id       BIGSERIAL PRIMARY KEY,
    job          TEXT        NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    ok           BOOLEAN,
    items_ok     INT         NOT NULL DEFAULT 0,
    items_err    INT         NOT NULL DEFAULT 0,
    duration_ms  INT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS feed_run_job_idx ON feed_run (job, started_at DESC);

-- ── views ──────────────────────────────────────────────────────────────────

-- Feed 1: fixtures and their markets and runners. No odds column exists in
-- this view, so "no odds" is a property of the schema, not a filter.
CREATE OR REPLACE VIEW v_feed1 AS
SELECT f.fixture_id,
       f.event_type_id,
       f.event_type_name,
       f.sport_slug,
       f.event_name,
       f.competition_id,
       f.competition_name,
       f.country_code,
       f.venue          AS fixture_venue,
       f.open_date,
       f.is_active      AS fixture_active,
       m.market_id,
       m.market_type_code,
       m.market_name,
       m.market_start_time,
       m.venue          AS market_venue,
       m.status         AS market_status,
       m.is_active      AS market_active,
       m.is_resolved,
       m.total_matched,
       s.selection_id,
       s.runner_name,
       s.sort_priority,
       s.handicap,
       s.status         AS selection_status
FROM fixture f
JOIN market m    ON m.fixture_id = f.fixture_id
JOIN selection s ON s.market_id  = m.market_id
-- Resolved markets ARE published, and this is the one line that makes results
-- reachable at all.
--
-- write_result() marks a settled market `is_resolved = TRUE, is_active = FALSE`
-- (and its selections likewise) so the odds job stops polling it — that part is
-- right. But filtering the view on is_active alone then REMOVES the settled
-- market from both feeds, so a consumer can never read the result it just
-- collected: measured on a real run, 6 markets in market_result and 0 visible
-- in v_feed2. Auto-settlement silently never fires.
--
-- Bounded to RESOLVED_RETENTION_DAYS so a long-running collector does not
-- accumulate every match it has ever seen. A venue settles within days, not
-- weeks, and seven is generous.
WHERE f.is_active
  AND (m.is_active OR (m.is_resolved AND m.retired_at > now() - interval '7 days'))
  AND (s.is_active OR m.is_resolved);

-- Feed 2: feed 1 plus the newest odds and the result, if we have one.
-- Prices come out of the ladder JSONB with a LATERAL, so the read is one
-- indexed lookup per selection rather than a join against every snapshot.
-- Both LATERALs below guard the TYPE of the payload, not just the presence of
-- the key. COALESCE catches a missing 'selections' but not a JSON null (jsonb
-- 'null' is non-NULL to COALESCE), not an object and not a scalar — and the
-- ::BIGINT cast throws on any non-numeric selection_id. There is no per-row
-- isolation on the read side, so one malformed row made every SELECT against
-- v_feed2 raise InvalidParameterValue / InvalidTextRepresentation and took
-- BOTH feeds down for every reader.
CREATE OR REPLACE VIEW v_feed2 AS
SELECT v.*,
       oc.captured_at                                        AS odds_captured_at,
       EXTRACT(EPOCH FROM (now() - oc.captured_at))          AS price_age_seconds,
       oc.publish_time,
       oc.inplay,
       oc.bet_delay,
       oc.ladder,
       bsel.b                                             AS odds,
       mr.status                                           AS result_status,
       mr.voided                                           AS result_voided,
       mr.winner_selection_ids,
       mr.settled_at                                       AS result_settled_at,
       mr.observed_at                                      AS result_observed_at,
       rsel.status                                         AS result_selection_status,
       es.home                                             AS home,
       es.away                                             AS away,
       es.home_score,
       es.away_score,
       es.captured_at                                      AS score_captured_at
FROM v_feed1 v
LEFT JOIN market_odds_current oc ON oc.market_id = v.market_id
LEFT JOIN LATERAL (
        SELECT x AS b
          FROM jsonb_array_elements(
                   CASE WHEN jsonb_typeof(oc.ladder -> 'selections') = 'array'
                        THEN oc.ladder -> 'selections'
                        ELSE '[]'::jsonb END) AS x
         WHERE (x ->> 'selection_id') ~ '^-?\d+$'
           AND (x ->> 'selection_id')::BIGINT = v.selection_id
     ) bsel ON TRUE
LEFT JOIN market_result mr ON mr.market_id = v.market_id
LEFT JOIN LATERAL (
        SELECT x ->> 'status' AS status
        FROM jsonb_array_elements(
                   CASE WHEN jsonb_typeof(mr.runners) = 'array'
                        THEN mr.runners
                        ELSE '[]'::jsonb END) AS x
         WHERE (x ->> 'selection_id') ~ '^-?\d+$'
           AND (x ->> 'selection_id')::BIGINT = v.selection_id
     ) rsel ON TRUE
LEFT JOIN event_score es ON es.fixture_id = v.fixture_id;

-- The odds job's plan. A function rather than a view because the grace window
-- and horizon are configuration.
--
-- The grace window is the BACKSTOP, not the exit condition. A market leaves
-- scope when Betfair says it is over — a terminal status — and the grace is
-- the minimum wait before we act on that. Deciding on the clock alone meant
-- feed 2 stopped collecting in-play prices 30 minutes after kickoff (the
-- default sports are soccer and tennis, and a soccer match runs about two
-- hours), and the odds job could then never observe the settlement.
CREATE OR REPLACE FUNCTION poll_scope(grace_minutes INT, horizon_hours INT)
RETURNS TABLE (
    market_id         TEXT,
    fixture_id        TEXT,
    market_start_time TIMESTAMPTZ,
    starting          BOOLEAN
) AS $$
    SELECT m.market_id,
           m.fixture_id,
           m.market_start_time,
           (m.market_start_time <= now())
    FROM market m
    WHERE m.is_active
      AND m.market_start_time IS NOT NULL
      AND m.market_start_time <  now() + make_interval(hours => horizon_hours)
      AND (
            -- not started, or started within the grace window
            m.market_start_time > now() - make_interval(mins => grace_minutes)
         OR -- started and still running: in scope until Betfair calls it
            (m.market_start_time <= now()
             AND upper(coalesce(m.status, ''))
                 <> ALL (ARRAY['CLOSED', 'CLOSED_EVENT', 'SETTLED', 'VOIDED']))
      )
    ORDER BY m.market_start_time
$$ LANGUAGE sql STABLE;

-- Markets that are active, already past their start time, and have no result
-- yet. Feed 1 sweeps these so a finished event still gets a result when the
-- 5-minute job was down when it settled.
CREATE OR REPLACE FUNCTION unresolved_scope(max_age_hours INT)
RETURNS TABLE (market_id TEXT, fixture_id TEXT, market_start_time TIMESTAMPTZ)
AS $$
    SELECT m.market_id, m.fixture_id, m.market_start_time
    FROM market m
    WHERE m.is_active
      AND m.is_resolved = FALSE
      AND m.market_start_time IS NOT NULL
      AND m.market_start_time <  now()
      AND m.market_start_time >  now() - make_interval(hours => max_age_hours)
    ORDER BY m.market_start_time
$$ LANGUAGE sql STABLE;