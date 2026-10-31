"""Kafka-backed URL queue for distributed crawling.

One topic (default ``crawl.urls``) carries URL messages; every crawler
replica joins the same consumer group, so Kafka assigns each URL to
exactly one live worker and competing replicas never fetch the same
page. The message key is the normalized URL, so repeat sightings of one
URL land on the same partition in order.

Delivery is at-least-once: the consumer commits an offset only after
the URL was crawled and indexed. Duplicates (rebalance, retry) collapse
in the Postgres frontier store, whose normalized-URL primary key makes
re-enqueue a no-op and whose ``done`` rows are never refetched.

No broker, no problem at import time: the ``kafka`` driver is imported
lazily inside the constructors, and every missing-driver or
unreachable-broker failure surfaces as ``ProviderError(UNAVAILABLE)``
so callers (API resume, CLI worker) degrade or fail loud instead of
crashing at import.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.indexing.crawler import normalize_url

#: Driver import path (``kafka-python-ng`` provides the ``kafka`` package).
_DRIVER_MODULE = "kafka"

#: Error code used on every surface from this module (mirrors storage/...).
_PROVIDER_NAME = "kafka_queue"

#: Defaults mirroring the ``kafka:`` config section shape.
DEFAULT_TOPIC = "crawl.urls"
DEFAULT_GROUP_ID = "crawlers"
DEFAULT_PUBLISH_TIMEOUT_S = 5.0
DEFAULT_POLL_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class KafkaMessage:
    """One consumed URL message (offset committed separately).

    ``priority`` mirrors the frontier store semantic: ``None`` (absent
    from the payload, e.g. pre-upgrade messages) keeps whatever the
    store already has; an explicit int overwrites it on conflict.
    """

    url: str
    depth: int = 0
    priority: int | None = None


def _coerce_enabled(value: Any) -> bool:
    """Coerce an ``enabled`` flag (env resolution stringifies scalars).

    Config env substitution (``config.py``) replaces ``${VAR:-default}``
    on raw text, so YAML booleans arrive as ``"true"``/``"false"`` strings
    (same as ``frontier.enabled``). Mirror the ``_is_truthy`` precedent
    from ``plugins/user_restrictions.py``; anything else fails closed.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return value == 1
        raise ConfigError(f"Config 'kafka.enabled' must be a boolean, got {value!r}.")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no", ""):
            return False
    raise ConfigError(f"Config 'kafka.enabled' must be a boolean, got {value!r}.")


def validate_kafka_section(section: Any) -> dict[str, Any]:
    """Validate the ``kafka:`` config section (fail-closed, like the rest).

    Unknown keys are rejected; ``enabled: false`` (or a missing section)
    disables Kafka and keeps the store-backed queue. Returns the
    normalized section with defaults filled.
    """
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ConfigError("Config section 'kafka' must be a mapping.")
    known = {
        "enabled",
        "bootstrap_servers",
        "topic",
        "group_id",
        "publish_timeout_s",
        "poll_timeout_s",
        "cluster_id",
        "controller_quorum",
    }
    unknown = sorted(set(section) - known)
    if unknown:
        raise ConfigError(
            f"Unknown 'kafka' keys: {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(known))}."
        )
    normalized: dict[str, Any] = {
        "enabled": False,
        "bootstrap_servers": ["localhost:9092"],
        "topic": DEFAULT_TOPIC,
        "group_id": DEFAULT_GROUP_ID,
        "publish_timeout_s": DEFAULT_PUBLISH_TIMEOUT_S,
        "poll_timeout_s": DEFAULT_POLL_TIMEOUT_S,
        "cluster_id": "",
        "controller_quorum": [],
    }
    if "enabled" in section:
        normalized["enabled"] = _coerce_enabled(section["enabled"])
    if "bootstrap_servers" in section:
        raw = section["bootstrap_servers"]
        if isinstance(raw, str):
            servers = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, list) and all(isinstance(p, str) for p in raw):
            servers = [part.strip() for part in raw]
        else:
            raise ConfigError(
                "Config 'kafka.bootstrap_servers' must be a "
                "comma-separated string or a list of strings."
            )
        servers = [part for part in servers if part]
        if not servers:
            raise ConfigError(
                "Config 'kafka.bootstrap_servers' must name at least one broker."
            )
        normalized["bootstrap_servers"] = servers
    for key in ("topic", "group_id"):
        if key in section:
            value = section[key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"Config 'kafka.{key}' must be a non-empty string.")
            normalized[key] = value.strip()
    for key in ("publish_timeout_s", "poll_timeout_s"):
        if key in section:
            value = section[key]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ConfigError(f"Config 'kafka.{key}' must be a positive number.")
            normalized[key] = float(value)
    if "cluster_id" in section:
        value = section["cluster_id"]
        if not isinstance(value, str):
            raise ConfigError("Config 'kafka.cluster_id' must be a string.")
        normalized["cluster_id"] = value.strip()
    if "controller_quorum" in section:
        raw = section["controller_quorum"]
        if isinstance(raw, str):
            servers = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, list) and all(isinstance(p, str) for p in raw):
            servers = [part.strip() for part in raw]
        else:
            raise ConfigError(
                "Config 'kafka.controller_quorum' must be a "
                "comma-separated string or a list of strings."
            )
        normalized["controller_quorum"] = [part for part in servers if part]
    return normalized


def kafka_enabled(config: Mapping[str, Any] | None) -> bool:
    """True when the app config enables the Kafka queue with brokers set."""
    if not isinstance(config, Mapping):
        return False
    try:
        section = validate_kafka_section(config.get("kafka"))
    except ConfigError:
        return False
    return bool(section.get("enabled")) and bool(section.get("bootstrap_servers"))


def kafka_section(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalized ``kafka:`` section ({} when absent/invalid)."""
    if not isinstance(config, Mapping):
        return {}
    try:
        return validate_kafka_section(config.get("kafka"))
    except ConfigError:
        return {}


def _load_driver() -> tuple[Any, Any]:
    """Import the driver pair (module, KafkaError); UNAVAILABLE when absent."""
    try:
        import kafka  # type: ignore[import-not-found, import-untyped]

        from kafka.errors import KafkaError  # type: ignore[import-not-found, import-untyped]
    except ImportError as exc:
        raise ProviderError(
            "Kafka driver is not installed; install the kafka extra to "
            "use the Kafka queue backend",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        ) from exc
    return kafka, KafkaError


def _error(message: str, exc: Exception | None = None) -> ProviderError:
    return ProviderError(
        message,
        provider_name=_PROVIDER_NAME,
        code=ProviderError.UNAVAILABLE,
    )


class KafkaQueue:
    """Thin Kafka URL queue: ``publish`` seeds/links, ``poll``/``commit`` consume.

    Pass ``producer``/``consumer_factory`` fakes in tests to stay offline;
    otherwise the driver is imported lazily here (never at module import).
    ``poll`` returns ``None`` on an idle timeout; every broker failure
    raises ``ProviderError(UNAVAILABLE)``.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: list[str],
        topic: str = DEFAULT_TOPIC,
        group_id: str = DEFAULT_GROUP_ID,
        publish_timeout_s: float = DEFAULT_PUBLISH_TIMEOUT_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        producer: Any | None = None,
        consumer_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not bootstrap_servers:
            raise _error("Kafka queue needs at least one bootstrap server.")
        if not topic.strip():
            raise _error("Kafka queue needs a non-empty topic.")
        self._topic = topic
        self._publish_timeout_s = publish_timeout_s
        self._poll_timeout_s = poll_timeout_s
        self._consumer_factory = consumer_factory
        self._consumer: Any | None = None
        self._pending_record: Any | None = None
        if producer is None:
            kafka, kafka_error = _load_driver()
            self._kafka_error: Any = kafka_error
            try:
                producer = kafka.KafkaProducer(
                    bootstrap_servers=list(bootstrap_servers),
                    key_serializer=lambda key: key.encode("utf-8"),
                    value_serializer=lambda value: json.dumps(value).encode("utf-8"),
                    acks="all",
                    retries=3,
                    max_block_ms=int(publish_timeout_s * 1000),
                    request_timeout_ms=int(publish_timeout_s * 1000),
                )
            except Exception as exc:
                raise _error(
                    f"cannot create Kafka producer for {bootstrap_servers}: {exc}"
                ) from exc
        else:
            self._kafka_error = Exception
        self._producer = producer
        self._group_id = group_id
        self._bootstrap_servers = list(bootstrap_servers)

    @classmethod
    def from_config(
        cls,
        section: Mapping[str, Any],
        *,
        producer: Any | None = None,
        consumer_factory: Callable[[], Any] | None = None,
    ) -> "KafkaQueue":
        """Build from a validated ``kafka:`` section (test fakes injectable)."""
        normalized = validate_kafka_section(section)
        return cls(
            bootstrap_servers=list(normalized["bootstrap_servers"]),
            topic=str(normalized["topic"]),
            group_id=str(normalized["group_id"]),
            publish_timeout_s=float(normalized["publish_timeout_s"]),
            poll_timeout_s=float(normalized["poll_timeout_s"]),
            producer=producer,
            consumer_factory=consumer_factory,
        )

    @classmethod
    def from_app_config(
        cls,
        config: Mapping[str, Any] | None,
        *,
        producer: Any | None = None,
        consumer_factory: Callable[[], Any] | None = None,
    ) -> "KafkaQueue":
        """Build from the top-level app config (reads ``kafka:``)."""
        section = config.get("kafka") if isinstance(config, Mapping) else None
        return cls.from_config(
            section if isinstance(section, Mapping) else {},
            producer=producer,
            consumer_factory=consumer_factory,
        )

    # -- produce ---------------------------------------------------------

    def publish(self, url: str, depth: int = 0, priority: int | None = None) -> bool:
        """Publish one URL (keyed by normalized URL). False when blank.

        ``priority`` is only carried when explicit (``None`` omits the
        key so consumers keep the stored value); an explicit int,
        including ``0``, overwrites it on conflict. Raises
        ``ProviderError(UNAVAILABLE)`` when the broker rejects or
        times out, so callers can fall back to the store queue.
        """
        if not isinstance(url, str) or not url.strip():
            return False
        cleaned = url.strip()
        payload: dict[str, Any] = {"url": cleaned, "depth": int(depth)}
        if priority is not None:
            payload["priority"] = int(priority)
        try:
            future = self._producer.send(
                self._topic,
                key=normalize_url(cleaned),
                value=payload,
            )
            future.get(timeout=self._publish_timeout_s)
        except Exception as exc:
            if isinstance(exc, self._kafka_error):
                raise _error(f"Kafka publish to '{self._topic}' failed: {exc}") from exc
            raise _error(f"Kafka publish to '{self._topic}' failed: {exc}") from exc
        return True

    # -- consume ----------------------------------------------------------

    def _consumer_instance(self) -> Any:
        if self._consumer is None:
            if self._consumer_factory is not None:
                self._consumer = self._consumer_factory()
            else:
                kafka, _ = _load_driver()
                try:
                    self._consumer = kafka.KafkaConsumer(
                        self._topic,
                        bootstrap_servers=list(self._bootstrap_servers),
                        group_id=self._group_id,
                        enable_auto_commit=False,
                        auto_offset_reset="earliest",
                        key_deserializer=lambda raw: raw.decode("utf-8") if raw else "",
                        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
                    )
                except Exception as exc:
                    raise _error(
                        f"cannot create Kafka consumer for '{self._topic}': {exc}"
                    ) from exc
        return self._consumer

    def check_available(self, timeout_s: float = 5.0) -> bool:
        """True when a broker answers (fail-fast pre-flight for the CLI)."""
        try:
            consumer = self._consumer_instance()
            connected = consumer.bootstrap_connected()
        except Exception:
            return False
        if connected:
            return True
        try:
            import time as _time

            deadline = _time.monotonic() + max(0.0, timeout_s)
            while _time.monotonic() < deadline:
                _time.sleep(0.2)
                try:
                    if consumer.bootstrap_connected():
                        return True
                except Exception:
                    return False
        except Exception:
            return False
        return False

    def poll(self) -> KafkaMessage | None:
        """Return the next URL message, or None on an idle timeout.

        Raises ``ProviderError(UNAVAILABLE)`` when the broker is
        unreachable, so the worker loop can fail loud instead of
        busy-spinning on nothing.
        """
        try:
            batches = self._consumer_instance().poll(
                timeout_ms=int(self._poll_timeout_s * 1000)
            )
        except Exception as exc:
            raise _error(f"Kafka poll on '{self._topic}' failed: {exc}") from exc
        records: list[Any] = []
        try:
            for batch in batches.values():
                records.extend(batch)
        except AttributeError:
            records = list(batches or [])
        if not records:
            self._pending_record = None
            return None
        record = records[0]
        self._pending_record = record
        value = getattr(record, "value", None)
        priority: int | None = None
        if isinstance(value, Mapping):
            url = str(value.get("url", "") or "")
            try:
                depth = int(value.get("depth", 0) or 0)
            except (TypeError, ValueError):
                depth = 0
            raw_priority: Any = value.get("priority", None)
            if raw_priority is not None:
                try:
                    priority = int(raw_priority)
                except (TypeError, ValueError):
                    priority = None
        else:
            url, depth = str(value or ""), 0
        if not url.strip():
            return None
        return KafkaMessage(url=url.strip(), depth=depth, priority=priority)

    def commit(self) -> None:
        """Commit the last polled offset (call after a successful crawl)."""
        if self._consumer is None or self._pending_record is None:
            return
        try:
            self._consumer.commit()
        except Exception as exc:
            raise _error(f"Kafka commit on '{self._topic}' failed: {exc}") from exc
        finally:
            self._pending_record = None

    def close(self) -> None:
        """Flush/close producer and consumer (best effort)."""
        producer, self._producer = self._producer, None
        if producer is not None:
            with contextlib.suppress(Exception):
                producer.flush(timeout=self._publish_timeout_s)
            with contextlib.suppress(Exception):
                producer.close(timeout=self._publish_timeout_s)
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            with contextlib.suppress(Exception):
                consumer.close(autocommit=False)


def open_kafka_queue(
    config: Mapping[str, Any] | None,
    *,
    producer: Any | None = None,
    consumer_factory: Callable[[], Any] | None = None,
) -> KafkaQueue:
    """Build the queue from app config (test fakes injectable)."""
    return KafkaQueue.from_app_config(
        config, producer=producer, consumer_factory=consumer_factory
    )


def topic_group_lag(
    bootstrap_servers: list[str],
    topic: str,
    group_id: str,
    timeout_s: float = 10.0,
) -> dict[str, int]:
    """Total unconsumed messages for one topic/group (scale signal).

    Best-effort read for the admin status payload: creates a throwaway
    consumer, compares log-end offsets to the group's commits, and
    closes it. Raises ``ProviderError(UNAVAILABLE)`` when the driver is
    missing or the broker fails, so callers degrade to lag-unknown.
    """
    if not bootstrap_servers or not topic.strip() or not group_id.strip():
        raise _error("group lag needs bootstrap servers, a topic, and a group id.")
    kafka, _ = _load_driver()
    consumer = None
    try:
        try:
            consumer = kafka.KafkaConsumer(
                group_id=group_id.strip(),
                bootstrap_servers=list(bootstrap_servers),
                enable_auto_commit=False,
                request_timeout_ms=max(1000, int(timeout_s * 1000)),
            )
        except Exception as exc:
            raise _error(f"cannot create lag-probe consumer: {exc}") from exc
        try:
            partitions = consumer.partitions_for_topic(topic.strip())
        except Exception as exc:
            raise _error(f"cannot list partitions for '{topic}': {exc}") from exc
        if not partitions:
            return {"total_lag": 0, "partitions": 0}
        assignments = [kafka.TopicPartition(topic.strip(), part) for part in partitions]
        try:
            ends = consumer.end_offsets(assignments)
        except Exception as exc:
            raise _error(f"cannot read log-end offsets for '{topic}': {exc}") from exc
        total = 0
        for assignment in assignments:
            try:
                committed = consumer.committed(assignment)
            except Exception:
                committed = None
            end = ends.get(assignment) or 0
            done = committed if isinstance(committed, int) else 0
            total += max(0, int(end) - done)
        return {"total_lag": total, "partitions": len(assignments)}
    finally:
        if consumer is not None:
            with contextlib.suppress(Exception):
                consumer.close(autocommit=False)


def describe_topic_layout(
    bootstrap_servers: list[str],
    topic: str,
    timeout_s: float = 10.0,
) -> dict[str, int]:
    """Live ``{partitions, replication_factor}`` for a topic.

    Reads cluster metadata through a throwaway client (replication
    factor from partition 0's replica set). Raises
    ``ProviderError(UNAVAILABLE)`` when the driver is missing or the
    broker fails, so routes degrade to 503 instead of 500.
    """
    if not bootstrap_servers or not topic.strip():
        raise _error("topic layout needs bootstrap servers and a topic.")
    _load_driver()
    name = topic.strip()
    try:
        from kafka import KafkaClient  # type: ignore[import-not-found, import-untyped]
    except ImportError as exc:
        raise _error("Kafka driver is not installed.") from exc
    client = None
    try:
        try:
            client = KafkaClient(
                bootstrap_servers=list(bootstrap_servers),
                request_timeout_ms=max(1000, int(timeout_s * 1000)),
            )
        except Exception as exc:
            raise _error(f"cannot connect for topic layout: {exc}") from exc
        try:
            if client.cluster.partitions_for_topic(name) is None:
                future = client.cluster.request_update()
                client.poll(future, timeout_ms=max(1000, int(timeout_s * 1000)))
            parts = client.cluster.partitions_for_topic(name) or set()
        except Exception as exc:
            raise _error(f"cannot list partitions for '{name}': {exc}") from exc
        if not parts:
            raise _error(f"topic '{name}' has no partitions (missing?).")
        try:
            meta = client.cluster.partition_metadata(name, min(parts))
            replicas = list(getattr(meta, "replicas", None) or [])
        except Exception as exc:
            raise _error(f"cannot read replicas for '{name}': {exc}") from exc
        if not replicas:
            raise _error(f"topic '{name}' reports no replicas.")
        return {"partitions": len(parts), "replication_factor": len(replicas)}
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def plan_reassignment(
    topic: str,
    partition_count: int,
    replication_factor: int,
    broker_ids: list[int],
) -> list[dict[str, Any]]:
    """Even-spread replica plan in ``kafka-reassign-partitions`` JSON shape.

    Pure function (no broker contact): replica ``r`` of partition ``p``
    lands on ``broker_ids[(p + r) % n]``. Raises ``ProviderError`` when
    the inputs cannot form a valid assignment.
    """
    if not isinstance(topic, str) or not topic.strip():
        raise _error("reassignment needs a non-empty topic.")
    try:
        partitions = int(partition_count)
        replicas = int(replication_factor)
        brokers = [int(bid) for bid in broker_ids]
    except (TypeError, ValueError) as exc:
        raise _error(f"invalid reassignment inputs: {exc}") from exc
    if partitions <= 0:
        raise _error("reassignment needs at least one partition.")
    if replicas <= 0:
        raise _error("reassignment needs a positive replication factor.")
    if len(brokers) < replicas:
        raise _error(
            f"replication factor {replicas} exceeds {len(brokers)} target brokers."
        )
    if len(set(brokers)) != len(brokers):
        raise _error("target broker ids must be distinct.")
    name = topic.strip()
    return [
        {
            "topic": name,
            "partition": part,
            "replicas": [
                brokers[(part + replica) % len(brokers)] for replica in range(replicas)
            ],
        }
        for part in range(partitions)
    ]


def purge_topic(
    config: Mapping[str, Any] | None,
    *,
    admin_client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Delete and recreate the crawl topic (admin queue-clean).

    Clearing the store alone is not a clean: already-published messages
    would refill it on resume, so the topic goes too. The topic is
    recreated with the same partition count and replication factor it
    had (described first); a missing topic is created with one
    partition. Consumer-group offsets for a deleted topic are dropped
    by the broker, and paused workers hold no commits during the
    operation, so the next resume starts the group from the beginning
    of the fresh topic (``auto_offset_reset=earliest``).

    Returns ``{"purged": True, "partitions": n}``. Raises
    ``ProviderError(UNAVAILABLE)`` when the driver is missing or the
    broker fails. Unknown-topic on delete and topic-exists on create
    are tolerated (idempotent reruns, concurrent recreates).
    """
    section = config.get("kafka") if isinstance(config, Mapping) else None
    normalized = validate_kafka_section(section if isinstance(section, Mapping) else {})
    topic = str(normalized["topic"])
    timeout_ms = int(float(normalized.get("publish_timeout_s", 5.0)) * 1000)
    try:
        from kafka.admin import NewTopic  # type: ignore[import-not-found, import-untyped]
    except ImportError:
        # Driver-absent runtimes (and their fakes) only need the shape.
        @dataclass(frozen=True)
        class NewTopic:  # type: ignore[no-redef]
            name: str
            num_partitions: int
            replication_factor: int

    try:
        from kafka.errors import (  # type: ignore[import-not-found, import-untyped]
            TopicAlreadyExistsError,
            UnknownTopicOrPartitionError,
        )

        unknown_topic_errno = int(UnknownTopicOrPartitionError.errno)
        topic_exists_errno = int(TopicAlreadyExistsError.errno)
    except ImportError:
        # Stable Kafka protocol codes; lets driver-less fakes speak them.
        unknown_topic_errno, topic_exists_errno = 3, 36
    try:
        if admin_client_factory is not None:
            admin = admin_client_factory()
        else:
            _load_driver()
            from kafka.admin import KafkaAdminClient  # type: ignore[import-not-found, import-untyped]

            admin = KafkaAdminClient(
                bootstrap_servers=list(normalized["bootstrap_servers"]),
                request_timeout_ms=timeout_ms,
            )
    except ImportError as exc:
        raise _error(f"Kafka admin client unavailable: {exc}") from exc
    try:
        described = admin.describe_topics([topic]) or []
        meta = described[0] if described else {}
        partitions = list(meta.get("partitions", []) or [])
        num_partitions = len(partitions) or 1
        first = partitions[0] if partitions else {}
        replicas = list(first.get("replicas", [None]) or [None])
        replication_factor = max(1, len([r for r in replicas if r is not None]))
    except Exception:
        num_partitions, replication_factor = 1, 1
    try:
        try:
            delete_response = admin.delete_topics([topic], timeout_ms=timeout_ms)
        except TypeError:
            # Older drivers take no timeout: same call, broker default.
            delete_response = admin.delete_topics([topic])
        for _, code, message in getattr(delete_response, "topic_errors", []) or []:
            if int(code) not in (0, unknown_topic_errno):
                raise _error(f"Kafka delete of topic '{topic}' failed: {message}")
        new_topic = NewTopic(topic, num_partitions, replication_factor)
        try:
            create_response = admin.create_topics([new_topic], timeout_ms=timeout_ms)
        except TypeError:
            create_response = admin.create_topics([new_topic])
        for _, code, message in getattr(create_response, "topic_errors", []) or []:
            if int(code) not in (0, topic_exists_errno):
                raise _error(f"Kafka create of topic '{topic}' failed: {message}")
    except ProviderError:
        raise
    except Exception as exc:
        raise _error(f"Kafka purge of topic '{topic}' failed: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            close = getattr(admin, "close", None)
            if callable(close):
                close()
    return {"purged": True, "partitions": num_partitions}
