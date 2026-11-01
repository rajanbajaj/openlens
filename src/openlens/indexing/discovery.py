"""Feed/sitemap discovery: find new article URLs without crawling.

News sites and blogs publish machine-readable inventories — XML
sitemaps (``<urlset>``/``<sitemapindex>``), RSS (``<rss>``) and Atom
(``<feed>``) — listing fresh URLs with dates. One cheap fetch of those
documents discovers a day's new articles; the frontier dedups against
already-known URLs, so only genuinely new pages enter the queue.

Only parsing lives here (offline, stdlib ElementTree, no new
dependencies); fetching is the worker's job so tests stay hermetic.
Fail-closed: malformed/empty documents yield ``[]``, and every
returned pair is ``(url, date_text_or_empty)``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

#: Hard cap per discovery document: a sitemap index can name thousands
#: of children; the frontier caps intake separately, but parsing stays
#: bounded here too.
_MAX_ENTRIES = 5000

#: Fetch caps: discovery documents are small by design; abort early
#: rather than downloading a multi-megabyte sitemap.
_FETCH_TIMEOUT_S = 15.0
_FETCH_MAX_BYTES = 1_000_000
_FETCH_CHUNK_BYTES = 65_536


def _local(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree keeps."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(element: Any) -> str:
    chunks = [chunk.strip() for chunk in element.itertext()]
    return " ".join(chunk for chunk in chunks if chunk).strip()


def classify_discovery_xml(xml_text: Any) -> str:
    """``"sitemap"`` for ``<urlset>``, ``"sitemap-index"`` for
    ``<sitemapindex>``, ``"feed"`` for RSS ``<rss>`` or Atom ``<feed>``,
    else ``""`` (never raises)."""
    root = _parse(xml_text)
    if root is None:
        return ""
    kind = _local(root.tag)
    if kind == "urlset":
        return "sitemap"
    if kind == "sitemapindex":
        return "sitemap-index"
    if kind in ("rss", "feed"):
        return "feed"
    return ""


def fetch_discovery_text(
    url: str,
    *,
    user_agent: str = "OpenLensBot/1.0",
    timeout_s: float = _FETCH_TIMEOUT_S,
    max_bytes: int = _FETCH_MAX_BYTES,
) -> str | None:
    """Fetch one feed/sitemap document, size-capped (``None`` on any
    failure: DNS, timeout, non-200, over-cap, decode error). Never
    raises — discovery must not fail a crawl."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        with httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        ) as client:
            with client.stream("GET", url.strip()) as response:
                if response.status_code != 200:
                    return None
                chunks: list[bytes] = []
                seen = 0
                for chunk in response.iter_bytes(_FETCH_CHUNK_BYTES):
                    seen += len(chunk)
                    if seen > max_bytes:
                        return None
                    chunks.append(chunk)
                raw = b"".join(chunks)
    except Exception:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _parse(xml_text: Any, limit: int = _MAX_ENTRIES) -> Any | None:
    if not isinstance(xml_text, str) or not xml_text.strip():
        return None
    if len(xml_text) > 5_000_000:
        return None
    try:
        # S314 is contained here: inputs are size-capped above, feed URLs
        # are admin-configured (not crawler-discovered), and every parse
        # failure is swallowed to [] by the callers — a hostile document
        # can at worst waste one bounded fetch, never crash the worker.
        return ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError:
        return None


def discover_from_sitemap(
    xml_text: Any, limit: int = _MAX_ENTRIES
) -> list[tuple[str, str]]:
    """Parse ``<urlset>`` (yields page URLs + ``lastmod``) or
    ``<sitemapindex>`` (yields child sitemap URLs for one more fetch
    round). Unknown roots and malformed XML yield ``[]``."""
    root = _parse(xml_text, limit)
    if root is None:
        return []
    kind = _local(root.tag)
    found: list[tuple[str, str]] = []
    if kind == "urlset":
        for url_element in root.iter():
            if _local(url_element.tag) != "url":
                continue
            loc = ""
            lastmod = ""
            for child in url_element:
                name = _local(child.tag)
                if name == "loc" and not loc:
                    loc = _text(child)
                elif name == "lastmod" and not lastmod:
                    lastmod = _text(child)
            if loc:
                found.append((loc, lastmod))
            if len(found) >= limit:
                break
        return found
    if kind == "sitemapindex":
        for sm_element in root.iter():
            if _local(sm_element.tag) != "sitemap":
                continue
            for child in sm_element:
                if _local(child.tag) == "loc":
                    loc = _text(child)
                    if loc:
                        found.append((loc, ""))
                    break
            if len(found) >= limit:
                break
        return found
    return []


def lastmod_hint_priority(date_text: Any) -> int:
    """Map a sitemap ``<lastmod>`` to a dequeue priority hint (0..2).

    Scheduling hint only: the caller uses it to order freshly
    discovered URLs in the frontier queue. The value is never stored
    as a content date and never written to document metadata (content
    dates come from the fetched page alone). Fresh entries (``<= 7``
    days old) float above the bulk backlog at ``2``, recent ones
    (``<= 30`` days) at ``1``; missing, unparseable, or future values
    degrade to ``0`` (neutral, never raised).
    """
    if not isinstance(date_text, str) or not date_text.strip():
        return 0
    try:
        stamped = datetime.fromisoformat(date_text.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - stamped).total_seconds() / 86400.0
    if age_days < 0:
        return 0
    if age_days <= 7:
        return 2
    if age_days <= 30:
        return 1
    return 0


def discover_from_feed(
    xml_text: Any, limit: int = _MAX_ENTRIES
) -> list[tuple[str, str]]:
    """Parse RSS ``<item>`` (``link`` + ``pubDate``) or Atom ``<entry>``
    (``link href`` + ``updated``/``published``). Unknown roots and
    malformed XML yield ``[]``."""
    root = _parse(xml_text, limit)
    if root is None:
        return []
    kind = _local(root.tag)
    found: list[tuple[str, str]] = []
    if kind == "rss":
        for item in root.iter():
            if _local(item.tag) != "item":
                continue
            link = ""
            date = ""
            for child in item:
                name = _local(child.tag)
                if name == "link" and not link:
                    link = _text(child)
                elif name in ("pubDate", "date") and not date:
                    date = _text(child)
            if link:
                found.append((link, date))
            if len(found) >= limit:
                break
        return found
    if kind == "feed":
        for entry in root.iter():
            if _local(entry.tag) != "entry":
                continue
            link = ""
            date = ""
            for child in entry:
                name = _local(child.tag)
                if name == "link" and not link:
                    href = (child.attrib.get("href") or "").strip()
                    if href:
                        link = href
                elif name in ("updated", "published") and not date:
                    date = _text(child)
            if link:
                found.append((link, date))
            if len(found) >= limit:
                break
        return found
    return []
