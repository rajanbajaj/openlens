"""Mode-category filter: category modes return categorical results.

Ranking profiles only rescore; without this filter ``news`` / ``shopping`` /
``travel`` modes return the identical result set as ``default`` (e.g. code
hosts presented as news). This FILTERING plugin keeps, per mode, only hits
whose indexed ``site_category`` metadata matches — ``travel`` has no
category value, so it matches the ranking profile's travel keywords
against title/snippet/url instead.

Fail-open on empty: when the filter would remove every hit (common on a
corpus with no categorized pages yet), the original results are served
with a ``mode-category-filter-fallback`` metadata flag so the UI can say
so honestly. ``child-safe`` is not handled here — the safety filter owns
that mode and must never fall back.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import PipelineStage, PluginType
from openlens.plugins.base import BasePlugin

#: Modes this filter acts on. ``travel`` has no ``site_category`` value;
#: it matches against text instead (same keywords the travel profile boosts).
MODE_CATEGORY: dict[str, str] = {
    "news": "news",
    "shopping": "shopping",
}

#: Text fallback for ``travel`` (mirrors the travel ranking keywords).
TRAVEL_KEYWORDS: tuple[str, ...] = ("flight", "hotel", "guide", "itinerary", "trip")


def _metadata(result: Any) -> dict[str, Any]:
    metadata = getattr(result, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _site_category(result: Any) -> str:
    category = _metadata(result).get("site_category", "")
    return category.lower() if isinstance(category, str) else ""


def _matches_travel(result: Any) -> bool:
    haystack = " ".join(
        part
        for part in (
            getattr(result, "title", ""),
            getattr(result, "snippet", ""),
            getattr(result, "url", ""),
        )
        if isinstance(part, str)
    ).lower()
    return any(keyword in haystack for keyword in TRAVEL_KEYWORDS)


class ModeCategoryFilter(BasePlugin):
    """Keep only hits matching the request mode's content category."""

    @property
    def name(self) -> str:
        return "mode-category-filter"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    def run(self, context: SearchContext) -> SearchContext:
        mode = context.request.mode or ""
        if mode not in MODE_CATEGORY and mode != "travel":
            return context
        kept = [
            result
            for result in context.results
            if (
                _matches_travel(result)
                if mode == "travel"
                else _site_category(result) == MODE_CATEGORY[mode]
            )
        ]
        removed = len(context.results) - len(kept)
        context.applied_filters.append(self.name)
        context.metadata["mode-category-filter-removed"] = removed
        if not kept and context.results:
            context.metadata["mode-category-filter-fallback"] = True
            return context
        context.results = kept
        return context
