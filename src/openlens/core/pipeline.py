"""Deterministic plugin pipeline: ordering validation + stage runner.

Fail-closed rule: a mandatory plugin that raises propagates (the request
fails; unfiltered results never leak). An optional plugin that raises is
isolated: the failure is recorded in ``SearchContext`` diagnostics and the
pipeline continues with the remaining plugins.
"""

from __future__ import annotations

import time
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    SearchPlugin,
)


def validate_and_order(plugins: list[SearchPlugin]) -> list[SearchPlugin]:
    """Validate dependencies/ordering and return deterministic execution order.

    Raises:
        ConfigError: duplicate names, unknown dependencies, a dependency in a
            later stage than its dependent, dependency cycles, or a plugin with
            no valid stage.
    """
    by_name: dict[str, SearchPlugin] = {}
    for plugin in plugins:
        if plugin.name in by_name:
            raise ConfigError(
                f"Duplicate plugin name '{plugin.name}'. Startup validation failed."
            )
        if not isinstance(plugin.stage, PipelineStage):
            raise ConfigError(
                f"Plugin '{plugin.name}' declares invalid stage {plugin.stage!r}."
            )
        by_name[plugin.name] = plugin

    stage_index = {stage: i for i, stage in enumerate(PipelineStage.ordered())}
    for plugin in plugins:
        for dep in plugin.dependencies:
            if dep not in by_name:
                raise ConfigError(
                    f"Plugin '{plugin.name}' depends on unknown plugin '{dep}'."
                )
            if stage_index[by_name[dep].stage] > stage_index[plugin.stage]:
                raise ConfigError(
                    f"Plugin '{plugin.name}' ({plugin.stage.value}) runs before its "
                    f"dependency '{dep}' ({by_name[dep].stage.value})."
                )

    # Stable topological sort: among ready plugins pick the earliest
    # (stage, declaration order) first, so runs are deterministic.
    position = {plugin.name: i for i, plugin in enumerate(plugins)}
    remaining = {plugin.name for plugin in plugins}
    ordered: list[SearchPlugin] = []
    while remaining:
        ready = [
            by_name[name]
            for name in remaining
            if all(dep not in remaining for dep in by_name[name].dependencies)
        ]
        if not ready:
            raise ConfigError(
                f"Circular plugin dependencies among: {sorted(remaining)}."
            )
        ready.sort(key=lambda p: (stage_index[p.stage], position[p.name]))
        chosen = ready[0]
        ordered.append(chosen)
        remaining.discard(chosen.name)
    return ordered


class Pipeline:
    """Runs plugins in validated stage order with mandatory/optional semantics."""

    def __init__(self, plugins: list[SearchPlugin] | tuple[SearchPlugin, ...]) -> None:
        self._plugins = validate_and_order(list(plugins))

    @property
    def plugins(self) -> list[SearchPlugin]:
        """Plugins in execution order."""
        return list(self._plugins)

    @property
    def stages_used(self) -> list[PipelineStage]:
        """Distinct stages covered, in execution order."""
        seen: list[PipelineStage] = []
        for plugin in self._plugins:
            if plugin.stage not in seen:
                seen.append(plugin.stage)
        return seen

    def run(self, context: SearchContext) -> SearchContext:
        """Execute every plugin against ``context`` in order.

        Mandatory-plugin failures propagate (fail closed). Optional-plugin
        failures are recorded in diagnostics and skipped over.
        """
        for plugin in self._plugins:
            started = time.perf_counter()
            try:
                updated = plugin.run(context)
                if updated is not None:
                    context = updated
            except PluginError as exc:
                latency_ms = (time.perf_counter() - started) * 1000.0
                context.record_plugin_execution(
                    plugin.name, plugin.stage, "failed", latency_ms, str(exc)
                )
                context.add_error(f"{plugin.name}: {exc}")
                if plugin.mandatory or exc.mandatory:
                    raise
                continue
            except Exception as exc:  # noqa: BLE001 — isolate optional plugins
                latency_ms = (time.perf_counter() - started) * 1000.0
                context.record_plugin_execution(
                    plugin.name, plugin.stage, "failed", latency_ms, str(exc)
                )
                context.add_error(f"{plugin.name}: {exc}")
                if plugin.mandatory:
                    raise PluginError(
                        str(exc),
                        plugin_name=plugin.name,
                        stage=plugin.stage,
                        mandatory=True,
                    ) from exc
                continue
            else:
                latency_ms = (time.perf_counter() - started) * 1000.0
                context.record_plugin_execution(
                    plugin.name, plugin.stage, "ok", latency_ms
                )
        return context


def assemble_explanations(context: SearchContext) -> SearchContext:
    """EXPLANATION stage: guarantee per-result reasons without touching scores.

    ``original_score`` is never modified here; every result keeps its
    provider signal alongside the ranked ``final_score`` plus at least one
    human-readable reason.
    """
    for result in context.results:
        if not result.reasons:
            result.reasons.append(
                f"provider score {result.original_score:.3f}, "
                f"ranked score {result.final_score:.3f}"
            )
    return context


def response_payload(context: SearchContext) -> dict[str, Any]:
    """Shared response fields assembled from pipeline context."""
    return {
        "results": list(context.results),
        "total": len(context.results),
        "applied_filters": list(context.applied_filters),
        "diagnostics": context.diagnostics(),
    }
