"""Shared configuration loader.

All waves code against `load_config`: YAML files with `${ENV_VAR}` (or
`${ENV_VAR:-default}`) references resolved at load time. Unresolved secrets
fail startup validation loudly with ConfigError — never log or return them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from openlens.core.interfaces import ConfigError

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve_string(value: str, source: str) -> str:
    """Resolve `${VAR}` / `${VAR:-default}` references in one string."""

    def replace(match: re.Match[str]) -> str:
        var_name, default = match.group(1), match.group(2)
        if var_name in os.environ:
            return os.environ[var_name]
        if default is not None:
            return default
        raise ConfigError(
            f"Unresolved environment variable '${{{var_name}}}' in config '{source}'. "
            "Set it or use '${VAR:-default}'. Startup validation failed."
        )

    return _ENV_PATTERN.sub(replace, value)


def _resolve_node(node: Any, source: str) -> Any:
    if isinstance(node, str):
        return _resolve_string(node, source)
    if isinstance(node, dict):
        return {key: _resolve_node(val, source) for key, val in node.items()}
    if isinstance(node, list):
        return [_resolve_node(item, source) for item in node]
    return node


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML config file with `${ENV_VAR}` resolution.

    Raises:
        ConfigError: file missing/unreadable, invalid YAML, top-level mapping
            absent, or any `${ENV_VAR}` without a value or default.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: '{config_path}'. Startup validation failed.")
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config '{config_path}': {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in config '{config_path}': {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"Config '{config_path}' must be a top-level mapping, "
            f"got {type(data).__name__}. Startup validation failed."
        )
    return _resolve_node(data, str(config_path))


def validate_config(config: dict[str, Any], required_sections: list[str] | tuple[str, ...] = ()) -> None:
    """Ensure required top-level sections exist. Raises ConfigError if not."""
    missing = [key for key in required_sections if key not in config]
    if missing:
        raise ConfigError(
            f"Config missing required section(s): {', '.join(missing)}. "
            "Startup validation failed."
        )
