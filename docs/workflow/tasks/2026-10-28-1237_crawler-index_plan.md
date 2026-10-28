# Crawler & Index Management Plan

## Goal

Give the custom index a real ingestion path: crawl, index, update, and
reindex documents with dedup and content hashes — the differentiation bet
from the original plan §6/§13.

Falsifiable bar: starting from a stub HTTP site with N pages, one crawl
indexes exactly N documents; an unchanged re-crawl changes zero rows; a
one-page edit causes an incremental run to rewrite exactly that page's
rows; all verified by row/hash assertions, not log inspection.

## Reality baseline (verified 2026-10-28 against the tree)

- `src/openlens/indexing/` holds only `indexer.py` (20 lines:
  `DocumentIndexer.index_documents` / `index_seed` methods) and
  `seed.py` (plus `__init__.py`). No `crawler.py`, no frontier, no
  politeness code.
- `src/openlens/storage/repository.py` schema is `documents(id PK, title,
  content, url, metadata-JSON)` plus FTS5 `(doc_id UNINDEXED, title,
  content)`. It has `add_document` + `add_documents` (INSERT OR REPLACE +
  FTS DELETE+INSERT per doc — an unchanged re-`add` still rewrites the
  FTS row today), `get`, `count`, `search` (limit/offset already
  supported), `close`. It has NO update/delete-by-id, NO hash lookup,
  NO content-hash column, NO index on `url`, and NO WAL
  (`journal_mode` is never set; SQLite default applies).
- ALL repository access (reads and writes) serializes on one
  `threading.Lock` over a single sqlite3 connection
  (`check_same_thread=False`); `add_documents` additionally holds the
  connection transaction (`with self._lock, self._conn:`) for the
  whole batch, so one bulk call starves every query for its duration.
- `configs/default.yaml` has NO `indexing` section, and nothing reads
  one: `load_config` (`config.py`) is permissive (no unknown-key
  rejection) and `build_engine` (`main.py`) ignores sections it does
  not know. Unknown-key rejection for a new `indexing` section must
  therefore be implemented in the crawler/indexer `configure()` that
  consumes it — the loader will not do it for free.
- Query-time URL dedup already exists (`retrieval/deduplicator.py`,
  called inside `CustomIndexProvider.search`); ingest-time dedup is new
  and must not contradict it (see step 2). Ingest URL normalization
  MUST reuse the query-time rule (`url.strip().lower().rstrip("/")`
  in `dedupe_key`) or a single shared helper both sides import — two
  divergent normalizers would let ingest store what query-time dedup
  then drops (or vice versa).
- `Document` (`core/interfaces.py`) has `id/title/content/url/metadata`
  and no `source` field; seed metadata is only `{"topic": ...}`. The
  ranker reads `metadata["trusted"]` (bool) and
  `metadata["recency_score"]` (0..1); keyword boosts read title/snippet
  text, not metadata. "Preserve ranking-critical fields" below means
  exactly: stable `id`, verbatim `title`/`content`, and any
  `trusted`/`recency_score` already in metadata.

## Scope (all connected: one ingestion pipeline)

- `indexing/crawler.py`: polite fetcher (robots.txt, crawl-delay,
  timeout, max pages/bytes, allowlist/denylist of domains) producing raw
  documents. HTTP goes through `httpx` (already a dependency; providers
  bound it via `timeout_s` in `providers/base.py` — reuse that pattern,
  never an unbounded client). Max-bytes is enforced while streaming,
  not after reading the whole body; only configured content types are
  indexed. Frontier is a bounded on-disk queue (cap: max queued URLs,
  evict-oldest with a counter); per-domain politeness state is a bounded
  dict (cap: max tracked domains, LRU eviction); the robots.txt cache
  is bounded the same way (one fetch per host per crawl, never per URL).
- `indexing/indexer.py` extensions: add/update/delete by id, full
  reindex, incremental runs (only changed content reindexed via SHA-256
  content hash), ingest-time URL+hash dedup. Hash is stored as a real
  `content_hash TEXT` column (migrated with `ALTER TABLE` + backfill on
  existing DBs), NOT inside the metadata JSON blob, so hash lookups and
  dedup stay indexed O(log n). Canonical hash input is pinned:
  `sha256(content.encode("utf-8"))` over the stored `content` only
  (title/url changes without a content change are updates via the URL
  path in step 2, not hash changes). The migration is idempotent
  (safe on a fresh DB, a pre-hash DB, and a second run) and backfills
  by the same formula.
- `storage/repository.py`: `update_document`, `delete_document` (removes
  both `documents` and `documents_fts` rows), `get_hash(doc_id)` /
  `hash_exists(hash)`, UNIQUE index on normalized URL, index on
  `content_hash`. Reindex preserves ranking-critical stored fields:
  stable `id`s, verbatim `title`/`content`, and stored
  `trusted`/`recency_score` metadata — scores themselves are computed
  at query time and need no preservation.
- Config: `indexing` section in `configs/default.yaml` (policies only:
  delays, timeouts, caps, domain lists; no URLs fetched unless
  configured). Unknown keys rejected loudly via `ConfigError` raised by
  the crawler/indexer `configure()` (see Reality baseline: the loader
  itself rejects nothing).

## Steps

1. Repository primitives: `update_document` / `delete_document` / hash
   lookup + `content_hash` column migration + URL/hash indexes, with unit
   tests asserting FTS rows follow document rows on update and delete,
   and migration tests (fresh DB, pre-hash DB, migration run twice —
   same hashes, same row count each time).
2. Indexer update/reindex/incremental flows + unit tests. Ingest dedup
   rule (reconciled with query-time `deduplicator.py` via the shared
   normalization above): same normalized URL + same hash → skip write;
   same URL + new hash → update; new URL + known hash → skip as
   duplicate (record counter). Chunked commits (default 500 docs/batch,
   implemented as one `add_documents` call per chunk so the repository
   lock is released between chunks) so a full reindex never holds the
   repository lock for the whole corpus.
3. Crawler with politeness policies + offline tests against a local stub
   HTTP server (`http.server` in a thread, no network): robots.txt
   respected, crawl-delay honored (assert elapsed >= delay), caps
   enforced, domain allow/deny enforced, robots fetched once per host
   (assert stub hit count), max-bytes aborts an oversized body.
4. End-to-end: crawl stub site → index → update → reindex, asserting
   exact row counts, exact FTS searchability of new content, and
   un-searchability of deleted content.

## Validation Plan

- `uv run pytest tests/unit/test_crawler_index.py tests/unit/test_custom_index.py -q`
  green (new + existing indexer/provider tests, incl. untouched seed
  behavior); then `uv run pytest tests -q` green.
- `uv run ruff check src tests`, `uv run ruff format --check src tests`,
  `uv run mypy src` clean.
- Manual/offline script (stub server): crawl N pages → `count() == N`;
  re-crawl unchanged → `count() == N` and zero `UPDATE`s (assert via a
  write counter or unchanged `content_hash` mtimes); edit one page →
  incremental run rewrites exactly 1 row; delete one page → still
  indexed until explicit delete, then `search()` no longer returns it.

## Scalability notes

| Bottleneck | Breaks at | Fix shape (folded in or flagged) |
|---|---|---|
| Single global lock + one connection: bulk index starves queries | ~10x (crawl writes concurrent with queries) | Chunked commits (in scope, step 2); WAL mode + reader/writer split flagged for synthesis, not this task |
| Full reindex rewrites every FTS row (DELETE+INSERT/doc) | ~10x docs (linear write window) | Incremental-via-hash is the default path (in scope); full reindex kept for schema changes only |
| Unindexed URL/hash scans would make ingest O(corpus) per doc | ~10x docs | UNIQUE index on normalized URL + index on `content_hash` (in scope) |
| Unbounded frontier / politeness dict | 100x pages/domains (memory) | Bounded on-disk queue + LRU domain state (in scope) |
| `robots.txt` re-fetched per URL (network N+1, latency on crawl path) | 10x pages per host | Bounded per-host robots cache, one fetch per crawl (in scope, step 3) |
| Sequential fetch + serialized crawl-delay: one slow host stalls the whole crawl | 10x hosts/pages | Per-host delay accounting only (in scope); concurrent workers flagged — needs the off-process writer below |
| Re-download of unchanged pages (hash compared only after a full fetch) | 10x pages (bandwidth + hash CPU O(total bytes)) | Store `ETag`/`Last-Modified` in metadata when served and send conditional requests (cheap, in scope); hash still decides the write |
| `search()` holds the same lock for `COUNT(*)` + `MATCH` scan | 100x docs (query latency under crawl load) | No fix in this task beyond chunking; lock/WAL split flagged below — this is the first read-path wall |

Rewrite-forcing risk (flagged, not built): moving indexing off-process
(background worker / separate writer) once crawl throughput needs exceed
what chunked in-process commits allow. The same move is forced when the
shared-lock `COUNT`+`MATCH` read path (last row) can no longer meet
query latency during crawls — flagged for synthesis with the
db-connectors plan (review note D1: this plan lands
`delete`/`content_hash`/indexes first; that plan ports — never
re-implements — them into the `storage/base.py` contract).

## Non-goals

- Distributed crawling, JS rendering, sitemap discovery.
- Frontier persistence beyond a bounded on-disk queue (no DAG scheduler,
  no resume-across-versions).
- Query-time ranking changes: reindex preserves stored metadata only;
  scoring/ranking behavior belongs to the scoring/rerank packs.
- WAL/connection-pool rework of the repository (flagged above; needs
  cross-plan coordination with providers + observability).
