#!/usr/bin/env bash
# Install and configure PostgreSQL without Docker.
#
# Run this yourself with sudo — it needs root to install packages:
#
#     sudo ./scripts/install_postgres.sh
#
# What it does:
#   1. apt install postgresql (the only step that needs root)
#   2. generate a random password for a `betfair` role
#   3. create the `betfeed` database owned by it
#   4. write DATABASE_URL into the project's .env (or append it)
#
# Safe to re-run: it will not reset the password of an existing role, and the
# database is only created if missing. To rotate the password, delete the role
# first or edit .env by hand.

set -euo pipefail

# A reused password comes out of an existing DATABASE_URL still percent-encoded,
# and has to go back in percent-encoded (for the URL) and decoded (for SQL).
# Generated passwords are alnum-only, so this only matters on the reuse path —
# but that is the path the script's own documentation tells people to take.
#
# Unreserved per RFC 3986: alphanumerics plus - . _ ~
urlencode() {
    local s="$1" out="" i c
    for (( i = 0; i < ${#s}; i++ )); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9._~-]) out+="$c" ;;
            # Escape EVERY byte, not just the first: od prints one hex byte per
            # byte, so a multi-byte character is two bytes and needs two %XX.
            *) out+="$(printf '%s' "$c" | od -An -tx1 | tr -d ' \n' | sed 's/\(..\)/%\1/g')" ;;
        esac
    done
    printf '%s' "$out"
}

urldecode() {
    local rest="$1" out="" chunk
    while [[ "$rest" == *%* ]]; do
        chunk="${rest:0:3}"
        if [[ "$chunk" =~ ^%[0-9A-Fa-f][0-9A-Fa-f]$ ]]; then
            out+="$(printf "\\x${chunk:1}")"
            rest="${rest:3}"
        else
            # Not a valid escape — keep the character rather than eat it.
            out+="${chunk:0:1}"
            rest="${rest:1}"
        fi
    done
    printf '%s' "$out$rest"
}

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
DB_NAME="${DB_NAME:-betfeed}"
DB_USER="${DB_USER:-betfair}"

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root. Run: sudo $0" >&2
    exit 1
fi

echo "==> Installing PostgreSQL"
export DEBIAN_FRONTEND=noninteractive
if ! command -v psql >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq postgresql postgresql-contrib
else
    echo "    psql already present, skipping install"
fi

echo "==> Starting PostgreSQL"
if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now postgresql
else
    service postgresql start
fi

# Wait for the cluster to accept connections rather than assuming it is up.
for i in $(seq 1 30); do
    if sudo -u postgres psql -c 'SELECT 1' >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

DB_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
REUSE_PASSWORD=0
if [[ -f "$ENV_FILE" ]]; then
    EXISTING="$(grep -E '^DATABASE_URL=' "$ENV_FILE" || true)"
    if [[ -n "$EXISTING" ]]; then
        # The password is whatever sits between the first ':' and the '@' that
        # ends the authority, still percent-encoded exactly as the URL had it.
        ENCODED="$(printf '%s' "${EXISTING#DATABASE_URL=}" \
            | sed -E 's|^[a-zA-Z0-9+.-]+://[^:/?#]*:([^@]*)@.*$|\1|')"
        DB_PASSWORD="$(urldecode "$ENCODED")"
        REUSE_PASSWORD=1
        echo "==> Reusing the password already in .env"
    fi
fi

ROLE_EXISTS="$(sudo -u postgres psql -tAc \
    -v db_user="$DB_USER" \
    "SELECT 1 FROM pg_roles WHERE rolname = :'db_user'" || true)"
# Only reset a password we do not already have a copy of, otherwise a re-run
# would silently break a working .env.
ALTER_PASSWORD=0
if [[ "$REUSE_PASSWORD" -eq 0 && "$ROLE_EXISTS" == "1" ]]; then
    ALTER_PASSWORD=1
fi

echo "==> Creating role and database"
# Every value reaches Postgres as DATA, never as string interpolation.
#
#   CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
#
# was plain interpolation inside SQL running as the postgres superuser, and a
# password is HAND-CHOSEN on the documented rotation path (delete the role, keep
# .env): an apostrophe was a syntax error at best, crafted content was statement
# injection as superuser.
#
# Two layers matter, and both were needed:
#   1. format() with %I/%L inside the DO block — but on its own that is NOT
#      enough, because the password was still pasted into the SQL TEXT as a
#      string literal, so a quote broke it just the same.
#   2. psql variables (:'var') — but those are not substituted inside a
#      dollar-quoted body, which is exactly where they were needed.
#
# So: psql variables set session GUCs, and the DO block reads them back with
# current_setting(). Nothing operator-supplied is ever part of the SQL text.
sudo -u postgres psql -v ON_ERROR_STOP=1 \
    -v db_user="$DB_USER" -v db_password="$DB_PASSWORD" \
    -v alter_password="$ALTER_PASSWORD" \
    --username=postgres --dbname=postgres <<'SQL'
SET betfeed.db_user = :'db_user';
SET betfeed.db_password = :'db_password';
SET betfeed.alter_password = :'alter_password';
DO $$
DECLARE
    v_user     TEXT := current_setting('betfeed.db_user');
    v_password TEXT := current_setting('betfeed.db_password');
    v_alter    BOOLEAN := current_setting('betfeed.alter_password')::int <> 0;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_user) THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', v_user, v_password);
        RAISE NOTICE 'created role %', v_user;
    ELSIF v_alter THEN
        -- Existing role with no password recorded in .env: align them so the
        -- DATABASE_URL we are about to write actually works.
        EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', v_user, v_password);
        RAISE NOTICE 'reset password for existing role %', v_user;
    ELSE
        RAISE NOTICE 'role % already exists — password unchanged', v_user;
    END IF;
END
$$;
SQL

if ! sudo -u postgres psql -tAc -v db_name="$DB_NAME" \
    "SELECT 1 FROM pg_database WHERE datname = :'db_name'" | grep -q 1; then
    sudo -u postgres createdb --owner="${DB_USER}" "${DB_NAME}"
    echo "    created database ${DB_NAME}"
else
    echo "    database ${DB_NAME} already exists"
fi

DATABASE_URL="postgresql://${DB_USER}:$(urlencode "$DB_PASSWORD")@127.0.0.1:5432/${DB_NAME}"

echo "==> Writing DATABASE_URL to $ENV_FILE"

# The file must be OWNED BY THE OPERATOR, not by root.
#
# This script has to run under sudo (it installs packages and writes to
# /var), so anything it creates is root-owned by default. It then chmods .env
# to 600 — which locks the operator OUT of the very file the next step tells
# them to edit, and out of every later `betfeed` run, since the collector reads
# .env as the invoking user. Observed on a real run: `PermissionError: .env`
# from load_config, with the credentials already present and correct.
#
# So: remember who ran sudo, and hand the file back to them. 600 stays — the
# file holds a Betfair password and a database password.
INVOKING_USER="${SUDO_USER:-${USER:-root}}"
INVOKING_GROUP="$(id -gn "$INVOKING_USER" 2>/dev/null || echo "$INVOKING_USER")"

if [[ -f "$ENV_FILE" ]]; then
    if [[ -w "$ENV_FILE" || ! -O "$ENV_FILE" ]]; then
        chown "$INVOKING_USER:$INVOKING_GROUP" "$ENV_FILE" 2>/dev/null || true
    fi
    chmod 600 "$ENV_FILE"
    if grep -qE '^DATABASE_URL=' "$ENV_FILE"; then
        # sed -i| is GNU-only; the temp-file form works everywhere.
        tmp="$(mktemp)"
        grep -vE '^DATABASE_URL=' "$ENV_FILE" > "$tmp"
        printf 'DATABASE_URL=%s\n' "$DATABASE_URL" >> "$tmp"
        cat "$tmp" > "$ENV_FILE"
        rm -f "$tmp"
    else
        printf '\nDATABASE_URL=%s\n' "$DATABASE_URL" >> "$ENV_FILE"
    fi
else
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    printf '\nDATABASE_URL=%s\n' "$DATABASE_URL" >> "$ENV_FILE"
    chown "$INVOKING_USER:$INVOKING_GROUP" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "    created .env from .env.example (owned by ${INVOKING_USER}) — add your Betfair app key next"
fi

# A 600 file is only useful if the operator can actually open it. If the chown
# did not take (unusual: NFS, a shared filesystem, a container), say so loudly
# rather than leaving a PermissionError to be discovered later.
if ! sudo -u "$INVOKING_USER" test -r "$ENV_FILE" 2>/dev/null; then
    echo "!! WARNING: $ENV_FILE is not readable by ${INVOKING_USER}." >&2
    echo "!! Run:  sudo chown ${INVOKING_USER}:${INVOKING_GROUP} $ENV_FILE" >&2
fi

echo
echo "Done. Next:"
echo "  1. add BETFAIR_APP_KEY to $ENV_FILE (and username/password, or certs)"
echo "  2. uv run betfeed migrate"
echo "  3. uv run betfeed run fixtures   # first population"
echo "  4. uv run betfeed run odds"
echo "  5. uv run betfeed health"