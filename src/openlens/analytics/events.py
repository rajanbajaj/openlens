"""Analytics event pipeline: Kafka topic, worker, and separate store.

Server-side analytics (search feedback today; user-side interaction
events later) flow ``API -> analytics.events topic -> analytics worker ->
analytics database``. The API path only publishes (fire-and-forget, never
in the search latency path); the worker batch-inserts and enforces
retention. The analytics database is separate from the index database so
append volume can never contend with search reads or frontier writes.

Privacy: event names and prop keys are a closed allowlist (fail-closed
like the feedback endpoint). Raw query text is never stored — only its
hash. No identity, no sessions, no cookies.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

ANALYTICS_TOPIC = "analytics.events"
ANALYTICS_GROUP_ID = "analytics"

EVENT_SCHEMA_VERSION = 1

#: Server-produced event names. ``client.*`` is reserved for the future
#: user-side track and rejected until it ships (fail-closed).
EVENT_NAMES = frozenset(
    {
        "search.feedback",
        "search.served",
    }
)

#: Allowed prop keys per event. Anything else is dropped, never stored.
EVENT_PROPS: dict[str, frozenset[str]] = {
    "search.feedback": frozenset({"doc_id", "action", "reason"}),
    "search.served": frozenset({"query_hash", "mode", "result_count"}),
}

MAX_PROP_KEYS = 16
MAX_PROP_VALUE_LEN = 256


def hash_query(query: str) -> str:
    """Anonymized query fingerprint (raw text is never stored)."""
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()


@dataclass
class AnalyticsEvent:
    """One validated analytics event, ready to publish."""

    name: str
    props: dict[str, Any] = field(default_factory=dict)
    source: str = "server"
    at: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "v": EVENT_SCHEMA_VERSION,
            "name": self.name,
            "source": self.source,
            "at": self.at or time.time(),
            "props": dict(self.props),
        }


def normalize_event(raw: Any) -> AnalyticsEvent | None:
    """Validate an inbound event dict; None when unusable (fail-closed).

    Unknown names, non-server sources, and oversized payloads are dropped
    rather than stored. Prop keys outside the per-event allowlist are
    dropped; values are coerced to short strings/numbers/bools.
    """
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("name")
    if name not in EVENT_NAMES:
        return None
    source = raw.get("source", "server")
    if source != "server":
        return None
    allowed = EVENT_PROPS.get(str(name), frozenset())
    props: dict[str, Any] = {}
    incoming = raw.get("props")
    if isinstance(incoming, Mapping):
        for key, value in incoming.items():
            if len(props) >= MAX_PROP_KEYS:
                break
            if not isinstance(key, str) or key not in allowed:
                continue
            if isinstance(value, bool):
                props[key] = value
            elif isinstance(value, (int, float)):
                props[key] = value
            else:
                text = str(value or "")[:MAX_PROP_VALUE_LEN]
                if text:
                    props[key] = text
    try:
        at = float(raw.get("at", 0.0) or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    if at <= 0:
        at = time.time()
    return AnalyticsEvent(name=str(name), props=props, source="server", at=at)


def feedback_event(doc_id: str, action: str, reason: str | None) -> AnalyticsEvent:
    """Build the server-side event mirroring a feedback tally."""
    props: dict[str, Any] = {"doc_id": doc_id, "action": action}
    if reason:
        props["reason"] = reason
    return AnalyticsEvent(name="search.feedback", props=props)


def served_event(query: str, mode: str, result_count: int) -> AnalyticsEvent:
    """Build the server-side event for a completed search."""
    return AnalyticsEvent(
        name="search.served",
        props={
            "query_hash": hash_query(query),
            "mode": mode,
            "result_count": max(0, int(result_count)),
        },
    )


class AnalyticsQueue:
    """Thin Kafka event queue: fire-and-forget publish, batched consume.

    Separate from the URL-shaped ``KafkaQueue``: values are arbitrary
    event dicts. Publish never waits (at-most-once analytics can afford
    broker hiccups); the worker polls and commits explicitly. The driver
    is imported lazily; pass fakes in tests to stay offline.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: list[str],
        topic: str = ANALYTICS_TOPIC,
        group_id: str = ANALYTICS_GROUP_ID,
        producer: Any | None = None,
        consumer_factory: Any | None = None,
    ) -> None:
        if not bootstrap_servers:
            raise ValueError("analytics queue needs at least one bootstrap server")
        if not topic.strip():
            raise ValueError("analytics queue needs a non-empty topic")
        self._topic = topic
        self._group_id = group_id
        self._bootstrap_servers = list(bootstrap_servers)
        self._consumer_factory = consumer_factory
        self._consumer: Any | None = None
        self._pending: list[Any] = []
        if producer is None:
            from openlens.indexing.kafka_queue import _load_driver

            kafka, _ = _load_driver()
            producer = kafka.KafkaProducer(
                bootstrap_servers=list(bootstrap_servers),
                key_serializer=lambda key: (key or "").encode("utf-8"),
                value_serializer=lambda value: json.dumps(value).encode("utf-8"),
                acks="all",
                retries=3,
                linger_ms=1000,
            )
        self._producer = producer

    @classmethod
    def from_app_config(
        cls,
        config: Mapping[str, Any] | None,
        *,
        producer: Any | None = None,
        consumer_factory: Any | None = None,
    ) -> "AnalyticsQueue":
        """Build from the app ``kafka:`` section (bootstrap servers only)."""
        from openlens.indexing.kafka_queue import kafka_section

        section = kafka_section(config if isinstance(config, Mapping) else {})
        servers = list(section.get("bootstrap_servers", ["localhost:9092"]))
        analytics = config.get("analytics", {}) if isinstance(config, Mapping) else {}
        if not isinstance(analytics, Mapping):
            analytics = {}
        topic = str(analytics.get("topic", ANALYTICS_TOPIC) or ANALYTICS_TOPIC)
        group_id = str(
            analytics.get("group_id", ANALYTICS_GROUP_ID) or ANALYTICS_GROUP_ID
        )
        return cls(
            bootstrap_servers=servers,
            topic=topic,
            group_id=group_id,
            producer=producer,
            consumer_factory=consumer_factory,
        )

    def publish_event(self, event: AnalyticsEvent) -> bool:
        """Buffer one event for sending; never waits, never raises."""
        try:
            self._producer.send(self._topic, key=event.name, value=event.to_payload())
            return True
        except Exception:
            return False

    def poll_events(
        self, timeout_ms: int = 1000, max_records: int = 500
    ) -> list[dict[str, Any]]:
        """Return up to ``max_records`` raw event dicts (offset uncommitted)."""
        consumer = self._consumer_instance()
        try:
            batches = consumer.poll(timeout_ms=max(0, timeout_ms))
        except Exception:
            return []
        records: list[Any] = []
        try:
            for batch in batches.values():
                records.extend(batch)
        except AttributeError:
            records = list(batches or [])
        out: list[dict[str, Any]] = []
        for record in records[: max(0, max_records)]:
            self._pending.append(record)
            value = getattr(record, "value", None)
            if isinstance(value, dict):
                out.append(value)
        return out

    def commit(self) -> None:
        """Commit polled offsets (call after a successful store flush)."""
        if self._consumer is None or not self._pending:
            return
        with contextlib.suppress(Exception):
            self._consumer.commit()
        self._pending = []

    def close(self) -> None:
        """Flush and close producer/consumer (best effort)."""
        producer, self._producer = self._producer, None
        if producer is not None:
            with contextlib.suppress(Exception):
                producer.flush(timeout=5)
            with contextlib.suppress(Exception):
                producer.close(timeout=5)
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            with contextlib.suppress(Exception):
                consumer.close()

    def _consumer_instance(self) -> Any:
        if self._consumer is None:
            if self._consumer_factory is not None:
                self._consumer = self._consumer_factory()
            else:
                from openlens.indexing.kafka_queue import _load_driver

                kafka, _ = _load_driver()
                self._consumer = kafka.KafkaConsumer(
                    self._topic,
                    bootstrap_servers=list(self._bootstrap_servers),
                    group_id=self._group_id,
                    enable_auto_commit=False,
                    auto_offset_reset="earliest",
                    key_deserializer=lambda raw: raw.decode("utf-8") if raw else "",
                    value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
                )
        return self._consumer
