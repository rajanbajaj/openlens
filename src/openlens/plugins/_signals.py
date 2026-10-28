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
