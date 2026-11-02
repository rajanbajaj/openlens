# Troubleshoot: fleet rebalance storms and empty consumer assignments

Date: 2026-11-02. Stack: single KRaft broker, 24 → 4 main crawlers,
2–3 backoff crawlers, 4–12 analytics workers across the day.

## 1. Post-deploy rebalance storms (thundering herd, self-heals)

### Symptoms

- Kafka log fills with `heartbeat expiration` removals + rejoins,
  generations climbing fast (e.g. analytics gen 7 → 20 in minutes),
  across several groups at once.
- `describe --state` flips between `PreparingRebalance` and `Stable`.

### Root cause (normal)

Every redeploy restarts all consumers at once. Each one runs its
initial deep drain simultaneously, poll gaps exceed the session
timeout, and the coordinator kicks members faster than they rejoin.
It settles on its own once drains complete.

### How to tell storm from real problem

```bash
K=openlens-local-kafka-1-1
# 1. Timestamp the kicks — a burst at a deploy minute is the storm.
docker logs $K --since 30m | grep "heartbeat expiration" | head -5
# 2. Deaths vs stalls — restarts climbing means dying processes.
docker inspect --format '{{.Name}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' <name>
# 3. The consumer's own view (kicked clients log it).
docker logs <worker> --since 30m | grep -iE "heartbeat failed|commitfailed|rebalance|Unavailable"
# 4. Still happening? Same command twice, a minute apart.
docker exec $K /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:19092 --describe --group crawlers --state
```

Worry only when expiries continue > 10 min after the last restart,
a group never holds `Stable`, member count stays below replicas, or
lag grows while members flap. During the 2026-11-02 rollout all
three groups returned to Stable with lag 0 — stand down, no fix
needed. (Note: `crawlers` `0 claimed` on the dashboard is NORMAL on
the Kafka drain path — it dequeues without leases — watch
pages/min and lag instead.)

## 2. Analytics group: joined but assigned zero partitions (OPEN)

### Symptoms

- 12 members `Stable`, but `--members` shows `#PARTITIONS 0` for all.
- `--describe` and `--offsets` return empty tables (exit 0).
- ClickHouse `analytics_events` frozen (375 rows, max `at` 06:00 UTC)
  while the topic tip carries fresh valid events.

### Ruled out (with evidence)

- Member config: resolved the live worker config in-container —
  topic/group/store/bootstrap all correct; a hand-built
  kafka-python consumer with the same settings gets **zero**
  partitions on group `analytics` but **all 12 + live records** on
  a fresh group (`probe3`).
- Broker/topic: a Java console consumer on a fresh group reads the
  topic fine; scratch-topic produce/consume works.
- Dead members: member IPs match the 12 live containers exactly.
- Store writes: inserts go to `openlens_analytics.analytics_events`
  (verified `_table()`), and ClickHouse's own `system.query_log`
  shows the last successful insert at 06:00:15 — nothing failing
  since, nothing arriving.
- Process health: no restarts, no OOM, logs silent, host load low.

### Tried, did not fix

- Single-worker restart (no assignment change).
- Full group delete + rejoin (group `analytics` deleted with zero
  members, workers rejoined fresh — still zero partitions).

### Suspected next step (not yet executed)

Move workers to a fresh group id (e.g. `analytics-v2`), reset
offsets to latest first to avoid reprocessing the ~14k backlog into
ClickHouse duplicates, restart, verify assignment. Rationale: fresh
groups demonstrably work; only the `analytics` group state is
poisoned, and deletion did not clear it.

### Related real bug found along the way

All `crawl.page` events use the event name as the Kafka key, so they
all hash onto **one partition** (13,980 of 14,003 messages on
partition 7). Even healthy, 11 of 12 workers idle while one does all
analytics work. Fix: publish with no key (round-robin) — code change,
next release.

## 3. Backend `unhealthy` while serving 200s (OPEN)

- `docker exec` + in-container `urllib` to `127.0.0.1:8000/healthz`
  hangs past the 5 s probe timeout; the same request from outside
  (host port / other containers) succeeds, and app logs show steady
  200s. `print(1)` in-container is instant, so it is not exec
  overhead; no proxy env is set; uvicorn binds `0.0.0.0:8000`.
- Not yet root-caused. App traffic is unaffected — only the Docker
  health flag is wrong.

## 4. Stale bootstrap list (FIXED, v0.4.57)

`release.env` listed dead brokers
(`kafka-1,kafka-2,kafka-3`) long after the single-broker migration.
Every Python client burned DNS timeouts on the dead hosts at startup
and intermittently failed version probing. Fixed to `kafka-1:19092`
with the comment updated. If you see `Name or service not known`
for kafka-2/3 in any container log, the env did not propagate to
that service — recreate it.
