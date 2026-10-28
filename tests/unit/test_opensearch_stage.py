"""Unit tests for the blue-green OpenSearch lifecycle (offline fakes).

Every test runs without a cluster: a fake client records index/alias
calls, and ``opensearchpy.helpers.bulk`` is monkeypatched to land
documents in the fake. Covers naming, idempotent upserts, the atomic
alias flip, prune safety (never deletes the live index), the migrate
copy path, config validation, and secret safety (failures echo counts
and exception types only — never document bodies or credentials).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from openlens.core.interfaces import ConfigError, Document, ProviderError
from openlens.indexing import opensearch_stage as stage


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeNotFound(Exception):
    """Mimics opensearch-py's NotFoundError string shape (404 inside)."""


class FakeIndices:
    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self.created: dict[str, dict[str, Any]] = {}
        self.put_settings_calls: list[tuple[str, dict[str, Any]]] = []
        self.refreshed: list[str] = []
        self.deleted: list[str] = []
        self.update_calls: list[dict[str, Any]] = []
        self.aliases: dict[str, list[str]] = {}

    def create(self, index: str, body: dict[str, Any]) -> dict[str, bool]:
        self.created[index] = body
        self._client.docs.setdefault(index, {})
        self._client.mappings[index] = dict(body.get("mappings", {}))
        return {"acknowledged": True}

    def put_settings(self, index: str, body: dict[str, Any]) -> dict[str, bool]:
        self.put_settings_calls.append((index, body))
        return {"acknowledged": True}

    def refresh(self, index: str) -> dict[str, Any]:
        self.refreshed.append(index)
        return {}

    def exists(self, index: str) -> bool:
        return index in self._client.docs

    def get(self, index: str) -> dict[str, Any]:
        prefix = index[:-2] if index.endswith("_*") else index
        return {name: {} for name in self._client.docs if name.startswith(prefix)}

    def get_mapping(self, index: str) -> dict[str, Any]:
        if index not in self._client.mappings:
            raise FakeNotFound(f"NotFoundError(404, 'index {index} missing')")
        return {index: {"mappings": dict(self._client.mappings[index])}}

    def delete(self, index: str) -> dict[str, bool]:
        self.deleted.append(index)
        self._client.docs.pop(index, None)
        return {"acknowledged": True}

    def get_alias(self, name: str) -> dict[str, Any]:
        targets = self.aliases.get(name, [])
        if not targets:
            raise FakeNotFound(f"NotFoundError(404, 'alias {name} missing')")
        return {index: {"aliases": {name: {}}} for index in targets}

    def update_aliases(self, body: dict[str, Any]) -> dict[str, bool]:
        self.update_calls.append(body)
        for action in body.get("actions", []):
            if "add" in action:
                add = action["add"]
                current = self.aliases.setdefault(add["alias"], [])
                if add["index"] not in current:
                    current.append(add["index"])
            elif "remove" in action:
                remove = action["remove"]
                current = self.aliases.setdefault(remove["alias"], [])
                if remove["index"] in current:
                    current.remove(remove["index"])
        return {"acknowledged": True}


class FakeClient:
    """Minimal in-memory OpenSearch stand-in keyed by index -> id -> source."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict[str, Any]]] = {}
        self.mappings: dict[str, dict[str, Any]] = {}
        self.indices = FakeIndices(self)
        self.uploads: list[tuple[str, list[dict[str, Any]]]] = []
        self.cleared_scrolls: list[Any] = []

    def count(self, index: str) -> dict[str, int]:
        return {"count": len(self.docs.get(index, {}))}

    def search(
        self, index: str, body: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        rows = self.docs.get(index, {})
        size = (body or {}).get("size", 10)
        hits = [
            {"_id": doc_id, "_source": source}
            for doc_id, source in list(rows.items())[:size]
        ]
        page: dict[str, Any] = {"hits": {"hits": hits}}
        if "scroll" in kwargs:
            page["_scroll_id"] = f"scroll-{index}-{len(hits)}"
            page["_remaining"] = list(rows.items())[len(hits) :]
        return page

    def scroll(self, scroll_id: str, scroll: str) -> dict[str, Any]:
        return {"_scroll_id": scroll_id, "hits": {"hits": []}}

    def clear_scroll(self, scroll_id: str) -> dict[str, bool]:
        self.cleared_scrolls.append(scroll_id)
        return {"succeeded": True}


class PagedFakeClient(FakeClient):
    """Fake client that pages scroll reads in fixed-size chunks."""

    def __init__(self, page_size: int = 2) -> None:
        super().__init__()
        self._page_size = page_size
        self._cursors: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def search(
        self, index: str, body: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if "scroll" not in kwargs:
            return super().search(index, body, **kwargs)
        rows = list(self.docs.get(index, {}).items())
        return self._page(f"scroll-{index}-0", rows)

    def _page(
        self, scroll_id: str, rows: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        chunk, rest = rows[: self._page_size], rows[self._page_size :]
        self._cursors[scroll_id] = rest
        return {
            "_scroll_id": scroll_id,
            "hits": {
                "hits": [{"_id": doc_id, "_source": source} for doc_id, source in chunk]
            },
        }

    def scroll(self, scroll_id: str, scroll: str) -> dict[str, Any]:
        return self._page(scroll_id, self._cursors.get(scroll_id, []))


def _fake_bulk(client: FakeClient, actions: Any, **kwargs: Any) -> tuple[int, list]:
    items = list(actions)
    for action in items:
        client.docs.setdefault(action["_index"], {})[action["_id"]] = action["_source"]
    client.uploads.append((items[0]["_index"], items))
    return len(items), []


def _doc(
    doc_id: str = "d1",
    title: str = "title",
    content: str = "content",
    url: str | None = "HTTPS://Example.com/Page/",
) -> Document:
    return Document(id=doc_id, title=title, content=content, url=url)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def _bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opensearchpy.helpers.bulk", _fake_bulk)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_validate_section_defaults():
    settings = stage.validate_section({"hosts": ["http://a:9200"]})
    assert settings["hosts"] == ["http://a:9200"]
    assert settings["alias"] == stage.DEFAULT_ALIAS
    assert settings["index_prefix"] == stage.DEFAULT_PREFIX
    assert settings["timeout_s"] == 5
    assert settings["verify_certs"] is True


def test_validate_section_absent_or_empty_hosts_rejected():
    with pytest.raises(ConfigError):
        stage.validate_section(None)
    with pytest.raises(ConfigError):
        stage.validate_section({})


def test_validate_section_accepts_str_host_and_full_shape():
    settings = stage.validate_section(
        {
            "hosts": "http://a:9200",
            "alias": "serve",
            "index_prefix": "idx",
            "timeout_s": 7,
            "username": "u",
            "password": "p",
            "verify_certs": False,
        }
    )
    assert settings["hosts"] == ["http://a:9200"]
    assert settings["timeout_s"] == 7


@pytest.mark.parametrize(
    "section",
    [
        {"hosts": []},
        {"hosts": ["", ""]},
        {"hosts": [123]},
        {"alias": ""},
        {"index_prefix": ""},
        {"timeout_s": 0},
        {"timeout_s": -1},
        {"timeout_s": "soon"},
        "not-a-mapping",
    ],
)
def test_validate_section_rejects_malformed(section: Any):
    with pytest.raises(ConfigError):
        stage.validate_section(section)


# ---------------------------------------------------------------------------
# Naming, ids, source rendering
# ---------------------------------------------------------------------------


def test_stage_index_name_is_sortable_utc():
    moment = datetime(2026, 10, 28, 1, 2, 3, tzinfo=timezone.utc)
    assert stage.stage_index_name("openlens", moment) == "openlens_20261028010203"


def test_stage_document_id_prefers_normalized_url():
    assert stage.stage_document_id(_doc()) == "https://example.com/page"


def test_stage_document_id_falls_back_to_stored_id():
    assert stage.stage_document_id(_doc(url=None)) == "d1"


def test_to_source_doc_carries_hash_and_crawl_id():
    source = stage.to_source_doc(_doc(), "20261028")
    assert source["title"] == "title"
    assert source["url"] == "HTTPS://Example.com/Page/"
    assert source[stage.HASH_FIELD] == stage.content_hash_of("content")
    assert source[stage.CRAWL_FIELD] == "20261028"
    assert source["metadata"] == {}


# ---------------------------------------------------------------------------
# Create / load / finalize / verify
# ---------------------------------------------------------------------------


def test_create_stage_index_uses_load_settings_and_mappings(client: FakeClient):
    name = stage.create_stage_index(client, "openlens", datetime(2026, 1, 1))
    assert name == "openlens_20260101000000"
    body = client.indices.created[name]
    assert body["settings"]["index"]["refresh_interval"] == "-1"
    assert "title" in body["mappings"]["properties"]
    assert stage.HASH_FIELD in body["mappings"]["properties"]
    assert stage.CRAWL_FIELD in body["mappings"]["properties"]


def test_bulk_load_upserts_stable_ids(client: FakeClient, _bulk: None) -> None:
    docs = [_doc(), _doc("d2", url="https://example.com/other")]
    first = stage.bulk_load(client, "idx", docs, "c1")
    again = stage.bulk_load(client, "idx", docs, "c2")
    assert (first, again) == (2, 2)
    assert len(client.docs["idx"]) == 2  # repeat overwrites, no duplicates
    assert client.docs["idx"]["https://example.com/page"][stage.CRAWL_FIELD] == "c2"


def test_bulk_load_failure_echoes_no_bodies(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(client: Any, actions: Any, **kwargs: Any) -> tuple[int, list]:
        raise RuntimeError("cluster down")

    monkeypatch.setattr("opensearchpy.helpers.bulk", _boom)
    with pytest.raises(ProviderError) as excinfo:
        stage.bulk_load(client, "idx", [_doc(content="S3CR3T-body")], "c1")
    assert "S3CR3T-body" not in str(excinfo.value)


def test_finalize_restores_serving_settings(client: FakeClient) -> None:
    client.docs["idx"] = {"a": {}}
    assert stage.finalize_stage(client, "idx") == 1
    index, body = client.indices.put_settings_calls[0]
    assert index == "idx"
    assert body["index"]["refresh_interval"] == "1s"
    assert client.indices.refreshed == ["idx"]


def test_verify_ok_returns_count(client: FakeClient) -> None:
    client.docs["idx"] = {"a": {"title": "t"}}
    assert stage.verify_stage(client, "idx") == 1


def test_verify_rejects_thin_or_unreadable_index(client: FakeClient) -> None:
    client.docs["empty"] = {}
    with pytest.raises(ProviderError):
        stage.verify_stage(client, "empty", min_docs=1)
    client.docs["thin"] = {"a": {"title": "t"}}
    with pytest.raises(ProviderError):
        stage.verify_stage(client, "thin", min_docs=2)


# ---------------------------------------------------------------------------
# Alias promotion (atomic) and prune safety
# ---------------------------------------------------------------------------


def test_promote_flips_alias_in_one_atomic_call(client: FakeClient) -> None:
    client.docs.update({"old": {"a": {}}, "new": {"b": {}}})
    client.indices.aliases["openlens"] = ["old"]
    previous = stage.promote(client, "openlens", "new")
    assert previous == ["old"]
    assert client.indices.aliases["openlens"] == ["new"]
    assert len(client.indices.update_calls) == 1
    actions = client.indices.update_calls[0]["actions"]
    assert {"remove": {"index": "old", "alias": "openlens"}} in actions
    assert {"add": {"index": "new", "alias": "openlens"}} in actions


def test_promote_first_flip_has_no_previous(client: FakeClient) -> None:
    client.docs["new"] = {"b": {}}
    assert stage.promote(client, "openlens", "new") == []
    assert client.indices.aliases["openlens"] == ["new"]


def test_promote_missing_index_never_touches_alias(client: FakeClient) -> None:
    with pytest.raises(ProviderError):
        stage.promote(client, "openlens", "ghost")
    assert client.indices.update_calls == []


def test_alias_targets_sorted_and_empty_when_absent(client: FakeClient) -> None:
    assert stage.alias_targets(client, "openlens") == []
    client.indices.aliases["openlens"] = ["b", "a"]
    assert stage.alias_targets(client, "openlens") == ["a", "b"]


def test_prune_never_deletes_live_and_keeps_newest(client: FakeClient) -> None:
    for name in ("openlens_1", "openlens_2", "openlens_3", "other"):
        client.docs[name] = {}
    client.indices.aliases["openlens"] = ["openlens_3"]
    deleted = stage.prune(client, "openlens", "openlens", keep=1)
    assert deleted == ["openlens_1"]
    assert "openlens_3" in client.docs  # live index survives
    assert "other" in client.docs  # other prefixes untouched


def test_prune_rejects_negative_keep(client: FakeClient) -> None:
    with pytest.raises(ValueError):
        stage.prune(client, "openlens", "openlens", keep=-1)


def test_latest_stage_index_newest_or_none(client: FakeClient) -> None:
    assert stage.latest_stage_index(client, "openlens") is None
    client.docs["openlens_1"] = {}
    client.docs["openlens_2"] = {}
    assert stage.latest_stage_index(client, "openlens") == "openlens_2"


# ---------------------------------------------------------------------------
# Migrate (copy) path
# ---------------------------------------------------------------------------


def test_copy_index_preserves_ids_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source: FakeClient = PagedFakeClient(page_size=2)
    source.docs["src"] = {f"d{i}": {"title": f"t{i}"} for i in range(5)}
    source.mappings["src"] = {"properties": {"title": {"type": "text"}}}
    monkeypatch.setattr("opensearchpy.helpers.bulk", _fake_bulk)
    dest = FakeClient()
    written = stage.copy_index(source, dest, "src", "dst")
    assert written == 5
    assert set(dest.docs["dst"]) == {f"d{i}" for i in range(5)}
    assert dest.mappings["dst"] == {"properties": {"title": {"type": "text"}}}
    assert ("dst", {"index": {"refresh_interval": "1s"}}) in [
        (index, body) for index, body in dest.indices.put_settings_calls
    ]
    assert dest.indices.refreshed == ["dst"]
    assert source.cleared_scrolls  # scroll context released


def test_copy_index_rerun_overwrites_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeClient()
    source.docs["src"] = {"d1": {"title": "t"}}
    source.mappings["src"] = {}
    monkeypatch.setattr("opensearchpy.helpers.bulk", _fake_bulk)
    dest = FakeClient()
    assert stage.copy_index(source, dest, "src", "dst") == 1
    dest.docs["dst"]["stale"] = {"title": "stale"}
    assert stage.copy_index(source, dest, "src", "dst") == 1
    assert list(dest.docs["dst"]) == ["d1"]  # stale ids vanish with replace


def test_build_client_without_driver_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "opensearchpy", None)
    with pytest.raises(ProviderError) as excinfo:
        stage.build_client(["http://localhost:9200"])
    assert excinfo.value.code == ProviderError.UNAVAILABLE
