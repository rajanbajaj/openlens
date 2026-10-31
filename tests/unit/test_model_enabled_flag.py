"""MODEL_ENABLED env flag: string-safe enable parsing + coherent primary.

Regression: setting only MODEL_API_KEY never enabled the Meta backend
(``openai_compat`` shipped ``enabled: false`` with no env override), so AI
Mode fail-closed with "no model backend enabled" on a correctly-keyed
deploy. ``enabled`` now accepts ``MODEL_ENABLED=true``; interpolation
resolves env flags to *strings*, so parsing is explicit (a non-empty
string is always truthy — naive truthiness would enable unconditionally).
"""

from __future__ import annotations

import pytest

from openlens.core.engine import _build_model_manager
from openlens.core.interfaces import ConfigError
from openlens.models.manager import ModelManager, _is_enabled


def test_is_enabled_bool_and_int() -> None:
    assert _is_enabled(True) is True
    assert _is_enabled(False) is False
    assert _is_enabled(1) is True
    assert _is_enabled(0) is False


def test_is_enabled_strings_case_insensitive() -> None:
    for raw in ("true", "TRUE", " True ", "yes", "YES", "1", "on", "ON"):
        assert _is_enabled(raw) is True, raw
    for raw in ("false", "FALSE", " False ", "0", "no", "off", "", "maybe", "2"):
        assert _is_enabled(raw) is False, raw


def test_is_enabled_rejects_non_strings() -> None:
    assert _is_enabled(None) is False
    assert _is_enabled([]) is False
    assert _is_enabled(object()) is False


def test_from_config_accepts_string_true() -> None:
    manager = ModelManager.from_config(
        {
            "models": {
                "primary": "fake",
                "fake": {"enabled": "true", "type": "fake"},
            }
        }
    )
    assert manager.primary == "fake"


def test_from_config_treats_string_false_as_disabled() -> None:
    with pytest.raises(ConfigError, match="No enabled model backends"):
        ModelManager.from_config(
            {
                "models": {
                    "primary": "fake",
                    "fake": {"enabled": "false", "type": "fake"},
                }
            }
        )


def _models_yaml() -> dict:
    from openlens.main import load_models_config

    return load_models_config("configs/models.yaml")


def test_models_yaml_offline_safe_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_ENABLED", "false")
    monkeypatch.delenv("MODEL_PRIMARY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    cfg = _models_yaml()
    assert _build_model_manager(cfg) is None


def test_models_yaml_enables_openai_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_ENABLED", "true")
    monkeypatch.setenv("MODEL_API_KEY", "dummy-key-for-config-test")
    cfg = _models_yaml()
    manager = _build_model_manager(cfg)
    assert manager is not None
    assert manager.primary == "openai_compat"
    assert "openai_compat" in manager.backend_names


def test_models_yaml_primary_override_to_disabled_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_ENABLED", "true")
    monkeypatch.setenv("MODEL_API_KEY", "dummy-key-for-config-test")
    monkeypatch.setenv("MODEL_PRIMARY", "fake")
    cfg = _models_yaml()
    with pytest.raises(ConfigError, match="not enabled"):
        _build_model_manager(cfg)
