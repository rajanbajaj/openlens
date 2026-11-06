# Crawl diagnostics misreport: "disabled" lag plus zero workers mid-drain

Date: 2026-11-02 · Fixed in v0.4.40 / v0.4.41 (follows
[crawler-zero-workers-idle-heartbeat](crawler-zero-workers-idle-heartbeat.md))

## Symptoms

- The frontier dashboard reads `Kafka lag: disabled — store-backed
  queue` on a server where Kafka is enabled and the topics hold data
  (e.g. hundreds of thousands of messages in `crawl.urls`).
- `Workers (0)` / "No workers heartbeating" while crawler containers are
  up, healthy, and consuming; their logs show only the startup banner
  (`crawl=... queue=kafka ... seeds=0`).
- The backend access log shows exactly one heartbeat hit per crawler
  (all 200) and then nothing.

## Root causes (two, compounding)

1. **The store overlay rebuilt the payload without the flag.**
   `_frontier_status_payload` sets `kafka_enabled` from the serving
   config, but `_overlay_store_status` reconstructs
   `FrontierStatusSchema` field-by-field and dropped it — so on any
   server with a live store the endpoint reported `kafka_enabled:
   False` no matter the config. (Same trap as the pre-v0.4.35
   `cleared_queued` handling: fields set after the overlay survive;
   fields set before it must be threaded through the rebuild.)
2. **The initial store drain outlasts the worker-stale window.**
   `_drain_once()` is one blocking `run_index` call over the whole
   store backlog (100k+ rows after a crash/outage). The only beats
   around it are the forced startup ones; the live registry prunes
   entries after 240 s (`WORKER_STALE_AFTER_S`), so healthy replicas
   vanish from the dashboard until the drain returns. The v0.4.33 fix
   covered the idle-topic case; this is the deep-backlog case.

## Fix (v0.4.40 / v0.4.41)

v0.4.40 — tell the states apart instead of one bare "unknown":

- New `kafka_enabled` on the status payload (set from the serving
  config on every path, including the live socket which skips the lag
  probe by design).
- Dashboard renders three states: disabled (store-backed queue),
  a lag count, or probe-failed — and live-socket frames preserve the
  last GET probe instead of flashing back to unknown.

v0.4.41:

- `_overlay_store_status` preserves `kafka_enabled` through the
  rebuild (regression test `test_store_overlay_preserves_kafka_enabled`
  fails without it).
- New fail-open reporter thread in `run_kafka_worker` beats on a 5 s
  tick through blocking drains; the throttled progress call still sets
  the heartbeat cadence, and the thread stops in the run `finally`.
  Regression test `test_run_kafka_worker_beats_during_blocking_drain`
  (fails without the thread: 1 beat) also covers that a deep backlog
  can no longer prune live workers.

## How to confirm on prod

1. Deploy, then `GET /api/v1/frontier/status`: `kafka_enabled: true`
   with a numeric `kafka_lag_total`.
2. During a deep backlog, `docker logs <backend> | grep
   workers/heartbeat` keeps showing 200s every heartbeat interval and
   the dashboard keeps listing the replicas.
3. `kafka_lag_total` falls as the fleet drains `crawl.urls`.

## Do not chase these (ruled out for this signature)

- 403 heartbeat auth failures (compare the token on backend, crawler,
  and `.env` first — here all three matched).
- A down broker (kafka-ui showed the topics and their message counts).
- `checkpoint` zeros mid-pass (normal — persists at pass end only).
- One heartbeat hit per crawler at startup with silence after: that
  *is* this bug's fingerprint, not a crash (the beat is
  `suppress`ed, so failures and the drain wait are both silent).
