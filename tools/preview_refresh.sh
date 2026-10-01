#!/bin/bash
# Refresh the preview's writable copy of the record (PREVIEW_DB=copy).
#
# Dumps the live record -- a read-only operation -- and restores it over the
# preview database, replacing whatever previews wrote there. ~565 MB, a
# couple of minutes. Needs POSTGRES_PASSWORD for the live database.
#
#   POSTGRES_PASSWORD=... tools/preview_refresh.sh
set -euo pipefail

LIVE_HOST=${PREVIEW_LIVE_HOST:-10.0.0.2}
COPY_HOST=${PREVIEW_DB_HOST:-magenta-staging-pg}
COPY_PASSWORD=${PREVIEW_DB_PASSWORD:-staging}
DB=magenta_memory
DUMP=$(mktemp --suffix=.dump)
trap 'rm -f "$DUMP"' EXIT

echo "Dumping the live record from $LIVE_HOST..."
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h "$LIVE_HOST" -U magent -d "$DB" -Fc -f "$DUMP"

echo "Replacing the copy on $COPY_HOST..."
export PGPASSWORD="$COPY_PASSWORD"
psql -h "$COPY_HOST" -U magent -d postgres -qc "DROP DATABASE IF EXISTS $DB WITH (FORCE)"
psql -h "$COPY_HOST" -U magent -d postgres -qc "CREATE DATABASE $DB"
# A newer pg_restore may warn about settings the older server lacks
# (e.g. transaction_timeout); those warnings are harmless.
pg_restore -h "$COPY_HOST" -U magent -d "$DB" --no-owner --no-privileges -j 4 "$DUMP" || true

COUNT=$(psql -h "$COPY_HOST" -U magent -d "$DB" -Atc "select count(*) from conversations_message")
echo "Copy ready: $COUNT messages, as of $(date -u +%Y-%m-%dT%H:%MZ)."
