#!/usr/bin/env bash
# postgres-replica entrypoint: on first boot, basebackup from the
# primary and run as a hot-standby streaming replica. Later boots skip
# straight to postgres (data persists in the replica-data volume).
#
# Notes:
# - Runs as root (overridden compose command); data files are chowned
#   to postgres after the backup so the server can start.
# - hot_standby_feedback=on keeps long search queries from being
#   cancelled by recovery conflicts on the primary.
# - max_connections must be >= the primary's (WAL replay enforces it).
set -euo pipefail

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h postgres -p 5432 -U "$POSTGRES_USER" >/dev/null 2>&1; do
    sleep 2
  done
  PGPASSWORD="$POSTGRES_PASSWORD" pg_basebackup \
    -h postgres -p 5432 -U "$POSTGRES_USER" \
    -D "$PGDATA" -Fp -Xs -P -R
  chown -R postgres:postgres "$PGDATA"
  chmod 0700 "$PGDATA"
fi

exec docker-entrypoint.sh postgres \
  -c hot_standby_feedback=on \
  -c shared_buffers=256MB \
  -c max_connections="${POSTGRES_MAX_CONNECTIONS:-200}"
