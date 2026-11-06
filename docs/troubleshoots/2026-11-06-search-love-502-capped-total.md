# Troubleshoot: `love` search 502s on common terms (uncapped FTS total)

## Symptoms (found 2026-11-06)

- `POST /api/v1/search` with `{"query": "love"}` → `502
  {"error": {"code": "provider_timeout"}}` after ~14s. Rare terms
  return 200 in ~40ms on the same index.
- Backend log: `POST /api/v1/search HTTP/1.1 502 Bad Gateway`; the
  body says `provider 'custom_index' timed out after 5.0s`.

## Root cause

The page query counted **every** match with `COUNT(*) OVER ()` and
then ranked/sorted all of them. `love` matches ~44k documents:

- `SELECT COUNT(*) ... @@ plainto_tsquery('love')` alone: **4.4s**
  (44k heap fetches for visibility checks).
- Full page query with the window count: **4.0s**.
- Sum ≈ 8s against a 5s provider budget → timeout → 502.

Term-frequency scaling (not contention) is the signature: cost grows
with match count, idle box, healthy GIN index, no bloat.

## Fix

`src/openlens/storage/postgres_backend.py`: totals are exact up to
`MAX_EXACT_TOTAL = 10_000` and saturate above it. The page counts
through a `LIMIT 10001` probe (bitmap scan stops early, ~0.5s) while
the ranked CTE still scores **all** matches, so top-N order is
unchanged. Same-shape page query on prod data: **0.9s** (was 4.0s),
identical scores. UI renders `10,000+` at the cap
(`formatResultTotal` in `frontend/src/api/client.ts`).

Verified locally: 1729 backend + 312 frontend tests pass; ruff, mypy,
eslint, prettier, tsc clean. Prod still 502s until this ships —
release pending.

## Checks

```bash
# Count half of any suspect term (read-only, 20s cap):
docker exec openlens-local-postgres-1 sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c \
  "SET statement_timeout='"'"'20s'"'"'; \
   SELECT count(*) FROM documents \
   WHERE search_tsv @@ plainto_tsquery('"'"'english'"'"','"'"'love'"'"');"'
```
