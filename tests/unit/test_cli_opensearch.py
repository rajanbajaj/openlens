"""Unit tests for the ``opensearch`` CLI group (offline fakes).

``cli.main`` is called with argv lists; the OpenSearch client boundary is
faked (a tiny in-memory client), ``opensearchpy.helpers.bulk`` is
monkeypatched, and the repository/config seams are injected through the
module's documented seams. Covers every subcommand's happy path plus
validation failures and credential safety (sentinel passwords in the
environment never reach stdout/stderr).
"""

from __future__ import annotations

from typing import Any

import pytest
from _pytest.capture import CaptureFixture
from _pytest.monkeypatch import MonkeyPatch

import openlens.cli as cli
from openlens.core.interfaces import Document, ProviderError


class CliFakeClient:
    """Smallest client the CLI wiring needs: indices ops + count/search."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict[str, Any]]] = {}
        self.mappings: dict[str, dict[str, Any]] = {}
        self.indices = self.Indices(self)
        self.aliases: dict[str, list[str]] = {}

    class Indices:
        def __init__(self, outer: CliFakeClient) -> None:
            self._outer = outer

        def create(self, index: str, body: dict[str, Any]) -> dict[str, bool]:
            self._outer.docs.setdefault(index, {})
            self._outer.mappings[index] = dict(body.get("mappings", {}))
            return {"acknowledged": True}

        def put_settings(self, index: str, body: dict[str, Any]) -> dict[str, bool]:
            return {"acknowledged": True}

        def refresh(self, index: str) -> dict[str, Any]:
            return {}

        def exists(self, index: str) -> bool:
            return index in self._outer.docs

        def get(self, index: str) -> dict[str, Any]:
            prefix = index[:-2] if index.endswith("_*") else index
            return {name: {} for name in self._outer.docs if name.startswith(prefix)}

        def get_mapping(self, index: str) -> dict[str, Any]:
            if index not in self._outer.mappings:
                raise Exception(f"NotFoundError(404, 'index {index} missing')")
            return {index: {"mappings": dict(self._outer.mappings[index])}}

        def delete(self, index: str) -> dict[str, bool]:
            self._outer.docs.pop(index, None)
            return {"acknowledged": True}

        def get_alias(self, name: str) -> dict[str, Any]:
            targets = self._outer.aliases.get(name, [])
            if not targets:
                raise Exception(f"NotFoundError(404, 'alias {name} missing')")
            return {index: {} for index in targets}

        def update_aliases(self, body: dict[str, Any]) -> dict[str, bool]:
            for action in body.get("actions", []):
                if "add" in action:
                    add = action["add"]
                    current = self._outer.aliases.setdefault(add["alias"], [])
                    if add["index"] not in current:
                        current.append(add["index"])
                elif "remove" in action:
                    remove = action["remove"]
                    current = self._outer.aliases.setdefault(remove["alias"], [])
                    if remove["index"] in current:
                        current.remove(remove["index"])
            return {"acknowledged": True}

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
            page["_scroll_id"] = "scroll-1"
        return page

    def scroll(self, scroll_id: str, scroll: str) -> dict[str, Any]:
        return {"_scroll_id": scroll_id, "hits": {"hits": []}}

    def clear_scroll(self, scroll_id: str) -> dict[str, bool]:
        return {"succeeded": True}


def _fake_bulk(client: CliFakeClient, actions: Any, **kwargs: Any) -> tuple[int, list]:
    items = list(actions)
    for action in items:
        client.docs.setdefault(action["_index"], {})[action["_id"]] = action["_source"]
    return len(items), []


def _repo_with_docs() -> Any:
    from openlens.storage.repository import DocumentRepository

    repo = DocumentRepository(":memory:")
    repo.add_document(
        Document(id="a", title="t1", content="c1", url="https://x.test/1")
    )
    repo.add_document(
        Document(id="b", title="t2", content="c2", url="https://x.test/2")
    )
    return repo


@pytest.fixture
def client() -> CliFakeClient:
    return CliFakeClient()


def _wire(
    monkeypatch: MonkeyPatch,
    client: CliFakeClient,
    repo: Any | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    monkeypatch.setattr(
        "opensearchpy.helpers.bulk", lambda c, actions, **kw: _fake_bulk(c, actions)
    )
    monkeypatch.setattr(cli.stage_api, "build_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(
        cli,
        "load_app_config",
        lambda path=None: (
            {"opensearch_index": {"hosts": ["http://a:9200"]}}
            if config is None
            else config
        ),
    )
    if repo is not None:
        monkeypatch.setattr(cli, "_open_repository", lambda config: repo)


def test_stage_creates_timestamped_index(
    monkeypatch: MonkeyPatch, client: CliFakeClient, capsys: CaptureFixture
) -> None:
    _wire(monkeypatch, client)
    assert cli.main(["opensearch", "stage"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("stage ready: openlens_")
    assert len(client.docs) == 1


def test_load_fills_latest_stage_and_verifies(
    monkeypatch: MonkeyPatch, client: CliFakeClient, capsys: CaptureFixture
) -> None:
    repo = _repo_with_docs()
    _wire(monkeypatch, client, repo)
    try:
        assert cli.main(["opensearch", "load"]) == 0
    finally:
        repo.close()
    out = capsys.readouterr().out
    assert "loaded 2 document(s)" in out
    name = next(iter(client.docs))
    assert len(client.docs[name]) == 2


def test_load_refuses_empty_store(
    monkeypatch: MonkeyPatch, client: CliFakeClient, capsys: CaptureFixture
) -> None:
    from openlens.storage.repository import DocumentRepository

    repo = DocumentRepository(":memory:")
    _wire(monkeypatch, client, repo)
    try:
        assert cli.main(["opensearch", "load"]) == cli.FAILURE_EXIT
    finally:
        repo.close()
    assert "0 documents" in capsys.readouterr().err


def test_load_rejects_bad_chunk_size(
    monkeypatch: MonkeyPatch, client: CliFakeClient
) -> None:
    _wire(monkeypatch, client, _repo_with_docs())
    assert cli.main(["opensearch", "load", "--chunk-size", "0"]) == cli.FAILURE_EXIT


def test_verify_promote_status_prune_flow(
    monkeypatch: MonkeyPatch, client: CliFakeClient, capsys: CaptureFixture
) -> None:
    repo = _repo_with_docs()
    _wire(monkeypatch, client, repo)
    try:
        assert cli.main(["opensearch", "load"]) == 0
    finally:
        repo.close()
    name = next(iter(client.docs))
    capsys.readouterr()
    assert cli.main(["opensearch", "verify", "--index", name]) == 0
    assert f"{name} serves 2 document(s)" in capsys.readouterr().out
    assert cli.main(["opensearch", "promote", "--index", name]) == 0
    out = capsys.readouterr().out
    assert f"alias openlens now serves {name}" in out
    assert "cache purge" in out
    assert cli.main(["opensearch", "status"]) == 0
    assert f"openlens -> {name} (2 document(s))" in capsys.readouterr().out
    assert cli.main(["opensearch", "rollback", "--index", name]) == 0
    assert cli.main(["opensearch", "prune"]) == 0
    assert "nothing to prune" in capsys.readouterr().out


def test_promote_missing_index_fails(
    monkeypatch: MonkeyPatch, client: CliFakeClient, capsys: CaptureFixture
) -> None:
    _wire(monkeypatch, client)
    assert cli.main(["opensearch", "promote", "--index", "ghost"]) == cli.FAILURE_EXIT
    assert "promote failed" in capsys.readouterr().err


def test_invalid_section_fails_loudly(
    monkeypatch: MonkeyPatch, client: CliFakeClient, capsys: CaptureFixture
) -> None:
    _wire(monkeypatch, client, config={"opensearch_index": {"hosts": []}})
    assert cli.main(["opensearch", "status"]) == cli.FAILURE_EXIT
    assert "invalid opensearch_index section" in capsys.readouterr().err


def test_migrate_copies_between_endpoints(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture
) -> None:
    source, dest = CliFakeClient(), CliFakeClient()
    source.docs["idx"] = {"d1": {"title": "t"}}
    source.mappings["idx"] = {"properties": {}}

    def _build(hosts: list[str], *args: Any, **kwargs: Any) -> CliFakeClient:
        return source if hosts == ["http://src:9200"] else dest

    monkeypatch.setattr("opensearchpy.helpers.bulk", _fake_bulk)
    monkeypatch.setattr(cli.stage_api, "build_client", _build)
    assert (
        cli.main(
            [
                "opensearch",
                "migrate",
                "--source",
                "http://src:9200",
                "--dest",
                "http://dst:9200",
                "--index",
                "idx",
            ]
        )
        == 0
    )
    assert "migrated 1 document(s): idx -> idx" in capsys.readouterr().out
    assert list(dest.docs["idx"]) == ["d1"]


def test_migrate_failure_never_echoes_credentials(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture
) -> None:
    sentinel = "pw-OPENSEARCH-MIGRATE-SENTINEL-9f3c"
    monkeypatch.setenv("OPENSEARCH_SOURCE_PASSWORD", sentinel)
    monkeypatch.setenv("OPENSEARCH_DEST_PASSWORD", sentinel)

    def _boom(*args: Any, **kwargs: Any) -> CliFakeClient:
        raise ProviderError("boom", provider_name="x", code="unavailable")

    monkeypatch.setattr(cli.stage_api, "build_client", _boom)
    assert (
        cli.main(
            [
                "opensearch",
                "migrate",
                "--source",
                "http://src:9200",
                "--dest",
                "http://dst:9200",
                "--index",
                "idx",
            ]
        )
        == cli.FAILURE_EXIT
    )
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert "migrate failed" in captured.err


def test_prune_rejects_negative_keep(
    monkeypatch: MonkeyPatch, client: CliFakeClient
) -> None:
    _wire(monkeypatch, client)
    assert cli.main(["opensearch", "prune", "--keep", "-1"]) == cli.FAILURE_EXIT
