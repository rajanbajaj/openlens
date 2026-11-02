# Why the 3-broker Kafka cluster kept flapping (and why one broker is enough here)

Date: Nov 2026. Stack: 3 × `apache/kafka:3.9.0` in KRaft
combined `broker,controller` mode, 12 crawler replicas on one 16-CPU host.

## What we saw

- kafka-ui showed 3 brokers but the **active controller kept changing**
  (e.g. stuck on 2, then moving).
- The `crawlers` consumer group never held 12 members: typically 6,
  sometimes 2, while 12+ crawler containers were up and healthy.
- `Kafka lag: unknown — probe failed` in Crawl diagnostics; at one point
  `crawl.urls` went 1 hour with no new message and 0 consumers.
  (That last one was a separate consumer bug — slow-backlog drain plus
  join-only-on-first-poll — fixed in v0.4.49. The flapping below is
  independent of it.)

## What is normal vs what was wrong

- **Normal:** exactly one active controller at a time. All three nodes
  are controller *voters*; one is elected leader and the other two
  stand by. Seeing "active controller: 2" is healthy.
- **Wrong:** the leader kept changing. Every election forces all
  clients to refresh metadata, and consumer groups rebalance —
  which is why the member count flickered (6, then 2, then back)
  while the crawlers themselves never died.

## Likely cause (inferred, not proven)

Resource pressure on a shared host. Each broker is a JVM with
`mem_limit: 2G, cpus: 1.0`, sharing 16 CPUs with 12 crawlers,
Postgres, ClickHouse, and the frontends. A GC pause or CPU stall
longer than the controller session timeout looks like a dead
controller to the other two voters, so they elect a new one; when
the stalled node comes back, leadership can flap again. We never
captured a GC log proving it — but nothing else on that host
explains repeated elections with all containers reporting healthy.

Contributing fact: our topics never had real redundancy. User
topics auto-created with replication factor 1, so a dead broker
still meant an unavailable partition; only `__consumer_offsets`
(RF=3) was actually replicated. We paid the full cost of a quorum
(elections, ISR tracking, 6 GB RAM + 3 CPUs) without the benefit.

## Why one broker + 12 crawlers is fine

- Consumer parallelism comes from **partitions, not brokers**:
  `crawl.urls` has 12 partitions, so 12 crawlers each own one.
  A 13th would idle.
- The queue is transient (24h retention); Postgres is the source
  of truth and the store dedupes, so redelivery after an offset
  reset is harmless.
- No elections, no ISR churn, ~4 GB RAM and ~2 CPUs freed
  (single broker bumped to 4G/2CPU for headroom).

## When to go back to 3 brokers

- The backlog outgrows one broker's disk/throughput, or broker
  downtime (vs just crawler downtime) becomes unacceptable.
- To restore: re-add `kafka-2/kafka-3` (see the comment on the
  `kafka-1` service in `deploy/local/docker-compose.yml`), set the
  voter list and RFs back, and **wipe the kafka volumes first** —
  KRaft voter sets are sticky per volume and a 3-voter log will
  not boot as a 1-voter quorum (or vice versa).
