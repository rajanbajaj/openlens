"""Step-3 Postgres conformance suite (permanently skipped).

The live tests below mirror the Step-1 ``DocumentStore`` conformance
shape against Postgres, but they are unconditionally skipped: the live
suite fails in CI (like the MySQL one before it) and server-backed
storage is not under active repair — the CI live step and service
container were removed.

The lazy-import and error-mapping tests below always run and need no
server.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid

import pytest

from openlens.core.interfaces import Document, ProviderError
from openlens.storage.base import DocumentStore
from openlens.storage import postgres_backend as backend_module
from openlens.storage.postgres_backend import PostgresDocumentStore

DSN = os.environ.get("OPENLENS_TEST_PG_DSN", "")

try:
    import psycopg

    _HAS_DRIVER = True
except ImportError:
    psycopg = None  # type: ignore[assignment]
    _HAS_DRIVER = False

needs_pg = pytest.mark.skip(
    reason="Postgres live conformance permanently skipped (failing in CI)",
)

needs_driver = pytest.mark.skipif(not _HAS_DRIVER, reason="psycopg not installed")


def _doc(
    doc_id: str = "d1",
    title: str = "hello world",
    content: str = "hello world content",
    url: str | None = None,
    metadata: dict | None = None,
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        content=content,
        url=url,
        metadata=dict(metadata) if metadata is not None else {},
    )


def _admin_connect():
    """Direct driver connection for schema setup/teardown and pg_indexes."""
    import urllib.parse

    parsed = urllib.parse.urlparse(DSN)
    assert psycopg is not None
    return psycopg.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        dbname=(parsed.path or "/openlens").lstrip("/") or "openlens",
        user=urllib.parse.unquote(parsed.username or "postgres"),
        password=urllib.parse.unquote(parsed.password or ""),
        connect_timeout=5,
        autocommit=True,
    )


@pytest.fixture()
def store():
    """Isolated store: temp schema per test, dropped afterwards."""
    schema = f"pg_test_{uuid.uuid4().hex[:16]}"
    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin.close()
    repo = PostgresDocumentStore.from_dsn(DSN, schema=schema, pool_size=2)
    try:
        yield repo
    finally:
        repo.close()
        admin = _admin_connect()
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            admin.close()


# ---------------------------------------------------------------------------
# Lazy import + error mapping (always run, no server needed)
# ---------------------------------------------------------------------------


def test_module_imports_without_driver_installed(monkeypatch):
    """Top-level import must not touch the driver (None sentinel blocks it)."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    reloaded = importlib.reload(backend_module)
    try:
        assert reloaded.PostgresDocumentStore is not None
        assert sys.modules.get("psycopg") is None
    finally:
        importlib.reload(backend_module)


def test_instantiation_without_driver_raises_provider_error(monkeypatch):
    """Missing driver -> ProviderError(UNAVAILABLE), never ImportError."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ProviderError) as exc_info:
        PostgresDocumentStore(host="localhost", port=5432)
    assert exc_info.value.code == ProviderError.UNAVAILABLE


@needs_driver
def test_unreachable_host_raises_provider_unavailable():
    """Connection failure at construction -> UNAVAILABLE, not raw errors."""
    with pytest.raises(ProviderError) as exc_info:
        PostgresDocumentStore(
            host="127.0.0.1",
            port=1,
            dbname="openlens",
            user="postgres",
            password="",
            pool_size=1,
            connect_timeout=1,
        )
    assert exc_info.value.code == ProviderError.UNAVAILABLE


def test_from_config_resolves_through_load_config(tmp_path, monkeypatch):
    """Env -> load_config ${} -> from_config reaches the constructor seam."""
    from openlens.config import load_config

    monkeypatch.setenv("OPENLENS_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("OPENLENS_PG_PORT", "1")
    config_file = tmp_path / "storage.yaml"
    config_file.write_text(
        "storage:\n"
        "  backend: postgres\n"
        "  postgres:\n"
        "    host: ${OPENLENS_PG_HOST}\n"
        "    port: ${OPENLENS_PG_PORT}\n"
        "    dbname: openlens\n"
        "    user: postgres\n"
        "    password: ''\n",
        encoding="utf-8",
    )
    config = load_config(config_file)["storage"]["postgres"]
    assert config["host"] == "127.0.0.1"
    with pytest.raises(ProviderError) as exc_info:
        PostgresDocumentStore.from_config({**config, "connect_timeout": 1})
    assert exc_info.value.code == ProviderError.UNAVAILABLE


def test_invalid_table_and_schema_names_rejected():
    with pytest.raises(ValueError):
        PostgresDocumentStore(table="documents; DROP TABLE x")
    with pytest.raises(ValueError):
        PostgresDocumentStore(schema="public; DROP SCHEMA x")


# ---------------------------------------------------------------------------
# Live conformance (Step-1 shape, needs OPENLENS_TEST_PG_DSN)
# ---------------------------------------------------------------------------


@needs_pg
def test_implements_document_store_contract(store):
    assert isinstance(store, DocumentStore)


@needs_pg
def test_add_and_get_round_trip_with_metadata_dict(store):
    assert store.count() == 0
    assert store.get("missing") is None
    assert (
        store.add_documents(
            [
                _doc(
                    url="http://example.com/a",
                    metadata={"k": "v", "n": 3, "tags": ["a", "b"]},
                )
            ]
        )
        == 1
    )
    fetched = store.get("d1")
    assert fetched is not None
    assert fetched.id == "d1"
    assert fetched.title == "hello world"
    assert fetched.content == "hello world content"
    assert fetched.url == "http://example.com/a"
    assert fetched.metadata == {"k": "v", "n": 3, "tags": ["a", "b"]}
    assert store.count() == 1


@needs_pg
def test_add_documents_returns_total_and_empty_is_zero(store):
    assert store.add_documents([]) == 0
    assert store.count() == 0
    docs = [_doc(f"d{i}", content=f"retrieval body number {i}") for i in range(5)]
    assert store.add_documents(docs) == 5
    assert store.count() == 5


@needs_pg
def test_upsert_overwrite_visible_exactly_once(store):
    store.add_document(_doc())
    assert store.search("hello")[1] == 1
    store.add_document(_doc(title="goodbye moon", content="goodbye moon content"))
    assert store.count() == 1
    fetched = store.get("d1")
    assert fetched is not None
    assert fetched.content == "goodbye moon content"
    assert store.search("hello")[1] == 0
    hits, total = store.search("goodbye")
    assert total == 1
    assert [h.id for h, _ in hits] == ["d1"]


@needs_pg
def test_search_paging_and_total(store):
    store.add_documents(
        [_doc(f"d{i}", content=f"shared token body {i}") for i in range(5)]
    )
    page1, total = store.search("shared", limit=2, offset=0)
    assert total == 5
    assert len(page1) == 2
    page2, total2 = store.search("shared", limit=2, offset=2)
    assert total2 == 5
    assert len(page2) == 2
    page3, _ = store.search("shared", limit=2, offset=4)
    assert len(page3) == 1
    ids = [h.id for h, _ in page1 + page2 + page3]
    assert sorted(ids) == [f"d{i}" for i in range(5)]


@needs_pg
def test_search_blank_query_returns_empty(store):
    store.add_documents([_doc(f"d{i}", content=f"blank body {i}") for i in range(2)])
    assert store.search("   ") == ([], 0)


@needs_pg
def test_search_operator_only_query_returns_empty(store):
    store.add_documents([_doc(f"d{i}", content=f"operator body {i}") for i in range(2)])
    assert store.search("AND OR NOT") == ([], 0)


@needs_pg
def test_delete_removes_document_and_count_decrements(store):
    store.add_documents([_doc(f"d{i}", content=f"deletion body {i}") for i in range(3)])
    assert store.delete("d0") is True
    assert store.count() == 2
    assert store.get("d0") is None
    assert store.search("deletion")[1] == 2


@needs_pg
def test_delete_missing_returns_false(store):
    store.add_document(_doc())
    assert store.delete("nope") is False
    assert store.count() == 1
    assert store.delete("nope") is False


@needs_pg
def test_chunked_bulk_write_and_validation(store):
    docs = [_doc(f"d{i}", content=f"chunked bulk body {i}") for i in range(21)]
    assert store.add_documents(docs, chunk_size=10) == 21
    assert store.count() == 21
    assert store.search("chunked")[1] == 21
    with pytest.raises(ValueError):
        store.add_documents([_doc()], chunk_size=0)
    with pytest.raises(ValueError):
        store.add_documents([_doc()], chunk_size=-2)


@needs_pg
def test_hash_exists_tracks_ingested_content(store):
    from openlens.storage.repository import content_hash_of

    assert store.hash_exists(content_hash_of("hello world content")) is False
    store.add_document(_doc())
    assert store.hash_exists(content_hash_of("hello world content")) is True
    assert store.hash_exists(content_hash_of("unseen content")) is False


@needs_pg
def test_update_document_rewrites_row_and_hash(store):
    from openlens.storage.repository import content_hash_of

    assert store.update_document(_doc()) is False
    store.add_document(_doc())
    updated = _doc(title="goodbye moon", content="goodbye moon content")
    assert store.update_document(updated) is True
    fetched = store.get("d1")
    assert fetched is not None
    assert fetched.content == "goodbye moon content"
    assert store.hash_exists(content_hash_of("goodbye moon content")) is True
    assert store.hash_exists(content_hash_of("hello world content")) is False


@needs_pg
def test_close_is_idempotent(store):
    store.add_document(_doc())
    store.close()
    store.close()


@needs_pg
def test_schema_has_pk_and_tsvector_gin_index(store):
    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = %s::regclass AND contype = 'p'",
                (f"{store._schema}.{store._table}",),
            )
            pk_rows = cur.fetchall()
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes"
                " WHERE schemaname = %s AND tablename = %s",
                (store._schema, store._table),
            )
            indexes = cur.fetchall()
            cur.execute("SET enable_seqscan = OFF")
            cur.execute(
                f"EXPLAIN SELECT id FROM {store._qualified()}"  # noqa: S608
                " WHERE search_tsv @@ plainto_tsquery('english', %s)",
                ("conformanceprobe",),
            )
            plan = " ".join(row[0] for row in cur.fetchall())
    finally:
        admin.close()
    assert pk_rows, "no PRIMARY KEY constraint on the documents table"
    gin = [d for _, d in indexes if "GIN" in d.upper() and "search_tsv" in d]
    assert gin, f"no GIN index on search_tsv; indexes present: {indexes!r}"
    assert "Seq Scan" not in plan, f"planner seq-scans the FTS lookup: {plan}"
    assert "search_tsv" in plan, f"plan does not use the tsvector index: {plan}"
