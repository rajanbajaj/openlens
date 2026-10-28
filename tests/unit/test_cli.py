"""Unit tests for the operator CLI (``openlens.cli``).

All tests call :func:`openlens.cli.main` directly with an argv list (no
network, no keys, no cluster). The engine, repository, and indexer entry
point are injected through the module's documented seams
(``openlens.cli.build_engine``, ``openlens.cli.DocumentRepository``,
``openlens.indexing.run_index``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _pytest.capture import CaptureFixture
from _pytest.monkeypatch import MonkeyPatch

import openlens.cli as cli
import openlens.indexing as indexing_api
import openlens.main as main_module
from openlens.api.schemas import SearchResponseSchema
from openlens.core.interfaces import (
    ConfigError,
    Engine,
    ProviderError,
    ProviderPrefs,
    SearchRequest,
    SearchResponse,
    SearchResult,
)


class FakeEngine(Engine):
    """In-memory Engine: records requests, replays canned responses."""

    def __init__(
        self,
        response: SearchResponse | None = None,
        health: dict[str, bool] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.requests: list[SearchRequest] = []
        self._response = response
        self._health = dict(health) if health is not None else {"fake": True}
        self._error = error

    def configure(self, config: dict[str, Any]) -> None:
        return None

    def search(self, request: SearchRequest) -> SearchResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if self._response is not None:
            return self._response
        return SearchResponse(query=request.query, mode=request.mode)

    def health_check(self) -> dict[str, bool]:
        return dict(self._health)


def _hit(hit_id: str = "doc-1", provider: str = "fake") -> SearchResult:
    return SearchResult(
        id=hit_id,
        title="A title",
        snippet="a snippet",
        original_score=1.0,
        final_score=0.9,
        url="http://example.com/1",
        provider=provider,
        reasons=["test"],
    )


def _response() -> SearchResponse:
    return SearchResponse(
        query="hello",
        mode="default",
        results=[_hit()],
        total=1,
        applied_filters=[],
        diagnostics={"fallback_used": False},
    )


def _install_engine(
    monkeypatch: MonkeyPatch, engine: FakeEngine
) -> list[SearchRequest]:
    monkeypatch.setattr(cli, "build_engine", lambda config: engine)
    monkeypatch.setattr(cli, "load_app_config", lambda path=None: {"ok": True})
    return engine.requests


def test_search_json_matches_response_schema(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    requests = _install_engine(monkeypatch, FakeEngine(response=_response()))
    assert cli.main(["search", "hello", "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    SearchResponseSchema(**payload)  # must satisfy the wire schema
    assert payload["query"] == "hello"
    assert payload["results"][0]["id"] == "doc-1"
    assert requests[0].provider_prefs == ProviderPrefs()


def test_search_emits_one_json_log_line_with_request_id(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    """Plan falsifiable claim: CLI search logs one JSON INFO record.

    The record carries a generated request_id and never the raw query
    (stdout keeps the response payload; the log line goes to stderr).
    """
    _install_engine(monkeypatch, FakeEngine(response=_response()))
    assert cli.main(["search", "hello", "--json"]) == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["request_id"]
    assert record["mode"] == "default"
    assert record["limit"] == 10
    assert "hello" not in lines[0]
    # Response payload is untouched on stdout.
    assert json.loads(captured.out)["query"] == "hello"


def test_search_pretty_is_human_readable(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    engine = FakeEngine(response=_response())
    requests = _install_engine(monkeypatch, engine)
    assert cli.main(["search", "hello", "--pretty"]) == 0
    out = capsys.readouterr().out
    assert "doc-1" in out
    assert "A title" in out
    assert "1 result(s)" in out
    # Same payload semantics as the JSON rendering: identical request.
    assert requests[0].query == "hello"
    assert requests[0].mode == "default"
    assert requests[0].limit == 10


def test_search_mode_limit_and_provider_forwarded(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    requests = _install_engine(monkeypatch, FakeEngine(response=_response()))
    assert (
        cli.main(
            [
                "search",
                "q",
                "--mode",
                "news",
                "--limit",
                "3",
                "--provider",
                "custom_index",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert requests[0].mode == "news"
    assert requests[0].limit == 3
    # --provider must reach SearchRequest.provider_prefs, never be ignored.
    assert requests[0].provider_prefs == ProviderPrefs(primary="custom_index")


def test_search_engine_failure_is_nonzero_without_traceback(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    _install_engine(
        monkeypatch,
        FakeEngine(error=ProviderError("boom", provider_name="fake")),
    )
    assert cli.main(["search", "q"]) != 0
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("subcommand", ["add", "update", "reindex"])
def test_index_mutating_subcommands_delegate_to_run_index(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    subcommand: str,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeStats:
        added = 2
        updated = 1
        skipped_unchanged = 0
        skipped_duplicates = 0

    def fake_run_index(
        urls: list[str], repository: Any, config: dict[str, Any]
    ) -> FakeStats:
        calls.append((urls, repository, config))
        return FakeStats()

    monkeypatch.setattr(indexing_api, "run_index", fake_run_index)
    monkeypatch.setattr(
        cli,
        "load_app_config",
        lambda path=None: {
            "providers": {"custom_index": {"db_path": ":memory:"}},
            "indexing": {"max_pages": 5},
        },
    )
    sentinel = object()
    monkeypatch.setattr(cli, "DocumentRepository", lambda db_path: sentinel)

    assert cli.main(["index", subcommand, "http://example.com/1"]) == 0
    out = capsys.readouterr().out
    assert len(calls) == 1
    assert calls[0][0] == ["http://example.com/1"]
    assert calls[0][1] is sentinel  # repository seam, not raw sqlite
    assert calls[0][2] == {"max_pages": 5}
    assert "added=2" in out


def test_index_not_available_exits_nonzero(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(indexing_api, "run_index", None)
    monkeypatch.setattr(
        cli,
        "load_app_config",
        lambda path=None: {"providers": {}},
    )
    monkeypatch.setattr(cli, "DocumentRepository", lambda db_path: object())
    assert cli.main(["index", "add", "http://example.com/1"]) != 0
    captured = capsys.readouterr()
    assert "not available" in captured.err.lower()


def test_index_list_reports_repository_count(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    class FakeRepo:
        def count(self) -> int:
            return 7

    monkeypatch.setattr(cli, "load_app_config", lambda path=None: {"providers": {}})
    monkeypatch.setattr(cli, "DocumentRepository", lambda db_path: FakeRepo())
    assert cli.main(["index", "list"]) == 0
    assert "7" in capsys.readouterr().out


def test_config_validate_passes_on_default_yaml(
    capsys: CaptureFixture[str],
) -> None:
    assert cli.main(["config", "validate"]) == 0
    assert "OK" in capsys.readouterr().out


def test_config_validate_rejects_malformed_tmp_config(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("{invalid: [yaml\n\tbad indent: : :", encoding="utf-8")
    assert cli.main(["config", "validate", "--config", str(bad)]) != 0
    captured = capsys.readouterr()
    assert captured.err.strip()  # human-readable stderr
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_config_validate_rejects_semantically_bad_config(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("providers: []\n", encoding="utf-8")
    assert cli.main(["config", "validate", "--config", str(bad)]) != 0
    assert "Traceback" not in capsys.readouterr().err


def test_config_validate_does_not_boot_server(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    def _bomb(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("server boot path must not be used")

    monkeypatch.setattr(main_module, "create_app", _bomb)
    assert cli.main(["config", "validate"]) == 0
    capsys.readouterr()


def test_config_validate_creates_no_data_side_effects(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "good.yaml"
    config.write_text(
        "providers:\n"
        "  strategy: single\n"
        "  primary: custom_index\n"
        "  custom_index:\n"
        "    enabled: true\n"
        "    type: custom_index\n"
        "    db_path: ':memory:'\n"
        "plugins: []\n"
        "ranking:\n"
        "  default_profile: default\n",
        encoding="utf-8",
    )
    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    capsys.readouterr()
    assert not (tmp_path / "data").exists()
    assert list(tmp_path.glob("*.db")) == []


def test_providers_health_reports_offline_table(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    engine = FakeEngine(health={"alpha": True, "beta": False})
    _install_engine(monkeypatch, engine)
    assert cli.main(["providers", "health"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "reachable" in out
    assert "beta" in out and "unreachable" in out


def test_providers_health_build_failure_is_nonzero(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    def _fail(config: dict[str, Any]) -> Engine:
        raise ConfigError("no providers")

    monkeypatch.setattr(cli, "build_engine", _fail)
    monkeypatch.setattr(cli, "load_app_config", lambda path=None: {})
    assert cli.main(["providers", "health"]) != 0
    assert "Traceback" not in capsys.readouterr().err


def test_module_entrypoint_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "openlens.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert "search" in proc.stdout
