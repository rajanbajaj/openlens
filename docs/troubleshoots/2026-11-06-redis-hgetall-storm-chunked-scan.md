# Troubleshoot: Redis HGETALL storm + crawler socket-timeout crash-loop

## Symptoms (found 2026-11-06)

- Crawlers log `failed to read frontier URLs: Timeout reading from
  socket` / `frontier redis backend unreachable`, ~8–9 hits per worker
  per hour; 10 container restarts each; docs count flat at 162,295
  with ~10k URLs ready (nothing being indexed).
- Redis `commandstats` 45s apart: `HGETALL` 196,952,881 →
  197,736,572 (**~17.4k/s sustained**), `SSCAN` +1,506. Roughly one
  full 251k-URL scan every ~15s with crawlers otherwise idle.

## Root cause

`RedisFrontierStore._iter_metas` scanned the whole `seen` set, then
built **one pipeline with an HGETALL per known URL** (250k+ commands,
multi-second reply) — every full scan tripped the 2s client socket
timeout and killed its caller. Ten call sites (purge, validators,
admin reads) share the path, so any polling caller weaponizes it. (A
failing regression test for this already existed in the tree,
unimplemented.)

## Fix

`src/openlens/storage/frontier_redis.py`: meta reads go out in
`_META_PIPELINE_BATCH` (500) command chunks — one bounded EXECUTE per
chunk, so full scans degrade to slow instead of dying. The
pre-existing `test_iter_metas_chunks_pipeline_executes` now passes.
Fleet already downscaled to 2 crawlers + 2 analytics workers to cut
concurrent scan load. Release pending.

## Checks

```bash
docker logs openlens-local-crawler-4 --since 30m 2>&1 | grep -c "Timeout reading from socket"
docker exec openlens-local-redis-frontier-1 redis-cli -n 0 info commandstats | grep -E "hgetall|sscan"
docker exec openlens-local-redis-frontier-1 redis-cli slowlog get 10
```

Post-release, re-sample `commandstats`: HGETALL/s should track real
drain activity, not a ~15s scan metronome. If timeouts persist with
chunking in place, find the polling caller (admin screens, purge
jobs) before resizing Redis.
