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
        DB_PASSWORD="$(printf '%s' "$EXISTING" | sed -E 's|.*://[^:]+:([^@]*)@.*|\1|')"
        REUSE_PASSWORD=1
        echo "==> Reusing the password already in .env"
    fi
fi

ROLE_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" || true)"
# Only reset a password we do not already have a copy of, otherwise a re-run
# would silently break a working .env.
ALTER_PASSWORD=0
if [[ "$REUSE_PASSWORD" -eq 0 && "$ROLE_EXISTS" == "1" ]]; then
    ALTER_PASSWORD=1
fi

echo "==> Creating role and database"
sudo -u postgres psql -v ON_ERROR_STOP=1 \
    --username=postgres --dbname=postgres <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
        RAISE NOTICE 'created role ${DB_USER}';
    ELSIF ${ALTER_PASSWORD} THEN
        -- Existing role with no password recorded in .env: align them so the
        -- DATABASE_URL we are about to write actually works.
        EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', '${DB_USER}', '${DB_PASSWORD}');
        RAISE NOTICE 'reset password for existing role ${DB_USER}';
    ELSE
        RAISE NOTICE 'role ${DB_USER} already exists — password unchanged';
    END IF;
END
\$\$;
SQL

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    sudo -u postgres createdb --owner="${DB_USER}" "${DB_NAME}"
    echo "    created database ${DB_NAME}"
else
    echo "    database ${DB_NAME} already exists"
fi

DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"

echo "==> Writing DATABASE_URL to $ENV_FILE"
if [[ -f "$ENV_FILE" ]]; then
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
    chmod 600 "$ENV_FILE"
    echo "    created .env from .env.example — add your Betfair app key next"
fi

echo
echo "Done. Next:"
echo "  1. add BETFAIR_APP_KEY to $ENV_FILE (and username/password, or certs)"
echo "  2. uv run betfeed migrate"
echo "  3. uv run betfeed run fixtures   # first population"
echo "  4. uv run betfeed run odds"
echo "  5. uv run betfeed health"