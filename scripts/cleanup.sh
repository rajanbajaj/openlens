#!/usr/bin/env bash
# Wipe indexed documents + crawler state for a fresh crawl.
#
# Runs ON THE RELEASE SERVER (needs docker, the local backend, and the
# server-side secrets file). Never prints secrets: only lengths and
# counts reach stdout.
#
# Usage:
#   ./scripts/cleanup.sh [seed-url ...]
# With no seeds it resumes empty; add seeds via the admin UI
# (Frontier drawer -> paste seeds -> Resume).
#
# Spares frontier_settings and app_config_docs by design, so dashboard
# settings and admin-persisted config survive the wipe.
#
# Env overrides:
#   RELEASE_ENV  path to the secrets file (default below)
#   OPENLENS_API_URL  backend base URL (default http://localhost:8000)
#   SEED_PRIORITY  priority applied to CLI seeds (default 1)
set -euo pipefail

RELEASE_ENV="${RELEASE_ENV:-/home/rajan/servers/release.env}"
API="${OPENLENS_API_URL:-http://localhost:8000}"

[ -f "$RELEASE_ENV" ] || {
  echo "secrets file not found: $RELEASE_ENV (override with RELEASE_ENV=path)" >&2
  exit 1
}

# shellcheck disable=SC1090
set -a
# shellcheck disable=SC1091
. "$RELEASE_ENV"
set +a

TOKEN="${OPENLENS_FRONTIER_ADMIN_TOKEN:?missing OPENLENS_FRONTIER_ADMIN_TOKEN}"
PGUSER="${POSTGRES_USER:-openlens}"
PGDB="${POSTGRES_DB:-openlens}"
PGCONT="$(docker ps --format '{{.Names}}' | grep -i postgres | head -1)"
[ -n "$PGCONT" ] || {
  echo "no postgres container found" >&2
  exit 1
}

auth=(-H "X-Admin-Token: $TOKEN")

echo "== 1/5 pause =="
curl -sS -X POST "$API/api/v1/frontier/pause" "${auth[@]}"
echo

echo "== 2/5 confirm paused =="
STATUS="$(curl -sS "$API/api/v1/frontier/status")"
echo "$STATUS" | grep -o '"paused":[a-z]*' || true
echo "$STATUS" | grep -q '"paused":true' || {
  echo "pause did not land; aborting before wipe" >&2
  exit 1
}

echo "== 3/5 truncate documents + crawler state =="
for t in documents frontier_queue crawl_workers domain_politeness crawl_checkpoints; do
  docker exec "$PGCONT" psql -U "$PGUSER" -d "$PGDB" -c "TRUNCATE public.$t;"
done

echo "== 4/5 purge kafka backlog =="
CLEAR="$(curl -sS -X POST "$API/api/v1/frontier/clear" "${auth[@]}")"
echo "$CLEAR"
if ! echo "$CLEAR" | grep -q '"kafka_purged":true'; then
  echo "kafka purge failed; restarting crawlers and retrying clear"
  # shellcheck disable=SC2046
  docker restart $(docker ps --format '{{.Names}}' | grep -i crawler)
  sleep 5
  CLEAR="$(curl -sS -X POST "$API/api/v1/frontier/clear" "${auth[@]}")"
  echo "$CLEAR"
  echo "$CLEAR" | grep -q '"kafka_purged":true' || {
    echo "kafka purge still failing; aborting before resume" >&2
    exit 1
  }
fi

echo "== 5/5 resume =="
if [ "$#" -gt 0 ]; then
  SEEDS_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")"
  curl -sS -X POST "$API/api/v1/frontier/resume" "${auth[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"seeds\":$SEEDS_JSON,\"priority\":${SEED_PRIORITY:-1}}"
else
  curl -sS -X POST "$API/api/v1/frontier/resume" "${auth[@]}" \
    -H 'Content-Type: application/json' -d '{}'
fi
echo
echo "done; queue snapshot:"
curl -sS "$API/api/v1/frontier/status" | head -c 600
echo
