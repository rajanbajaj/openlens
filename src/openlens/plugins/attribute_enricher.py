"""Attribute enricher: normalize product/hotel/article metadata.

ENRICHMENT-stage, optional plugin. Parses raw per-result metadata once into
normalized keys that downstream rerankers read directly (rerankers never
re-parse raw metadata):

- ``price``: float when parseable, else ``None`` (missing). Unparseable
  values keep the raw string in place plus a warning.
- ``currency``: upper-cased ``str`` (e.g. ``"USD"``), else ``None``.
  Unparseable values keep the raw value plus a warning.
- ``location``: stripped ``str``, else ``None``. Compared case-insensitively
  downstream.
- ``facilities``: ``list[str]`` of lower-cased stripped names, else ``[]``
  (missing). A comma-separated string is split; unparseable values keep the
  raw value plus a warning.
- ``published``: normalized ``"YYYY-MM-DD"`` date string, else ``None``.
  Accepts ``date``/``datetime`` objects and common string formats.
  Unparseable values keep the raw string plus a warning.
- ``perspective``: lower-cased ``str`` label, default ``"unknown"`` when
  missing or blank. Unparseable (non-string) values keep the raw value
  plus a warning.
- ``license``/``author``/``cuisine``/``isbn``/``publisher``: stripped
  ``str`` or ``None`` (missing). Unparseable values keep the raw value
  plus a warning.
- ``ingredients``/``dietary_tags``/``allergens``: ``list[str]`` of
  lower-cased stripped names, else ``[]`` (missing). A comma-separated
  string is split; unparseable values keep the raw value plus a warning.
- ``rating``: float when parseable, else ``None`` (missing).
- ``rating_count``: int when parseable, else ``None`` (missing).
- ``brand``/``availability``/``booking_url``/``style``/``size``/
  ``occasion``: stripped ``str``, else ``None`` (missing).

Missing attributes degrade to the documented neutral sentinels above.
Unparseable values keep the raw value plus a warning and never raise.
This plugin never emits trust warnings (no ``trust``/``unverified``/
``distrusted`` wording), so it never overlaps with the trust-enricher.
Deterministic: results are processed in incoming order with no randomness,
wall-clock reads, or cross-request state.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import PipelineStage, PluginType
from openlens.plugins.base import BasePlugin

_MISSING: frozenset[str] = frozenset(
    {
        "price",
        "currency",
        "location",
        "facilities",
        "published",
        "perspective",
        "rating",
        "rating_count",
        "brand",
        "availability",
        "booking_url",
        "style",
        "size",
        "occasion",
        "license",
        "author",
        "cuisine",
        "isbn",
        "publisher",
        "ingredients",
        "dietary_tags",
        "allergens",
    }
)

_PUBLISHED_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d %b %Y",
    "%b %d, %Y",
    "%m/%d/%Y",
)


def _warn(result: Any, message: str) -> None:
    result.warnings.append(f"attribute-enricher: {message}")


def _normalize_price(value: Any) -> tuple[Any, bool]:
    """Return (normalized, ok). Normalized is float or None (missing)."""
    if value is None:
        return None, True
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)):
        return float(value), True
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None, True
        cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
        if not cleaned or cleaned in {"-", ".", "-."}:
            return value, False
        try:
            return float(cleaned), True
        except ValueError:
            return value, False
    return value, False


def _normalize_currency(value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, True
    if isinstance(value, str):
        text = value.strip().upper()
        if not text:
            return None, True
        return text, True
    return value, False


def _normalize_location(value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, True
        return text, True
    return value, False


def _normalize_facilities(value: Any) -> tuple[Any, bool]:
    if value is None:
        return [], True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [], True
        return [part.strip().lower() for part in text.split(",") if part.strip()], True
    if isinstance(value, (list, tuple)):
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                normalized.append(item.strip().lower())
            else:
                return value, False
        return normalized, True
    return value, False


def _normalize_published(value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, True
    if isinstance(value, datetime):
        return value.date().isoformat(), True
    if isinstance(value, date):
        return value.isoformat(), True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, True
        try:
            return datetime.fromisoformat(text).date().isoformat(), True
        except ValueError:
            pass
        for fmt in _PUBLISHED_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat(), True
            except ValueError:
                continue
        return value, False
    return value, False


def _normalize_perspective(value: Any) -> tuple[Any, bool]:
    if value is None:
        return "unknown", True
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return "unknown", True
        return text, True
    return value, False


def _normalize_rating(value: Any) -> tuple[Any, bool]:
    """Rating float or None (missing); raw kept plus warning when odd."""
    if value is None:
        return None, True
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)):
        return float(value), True
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None, True
        try:
            return float(text), True
        except ValueError:
            return value, False
    return value, False


def _normalize_rating_count(value: Any) -> tuple[Any, bool]:
    """Rating-count int or None (missing); raw kept plus warning when odd."""
    if value is None:
        return None, True
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)):
        return int(value), True
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None, True
        try:
            return int(float(text)), True
        except ValueError:
            return value, False
    return value, False


def _normalize_tag_list(value: Any) -> tuple[Any, bool]:
    """Lower-cased stripped name list, or [] (missing)."""
    if value is None:
        return [], True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [], True
        return [part.strip().lower() for part in text.split(",") if part.strip()], True
    if isinstance(value, (list, tuple)):
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                normalized.append(item.strip().lower())
            else:
                return value, False
        return normalized, True
    return value, False


def _normalize_text_field(value: Any) -> tuple[Any, bool]:
    """Stripped string or None (missing); raw kept plus warning when odd."""
    if value is None:
        return None, True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, True
        return text, True
    return value, False


_NORMALIZERS = {
    "price": (_normalize_price, "price"),
    "currency": (_normalize_currency, "currency"),
    "location": (_normalize_location, "location"),
    "facilities": (_normalize_facilities, "facilities"),
    "published": (_normalize_published, "published"),
    "perspective": (_normalize_perspective, "perspective"),
    "rating": (_normalize_rating, "rating"),
    "rating_count": (_normalize_rating_count, "rating_count"),
    "brand": (_normalize_text_field, "brand"),
    "availability": (_normalize_text_field, "availability"),
    "booking_url": (_normalize_text_field, "booking_url"),
    "style": (_normalize_text_field, "style"),
    "size": (_normalize_text_field, "size"),
    "occasion": (_normalize_text_field, "occasion"),
    "license": (_normalize_text_field, "license"),
    "author": (_normalize_text_field, "author"),
    "cuisine": (_normalize_text_field, "cuisine"),
    "isbn": (_normalize_text_field, "isbn"),
    "publisher": (_normalize_text_field, "publisher"),
    "ingredients": (_normalize_tag_list, "ingredients"),
    "dietary_tags": (_normalize_tag_list, "dietary_tags"),
    "allergens": (_normalize_tag_list, "allergens"),
}


class AttributeEnricher(BasePlugin):
    """Normalize product/hotel/article metadata into documented keys."""

    @property
    def name(self) -> str:
        return "attribute-enricher"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.ENRICHER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.ENRICHMENT

    @property
    def mandatory(self) -> bool:
        return False

    def run(self, context: SearchContext) -> SearchContext:
        for result in context.results:
            for key in sorted(_MISSING):
                normalize, label = _NORMALIZERS[key]
                raw = result.metadata.get(key, None)
                normalized, ok = normalize(raw)
                if ok:
                    result.metadata[key] = normalized
                else:
                    result.metadata[key] = raw
                    _warn(result, f"unparseable {label} {raw!r}; kept raw value.")
        return context
