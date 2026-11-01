"""Date-range filter: time-boxed searches with exact totals.

Reads ``date_from`` / ``date_to`` (``YYYY-MM-DD``, inclusive) from the
request ``filters`` dict and keeps only hits whose publication date —
resolved by :func:`result_date` (``published_at`` → ``published`` →
``date_modified`` → ``date``, same keys the freshness scorer uses) —
falls inside the window. Undated hits are excluded while a bound is set: a range filter
must mean something, and the applied-filter chip says so honestly.

Bounds degrade, never fail: missing, blank, or non-string bounds are
treated as unset, and a present-but-unparseable bound deactivates the
whole range (a broken window never half-applies). With no usable
bound the plugin passes results through untouched (no applied-filter
entry). Unlike the mode-category filter there is no empty-result
fallback — zero hits is the honest answer to a range nothing matches,
and the fill loop in the engine still makes the total exact.

``sort: newest`` is NOT handled here (a filter plugin cannot outlive
the ranker); the engine orders the held window after ranking.
"""

from __future__ import annotations

import datetime as _datetime
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import PipelineStage, PluginType
from openlens.plugins._signals import result_date
from openlens.plugins.base import BasePlugin

#: Filter keys consumed by this plugin (reserved consumer keys).
DATE_FROM_KEY = "date_from"
DATE_TO_KEY = "date_to"

#: Applied-filter label surfaced to the UI/diagnostics.
APPLIED_LABEL = "date-range"


def _parse_bound(raw: Any) -> _datetime.date | None:
    """Strict ``YYYY-MM-DD`` bound; None when absent or malformed."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return _datetime.date.fromisoformat(text)
    except ValueError:
        return None


class DateRangeFilter(BasePlugin):
    """Keep only hits published inside the requested date window."""

    @property
    def name(self) -> str:
        return "date-range-filter"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    def run(self, context: SearchContext) -> SearchContext:
        filters = getattr(context.request, "filters", None)
        if not isinstance(filters, dict):
            return context
        raw_from = filters.get(DATE_FROM_KEY)
        raw_to = filters.get(DATE_TO_KEY)
        date_from = _parse_bound(raw_from)
        date_to = _parse_bound(raw_to)
        if date_from is None and date_to is None:
            return context
        # A present-but-unparseable bound deactivates the whole range:
        # half-applying a broken window would silently narrow results
        # the user never asked for.
        if (raw_from is not None and str(raw_from).strip() and date_from is None) or (
            raw_to is not None and str(raw_to).strip() and date_to is None
        ):
            return context
        kept = []
        for result in context.results:
            published = result_date(result)
            if published is None:
                continue
            day = published.date()
            if date_from is not None and day < date_from:
                continue
            if date_to is not None and day > date_to:
                continue
            kept.append(result)
        removed = len(context.results) - len(kept)
        context.applied_filters.append(APPLIED_LABEL)
        context.metadata["date-range-filter-removed"] = removed
        context.metadata["date-range-filter-kept"] = len(kept)
        context.results = kept
        return context
