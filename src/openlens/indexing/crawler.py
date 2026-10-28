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
import html.parser
import tempfile
import threading
import time
import urllib.parse
import urllib.robotparser
from collections import OrderedDict
from dataclasses import dataclass, field
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
DEFAULT_USER_AGENT = "OpenLensBot/1.0"

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
    }
)

_T = TypeVar("_T")


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


class _LinkTitleParser(html.parser.HTMLParser):
    """Collects the page title and all anchor hrefs in one pass."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = True
        if lowered == "a":
            for name, value in attrs:
                if name.lower() == "href" and value:
                    self.links.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()


@dataclass
class _FetchedPage:
    status: int
    url: str
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    bytes_aborted: bool = False


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
        self._robots_cache = _BoundedLru(self.max_tracked_domains)
        self._politeness = _BoundedLru(self.max_tracked_domains)

    # -- public crawl ----------------------------------------------------

    def crawl(self, seed_urls: list[str]) -> list[Document]:
        """Fetch seeds (following discovered links) into raw Documents."""
        stats = CrawlStats()
        self.last_stats = stats
        # One robots fetch per host per crawl: the cache lives only here.
        self._robots_cache = _BoundedLru(self.max_tracked_domains)
        frontier = BoundedFrontier(self.max_queued_urls, path=self._queue_path)
        try:
            depths: dict[str, int] = {}
            for seed in seed_urls:
                if not isinstance(seed, str) or not seed.strip():
                    continue
                cleaned = seed.strip()
                frontier.push(cleaned)
                depths.setdefault(normalize_url(cleaned), 0)
            stats.evicted = frontier.evicted
            documents: list[Document] = []
            seen: set[str] = set()
            attempts = 0
            while len(frontier) > 0 and attempts < self.max_pages:
                raw = frontier.pop()
                if raw is None:
                    break
                if normalize_url(raw) in seen:
                    continue
                seen.add(normalize_url(raw))
                host = urllib.parse.urlsplit(raw).hostname or ""
                host = host.lower()
                depth = depths.get(normalize_url(raw), 0)
                if self._denied(host):
                    stats.skipped_domain += 1
                    continue
                if not self._policy_allowed(host):
                    stats.skipped_policy += 1
                    continue
                if not self._policy_exempt(host) and not self._domain_allowed(host):
                    stats.skipped_domain += 1
                    continue
                if not self._depth_allowed(host, depth):
                    stats.skipped_depth += 1
                    continue
                if self.respect_robots and not self._allowed_by_robots(raw):
                    stats.skipped_robots += 1
                    continue
                self._wait_polite(host.lower(), raw)
                attempts += 1
                try:
                    page = self._fetch_bounded(raw)
                except CrawlError:
                    stats.timeouts += 1
                    continue
                except Exception:
                    stats.errors += 1
                    continue
                if page is None:
                    continue
                if page.status == 304:
                    stats.skipped_not_modified += 1
                    continue
                if page.status != 200:
                    stats.errors += 1
                    continue
                if page.bytes_aborted:
                    stats.aborted_bytes += 1
                    continue
                if not self._content_type_allowed(page.content_type):
                    stats.skipped_content_type += 1
                    continue
                stats.fetched += 1
                documents.append(self._to_document(page))
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
                            frontier.push(link)
            stats.evicted = frontier.evicted
            return documents
        finally:
            if self._queue_path is None:
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
            with httpx.Client(timeout=self.timeout_s, headers=headers) as c:
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
                    if response.status_code == 200:
                        self._remember_conditional(url, flat)
                    return _FetchedPage(
                        status=response.status_code,
                        url=final_url,
                        content_type=content_type,
                        body=b"".join(chunks),
                        headers=flat,
                        bytes_aborted=aborted,
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
            if parsed and parser.title:
                title = parser.title
        metadata: dict[str, Any] = {"content_type": page.content_type}
        if page.headers.get("etag"):
            metadata["etag"] = page.headers["etag"]
        if page.headers.get("last-modified"):
            metadata["last_modified"] = page.headers["last-modified"]
        return Document(
            id=normalize_url(page.url) or page.url,
            title=title,
            content=text,
            url=page.url,
            metadata=metadata,
        )
