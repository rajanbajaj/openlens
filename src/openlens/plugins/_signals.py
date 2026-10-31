"""Shared pure signal helpers for SCORING-stage plugins.

No I/O, no network, no config access: pure functions over
``SearchResult.metadata`` only. Later scorers (trust, freshness,
preference) share this namespace contract; the ranker consumes it
with neutral defaults for missing keys.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from openlens.core.interfaces import SearchResult

NEUTRAL_SIGNAL = 0.5
MIN_SIGNAL = 0.0
MAX_SIGNAL = 1.0
SIGNALS_KEY = "signals"
DATE_KEYS = ("published_at", "published", "date")


def clamp_signal(value: Any) -> float:
    """Coerce any value to a 0..1 signal; non-numeric becomes neutral."""
    if isinstance(value, bool):
        return NEUTRAL_SIGNAL
    try:
        number = float(value)
    except (TypeError, ValueError):
        return NEUTRAL_SIGNAL
    if math.isnan(number):
        return NEUTRAL_SIGNAL
    if number < MIN_SIGNAL:
        return MIN_SIGNAL
    if number > MAX_SIGNAL:
        return MAX_SIGNAL
    return number


def get_signal(result: SearchResult, name: str) -> float:
    """Read a named signal; missing/malformed entries return neutral 0.5."""
    metadata = result.metadata or {}
    signals = metadata.get(SIGNALS_KEY)
    if not isinstance(signals, dict):
        return NEUTRAL_SIGNAL
    if name not in signals:
        return NEUTRAL_SIGNAL
    return clamp_signal(signals[name])


def set_signal(result: SearchResult, name: str, value: Any) -> float:
    """Write a named signal clamped to 0..1; returns the stored value."""
    clamped = clamp_signal(value)
    signals = result.metadata.get(SIGNALS_KEY)
    if not isinstance(signals, dict):
        signals = {}
        result.metadata[SIGNALS_KEY] = signals
    signals[name] = clamped
    return clamped


def extract_domain(url: str) -> str:
    """Return the normalized hostname for a URL, or "" when unusable.

    Lowercased, port/userinfo stripped, trailing dot removed, and
    IDN (unicode) hostnames folded to ASCII (punycode) so that
    ``MÜNCHEN.DE`` and ``xn--mnchen-3ya.de`` compare equal.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "//" + candidate
    try:
        host = urlparse(candidate).hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".").lower()
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


#: Metadata keys carrying a provider-declared sponsored marker. ``sponsored``
#: is canonical (emitted by the offline crawler extractor from
#: ``rel="sponsored"`` markup); ``is_sponsored`` is the federated-provider
#: spelling. Both are read so provider-declared markers survive the
#: retrieval normalize boundary (which copies document metadata wholesale)
#: and stay flaggable downstream.
SPONSORED_MARKER_KEYS: tuple[str, ...] = ("sponsored", "is_sponsored")

#: Truthy string spellings for opt-in flags (``exclude_sponsored``,
#: sponsored markers). Mirrors the filter-pack ``_is_truthy`` set so the
#: filter and scorer agree on every spelling.
TRUTHY_STRINGS: frozenset[str] = frozenset({"true", "1", "yes"})


def is_truthy_flag(value: Any) -> bool:
    """True for explicit opt-ins: True / 1 / "true"/"1"/"yes".

    Case-insensitive with surrounding whitespace stripped. Bool True
    passes; every other type (False, 0, None, lists, dicts) is falsy.
    """
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return False


def is_sponsored_hit(metadata: dict[str, Any]) -> bool:
    """True when any provider-declared sponsored marker is truthy.

    Reads every key in ``SPONSORED_MARKER_KEYS`` with ``is_truthy_flag``
    semantics, so ``1``/``"true"``/``"yes"`` spellings from federated
    providers flag exactly like canonical ``sponsored: true``.
    """
    if not isinstance(metadata, dict):
        return False
    return any(is_truthy_flag(metadata.get(key)) for key in SPONSORED_MARKER_KEYS)


def coerce_sponsored_marker(metadata: dict[str, Any]) -> bool:
    """Canonicalize a truthy sponsored marker to real ``True`` in place.

    When ``is_sponsored_hit`` fires but ``metadata["sponsored"]`` is not
    already boolean ``True``, writes ``metadata["sponsored"] = True`` so
    downstream ``is True`` consumers keep working. Returns whether the
    hit is sponsored. The user-restrictions filter calls this at its
    boundary (the earliest owned touchpoint: enrichment runs later), so
    the scorer and ranker observe the canonical flag.
    """
    if not isinstance(metadata, dict):
        return False
    if is_sponsored_hit(metadata):
        metadata["sponsored"] = True
        return True
    return False


def parse_date(value: Any) -> Optional[datetime]:
    """Parse a pinned date value; None when absent or unparseable.

    Accepts ISO-8601 strings (including a trailing ``Z``) and epoch
    seconds as int/float. Naive datetimes are assumed UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def result_date(result: SearchResult) -> Optional[datetime]:
    """Return the result's publication date from the first present key.

    Key order is ``published_at``, ``published``, ``date``. None when
    no key is present or the present value is unparseable.
    """
    metadata = result.metadata or {}
    for key in DATE_KEYS:
        if key not in metadata or metadata[key] is None:
            continue
        return parse_date(metadata[key])
    return None
