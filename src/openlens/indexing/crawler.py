"""Polite web fetcher producing raw documents for the indexer.

Step 3 of the crawler-index plan. :class:`PoliteCrawler` crawls seed URLs
and returns :class:`Document` objects ready for
:class:`DocumentIndexer`. Every HTTP call goes through ``httpx`` with an
explicit ``timeout_s`` (mirroring the ``providers/base.py``
``timeout`` + ``run_bounded`` pattern: the client timeout bounds the
socket while a daemon-thread join bounds the whole fetch, so a hung host
can never stall a crawl unbounded).

Politeness and bounds:

- ``robots.txt`` is fetched at most once per host per :meth:`crawl`
  (bounded LRU cache, cleared on every crawl) and both ``Disallow`` and
  ``Crawl-delay`` are honored.
- A per-domain last-fetch timestamp map (bounded LRU) enforces
  ``max(configured crawl_delay_s, robots Crawl-delay)`` between hits.
- ``max_pages`` caps fetched pages; response bodies stream with a
  ``max_bytes`` abort cap (never buffered past the cap); only
  ``allowed_content_types`` are indexed.
- ``allow_domains`` / ``deny_domains`` gate which hosts are fetched.
- ``domain_policies`` adds per-domain overrides: ``allow`` (block or
  exempt one domain), ``rate_limit_s`` (per-domain minimum gap between
  hits, taking the max with ``crawl_delay_s`` and robots Crawl-delay),
  and ``max_depth`` (link-following cap for that domain;
  ``default_max_depth`` caps the rest, null means unlimited). Malformed
  policy config raises ``ConfigError`` (fail-closed at startup).
- The frontier is a FIFO queue with on-disk backing, capped at
  ``max_queued_urls`` with oldest-first eviction counted in stats.
- Servers that send ``ETag``/``Last-Modified`` get conditional
  re-requests; a ``304`` response skips the write (hash still decides).
"""

from __future__ import annotations

import collections
import contextlib
import email.utils
import html.parser
import json
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.robotparser
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

import httpx

from openlens.core.interfaces import ConfigError, Document

#: Default per-request timeout (seconds); mirrors providers/base.py.
DEFAULT_TIMEOUT_S = 5.0

#: Hard ceiling for ``timeout_s``; larger values fail validation.
MAX_TIMEOUT_S = 300.0

DEFAULT_CRAWL_DELAY_S = 0.0
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_QUEUED_URLS = 10_000
DEFAULT_MAX_TRACKED_DOMAINS = 1_000
DEFAULT_ALLOWED_CONTENT_TYPES: tuple[str, ...] = ("text/html", "text/plain")
#: Crawler User-Agent. Wikimedia and other bot policies require a
#: descriptive agent with contact info; bare `Bot/1.0` identifiers get
#: throttled or blocked (HTTP 403) regardless of robots.txt. The contact
#: below is overridable per deployment from the frontier settings
#: dashboard (`user_agent`); keep this default honest if you fork.
DEFAULT_USER_AGENT = "OpenLensBot/1.0 (+https://github.com/rajanbajaj/openlens)"

#: No per-domain policy configured: unlimited depth, global delay only.
DEFAULT_MAX_DEPTH: int | None = None

#: Allowed per-domain policy keys inside ``domain_policies``.
ALLOWED_DOMAIN_POLICY_KEYS = frozenset({"allow", "rate_limit_s", "max_depth"})

#: Allowed ``configure()`` keys; anything else raises ConfigError because
#: the config loader itself is permissive and rejects nothing.
ALLOWED_CONFIG_KEYS = frozenset(
    {
        "timeout_s",
        "crawl_delay_s",
        "max_pages",
        "max_bytes",
        "max_queued_urls",
        "max_tracked_domains",
        "allowed_content_types",
        "allow_domains",
        "deny_domains",
        "domain_policies",
        "default_max_depth",
        "user_agent",
        "respect_robots",
        "checkpoint_interval_pages",
        "max_attempts_per_url",
        "frontier_enabled",
        "min_visible_chars",
        "reject_soft_404",
    }
)

_T = TypeVar("_T")


def published_from_last_modified(value: str | None) -> str | None:
    """Normalize an HTTP ``Last-Modified`` header to an ISO ``YYYY-MM-DD`` date.

    The freshness scorer and attribute enricher read the ``published`` key
    (ISO format only), so the raw header value is converted here, once, at
    ingest. Unparseable or missing values yield ``None`` (neutral downstream).
    """
    if not value or not value.strip():
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    return parsed.date().isoformat()


def normalize_url(url: str) -> str:
    """Normalize a URL for frontier dedup.

    Reuses the query-time ingest rule from
    ``retrieval/deduplicator.py`` (strip, drop ``#fragment``,
    lowercase, strip trailing ``/``) so ingest stores what query-time
    dedup keeps, never a divergent form. Fragment-only link variants
    are never enqueued as separate fetches.
    """
    bare, _ = urllib.parse.urldefrag(url.strip())
    return bare.lower().rstrip("/")


class CrawlError(Exception):
    """Raised when a single fetch cannot complete (timeout or transport)."""


@dataclass
class CrawlStats:
    """Per-:meth:`PoliteCrawler.crawl` counters."""

    fetched: int = 0
    skipped_robots: int = 0
    skipped_domain: int = 0
    skipped_policy: int = 0
    skipped_depth: int = 0
    skipped_content_type: int = 0
    skipped_not_modified: int = 0
    skipped_quality: int = 0
    aborted_bytes: int = 0
    timeouts: int = 0
    errors: int = 0
    evicted: int = 0


class BoundedFrontier:
    """FIFO URL queue with on-disk backing and oldest-first eviction.

    Every mutation is persisted to a sibling file (one URL per line) so
    the queue survives a process restart within a crawl; the in-memory
    deque is capped at ``maxsize`` and pushing past the cap evicts the
    oldest entry while counting evictions.
    """

    def __init__(self, maxsize: int, *, path: Path | None = None) -> None:
        if maxsize <= 0:
            raise ValueError("frontier maxsize must be positive")
        self._maxsize = maxsize
        self._items: collections.deque[str] = collections.deque()
        self.evicted = 0
        if path is None:
            tmp = tempfile.NamedTemporaryFile(
                prefix="openlens-frontier-", suffix=".urls", delete=False
            )
            self._path = Path(tmp.name)
            tmp.close()
        else:
            self._path = Path(path)
        self._persist()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def path(self) -> Path:
        return self._path

    def push(self, url: str) -> None:
        while len(self._items) >= self._maxsize:
            self._items.popleft()
            self.evicted += 1
        self._items.append(url)
        self._persist()

    def pop(self) -> str | None:
        if not self._items:
            return None
        url = self._items.popleft()
        self._persist()
        return url

    def _persist(self) -> None:
        self._path.write_text("\n".join(self._items) + "\n", encoding="utf-8")

    def close(self) -> None:
        self._path.unlink(missing_ok=True)


class PersistentFrontier:
    """FrontierStore-backed FIFO queue with the BoundedFrontier interface.

    Push/pop/__len__/evicted mirror :class:`BoundedFrontier` so the
    :meth:`PoliteCrawler.crawl` loop works unchanged; rows live in the
    persistent store instead of an in-memory deque plus a write-only
    sidecar file. ``push`` accepts an optional ``depth`` (stored on the
    queue row); ``pop_entry`` returns the full stored entry (depth,
    attempts) for the persistent crawl path.
    """

    def __init__(
        self,
        store: Any,
        maxsize: int,
        on_push: Callable[[str, int], None] | None = None,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("frontier maxsize must be positive")
        self._store = store
        self._maxsize = maxsize
        self._on_push = on_push
        self.evicted = 0

    def __len__(self) -> int:
        counts = self._store.counts()
        return int(counts.get("queued", 0)) + int(counts.get("active", 0))

    @property
    def path(self) -> None:
        return None

    def push(self, url: str, depth: int = 0) -> None:
        is_new = self._store.enqueue(url, depth)
        if is_new and self._on_push is not None:
            # Best effort: the store row above is the durable record, so a
            # failing fan-out (e.g. Kafka down) never loses the URL.
            # Re-sightings skip the fan-out: the row is already queued or
            # settled and any drain picks it up by priority, so
            # republishing would only grow the topic (a popular URL
            # re-fires once per page linking it).
            with contextlib.suppress(Exception):
                self._on_push(url, depth)
        overflow = len(self) - self._maxsize
        if overflow > 0:
            self.evicted += self._store.evict_oldest(self._maxsize)

    def pop(self) -> str | None:
        entry = self.pop_entry()
        return entry.url if entry is not None else None

    def pop_entry(self) -> Any | None:
        return self._store.dequeue()

    def close(self) -> None:
        """No-op: rows persist in the store (resume reads them back)."""


class _BoundedLru:
    """Tiny LRU map with a hard capacity (oldest-evicted on overflow)."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._items: OrderedDict[str, Any] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._items:
            return default
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key: str, value: Any) -> None:
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = value
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)


def _clean_title(raw: Any) -> str | None:
    """Normalize a title candidate; None when it carries no real title.

    Unescapes entities and collapses whitespace. Returns None for empty
    strings and for icon-label runs (no word characters at all, e.g. the
    ``<title>`` text embedded in inline SVG icons like pure symbols), so
    the caller falls through to the next cascade step.
    """
    if not isinstance(raw, str):
        return None
    text = " ".join(html.unescape(raw).split())
    if not text:
        return None
    if not re.search(r"\w", text):
        return None
    return text


def resolve_page_title(
    head_title: Any = None,
    meta_title: Any = None,
    h1: Any = None,
    url: str = "",
) -> str:
    """Title cascade: cleaned head title -> og/twitter title -> h1 -> URL.

    Each candidate passes through :func:`_clean_title`; rejected
    candidates (missing, blank, or icon-label runs) fall through to the
    next step. The URL is the last resort and is returned verbatim.
    """
    for candidate in (head_title, meta_title, h1):
        cleaned = _clean_title(candidate)
        if cleaned:
            return cleaned
    return url if isinstance(url, str) else ""


#: Minimum visible-text chars for an indexable page (thin-content guard).
DEFAULT_MIN_VISIBLE_CHARS = 200

#: Soft-404 pages are short: markers only count below this length so a
#: long legit article mentioning "page not found" never trips the gate.
_SOFT_404_MAX_CHARS = 2000

#: Error-page markers matched against title + body (case-insensitive).
_SOFT_404_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"404\s*(page\s*)?not\s*found",
        r"page\s*not\s*found",
        r"page\s*(you(\s*['’]re|\s*are)?\s*looking\s*for\s*)?"
        r"(was\s*not\s*found|does\s*not\s*exist|could\s*not\s*be\s*found|not\s*exist)",
        r"does\s*not\s*exist",
        r"no\s*longer\s*exists",
        r"may\s*have\s*been\s*(removed|deleted|moved)",
        r"error\s*404",
    )
)


def classify_content_quality(
    title: Any,
    content: Any,
    *,
    min_visible_chars: int = DEFAULT_MIN_VISIBLE_CHARS,
) -> tuple[str, str]:
    """Gate a fetched page: ``("ok", "")`` or ``(verdict, reason)``.

    ``"soft_404"``: the title/body carries error-page markers on a thin
    page (a missing page served with HTTP 200 — status checks cannot
    catch these). ``"thin_content"``: visible text below
    ``min_visible_chars``. Pure function over strings; never raises.
    """
    text = content if isinstance(content, str) else ""
    visible = len(" ".join(text.split()))
    head = title if isinstance(title, str) else ""
    if visible < _SOFT_404_MAX_CHARS:
        haystack = f"{head}\n{text[:_SOFT_404_MAX_CHARS]}"
        if any(pattern.search(haystack) for pattern in _SOFT_404_PATTERNS):
            return (
                "soft_404",
                f"soft-404 page ({visible} chars): error markers on thin content",
            )
    try:
        floor = int(min_visible_chars)
    except (TypeError, ValueError):
        floor = DEFAULT_MIN_VISIBLE_CHARS
    if visible < max(0, floor):
        return (
            "thin_content",
            f"thin content ({visible} chars < {max(0, floor)})",
        )
    return ("ok", "")


class _LinkTitleParser(html.parser.HTMLParser):
    """Collects the page title and all anchor hrefs in one pass.

    Title capture is head-only and SVG-blind: ``<title>`` text inside
    ``<body>`` (notably the ``<title>`` elements embedded in inline SVG
    icons) is ignored, as is any ``<title>`` nested inside ``<svg>``.
    Documents without an explicit ``<head>`` wrapper still capture a
    top-level ``<title>`` (implied head) until ``<body>``/``</head>``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._in_head = False
        self._saw_head = False
        self._head_closed = False
        self._saw_body = False
        self._svg_depth = 0
        self.meta_titles: dict[str, str] = {}
        self._h1_parts: list[str] = []
        self._in_h1 = False
        self._h1_done = False

    def _title_scope_open(self) -> bool:
        if self._svg_depth > 0:
            return False
        if self._in_head:
            return True
        return not self._saw_head and not self._head_closed and not self._saw_body

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "svg":
            self._svg_depth += 1
        if lowered == "head":
            self._in_head = True
            self._saw_head = True
        if lowered == "body":
            self._saw_body = True
        if lowered == "title" and self._title_scope_open():
            self._in_title = True
        if lowered == "h1" and not self._h1_done and self._svg_depth == 0:
            self._in_h1 = True
        if lowered == "meta":
            norm = {name.lower(): (value or "") for name, value in attrs}
            key = (norm.get("property") or norm.get("name") or "").strip().lower()
            content = norm.get("content", "").strip()
            if key in ("og:title", "twitter:title") and content:
                self.meta_titles.setdefault(key, content)
        if lowered == "a":
            for name, value in attrs:
                if name.lower() == "href" and value:
                    self.links.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "svg" and self._svg_depth > 0:
            self._svg_depth -= 1
        if lowered == "head":
            self._in_head = False
            self._head_closed = True
        if lowered == "title":
            self._in_title = False
        if lowered == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_h1:
            self._h1_parts.append(data)

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()

    @property
    def head_title(self) -> str:
        """Raw scoped head title (clean via :func:`_clean_title`)."""
        return self.title

    @property
    def h1(self) -> str:
        """First ``<h1>`` text outside ``<svg>`` (raw, clean on use)."""
        return "".join(self._h1_parts).strip()

    @property
    def meta_title(self) -> str:
        """``og:title`` preferred over ``twitter:title`` (raw)."""
        return self.meta_titles.get("og:title", "") or self.meta_titles.get(
            "twitter:title", ""
        )

    def resolved_title(self, url: str = "") -> str:
        """Full cascade for this parse: head -> meta -> h1 -> URL."""
        return resolve_page_title(self.title, self.meta_title, self.h1, url)


#: Tags whose text is never visible (code, not content).
_NON_VISIBLE_TAGS = frozenset({"script", "style", "noscript", "template"})


class _VisibleTextParser(html.parser.HTMLParser):
    """Collects visible text in document order, skipping code tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _NON_VISIBLE_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _NON_VISIBLE_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._chunks).split())


def extract_visible_text(html_text: str) -> str:
    """Visible text of an HTML page (no tags, scripts, or styles).

    Never raises: unparseable input yields ``""`` and the caller keeps
    the raw body. Whitespace is collapsed so snippets, FTS, and model
    prompts consume prose, not markup.
    """
    if not isinstance(html_text, str) or not html_text:
        return ""
    parser = _VisibleTextParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return ""
    return parser.text


#: Max JSON-LD script blocks parsed per page (scalability bound).
_MAX_JSONLD_BLOCKS = 5

#: Max bytes parsed per JSON-LD script block.
_MAX_JSONLD_BYTES = 65536

#: Metadata keys the offline extractor may emit. This is the fixed
#: contract shared with the indexer preservation list
#: (``indexing/indexer.py::RANKING_METADATA_KEYS``) and the integrity
#: scorer (``plugins/integrity_scorer.py``): the crawler produces these
#: keys from fetched HTML alone — no network, no external engines.
EXTRACTED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "language",
        "author",
        "published",
        "section",
        "price",
        "currency",
        "availability",
        "brand",
        "rating",
        "rating_count",
        "isbn",
        "license",
        "publisher",
        "ingredients",
        "cuisine",
        "dietary_tags",
        "allergens",
        "total_minutes",
        "sponsored",
        "content_safety",
        "final_url",
        "redirect_chain",
        "domain_age_days",
        "booking_url",
        "style",
        "size",
        "occasion",
        "site_category",
        "site_category_confidence",
        "site_category_source",
        "kids_domain_hit",
        "news_domain_hit",
    }
)

#: Version stamp written to ``site_category_source`` by the rules
#: classifier below. Bump when the rules change so stored rows can be
#: told apart from re-runs of newer rules during backfill.
CATEGORY_RULES_VERSION = "rules-v1"

#: Allowed site-category values (mirrors the repository CHECK set).
SITE_CATEGORIES: tuple[str, ...] = ("shopping", "news", "kids", "other")

#: Category-domain config keys (see ``configs/category_domains.yaml``).
_CATEGORY_DOMAIN_KEYS: tuple[str, ...] = (
    "shopping_allow",
    "news_allow",
    "kids_allow",
    "adult_block",
)

#: Fallback when no ``category_domains.yaml`` is found: no domain claims
#: at all (fail-closed — content rules alone decide, kids never fires).
_DEFAULT_CATEGORY_DOMAINS: dict[str, list[str]] = {
    key: [] for key in _CATEGORY_DOMAIN_KEYS
}

#: Cache of loaded category-domain sets, keyed by resolved config path
#: ("" when only built-in defaults were found).
_category_domains_cache: dict[str, dict[str, frozenset[str]]] = {}


def _normalize_domain_entries(entries: Any) -> frozenset[str]:
    """Lowercase domain entries; strip whitespace, ``*.`` prefixes, paths."""
    normalized: set[str] = set()
    if not isinstance(entries, (list, tuple, set)):
        return frozenset()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if host.startswith("*."):
            host = host[2:]
        host = host.split("/")[0].strip().lstrip(".")
        if host:
            normalized.add(host)
    return frozenset(normalized)


def _category_domains_config_path(explicit: str | None = None) -> str | None:
    """Resolve the ``category_domains.yaml`` path (explicit wins)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root / "configs" / "category_domains.yaml")
    candidates.append(Path.cwd() / "configs" / "category_domains.yaml")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def load_category_domains(path: str | None = None) -> dict[str, frozenset[str]]:
    """Load category domain allow/block sets (cached, never raises).

    Reads ``configs/category_domains.yaml`` (``shopping_allow``,
    ``news_allow``, ``kids_allow``, ``adult_block``); missing or
    unparsable files yield empty sets (fail-closed). Matching is by
    registered suffix: ``example.com`` also matches ``shop.example.com``.
    """
    resolved = _category_domains_config_path(path)
    cache_key = resolved or ""
    if cache_key in _category_domains_cache:
        return _category_domains_cache[cache_key]
    raw: dict[str, frozenset[str]] = {
        key: frozenset(_DEFAULT_CATEGORY_DOMAINS[key]) for key in _CATEGORY_DOMAIN_KEYS
    }
    if resolved is not None:
        with contextlib.suppress(Exception):
            import yaml  # lazy: keeps module import light when unused

            with open(resolved, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            if isinstance(data, dict):
                for key in _CATEGORY_DOMAIN_KEYS:
                    raw[key] = _normalize_domain_entries(data.get(key))
    _category_domains_cache[cache_key] = raw
    return raw


def clear_category_domains_cache() -> None:
    """Drop the cached domain sets (tests loading different configs)."""
    _category_domains_cache.clear()


def _host_of(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        return (urllib.parse.urlsplit(url.strip()).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""


def _domain_hit(host: str, entries: frozenset[str]) -> bool:
    """True when host equals or is a subdomain of any entry."""
    if not host:
        return False
    for entry in entries:
        if host == entry or host.endswith("." + entry):
            return True
    return False


def classify_site_category(
    url: Any = None,
    metadata: dict[str, Any] | None = None,
    *,
    domains: dict[str, frozenset[str]] | None = None,
    has_product: bool = False,
    has_article: bool = False,
) -> dict[str, Any]:
    """Rules-first site-category classification (``rules-v1``).

    Evaluates every candidate and keeps the highest confidence
    (tie-break: kids > shopping > news):

    - kids: fail-closed — ``kids_allow`` domain hit AND not adult
      (``content_safety == "adult"`` or ``adult_block`` hit) -> 1.0.
      Adult pages are never kids.
    - shopping: domain hit -> 1.0; price + (availability | currency |
      brand) -> 0.9; price alone or Product type + brand -> 0.7;
      cart/checkout/product-path URL + price -> 0.6.
    - news: domain hit -> 0.9 (provisional); article type + (section |
      author & published) -> 0.85; article type alone -> 0.6.
      A 0.6 news verdict is provisional: consumers must never hard-filter
      on it, only demote or label.
    - otherwise -> ("other", 1.0).

    Always returns all five keys: ``site_category``,
    ``site_category_confidence``, ``site_category_source`` (always
    ``rules-v1``), ``kids_domain_hit`` and ``news_domain_hit`` (0/1).
    """
    meta = metadata if isinstance(metadata, dict) else {}
    domain_sets = domains if domains is not None else load_category_domains()
    host = _host_of(url)
    shopping_hit = _domain_hit(host, domain_sets.get("shopping_allow", frozenset()))
    news_hit = _domain_hit(host, domain_sets.get("news_allow", frozenset()))
    kids_hit = _domain_hit(host, domain_sets.get("kids_allow", frozenset()))
    adult_block_hit = _domain_hit(host, domain_sets.get("adult_block", frozenset()))
    adult = meta.get("content_safety") == "adult" or adult_block_hit

    price = meta.get("price")
    has_price = isinstance(price, (int, float)) and not isinstance(price, bool)
    has_avail_or_currency_or_brand = any(
        isinstance(meta.get(key), str) and str(meta.get(key)).strip()
        for key in ("availability", "currency", "brand")
    )
    brand = meta.get("brand")
    has_brand = isinstance(brand, str) and bool(brand.strip())
    url_text = url if isinstance(url, str) else ""
    commerce_path = bool(
        re.search(r"(cart|checkout|/product/|/products/|/p/|/dp/)", url_text.lower())
    )
    section = meta.get("section")
    has_section = isinstance(section, str) and bool(section.strip())
    author = meta.get("author")
    has_author = isinstance(author, str) and bool(author.strip())
    has_published = isinstance(meta.get("published"), str) and bool(
        str(meta.get("published")).strip()
    )

    candidates: list[tuple[str, float]] = []
    if kids_hit and not adult:
        candidates.append(("kids", 1.0))
    if shopping_hit:
        candidates.append(("shopping", 1.0))
    elif has_price and has_avail_or_currency_or_brand:
        candidates.append(("shopping", 0.9))
    elif commerce_path and has_price:
        # URL commerce signals alone are weak: path + price without any
        # structured commerce field stays below a bare price signal.
        candidates.append(("shopping", 0.6))
    elif has_price or (has_product and has_brand):
        candidates.append(("shopping", 0.7))
    if news_hit:
        candidates.append(("news", 0.9))
    elif has_article and (has_section or (has_author and has_published)):
        candidates.append(("news", 0.85))
    elif has_article:
        candidates.append(("news", 0.6))

    # Highest confidence wins; ties break kids > shopping > news.
    order = {"kids": 0, "shopping": 1, "news": 2}
    best: tuple[str, float] | None = None
    for category, confidence in candidates:
        if best is None:
            best = (category, confidence)
        elif (confidence, -order[category]) > (best[1], -order[best[0]]):
            best = (category, confidence)
    category, confidence = best if best is not None else ("other", 1.0)
    return {
        "site_category": category,
        "site_category_confidence": confidence,
        "site_category_source": CATEGORY_RULES_VERSION,
        "kids_domain_hit": int(kids_hit),
        "news_domain_hit": int(news_hit),
    }


#: Meta keys (``name``/``property``, lowercased) mapped to metadata fields.
_META_KEY_MAP: dict[str, str] = {
    "author": "author",
    "article:author": "author",
    "article:published_time": "published",
    "article:section": "section",
    "og:price:amount": "price",
    "product:price:amount": "price",
    "og:price:currency": "currency",
    "product:price:currency": "currency",
    "product:availability": "availability",
    "product:brand": "brand",
    "og:brand": "brand",
    "books:isbn": "isbn",
    "book:isbn": "isbn",
    "product:size": "size",
    "product:color": "style",
    "product:material": "style",
    "occasion": "occasion",
    "language": "language",
    "content-language": "language",
    "dc.language": "language",
}


class _PageDataParser(html.parser.HTMLParser):
    """Collects title, links, meta, JSON-LD, and sponsored markers in one pass.

    Purely syntactic: no network, no script execution. JSON-LD blocks are
    stored verbatim (capped in count and size); parsing happens in
    :func:`extract_page_attributes` so a malformed block never breaks the
    crawl of the page.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._in_head = False
        self._saw_head = False
        self._head_closed = False
        self._saw_body = False
        self._svg_depth = 0
        self._h1_parts: list[str] = []
        self._in_h1 = False
        self._h1_done = False
        self.meta_titles: dict[str, str] = {}
        self.lang: str | None = None
        self.meta: list[tuple[str, str]] = []
        self.time_datetimes: list[str] = []
        self.jsonld_blocks: list[str] = []
        self._jsonld_parts: list[str] | None = None
        self.sponsored_rel = False
        self.sponsored_attr = False
        self.booking_hrefs: list[str] = []

    def _title_scope_open(self) -> bool:
        if self._svg_depth > 0:
            return False
        if self._in_head:
            return True
        return not self._saw_head and not self._head_closed and not self._saw_body

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "svg":
            self._svg_depth += 1
        if lowered == "head":
            self._in_head = True
            self._saw_head = True
        if lowered == "body":
            self._saw_body = True
        if lowered == "title" and self._title_scope_open():
            self._in_title = True
        if lowered == "h1" and not self._h1_done and self._svg_depth == 0:
            self._in_h1 = True
        if lowered == "html":
            for name, value in attrs:
                if name.lower() == "lang" and value and value.strip():
                    self.lang = value.strip()
        if lowered == "a":
            href_value = ""
            rel_tokens: list[str] = []
            for name, value in attrs:
                lname = name.lower()
                if lname == "href" and value:
                    href_value = value.strip()
                    self.links.append(value.strip())
                if lname == "rel" and value:
                    rel_tokens = value.lower().split()
                    if "sponsored" in rel_tokens:
                        self.sponsored_rel = True
            if href_value and (
                "booking" in rel_tokens
                or "book-now" in rel_tokens
                or "booking" in href_value.lower()
                or "book-now" in href_value.lower()
                or "/book/" in href_value.lower()
                or "reserve" in href_value.lower()
            ):
                self.booking_hrefs.append(href_value)
        for name, value in attrs:
            if name.lower() == "data-sponsored" and value is not None:
                self.sponsored_attr = True
        if lowered == "meta":
            norm = {name.lower(): (value or "") for name, value in attrs}
            key = norm.get("property") or norm.get("name") or ""
            content = norm.get("content", "")
            if key.strip() and content:
                self.meta.append((key.strip().lower(), content.strip()))
                lowered_key = key.strip().lower()
                if lowered_key in ("og:title", "twitter:title"):
                    self.meta_titles.setdefault(lowered_key, content.strip())
        if lowered == "time":
            for name, value in attrs:
                if name.lower() == "datetime" and value and value.strip():
                    self.time_datetimes.append(value.strip())
        if lowered == "script":
            norm = {name.lower(): (value or "") for name, value in attrs}
            if norm.get("type", "").strip().lower() == "application/ld+json":
                if len(self.jsonld_blocks) < _MAX_JSONLD_BLOCKS:
                    self._jsonld_parts = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "svg" and self._svg_depth > 0:
            self._svg_depth -= 1
        if lowered == "head":
            self._in_head = False
            self._head_closed = True
        if lowered == "title":
            self._in_title = False
        if lowered == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = True
        if lowered == "script" and self._jsonld_parts is not None:
            block = "".join(self._jsonld_parts)
            if block.strip():
                self.jsonld_blocks.append(block[:_MAX_JSONLD_BYTES])
            self._jsonld_parts = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_h1:
            self._h1_parts.append(data)
        if self._jsonld_parts is not None:
            if sum(len(part) for part in self._jsonld_parts) < _MAX_JSONLD_BYTES:
                self._jsonld_parts.append(data)

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()

    @property
    def h1(self) -> str:
        """First ``<h1>`` text outside ``<svg>`` (raw, clean on use)."""
        return "".join(self._h1_parts).strip()

    @property
    def meta_title(self) -> str:
        """``og:title`` preferred over ``twitter:title`` (raw)."""
        return self.meta_titles.get("og:title", "") or self.meta_titles.get(
            "twitter:title", ""
        )

    def resolved_title(self, url: str = "") -> str:
        """Full cascade for this parse: head -> meta -> h1 -> URL."""
        return resolve_page_title(self.title, self.meta_title, self.h1, url)


def _ld_nodes(payload: Any) -> list[dict[str, Any]]:
    """Unwrap a JSON-LD payload into node dicts (follows ``@graph``)."""
    if isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            nodes: list[dict[str, Any]] = []
            for item in graph:
                nodes.extend(_ld_nodes(item))
            return nodes
        return [payload]
    if isinstance(payload, list):
        nodes = []
        for item in payload:
            nodes.extend(_ld_nodes(item))
        return nodes
    return []


def _node_types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type", "")
    if isinstance(raw, str):
        return {raw.strip().lower()} if raw.strip() else set()
    if isinstance(raw, list):
        return {str(t).strip().lower() for t in raw if str(t).strip()}
    return set()


def _name_of(value: Any) -> str | None:
    """First display name: plain strings win, ``{"name": ...}`` unwrapped."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    if isinstance(value, list):
        for item in value:
            name = _name_of(item)
            if name:
                return name
        return None
    return None


def _str_list_of(value: Any) -> list[str] | None:
    """Coerce a string or list of strings; None when nothing usable."""
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else None
    if isinstance(value, list):
        items = [
            item.strip() for item in value if isinstance(item, str) and item.strip()
        ]
        return items or None
    return None


def _last_token(value: Any) -> str | None:
    """Last URL/path token, lowercased (schema.org enum unwrapping)."""
    if not isinstance(value, str):
        value = _name_of(value)
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("/")
    if not text:
        return None
    return text.rsplit("/", 1)[-1].rsplit("#", 1)[-1].strip().lower() or None


def _to_float(value: Any) -> float | None:
    """Coerce a price/rating value; None when absent or unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = "".join(
            ch for ch in value.strip().replace(",", "") if ch.isdigit() or ch in ".-"
        )
        if not cleaned or cleaned in {"-", ".", "-."}:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def _to_iso_date(value: Any) -> str | None:
    """Normalize a date-time string to ``YYYY-MM-DD``; None when unparseable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


#: Compiled lazily on first duration parse to keep import time minimal.
_DURATION_RE: re.Pattern[str] | None = None


def _to_minutes(value: Any) -> int | None:
    """Parse an ISO-8601 duration (``PT1H30M``) into whole minutes."""
    global _DURATION_RE
    if _DURATION_RE is None:
        _DURATION_RE = re.compile(
            r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
        )
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip().upper())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, _seconds = (int(g) if g else 0 for g in match.groups())
    total = days * 1440 + hours * 60 + minutes
    return total or None


def _saw_page_signal(
    parser: _PageDataParser, out: dict[str, Any], parsed_any_node: bool
) -> bool:
    """True when the parse observed any recognizable page signal.

    Guards the always-emit contract (``sponsored`` bool, site-category
    quintet): real pages always carry signals, so they always carry the
    boundary keys; garbage input with no title, meta, JSON-LD node,
    ``<time>``, ``lang``, link, booking, h1, or sponsored marker still
    yields ``{}`` instead of a half-emitted dict.
    """
    if parsed_any_node:
        return True
    out_signal = any(
        key not in _CATEGORY_OUTPUT_KEYS for key in out if key != "sponsored"
    )
    if out_signal:
        return True
    return bool(
        parser.title
        or parser.h1
        or parser.meta
        or parser.lang
        or parser.time_datetimes
        or parser.links
        or parser.booking_hrefs
        or parser.sponsored_rel
        or parser.sponsored_attr
    )


#: Keys written by the site-category step (not page signals on their own).
_CATEGORY_OUTPUT_KEYS = frozenset(
    {
        "site_category",
        "site_category_confidence",
        "site_category_source",
        "kids_domain_hit",
        "news_domain_hit",
    }
)


def extract_page_attributes(html_text: str, url: str | None = None) -> dict[str, Any]:
    """Extract vertical metadata from raw HTML offline (no network).

    Reads JSON-LD (Product/Offer/AggregateRating, Book, Recipe,
    Article), OpenGraph/product meta tags, ``<time datetime>``, the
    ``<html lang>`` attribute, ``rel="sponsored"`` markers, and
    adult-content rating meta. Returns only keys with found values —
    except ``sponsored`` (always a real bool on real pages) and the
    site-category quintet (always classified, ``rules-v1``, on real
    pages) — so column promotion never sees a missing key; JSON-LD wins
    over meta tags, meta wins over ``<time>``. ``url`` feeds domain-hit
    category rules (content rules still apply without it). Input with no
    recognizable page signal yields ``{}``. Malformed markup or JSON
    never raises: unparseable pieces are skipped.
    """
    parser = _PageDataParser()
    try:
        parser.feed(html_text)
    except Exception:
        return {}
    out: dict[str, Any] = {}
    has_product = False
    has_article = False
    parsed_any_node = False

    def _set(key: str, value: Any) -> None:
        if key not in out and value is not None:
            out[key] = value

    for block in parser.jsonld_blocks:
        try:
            payload = json.loads(block)
        except (ValueError, RecursionError):
            continue
        for node in _ld_nodes(payload):
            if not isinstance(node, dict):
                continue
            parsed_any_node = True
            types = _node_types(node)
            if types & {"product"}:
                has_product = True
            if types & {"article", "newsarticle", "blogposting", "reportage"}:
                has_article = True
            if "author" in node or "creator" in node:
                _set("author", _name_of(node.get("author", node.get("creator"))))
            lowered = {str(k).lower(): v for k, v in node.items()}
            for key in ("datepublished", "datecreated"):
                if key in lowered:
                    raw_date = lowered[key]
                    _set(
                        "published",
                        _to_iso_date(raw_date) if isinstance(raw_date, str) else None,
                    )
                    break
            if node.get("inLanguage"):
                _set("language", str(node["inLanguage"]).strip().lower() or None)
            offers = node.get("offers")
            offer_nodes = offers if isinstance(offers, list) else [offers]
            for offer in offer_nodes:
                if not isinstance(offer, dict):
                    continue
                _set("price", _to_float(offer.get("price")))
                currency = offer.get("priceCurrency")
                if isinstance(currency, str) and currency.strip():
                    _set("currency", currency.strip().upper())
                _set("availability", _last_token(offer.get("availability")))
            rating_node = node.get("aggregateRating")
            if isinstance(rating_node, dict):
                _set("rating", _to_float(rating_node.get("ratingValue")))
                _set(
                    "rating_count",
                    _to_int(
                        rating_node.get("ratingCount", rating_node.get("reviewCount"))
                    ),
                )
            if types & {"product"}:
                _set("brand", _name_of(node.get("brand")))
                _set("size", _name_of(node.get("size")))
                style_bits: list[str] = []
                for style_key in ("color", "material", "pattern", "category"):
                    name = _name_of(node.get(style_key))
                    if name:
                        style_bits.append(name)
                if style_bits:
                    _set("style", ", ".join(style_bits))
                audience = node.get("audience")
                audience_type = (
                    audience.get("audienceType")
                    if isinstance(audience, dict)
                    else audience
                )
                occasion = _name_of(audience_type)
                if occasion:
                    _set("occasion", occasion)
            if types & {"book"}:
                _set("isbn", _name_of(node.get("isbn")))
                _set("license", _name_of(node.get("license")))
                _set("publisher", _name_of(node.get("publisher")))
            if types & {"recipe", "recipeinstruction"}:
                _set("ingredients", _str_list_of(node.get("recipeIngredient")))
                cuisine = node.get("recipeCuisine")
                _set("cuisine", _name_of(cuisine))
                diet = node.get("suitableForDiet")
                diet_items = diet if isinstance(diet, list) else [diet]
                _set(
                    "dietary_tags",
                    _str_list_of([_last_token(item) for item in diet_items]),
                )
                for raw_key, raw_value in node.items():
                    if "allergen" in str(raw_key).lower():
                        _set("allergens", _str_list_of(raw_value))
                        break
                for duration_key in ("totalTime", "cookTime", "prepTime"):
                    if duration_key in node:
                        _set("total_minutes", _to_minutes(node.get(duration_key)))
                        break
            if types & {"article", "newsarticle", "blogposting", "reportage"}:
                _set("section", _name_of(node.get("articleSection")))

    for meta_key, content in parser.meta:
        if meta_key in ("rating", "robots"):
            marker = content.lower()
            if "adult" in marker or "rta-" in marker:
                _set("content_safety", "adult")
            continue
        field = _META_KEY_MAP.get(meta_key)
        if field is None:
            continue
        if field == "price":
            _set("price", _to_float(content))
        elif field == "currency":
            _set("currency", content.strip().upper() or None)
        elif field == "published":
            _set("published", _to_iso_date(content) or content)
        elif field == "language":
            _set("language", content.strip().lower() or None)
        elif field == "isbn":
            cleaned = content.strip()
            _set("isbn", cleaned or None)
        else:
            _set(field, content or None)

    if parser.lang and "language" not in out:
        out["language"] = parser.lang.strip().lower()
    if "published" not in out:
        for stamp in parser.time_datetimes:
            dated = _to_iso_date(stamp)
            if dated:
                out["published"] = dated
                break
    # Sponsored is a real bool at the extraction boundary: downstream
    # filters branch on ``is True`` / truthiness, never on key presence.
    sponsored = bool(parser.sponsored_rel or parser.sponsored_attr)
    if parser.booking_hrefs:
        _set("booking_url", parser.booking_hrefs[0])
    out.update(
        classify_site_category(
            url, out, has_product=has_product, has_article=has_article
        )
    )
    out["sponsored"] = sponsored
    if not _saw_page_signal(parser, out, parsed_any_node):
        # Garbage in, empty out: unparseable input with no recognizable
        # page signal yields {} (never raises, never half-emits).
        return {}
    return {key: out[key] for key in sorted(out) if key in EXTRACTED_METADATA_KEYS}


@dataclass
class _FetchedPage:
    status: int
    url: str
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    bytes_aborted: bool = False
    requested_url: str = ""
    redirect_chain: list[str] = field(default_factory=list)


class PoliteCrawler:
    """Polite bounded fetcher producing raw :class:`Document` objects."""

    def __init__(self, *, queue_path: Path | str | None = None) -> None:
        self.timeout_s = DEFAULT_TIMEOUT_S
        self.crawl_delay_s = DEFAULT_CRAWL_DELAY_S
        self.max_pages = DEFAULT_MAX_PAGES
        self.max_bytes = DEFAULT_MAX_BYTES
        self.max_queued_urls = DEFAULT_MAX_QUEUED_URLS
        self.max_tracked_domains = DEFAULT_MAX_TRACKED_DOMAINS
        self.allowed_content_types = list(DEFAULT_ALLOWED_CONTENT_TYPES)
        self.allow_domains: list[str] = []
        self.deny_domains: list[str] = []
        self.domain_policies: dict[str, dict[str, Any]] = {}
        self.default_max_depth: int | None = DEFAULT_MAX_DEPTH
        self.user_agent = DEFAULT_USER_AGENT
        self.respect_robots = True
        self.checkpoint_interval_pages = 10
        self.max_attempts_per_url = 3
        self.frontier_enabled = True
        self.min_visible_chars = DEFAULT_MIN_VISIBLE_CHARS
        self.reject_soft_404 = True
        self.last_checkpoint: dict[str, Any] = {}
        self._queue_path = Path(queue_path) if queue_path else None
        self._robots_cache = _BoundedLru(DEFAULT_MAX_TRACKED_DOMAINS)
        self._politeness = _BoundedLru(DEFAULT_MAX_TRACKED_DOMAINS)
        self._conditional: dict[str, dict[str, str]] = {}
        self.last_stats = CrawlStats()

    def configure(self, config: dict[str, Any]) -> None:
        """Validate the ``indexing`` crawler section. Raise ConfigError."""
        if not isinstance(config, dict):
            raise ConfigError(
                "crawler config must be a mapping, "
                f"got {type(config).__name__}. Startup validation failed."
            )
        unknown = sorted(set(config) - ALLOWED_CONFIG_KEYS)
        if unknown:
            raise ConfigError(
                f"crawler: unknown indexing-config keys {unknown}. "
                "Startup validation failed."
            )
        timeout = config.get("timeout_s", DEFAULT_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError(
                f"crawler: 'timeout_s' must be a number, got {timeout!r}. "
                "Startup validation failed."
            )
        if not 0 < float(timeout) <= MAX_TIMEOUT_S:
            raise ConfigError(
                f"crawler: 'timeout_s' must be within (0, {MAX_TIMEOUT_S}], "
                f"got {timeout!r}. Startup validation failed."
            )
        delay = config.get("crawl_delay_s", DEFAULT_CRAWL_DELAY_S)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise ConfigError(
                f"crawler: 'crawl_delay_s' must be a number, got {delay!r}. "
                "Startup validation failed."
            )
        if not 0 <= float(delay) <= MAX_TIMEOUT_S:
            raise ConfigError(
                f"crawler: 'crawl_delay_s' must be within "
                f"[0, {MAX_TIMEOUT_S}], got {delay!r}. "
                "Startup validation failed."
            )
        for key in ("max_pages", "max_bytes", "max_queued_urls"):
            value = config.get(
                key,
                {
                    "max_pages": DEFAULT_MAX_PAGES,
                    "max_bytes": DEFAULT_MAX_BYTES,
                    "max_queued_urls": DEFAULT_MAX_QUEUED_URLS,
                }[key],
            )
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(
                    f"crawler: '{key}' must be an int, got {value!r}. "
                    "Startup validation failed."
                )
            if value <= 0:
                raise ConfigError(
                    f"crawler: '{key}' must be positive, got {value!r}. "
                    "Startup validation failed."
                )
        tracked = config.get("max_tracked_domains", DEFAULT_MAX_TRACKED_DOMAINS)
        if isinstance(tracked, bool) or not isinstance(tracked, int):
            raise ConfigError(
                f"crawler: 'max_tracked_domains' must be an int, "
                f"got {tracked!r}. Startup validation failed."
            )
        if tracked <= 0:
            raise ConfigError(
                "crawler: 'max_tracked_domains' must be positive, "
                f"got {tracked!r}. Startup validation failed."
            )
        content_types = config.get(
            "allowed_content_types", list(DEFAULT_ALLOWED_CONTENT_TYPES)
        )
        if (
            not isinstance(content_types, list)
            or not content_types
            or not all(isinstance(item, str) and item.strip() for item in content_types)
        ):
            raise ConfigError(
                "crawler: 'allowed_content_types' must be a non-empty "
                f"list of strings, got {content_types!r}. "
                "Startup validation failed."
            )
        allow = config.get("allow_domains", [])
        deny = config.get("deny_domains", [])
        for key, value in (("allow_domains", allow), ("deny_domains", deny)):
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ConfigError(
                    f"crawler: '{key}' must be a list of strings, "
                    f"got {value!r}. Startup validation failed."
                )
        policies = config.get("domain_policies", {})
        if not isinstance(policies, dict):
            raise ConfigError(
                "crawler: 'domain_policies' must be a mapping of domain "
                f"to policy, got {policies!r}. Startup validation failed."
            )
        normalized_policies: dict[str, dict[str, Any]] = {}
        for domain, policy in policies.items():
            if not isinstance(domain, str) or not domain.strip():
                raise ConfigError(
                    "crawler: 'domain_policies' keys must be non-empty "
                    f"strings, got {domain!r}. Startup validation failed."
                )
            if not isinstance(policy, dict):
                raise ConfigError(
                    f"crawler: policy for domain {domain!r} must be a "
                    f"mapping, got {policy!r}. Startup validation failed."
                )
            unknown_policy = sorted(set(policy) - ALLOWED_DOMAIN_POLICY_KEYS)
            if unknown_policy:
                raise ConfigError(
                    f"crawler: unknown policy keys {unknown_policy} for "
                    f"domain {domain!r}. Startup validation failed."
                )
            if "allow" in policy and not isinstance(policy["allow"], bool):
                raise ConfigError(
                    f"crawler: 'allow' for domain {domain!r} must be a "
                    f"bool, got {policy['allow']!r}. "
                    "Startup validation failed."
                )
            if "rate_limit_s" in policy:
                rate = policy["rate_limit_s"]
                if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                    raise ConfigError(
                        f"crawler: 'rate_limit_s' for domain {domain!r} "
                        f"must be a number, got {rate!r}. "
                        "Startup validation failed."
                    )
                if not 0 <= float(rate) <= MAX_TIMEOUT_S:
                    raise ConfigError(
                        f"crawler: 'rate_limit_s' for domain {domain!r} "
                        f"must be within [0, {MAX_TIMEOUT_S}], got {rate!r}. "
                        "Startup validation failed."
                    )
            if "max_depth" in policy:
                depth = policy["max_depth"]
                if isinstance(depth, bool) or not isinstance(depth, int):
                    raise ConfigError(
                        f"crawler: 'max_depth' for domain {domain!r} must "
                        f"be an int, got {depth!r}. Startup validation failed."
                    )
                if depth < 0:
                    raise ConfigError(
                        f"crawler: 'max_depth' for domain {domain!r} must "
                        f"be >= 0, got {depth!r}. Startup validation failed."
                    )
            normalized_policies[domain.strip().lower()] = dict(policy)
        default_depth = config.get("default_max_depth", DEFAULT_MAX_DEPTH)
        if default_depth is not None and (
            +isinstance(default_depth, bool) or not isinstance(default_depth, int)
        ):
            raise ConfigError(
                "crawler: 'default_max_depth' must be an int or null, "
                f"got {default_depth!r}. Startup validation failed."
            )
        if isinstance(default_depth, int) and default_depth < 0:
            raise ConfigError(
                "crawler: 'default_max_depth' must be >= 0, "
                f"got {default_depth!r}. Startup validation failed."
            )
        user_agent = config.get("user_agent", DEFAULT_USER_AGENT)
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ConfigError(
                f"crawler: 'user_agent' must be a non-empty string, "
                f"got {user_agent!r}. Startup validation failed."
            )
        respect = config.get("respect_robots", True)
        if not isinstance(respect, bool):
            raise ConfigError(
                f"crawler: 'respect_robots' must be a bool, got {respect!r}. "
                "Startup validation failed."
            )
        checkpoint_interval = config.get("checkpoint_interval_pages", 10)
        if (
            isinstance(checkpoint_interval, bool)
            or not isinstance(checkpoint_interval, int)
            or checkpoint_interval <= 0
        ):
            raise ConfigError(
                "crawler: 'checkpoint_interval_pages' must be a positive int, "
                f"got {checkpoint_interval!r}. Startup validation failed."
            )
        max_attempts = config.get("max_attempts_per_url", 3)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ConfigError(
                "crawler: 'max_attempts_per_url' must be an int >= 1, "
                f"got {max_attempts!r}. Startup validation failed."
            )
        frontier_enabled = config.get("frontier_enabled", True)
        if not isinstance(frontier_enabled, bool):
            raise ConfigError(
                f"crawler: 'frontier_enabled' must be a bool, "
                f"got {frontier_enabled!r}. Startup validation failed."
            )
        min_visible = config.get("min_visible_chars", DEFAULT_MIN_VISIBLE_CHARS)
        if (
            not isinstance(min_visible, int)
            or isinstance(min_visible, bool)
            or min_visible < 0
        ):
            raise ConfigError(
                "crawler: 'min_visible_chars' must be a non-negative int. "
                "Startup validation failed."
            )
        soft_404 = config.get("reject_soft_404", True)
        if not isinstance(soft_404, bool):
            raise ConfigError(
                "crawler: 'reject_soft_404' must be a bool. Startup validation failed."
            )
        self.timeout_s = float(timeout)
        self.crawl_delay_s = float(delay)
        self.max_pages = config.get("max_pages", DEFAULT_MAX_PAGES)
        self.max_bytes = config.get("max_bytes", DEFAULT_MAX_BYTES)
        self.max_queued_urls = config.get("max_queued_urls", DEFAULT_MAX_QUEUED_URLS)
        self.max_tracked_domains = tracked
        self.allowed_content_types = [item.strip().lower() for item in content_types]
        self.allow_domains = [item.strip().lower() for item in allow]
        self.deny_domains = [item.strip().lower() for item in deny]
        self.domain_policies = normalized_policies
        self.default_max_depth = default_depth
        self.user_agent = user_agent.strip()
        self.respect_robots = respect
        self.checkpoint_interval_pages = checkpoint_interval
        self.max_attempts_per_url = max_attempts
        self.frontier_enabled = frontier_enabled
        self.min_visible_chars = min_visible
        self.reject_soft_404 = soft_404
        self._robots_cache = _BoundedLru(self.max_tracked_domains)
        self._politeness = _BoundedLru(self.max_tracked_domains)

    # -- public crawl ----------------------------------------------------

    def crawl(
        self,
        seed_urls: list[str],
        *,
        frontier_store: Any | None = None,
        should_stop: Callable[[], bool] | None = None,
        crawl_id: str = "default",
        checkpoint_interval: int | None = None,
        on_link: Callable[[str, int], None] | None = None,
    ) -> list[Document]:
        """Fetch seeds (following discovered links) into raw Documents.

        The persistent path (``frontier_store`` is a
        ``storage/frontier.py::FrontierStore``) replaces the in-memory
        deque: seeds are enqueued (deduped by normalized URL), ``active``
        rows from an interrupted crawl are requeued first, stored
        ETag/Last-Modified validators re-seed the conditional map, and a
        checkpoint row is written every ``checkpoint_interval`` pages and
        once at the end (phase ``paused`` when ``should_stop`` fired,
        else ``done``). Calling crawl again with the same store resumes:
        ``done`` rows are never dequeued, so fetched URLs are not
        refetched. Without a store the legacy in-memory behavior holds.
        """
        from openlens.storage.frontier import (
            PHASE_DONE,
            PHASE_PAUSED,
            PHASE_RUNNING,
            CrawlCheckpoint,
        )

        stats = CrawlStats()
        self.last_stats = stats
        self.last_checkpoint = {}
        # One robots fetch per host per crawl: the cache lives only here.
        self._robots_cache = _BoundedLru(self.max_tracked_domains)
        store = frontier_store if self.frontier_enabled else None
        interval = (
            checkpoint_interval
            if checkpoint_interval is not None
            else self.checkpoint_interval_pages
        )
        if store is not None:
            # Lease-aware startup recovery: only expired (or legacy
            # ownerless) ``active`` rows return to ``queued``; rows with a
            # live heartbeat from another worker are never stolen. On
            # stores without lease support this degrades to the legacy
            # unconditional ``requeue_active``.
            store.reap_expired_leases()
            if hasattr(store, "validators"):
                for key, validators in store.validators().items():
                    headers: dict[str, str] = {}
                    if validators.get("etag"):
                        headers["If-None-Match"] = validators["etag"]
                    if validators.get("last_modified"):
                        headers["If-Modified-Since"] = validators["last_modified"]
                    if headers:
                        self._conditional[key] = headers
            for seed in seed_urls:
                if isinstance(seed, str) and seed.strip():
                    store.enqueue(seed.strip(), 0)
            frontier: Any = PersistentFrontier(
                store, self.max_queued_urls, on_push=on_link
            )
            try:
                preloaded = store.pending(
                    limit=self.max_queued_urls + len(seed_urls) + 1
                )
            except Exception:
                preloaded = []
            depths: dict[str, int] = {}
            for queued_entry in preloaded:
                depths.setdefault(normalize_url(queued_entry.url), queued_entry.depth)
            for seed in seed_urls:
                if isinstance(seed, str) and seed.strip():
                    depths.setdefault(normalize_url(seed.strip()), 0)
        else:
            frontier = BoundedFrontier(self.max_queued_urls, path=self._queue_path)
            depths = {}
            for seed in seed_urls:
                if not isinstance(seed, str) or not seed.strip():
                    continue
                cleaned = seed.strip()
                frontier.push(cleaned)
                depths.setdefault(normalize_url(cleaned), 0)
        stopped = False

        def _save_checkpoint(phase: str) -> None:
            if store is None:
                return
            checkpoint = CrawlCheckpoint(
                crawl_id=crawl_id,
                seeds=[s for s in seed_urls if isinstance(s, str) and s.strip()],
                phase=phase,
                pages_attempted=attempts,
                pages_indexed=len(documents),
                frontier_size=len(frontier),
                stats=vars(stats),
            )
            with contextlib.suppress(Exception):
                store.save_checkpoint(checkpoint)
            self.last_checkpoint = {
                "crawl_id": checkpoint.crawl_id,
                "phase": phase,
                "pages_attempted": attempts,
                "pages_indexed": len(documents),
                "frontier_size": len(frontier),
                "stats": dict(vars(stats)),
            }

        try:
            stats.evicted = frontier.evicted
            documents: list[Document] = []
            seen: set[str] = set()
            attempts = 0
            if store is not None:
                _save_checkpoint(PHASE_RUNNING)
            while len(frontier) > 0 and attempts < self.max_pages:
                if should_stop is not None:
                    with contextlib.suppress(Exception):
                        if should_stop():
                            stopped = True
                            break
                if store is not None:
                    entry = frontier.pop_entry()
                    if entry is None:
                        break
                    raw = entry.url
                    entry_depth = entry.depth
                    entry_attempts = entry.attempts
                else:
                    raw = frontier.pop()
                    if raw is None:
                        break
                    entry_depth = None
                    entry_attempts = 0
                if normalize_url(raw) in seen:
                    continue
                seen.add(normalize_url(raw))
                host = urllib.parse.urlsplit(raw).hostname or ""
                host = host.lower()
                if entry_depth is not None:
                    depth = entry_depth
                    depths[normalize_url(raw)] = depth
                else:
                    depth = depths.get(normalize_url(raw), 0)
                if self._denied(host):
                    stats.skipped_domain += 1
                    if store is not None:
                        store.mark_failed(raw, "denied by deny_domains")
                    continue
                if not self._policy_allowed(host):
                    stats.skipped_policy += 1
                    if store is not None:
                        store.mark_failed(raw, "denied by domain policy")
                    continue
                if not self._policy_exempt(host) and not self._domain_allowed(host):
                    stats.skipped_domain += 1
                    if store is not None:
                        store.mark_failed(raw, "domain not allowlisted")
                    continue
                if not self._depth_allowed(host, depth):
                    stats.skipped_depth += 1
                    if store is not None:
                        store.mark_failed(raw, "depth cap exceeded")
                    continue
                if self.respect_robots and not self._allowed_by_robots(raw):
                    stats.skipped_robots += 1
                    if store is not None:
                        store.mark_failed(raw, "disallowed by robots.txt")
                    continue
                self._wait_polite(host.lower(), raw)
                attempts += 1
                try:
                    page = self._fetch_bounded(raw)
                except CrawlError:
                    stats.timeouts += 1
                    if store is not None:
                        store.mark_failed(raw, "fetch timeout")
                        if entry_attempts + 1 < self.max_attempts_per_url:
                            store.requeue(raw)
                    continue
                except Exception:
                    stats.errors += 1
                    if store is not None:
                        store.mark_failed(raw, "fetch error")
                        if entry_attempts + 1 < self.max_attempts_per_url:
                            store.requeue(raw)
                    continue
                if page is None:
                    if store is not None:
                        store.mark_failed(raw, "empty fetch")
                    continue
                if page.status == 304:
                    stats.skipped_not_modified += 1
                    if store is not None:
                        store.mark_done(raw)
                    continue
                if page.status != 200:
                    stats.errors += 1
                    if store is not None:
                        store.mark_failed(raw, f"http {page.status}")
                        if entry_attempts + 1 < self.max_attempts_per_url:
                            store.requeue(raw)
                    continue
                if page.bytes_aborted:
                    stats.aborted_bytes += 1
                    if store is not None:
                        store.mark_failed(raw, "max_bytes abort")
                    continue
                if not self._content_type_allowed(page.content_type):
                    stats.skipped_content_type += 1
                    if store is not None:
                        store.mark_failed(raw, "content-type skipped")
                    continue
                stats.fetched += 1
                doc = self._to_document(page)
                verdict, reason = classify_content_quality(
                    doc.title, doc.content, min_visible_chars=self.min_visible_chars
                )
                if verdict == "soft_404" and not self.reject_soft_404:
                    verdict, reason = "ok", ""
                if verdict != "ok":
                    stats.skipped_quality += 1
                    if store is not None:
                        store.mark_failed(raw, reason)
                    continue
                documents.append(doc)
                if store is not None:
                    store.mark_done(
                        raw,
                        etag=page.headers.get("etag"),
                        last_modified=page.headers.get("last-modified"),
                    )
                if page.content_type.startswith("text/html"):
                    for link in self._extract_links(page):
                        if normalize_url(link) not in seen:
                            child_depth = depth + 1
                            child_host = (
                                urllib.parse.urlsplit(link).hostname or ""
                            ).lower()
                            if not self._depth_allowed(child_host, child_depth):
                                stats.skipped_depth += 1
                                continue
                            key = normalize_url(link)
                            if key not in depths or child_depth < depths[key]:
                                depths[key] = child_depth
                            if store is not None:
                                frontier.push(link, depths[key])
                            else:
                                frontier.push(link)
                if store is not None and interval > 0 and attempts % interval == 0:
                    _save_checkpoint(PHASE_RUNNING)
            stats.evicted = frontier.evicted
            _save_checkpoint(PHASE_PAUSED if stopped else PHASE_DONE)
            return documents
        finally:
            if store is None and self._queue_path is None:
                frontier.close()

    # -- politeness ------------------------------------------------------

    def _denied(self, host: str) -> bool:
        """True when the global ``deny_domains`` blocklist covers ``host``."""
        return any(
            host == entry or host.endswith("." + entry) for entry in self.deny_domains
        )

    def _domain_allowed(self, host: str) -> bool:
        if any(
            host == entry or host.endswith("." + entry) for entry in self.deny_domains
        ):
            return False
        if self.allow_domains and not any(
            host == entry or host.endswith("." + entry) for entry in self.allow_domains
        ):
            return False
        return True

    def _policy_for(self, host: str) -> dict[str, Any]:
        """Return the per-domain policy for ``host`` (exact or parent match).

        A policy keyed ``example.com`` also covers ``sub.example.com``;
        the most specific (longest) matching domain wins. Empty when no
        policy covers the host.
        """
        best: dict[str, Any] = {}
        best_len = -1
        for domain, policy in self.domain_policies.items():
            if host == domain or host.endswith("." + domain):
                if len(domain) > best_len:
                    best = policy
                    best_len = len(domain)
        return best

    def _policy_allowed(self, host: str) -> bool:
        """Enforce a per-domain ``allow`` override (fail-closed default).

        ``allow: false`` denies the host even when the global allowlist
        would permit it; ``allow: true`` exempts the host from the global
        ``allow_domains`` gate (global ``deny_domains`` still denies, since
        :meth:`_domain_allowed` runs first). No policy means allowed here.
        """
        policy = self._policy_for(host)
        if not policy:
            return True
        return policy.get("allow", True) is not False

    def _policy_exempt(self, host: str) -> bool:
        """True when a per-domain ``allow: true`` bypasses the allowlist gate.

        The global ``deny_domains`` blocklist still applies (checked first
        in :meth:`crawl`), so exemption never re-admits a denied host.
        """
        return self._policy_for(host).get("allow", False) is True

    def _rate_limit_for(self, host: str) -> float:
        """Per-domain minimum gap between hits (0.0 when unconfigured)."""
        policy = self._policy_for(host)
        rate = policy.get("rate_limit_s", 0.0)
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            return 0.0
        return max(0.0, float(rate))

    def _max_depth_for(self, host: str) -> int | None:
        """Per-domain link-following depth cap (None means unlimited)."""
        policy = self._policy_for(host)
        depth = policy.get("max_depth", self.default_max_depth)
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            return self.default_max_depth
        return depth

    def _depth_allowed(self, host: str, depth: int) -> bool:
        cap = self._max_depth_for(host)
        return cap is None or depth <= cap

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urllib.parse.urlsplit(url)
        cache_key = parts.netloc.lower()
        cached = self._robots_cache.get(cache_key)
        if cached is not None:
            return cast(urllib.robotparser.RobotFileParser, cached)
        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        parser.set_url(robots_url)
        try:
            with httpx.Client(
                timeout=self.timeout_s,
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = client.get(robots_url)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                parser.parse([])
        except Exception:
            parser.parse([])
        self._robots_cache.put(cache_key, parser)
        return parser

    def _allowed_by_robots(self, url: str) -> bool:
        try:
            return bool(self._robots_for(url).can_fetch(self.user_agent, url))
        except Exception:
            return True

    def _robots_delay(self, url: str) -> float:
        try:
            value = self._robots_for(url).crawl_delay(self.user_agent)
        except Exception:
            return 0.0
        return float(value) if value is not None else 0.0

    def _wait_polite(self, host: str, url: str) -> None:
        delay = max(self.crawl_delay_s, self._rate_limit_for(host))
        if self.respect_robots:
            delay = max(delay, self._robots_delay(url))
        last = self._politeness.get(host)
        now = time.monotonic()
        if last is not None and delay > 0:
            wait = delay - (now - float(last))
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        self._politeness.put(host, now)

    # -- fetching --------------------------------------------------------

    def _run_bounded(self, func: Callable[[], _T]) -> _T:
        """Mirror providers/base.py run_bounded: daemon thread + join.

        The httpx client inside ``func`` already carries ``timeout_s``;
        this join bounds everything else (DNS stalls, hung iterators)
        so one host can never stall a crawl unbounded.
        """
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = func()
            except BaseException as exc:  # captured and re-raised below
                box["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=self.timeout_s)
        if worker.is_alive():
            raise CrawlError(f"crawl fetch timed out after {self.timeout_s}s")
        if "error" in box:
            raise box["error"]
        return cast(_T, box["result"])

    def _fetch_bounded(self, url: str) -> _FetchedPage | None:
        return self._run_bounded(lambda: self._do_fetch(url))

    def _do_fetch(self, url: str) -> _FetchedPage | None:
        headers: dict[str, str] = {"User-Agent": self.user_agent}
        conditional = self._conditional.get(normalize_url(url), {})
        headers.update(conditional)
        try:
            with httpx.Client(
                timeout=self.timeout_s,
                headers=headers,
                follow_redirects=True,
            ) as c:
                client = c
                with client.stream("GET", url) as response:
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    chunks: list[bytes] = []
                    total = 0
                    aborted = False
                    for chunk in response.iter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > self.max_bytes:
                            aborted = True
                            break
                        chunks.append(chunk)
                    final_url = str(response.url)
                    flat = {k.lower(): v for k, v in response.headers.items()}
                    chain = [str(item.url) for item in response.history]
                    if response.status_code == 200:
                        self._remember_conditional(url, flat)
                    return _FetchedPage(
                        status=response.status_code,
                        url=final_url,
                        content_type=content_type,
                        body=b"".join(chunks),
                        headers=flat,
                        bytes_aborted=aborted,
                        requested_url=url,
                        redirect_chain=chain,
                    )
        except CrawlError:
            raise
        except Exception as exc:
            raise CrawlError(f"fetch failed for {url}: {exc}") from exc

    def _remember_conditional(self, url: str, headers: dict[str, str]) -> None:
        entry: dict[str, str] = {}
        if headers.get("etag"):
            entry["If-None-Match"] = headers["etag"]
        if headers.get("last-modified"):
            entry["If-Modified-Since"] = headers["last-modified"]
        if entry:
            key = normalize_url(url)
            stored = self._conditional.get(key, {})
            stored.update(entry)
            if len(self._conditional) >= self.max_tracked_domains:
                oldest = next(iter(self._conditional))
                del self._conditional[oldest]
            self._conditional[key] = stored

    # -- document building -----------------------------------------------

    def _content_type_allowed(self, content_type: str) -> bool:
        return any(
            content_type.startswith(allowed) for allowed in self.allowed_content_types
        )

    def _extract_links(self, page: _FetchedPage) -> list[str]:
        try:
            text = page.body.decode("utf-8", errors="replace")
        except Exception:
            return []
        parser = _LinkTitleParser()
        try:
            parser.feed(text)
        except Exception:
            return []
        links: list[str] = []
        for href in parser.links:
            absolute = urllib.parse.urljoin(page.url, href)
            scheme = urllib.parse.urlsplit(absolute).scheme.lower()
            if scheme in ("http", "https"):
                links.append(absolute)
        return links

    def _to_document(self, page: _FetchedPage) -> Document:
        text = page.body.decode("utf-8", errors="replace")
        title = page.url
        if page.content_type.startswith("text/html"):
            parser = _LinkTitleParser()
            parsed = True
            try:
                parser.feed(text)
            except Exception:
                parsed = False
            if parsed:
                # Head-only scoped title -> og/twitter:title -> first h1
                # -> URL: SVG icon titles never garble the headline.
                title = parser.resolved_title(page.url)
        metadata: dict[str, Any] = {"content_type": page.content_type}
        if page.headers.get("etag"):
            metadata["etag"] = page.headers["etag"]
        if page.headers.get("last-modified"):
            metadata["last_modified"] = page.headers["last-modified"]
            published = published_from_last_modified(page.headers["last-modified"])
            if published is not None:
                metadata["published"] = published
        if page.content_type.startswith("text/html"):
            # Never raises: extract_page_attributes returns {} on bad input.
            # Real pages always carry ``sponsored`` (a real bool) plus the
            # site-category quintet; signal-less input yields {} and the
            # document keeps just its content type.
            metadata.update(extract_page_attributes(text, url=page.url))
        if page.redirect_chain:
            metadata["redirect_chain"] = list(page.redirect_chain)
        if page.requested_url and page.url != page.requested_url:
            metadata["final_url"] = page.url
        # ``domain_age_days`` stays absent until a WHOIS/registration
        # source exists: the offline crawler never fabricates an age, and
        # the trust scorer treats a missing age as neutral.
        booking = metadata.get("booking_url")
        if isinstance(booking, str) and booking and page.url:
            metadata["booking_url"] = urllib.parse.urljoin(page.url, booking)
        content = text
        if page.content_type.startswith("text/html"):
            # Snippets, FTS, and model prompts consume prose, not markup:
            # store the visible text, never the raw tags/scripts/styles.
            cleaned = extract_visible_text(text)
            if cleaned:
                content = cleaned
        return Document(
            id=normalize_url(page.url) or page.url,
            title=title,
            content=content,
            url=page.url,
            metadata=metadata,
        )
