"""In-process Prometheus-text metrics registry (observability plan, step 2).

Stdlib only — no ``prometheus_client`` dependency. The registry is fed
from exactly one point: the API route after ``engine.search`` returns,
reading ``SearchResponse.diagnostics`` via :meth:`MetricsRegistry.observe`.
It never hooks providers or plugins directly.

Cardinality is bounded by construction: only the fixed label names in
:data:`ALLOWED_LABEL_NAMES` may appear (``provider``, ``plugin``,
``stage``, ``status``, plus ``le`` for the fixed histogram buckets).
Request-derived text (query strings, doc ids, error messages) is never
used as a label name or value. Rendering snapshots counters under a
:class:`threading.Lock` and formats the exposition text after the lock
is released, so scrapes never block searches longer than a dict copy.
"""

from __future__ import annotations

import threading
from typing import Any

#: Fixed label set. Every label emitted by :meth:`MetricsRegistry.render`
#: uses one of these names; anything else raises ``ValueError`` by
#: construction. ``le`` is the standard histogram bucket-bound label and
#: is fixed (its values are :data:`LATENCY_BUCKETS_MS`, never request data).
ALLOWED_LABEL_NAMES = frozenset({"provider", "plugin", "stage", "status", "le"})

#: Fixed histogram bucket bounds in milliseconds (last bound is +Inf).
LATENCY_BUCKETS_MS: tuple[float, ...] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    float("inf"),
)

_REQUESTS_METRIC = "openlens_requests_total"
_PROVIDER_LATENCY_SUM_METRIC = "openlens_provider_latency_ms_sum"
_PROVIDER_LATENCY_COUNT_METRIC = "openlens_provider_latency_ms_count"
_PROVIDER_LATENCY_BUCKET_METRIC = "openlens_provider_latency_ms_bucket"
_PROVIDER_ERRORS_METRIC = "openlens_provider_errors_total"
_PLUGIN_LATENCY_SUM_METRIC = "openlens_plugin_latency_ms_sum"
_PLUGIN_LATENCY_COUNT_METRIC = "openlens_plugin_latency_ms_count"
_PLUGIN_STATUS_METRIC = "openlens_plugin_executions_total"


def escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus exposition format."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_labels(labels: dict[str, str]) -> str:
    """Render ``{name="value", ...}`` for a fixed-label mapping.

    Raises:
        ValueError: if any label name is outside :data:`ALLOWED_LABEL_NAMES`.
    """
    for name in labels:
        if name not in ALLOWED_LABEL_NAMES:
            raise ValueError(
                f"unknown label name {name!r}; allowed: {sorted(ALLOWED_LABEL_NAMES)}"
            )
    inner = ",".join(
        f'{name}="{escape_label_value(str(val))}"'
        for name, val in sorted(labels.items())
    )
    return "{" + inner + "}" if inner else ""


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        if result != result:  # NaN guard: never record NaN into buckets
            return None
        return result
    return None


def _format_bound(bound: float) -> str:
    if bound == float("inf"):
        return "+Inf"
    if bound.is_integer():
        return str(int(bound))
    return repr(bound)


class MetricsRegistry:
    """Thread-safe fixed-label metrics registry.

    Disabled by default: :meth:`observe` is a no-op and :meth:`render`
    returns ``""`` until :meth:`enable` is called (wiring deferred to the
    integrate step owns the toggle).
    """

    def __init__(self, enabled: bool = False) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._requests_total: dict[str, int] = {}
        self._provider_latency_sum: dict[str, float] = {}
        self._provider_latency_count: dict[str, int] = {}
        self._provider_latency_buckets: dict[str, list[int]] = {}
        self._provider_errors: dict[str, int] = {}
        self._plugin_latency_sum: dict[tuple[str, str], float] = {}
        self._plugin_latency_count: dict[tuple[str, str], int] = {}
        self._plugin_status: dict[tuple[str, str, str], int] = {}

    @property
    def enabled(self) -> bool:
        """Return whether this registry records observations."""
        with self._lock:
            return self._enabled

    def enable(self) -> None:
        """Enable recording (the integrate step wires this to config)."""
        with self._lock:
            self._enabled = True

    def disable(self) -> None:
        """Disable recording; previously recorded series are kept."""
        with self._lock:
            self._enabled = False

    def reset(self) -> None:
        """Clear all recorded series (tests only)."""
        with self._lock:
            self._requests_total = {}
            self._provider_latency_sum = {}
            self._provider_latency_count = {}
            self._provider_latency_buckets = {}
            self._provider_errors = {}
            self._plugin_latency_sum = {}
            self._plugin_latency_count = {}
            self._plugin_status = {}

    def observe(self, diagnostics: dict[str, Any]) -> None:
        """Record one search from a ``SearchResponse.diagnostics`` payload.

        Only the known diagnostics keys are read
        (``provider_latencies_ms``, ``plugin_executions``, ``errors``);
        every other key — including any request-derived text — is ignored,
        so adversarial query content can never become a series or label.
        Never raises on malformed payloads: the metrics path must not fail
        the request path.
        """
        if not isinstance(diagnostics, dict):
            return
        with self._lock:
            if not self._enabled:
                return
            try:
                self._observe_locked(diagnostics)
            except Exception:  # noqa: BLE001 — metrics must never fail requests
                return

    def _observe_locked(self, diagnostics: dict[str, Any]) -> None:
        latencies = diagnostics.get("provider_latencies_ms", {})
        if not isinstance(latencies, dict):
            latencies = {}
        executions = diagnostics.get("plugin_executions", [])
        if not isinstance(executions, list):
            executions = []
        errors = diagnostics.get("errors", [])
        if not isinstance(errors, list):
            errors = []

        providers = {str(name) for name in latencies}
        for name, raw_latency in latencies.items():
            latency = _as_float(raw_latency)
            if latency is None or latency < 0:
                continue
            provider = str(name)
            self._provider_latency_sum[provider] = (
                self._provider_latency_sum.get(provider, 0.0) + latency
            )
            self._provider_latency_count[provider] = (
                self._provider_latency_count.get(provider, 0) + 1
            )
            buckets = self._provider_latency_buckets.get(provider)
            if buckets is None:
                buckets = [0] * len(LATENCY_BUCKETS_MS)
                self._provider_latency_buckets[provider] = buckets
            for index, bound in enumerate(LATENCY_BUCKETS_MS):
                if latency <= bound:
                    buckets[index] += 1

        for entry in executions:
            if not isinstance(entry, dict):
                continue
            plugin = entry.get("plugin")
            stage = entry.get("stage")
            status = entry.get("status")
            if not isinstance(plugin, str) or not isinstance(stage, str):
                continue
            if not isinstance(status, str) or not status:
                status = "unknown"
            latency = _as_float(entry.get("latency_ms", 0.0))
            if latency is None or latency < 0:
                latency = 0.0
            key = (plugin, stage)
            self._plugin_latency_sum[key] = (
                self._plugin_latency_sum.get(key, 0.0) + latency
            )
            self._plugin_latency_count[key] = self._plugin_latency_count.get(key, 0) + 1
            status_key = (plugin, stage, status)
            self._plugin_status[status_key] = self._plugin_status.get(status_key, 0) + 1

        error_count = sum(1 for item in errors if isinstance(item, str))
        for item in errors:
            if not isinstance(item, str):
                continue
            for provider in providers:
                if item == provider or item.startswith(provider + ":"):
                    self._provider_errors[provider] = (
                        self._provider_errors.get(provider, 0) + 1
                    )
                    break

        status_label = "error" if error_count else "ok"
        self._requests_total[status_label] = (
            self._requests_total.get(status_label, 0) + 1
        )
        for provider in providers:
            if provider not in self._provider_errors:
                self._provider_errors.setdefault(provider, 0)

    def render(self) -> str:
        """Render Prometheus text exposition; ``""`` when disabled or empty.

        Counters are snapshotted under the lock; the text is built after
        the lock is released so a scrape never blocks the request path.
        """
        with self._lock:
            if not self._enabled:
                return ""
            snapshot = (
                dict(self._requests_total),
                dict(self._provider_latency_sum),
                dict(self._provider_latency_count),
                {
                    name: list(counts)
                    for name, counts in self._provider_latency_buckets.items()
                },
                dict(self._provider_errors),
                dict(self._plugin_latency_sum),
                dict(self._plugin_latency_count),
                dict(self._plugin_status),
            )
        (
            requests_total,
            provider_sum,
            provider_count,
            provider_buckets,
            provider_errors,
            plugin_sum,
            plugin_count,
            plugin_status,
        ) = snapshot
        if (
            not requests_total
            and not provider_sum
            and not plugin_sum
            and not plugin_status
            and not provider_errors
        ):
            return ""
        lines: list[str] = []
        lines.append(f"# HELP {_REQUESTS_METRIC} Total search requests observed.")
        lines.append(f"# TYPE {_REQUESTS_METRIC} counter")
        for status in sorted(requests_total):
            lines.append(
                f"{_REQUESTS_METRIC}{format_labels({'status': status})} "
                f"{requests_total[status]}"
            )
        if provider_sum or provider_count:
            lines.append(
                f"# HELP {_PROVIDER_LATENCY_SUM_METRIC} "
                "Per-provider search latency sum in milliseconds."
            )
            lines.append(f"# TYPE {_PROVIDER_LATENCY_SUM_METRIC} counter")
            for provider in sorted(provider_sum):
                lines.append(
                    f"{_PROVIDER_LATENCY_SUM_METRIC}"
                    f"{format_labels({'provider': provider})} "
                    f"{provider_sum[provider]!r}"
                )
            lines.append(
                f"# HELP {_PROVIDER_LATENCY_COUNT_METRIC} "
                "Per-provider search latency sample count."
            )
            lines.append(f"# TYPE {_PROVIDER_LATENCY_COUNT_METRIC} counter")
            for provider in sorted(provider_count):
                lines.append(
                    f"{_PROVIDER_LATENCY_COUNT_METRIC}"
                    f"{format_labels({'provider': provider})} "
                    f"{provider_count[provider]}"
                )
            lines.append(
                f"# HELP {_PROVIDER_LATENCY_BUCKET_METRIC} "
                "Per-provider search latency fixed-bucket histogram counts."
            )
            lines.append(f"# TYPE {_PROVIDER_LATENCY_BUCKET_METRIC} counter")
            for provider in sorted(provider_buckets):
                counts = provider_buckets[provider]
                for bound, count in zip(LATENCY_BUCKETS_MS, counts):
                    lines.append(
                        f"{_PROVIDER_LATENCY_BUCKET_METRIC}"
                        f"{format_labels({'provider': provider, 'le': _format_bound(bound)})} "  # noqa: E501
                        f"{count}"
                    )
        if provider_errors:
            lines.append(
                f"# HELP {_PROVIDER_ERRORS_METRIC} Per-provider attributed error count."
            )
            lines.append(f"# TYPE {_PROVIDER_ERRORS_METRIC} counter")
            for provider in sorted(provider_errors):
                lines.append(
                    f"{_PROVIDER_ERRORS_METRIC}"
                    f"{format_labels({'provider': provider})} "
                    f"{provider_errors[provider]}"
                )
        if plugin_sum or plugin_count:
            lines.append(
                f"# HELP {_PLUGIN_LATENCY_SUM_METRIC} "
                "Per-plugin execution latency sum in milliseconds."
            )
            lines.append(f"# TYPE {_PLUGIN_LATENCY_SUM_METRIC} counter")
            for plugin, stage in sorted(plugin_sum):
                lines.append(
                    f"{_PLUGIN_LATENCY_SUM_METRIC}"
                    f"{format_labels({'plugin': plugin, 'stage': stage})} "
                    f"{plugin_sum[(plugin, stage)]!r}"
                )
            lines.append(
                f"# HELP {_PLUGIN_LATENCY_COUNT_METRIC} Per-plugin execution count."
            )
            lines.append(f"# TYPE {_PLUGIN_LATENCY_COUNT_METRIC} counter")
            for plugin, stage in sorted(plugin_count):
                lines.append(
                    f"{_PLUGIN_LATENCY_COUNT_METRIC}"
                    f"{format_labels({'plugin': plugin, 'stage': stage})} "
                    f"{plugin_count[(plugin, stage)]}"
                )
        if plugin_status:
            lines.append(
                f"# HELP {_PLUGIN_STATUS_METRIC} "
                "Per-plugin executions by status (exclusions are non-ok)."
            )
            lines.append(f"# TYPE {_PLUGIN_STATUS_METRIC} counter")
            for plugin, stage, status in sorted(plugin_status):
                lines.append(
                    f"{_PLUGIN_STATUS_METRIC}"
                    f"{format_labels({'plugin': plugin, 'stage': stage, 'status': status})} "  # noqa: E501
                    f"{plugin_status[(plugin, stage, status)]}"
                )
        return "\n".join(lines) + "\n"


#: Process-wide default registry (disabled by default; the integrate step
#: wires it to ``metrics.enabled`` and the ``GET /metrics`` route).
registry = MetricsRegistry(enabled=False)


def get_registry() -> MetricsRegistry:
    """Return the process-wide default registry."""
    return registry
