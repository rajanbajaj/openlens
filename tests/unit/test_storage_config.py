"""Backend selection + lifecycle tests (db-connectors plan, step 4).

`storage.backend` is the single switch (`sqlite` | `mysql` | `postgres`);
the shipped `configs/default.yaml` selects `postgres`, while the code
falls back to `sqlite` when no `storage:` section exists (offline
default used here). The provider-entry `db_path` stays the SQLite
backend's field. Offline only: server-backend cases assert construction
fails as `ProviderError(UNAVAILABLE)` (missing driver or refused
connection) and never import a driver.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

from openlens.core.interfaces import (
    ConfigError,
    Document,
    ProviderError,
    SearchRequest,
)
from openlens.main import _manager_section, _storage_section, build_engine
from openlens.providers.custom_index import CustomIndexProvider, open_store

_UNREACHABLE_MYSQL = {
    "backend": "mysql",
    "mysql": {
        "host": "127.0.0.1",
        "port": 1,
        "database": "openlens",
        "user": "root",
        "password": "",
        "pool_size": 1,
        "connect_timeout": 1,
    },
}

_UNREACHABLE_POSTGRES = {
    "backend": "postgres",
    "postgres": {
        "host": "127.0.0.1",
        "port": 1,
        "dbname": "openlens",
        "user": "postgres",
        "password": "",
        "pool_size": 1,
        "connect_timeout": 1,
    },
}


def _minimal_config(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "providers": {
            "strategy": "single",
            "primary": "custom_index",
            "custom_index": {
                "enabled": True,
                "type": "custom_index",
                "db_path": ":memory:",
                "seed": False,
            },
        },
        "plugins": [],
        "ranking": {"default_profile": "default"},
    }
    if extra:
        config.update(extra)
    return config


def _drop_drivers() -> None:
    sys.modules.pop("pymysql", None)
    sys.modules.pop("psycopg", None)


# -- backend switch validation -------------------------------------------


def test_unknown_backend_raises_config_error() -> None:
    provider = CustomIndexProvider(db_path=":memory:")
    with pytest.raises(ConfigError):
        provider.configure({"storage": {"backend": "cassandra"}, "seed": False})
    with pytest.raises(ConfigError):
        open_store({"storage": {"backend": "cassandra"}})
    with pytest.raises(ConfigError):
        _storage_section({"storage": {"backend": "cassandra"}})
    with pytest.raises(ConfigError):
        build_engine(_minimal_config({"storage": {"backend": "cassandra"}}))


def test_non_string_backend_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        open_store({"storage": {"backend": 42}})


def test_malformed_storage_section_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        _storage_section({"storage": ["sqlite"]})
    with pytest.raises(ConfigError):
        _storage_section({"storage": {"backend": "mysql", "mysql": "x"}})
    with pytest.raises(ConfigError):
        open_store({"storage": {"backend": "sqlite", "sqlite": "x"}})
    with pytest.raises(ConfigError):
        CustomIndexProvider(db_path=":memory:").configure(
            {"storage": "sqlite", "seed": False}
        )


def test_empty_storage_mapping_means_sqlite() -> None:
    assert _storage_section({}) == {}
    assert _storage_section({"storage": {}}) == {}
    store = open_store({"db_path": ":memory:"})
    try:
        assert store.count() == 0
    finally:
        store.close()


def test_shipped_default_yaml_selects_postgres() -> None:
    from pathlib import Path

    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    with (repo_root / "configs" / "default.yaml").open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    storage = config.get("storage", {})
    assert storage.get("backend") == "postgres", (
        "[configs/default.yaml] shipped default store must be postgres"
    )
    assert set(storage.get("postgres", {})) >= {
        "host",
        "port",
        "dbname",
        "user",
        "password",
        "table",
    }, "[configs/default.yaml] postgres section must carry connection fields"


# -- unreachable-but-configured backends ---------------------------------


def test_unreachable_mysql_raises_unavailable_at_construction() -> None:
    with pytest.raises(ProviderError) as exc_info:
        open_store({"storage": _UNREACHABLE_MYSQL})
    assert exc_info.value.code == ProviderError.UNAVAILABLE


def test_unreachable_postgres_raises_unavailable_at_construction() -> None:
    with pytest.raises(ProviderError) as exc_info:
        open_store({"storage": _UNREACHABLE_POSTGRES})
    assert exc_info.value.code == ProviderError.UNAVAILABLE


def test_configure_unreachable_backend_raises_unavailable() -> None:
    provider = CustomIndexProvider(db_path=":memory:")
    with pytest.raises(ProviderError) as exc_info:
        provider.configure({"storage": _UNREACHABLE_MYSQL, "seed": False})
    assert exc_info.value.code == ProviderError.UNAVAILABLE


# -- lazy driver imports --------------------------------------------------


def test_backend_modules_import_without_drivers() -> None:
    _drop_drivers()
    importlib.import_module("openlens.storage.mysql_backend")
    importlib.import_module("openlens.storage.postgres_backend")
    assert "pymysql" not in sys.modules
    assert "psycopg" not in sys.modules


def test_unconfigured_backend_driver_never_imported() -> None:
    _drop_drivers()
    importlib.import_module("openlens.storage.mysql_backend")
    importlib.import_module("openlens.storage.postgres_backend")
    before = set(sys.modules)
    assert "pymysql" not in before
    assert "psycopg" not in before
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    assert provider.document_count() == 0
    assert "pymysql" not in sys.modules
    assert "psycopg" not in sys.modules


# -- SQLite default + back-compat -----------------------------------------


def test_sqlite_default_boots_with_no_storage_section() -> None:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    assert provider.document_count() == 0
    provider.index_documents([Document(id="a", title="hello", content="hello world")])
    assert provider.document_count() == 1
    hits = provider.search(SearchRequest(query="hello", limit=10))
    assert [h.id for h in hits] == ["a"]


def test_empty_config_boots_sqlite_memory_default() -> None:
    provider = CustomIndexProvider()
    provider.configure({"seed": False})
    assert provider.document_count() == 0


def test_build_engine_boots_with_no_storage_section() -> None:
    engine = build_engine(_minimal_config())
    assert engine is not None


def test_storage_section_forwarded_to_provider_entry() -> None:
    section = _manager_section(
        _minimal_config({"storage": {"backend": "sqlite"}}),
    )
    entry = section["providers"]["custom_index"]
    assert entry["storage"] == {"backend": "sqlite"}


def test_entry_level_storage_not_overridden() -> None:
    config = _minimal_config({"storage": {"backend": "mysql"}})
    config["providers"]["custom_index"]["storage"] = {"backend": "sqlite"}
    section = _manager_section(config)
    assert section["providers"]["custom_index"]["storage"] == {"backend": "sqlite"}


# -- sqlite db_path resolution --------------------------------------------


def test_storage_sqlite_db_path_wins_over_entry(tmp_path) -> None:
    nested = tmp_path / "new-store" / "idx.db"
    # Provider-level configure does not create parent dirs (build_engine
    # does); create it here so this test pins resolution, not makedirs.
    nested.parent.mkdir(parents=True)
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure(
        {
            "db_path": ":memory:",
            "storage": {"backend": "sqlite", "sqlite": {"db_path": str(nested)}},
            "seed": False,
        }
    )
    assert nested.is_file()
    provider.index_documents([Document(id="a", title="t", content="c")])
    assert provider.document_count() == 1


def test_entry_db_path_fallback_without_sqlite_subsection(tmp_path) -> None:
    target = tmp_path / "entry.db"
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure(
        {"db_path": str(target), "storage": {"backend": "sqlite"}, "seed": False}
    )
    assert target.is_file()


def test_build_engine_precreates_storage_sqlite_parent(tmp_path) -> None:
    nested = tmp_path / "deep" / "nested" / "idx.db"
    assert not nested.parent.exists()
    config = _minimal_config(
        {"storage": {"backend": "sqlite", "sqlite": {"db_path": str(nested)}}}
    )
    engine = build_engine(config)
    assert engine is not None
    assert nested.parent.is_dir()


# -- lifecycle: configure closes the previous repository ------------------


def test_configure_closes_previous_repository() -> None:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    first = provider._repository
    provider.index_documents([Document(id="a", title="t", content="c")])
    assert provider.document_count() == 1
    provider.configure({"db_path": ":memory:", "seed": False})
    assert first._closed is True
    # The new store is isolated (seed disabled) and functional.
    assert provider._repository is not first
    assert provider.document_count() == 0


def test_reconfigure_sqlite_keeps_search_working() -> None:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": True})
    assert provider.document_count() > 0
    hits = provider.search(SearchRequest(query="python", limit=10))
    assert hits


def test_provider_close_is_idempotent() -> None:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    provider.close()
    provider.close()
