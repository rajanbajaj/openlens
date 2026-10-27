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
