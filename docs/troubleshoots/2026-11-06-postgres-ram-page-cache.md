# Troubleshoot: Postgres container RAM "full" is page cache, not a leak

## Symptoms (found 2026-11-06)

- `docker stats`: `openlens-local-postgres-1` at 3.6G / 4G (90%).

## Diagnosis (read-only, no action taken)

```bash
docker exec openlens-local-postgres-1 cat /sys/fs/cgroup/memory.stat | grep -E "^(anon|file) "
# anon 62M (real RSS) vs file ~4.1G (reclaimable page cache)
```

97% of the usage is file cache from table/index scans (warm cache —
it makes search fast), not a leak. Supporting numbers:
`shared_buffers = 1GB`, `work_mem = 4MB`, `max_connections = 400`,
41 idle connections holding negligible RSS.

## Rule

Do **not** restart Postgres for memory: a restart just colds the
cache and makes search slow again (see
`2026-11-06-search-love-502-capped-total.md`). If sustained writer
CPU plus cache pressure approaches the 4G ceiling, vertically scale
Postgres through the scale workflow (limits persist) instead.
