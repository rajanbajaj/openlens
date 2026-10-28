"""Config resolution for installed-package layouts (e.g. docker).

`pip install .` copies the package to site-packages, which has no sibling
configs/ — so load_app_config must fall back past the source-tree default.
"""

from __future__ import annotations

from openlens.main import load_app_config

MINIMAL = (
    "providers:\n"
    "  strategy: single\n"
    "  primary: custom_index\n"
    "  custom_index:\n"
    "    enabled: true\n"
    "    type: custom_index\n"
)


def test_explicit_path_honored(tmp_path):
    cfg = tmp_path / "custom.yaml"
    cfg.write_text(MINIMAL)
    assert load_app_config(cfg)["providers"]["primary"] == "custom_index"


def test_openlens_config_env_honored(tmp_path, monkeypatch):
    cfg = tmp_path / "env.yaml"
    cfg.write_text(MINIMAL)
    monkeypatch.setenv("OPENLENS_CONFIG", str(cfg))
    assert load_app_config()["providers"]["primary"] == "custom_index"


def test_cwd_fallback(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "configs").mkdir(parents=True)
    (work / "configs" / "default.yaml").write_text(MINIMAL)
    monkeypatch.chdir(work)
    monkeypatch.delenv("OPENLENS_CONFIG", raising=False)
    assert load_app_config()["providers"]["primary"] == "custom_index"


def test_missing_everywhere_yields_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENLENS_CONFIG", raising=False)
    assert load_app_config(tmp_path / "definitely.yaml") == {}


BOOT_OVERLAY = (
    "providers:\n"
    "  strategy: single\n"
    "  primary: custom_index\n"
    "  custom_index:\n"
    "    enabled: true\n"
    "    type: custom_index\n"
    "    timeout_s: 5\n"
    '    db_path: "{db}"\n'
    "plugins: []\n"
    "ranking: {{}}\n"
)


def _boot_overlay(tmp_path, monkeypatch):
    from pathlib import Path

    db = Path(str(tmp_path)) / "boot.db"
    cfg = Path(str(tmp_path)) / "boot.yaml"
    cfg.write_text(BOOT_OVERLAY.format(db=db))
    monkeypatch.setenv("OPENLENS_CONFIG", str(cfg))
    return db


def test_create_app_alone_writes_nothing(tmp_path, monkeypatch):
    """Import-time and factory-time must not touch the index.

    Regression: the module-level default-engine boot used to seed every
    CLI invocation's store at import time.
    """
    import openlens.main as main_module

    db = _boot_overlay(tmp_path, monkeypatch)
    app = main_module.create_app()
    assert app.state.engine is None
    assert not db.exists()


def test_lifespan_boots_default_engine_on_serve(tmp_path, monkeypatch):
    import openlens.main as main_module
    from fastapi.testclient import TestClient

    db = _boot_overlay(tmp_path, monkeypatch)
    app = main_module.create_app()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert app.state.engine is not None
    assert db.exists()  # seeded at serve-boot, never at import/factory time
