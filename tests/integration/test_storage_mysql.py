"""Step-2 MySQL conformance suite (permanently skipped).

The live tests below mirror the Step-1 ``DocumentStore`` conformance
shape against MySQL, but they are unconditionally skipped: no live
MySQL server runs in CI (the CI live step and service container were
removed), and the backend is not under active repair.

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
from openlens.storage import mysql_backend as backend_module
from openlens.storage.mysql_backend import MySQLDocumentStore

DSN = os.environ.get("OPENLENS_TEST_MYSQL_DSN", "")

try:
    import pymysql

    _HAS_DRIVER = True
except ImportError:
    pymysql = None  # type: ignore[assignment]
    _HAS_DRIVER = False

needs_mysql = pytest.mark.skip(
    reason="MySQL live conformance permanently skipped (no live server in CI)",
)

needs_driver = pytest.mark.skipif(not _HAS_DRIVER, reason="pymysql not installed")


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


def _admin_connect(database: str | None = None):
    """Direct driver connection for schema setup/teardown and SHOW INDEX."""
    import urllib.parse

    parsed = urllib.parse.urlparse(DSN)
    return pymysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        database=database
        if database is not None
        else (parsed.path or "/openlens").lstrip("/") or "openlens",
        user=urllib.parse.unquote(parsed.username or "root"),
        password=urllib.parse.unquote(parsed.password or ""),
        charset="utf8mb4",
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )


@pytest.fixture()
def store():
    """Isolated store: temp schema per test, dropped afterwards."""
    schema = f"openlens_test_{uuid.uuid4().hex[:16]}"
    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4")
        admin.commit()
    finally:
        admin.close()
    repo = MySQLDocumentStore.from_dsn(DSN, database=schema, pool_size=2)
    try:
        yield repo
    finally:
        repo.close()
        admin = _admin_connect()
        try:
            with admin.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{schema}`")
            admin.commit()
        finally:
            admin.close()


# ---------------------------------------------------------------------------
# Lazy import + error mapping (always run, no server needed)
# ---------------------------------------------------------------------------


def test_module_imports_without_driver_installed(monkeypatch):
    """Top-level import must not touch the driver (None sentinel blocks it)."""
    monkeypatch.setitem(sys.modules, "pymysql", None)
    reloaded = importlib.reload(backend_module)
    try:
        assert reloaded.MySQLDocumentStore is not None
        assert sys.modules.get("pymysql") is None
    finally:
        importlib.reload(backend_module)


def test_instantiation_without_driver_raises_provider_error(monkeypatch):
    """Missing driver -> ProviderError(UNAVAILABLE), never ImportError."""
    monkeypatch.setitem(sys.modules, "pymysql", None)
    with pytest.raises(ProviderError) as exc_info:
        MySQLDocumentStore(host="localhost", port=3306)
    assert exc_info.value.code == ProviderError.UNAVAILABLE


@needs_driver
def test_unreachable_host_raises_provider_unavailable():
    """Connection failure at construction -> UNAVAILABLE, not raw errors."""
    with pytest.raises(ProviderError) as exc_info:
        MySQLDocumentStore(
            host="127.0.0.1",
            port=1,
            database="openlens",
            user="root",
            password="",
            pool_size=1,
            connect_timeout=1,
        )
    assert exc_info.value.code == ProviderError.UNAVAILABLE


def test_from_config_resolves_through_load_config(tmp_path, monkeypatch):
    """Env -> load_config ${} -> from_config reaches the constructor seam."""
    from openlens.config import load_config

    monkeypatch.setenv("OPENLENS_MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("OPENLENS_MYSQL_PORT", "1")
    config_file = tmp_path / "storage.yaml"
    config_file.write_text(
        "storage:\n"
        "  backend: mysql\n"
        "  mysql:\n"
        "    host: ${OPENLENS_MYSQL_HOST}\n"
        "    port: ${OPENLENS_MYSQL_PORT}\n"
        "    database: openlens\n"
        "    user: root\n"
        "    password: ''\n",
        encoding="utf-8",
    )
    config = load_config(config_file)["storage"]["mysql"]
    assert config["host"] == "127.0.0.1"
    with pytest.raises(ProviderError) as exc_info:
        MySQLDocumentStore.from_config({**config, "connect_timeout": 1})
    assert exc_info.value.code == ProviderError.UNAVAILABLE


def test_invalid_table_name_rejected():
    with pytest.raises(ValueError):
        MySQLDocumentStore(table="documents; DROP TABLE x")


# ---------------------------------------------------------------------------
# Live conformance (Step-1 shape, needs OPENLENS_TEST_MYSQL_DSN)
# ---------------------------------------------------------------------------


@needs_mysql
def test_implements_document_store_contract(store):
    assert isinstance(store, DocumentStore)


@needs_mysql
def test_add_and_get_round_trip_with_metadata_dict(store):
    assert store.count() == 0
    assert store.get("missing") is None
    assert (
        store.add_documents(
            [_doc(url="http://example.com/a", metadata={"k": "v", "n": 3})]
        )
        == 1
    )
    fetched = store.get("d1")
    assert fetched is not None
    assert fetched.id == "d1"
    assert fetched.title == "hello world"
    assert fetched.content == "hello world content"
    assert fetched.url == "http://example.com/a"
    assert fetched.metadata == {"k": "v", "n": 3}
    assert store.count() == 1


@needs_mysql
def test_add_documents_returns_total_and_empty_is_zero(store):
    assert store.add_documents([]) == 0
    assert store.count() == 0
    docs = [_doc(f"d{i}", content=f"retrieval body number {i}") for i in range(5)]
    assert store.add_documents(docs) == 5
    assert store.count() == 5


@needs_mysql
def test_upsert_overwrite_visible_exactly_once(store):
    store.add_document(_doc())
    assert store.search("hello")[1] == 1
    store.add_document(_doc(title="goodbye moon", content="goodbye moon content"))
    assert store.count() == 1
    fetched = store.get("d1")
    assert fetched is not None
    assert fetched.content == "goodbye moon content"
    hits, total = store.search("goodbye")
    assert total == 1
    assert [h.id for h, _ in hits] == ["d1"]


@needs_mysql
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


@needs_mysql
def test_search_blank_query_returns_empty(store):
    store.add_documents([_doc(f"d{i}", content=f"blank body {i}") for i in range(2)])
    assert store.search("   ") == ([], 0)


@needs_mysql
def test_delete_removes_document_and_count_decrements(store):
    store.add_documents([_doc(f"d{i}", content=f"deletion body {i}") for i in range(3)])
    assert store.delete("d0") is True
    assert store.count() == 2
    assert store.get("d0") is None
    assert store.search("deletion")[1] == 2


@needs_mysql
def test_delete_missing_returns_false(store):
    store.add_document(_doc())
    assert store.delete("nope") is False
    assert store.count() == 1
    assert store.delete("nope") is False


@needs_mysql
def test_chunked_bulk_write_and_validation(store):
    docs = [_doc(f"d{i}", content=f"chunked bulk body {i}") for i in range(21)]
    assert store.add_documents(docs, chunk_size=10) == 21
    assert store.count() == 21
    assert store.search("chunked")[1] == 21
    with pytest.raises(ValueError):
        store.add_documents([_doc()], chunk_size=0)
    with pytest.raises(ValueError):
        store.add_documents([_doc()], chunk_size=-2)


@needs_mysql
def test_close_is_idempotent(store):
    store.add_document(_doc())
    store.close()
    store.close()


@needs_mysql
def test_schema_has_pk_and_fulltext_index(store):
    schema = store._schema
    admin = _admin_connect(database=schema)
    try:
        with admin.cursor() as cur:
            cur.execute(f"SHOW INDEX FROM `{store._table}`")
            index_rows = cur.fetchall()
    finally:
        admin.close()
    names = {row["Key_name"] for row in index_rows}
    fulltext = {
        row["Key_name"] for row in index_rows if row.get("Index_type") == "FULLTEXT"
    }
    assert "PRIMARY" in names
    assert fulltext, f"no FULLTEXT index; indexes present: {sorted(names)}"
