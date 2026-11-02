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

SLOT="${POSTGRES_REPLICATION_SLOT:-openlens_replica_slot}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h postgres -p 5432 -U "$POSTGRES_USER" >/dev/null 2>&1; do
    sleep 2
  done
  # Reserve WAL on the primary BEFORE the backup: without a slot a
  # lagging standby dies with "requested WAL segment has already been
  # removed" the moment the primary recycles it (Nov 2026 incident).
  # The slot lives on the primary, so it survives replica rebuilds
  # and already-existing is fine — hence `|| true`.
  PGPASSWORD="$POSTGRES_PASSWORD" psql -h postgres -p 5432 \
    -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_create_physical_replication_slot('$SLOT', true)" || true
  PGPASSWORD="$POSTGRES_PASSWORD" pg_basebackup \
    -h postgres -p 5432 -U "$POSTGRES_USER" -S "$SLOT" \
    -D "$PGDATA" -Fp -Xs -P -R
  chown -R postgres:postgres "$PGDATA"
  chmod 0700 "$PGDATA"
fi

exec docker-entrypoint.sh postgres \
  -c hot_standby_feedback=on \
  -c shared_buffers=256MB \
  -c max_connections="${POSTGRES_MAX_CONNECTIONS:-200}" \
  -c primary_slot_name="$SLOT"
