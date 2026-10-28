"""Mandatory privacy filter: PII exclusion / redaction.

Fail-closed: any internal failure raises ``PluginError(mandatory=True)`` so
the pipeline aborts instead of leaking unfiltered results. Pattern sets are
pinned in config (explicit regex list per class); street-address matching
uses only config-supplied patterns — no invented address grammar.
"""

from __future__ import annotations

import re
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
    SearchResult,
)
from openlens.plugins.base import BasePlugin

#: Canonical PII class names.
PII_CLASSES: tuple[str, ...] = (
    "email",
    "phone",
    "government_id",
    "street_address",
)

#: ReDoS caps validated at ``configure()`` time.
MAX_PATTERNS_PER_CLASS = 32
MAX_PATTERN_LENGTH = 512

REDACTION_PLACEHOLDER = "[REDACTED]"

#: Built-in patterns used when config supplies none for a class.
#: Street addresses intentionally have no default — only config
#: patterns may match them.
DEFAULT_PATTERNS: dict[str, tuple[str, ...]] = {
    "email": (r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",),
    "phone": (r"\+?1[-.\s]?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}",),
    "government_id": (r"\b\d{3}-\d{2}-\d{4}\b",),
    "street_address": (),
}

_DEFAULT_ACTION = "exclude"

_CLASS_ALIASES: dict[str, str] = {
    "email": "email",
    "emails": "email",
    "e_mail": "email",
    "e-mail": "email",
    "phone": "phone",
    "phones": "phone",
    "telephone": "phone",
    "phone_number": "phone",
    "phone_numbers": "phone",
    "government_id": "government_id",
    "government_ids": "government_id",
    "gov_id": "government_id",
    "gov_ids": "government_id",
    "governmentid": "government_id",
    "ssn": "government_id",
    "social_security": "government_id",
    "passport": "government_id",
    "national_id": "government_id",
    "tax_id": "government_id",
    "street_address": "street_address",
    "street_addresses": "street_address",
    "address": "street_address",
    "addresses": "street_address",
    "streetaddress": "street_address",
}

_ACTION_ALIASES: dict[str, str] = {
    "redact": "redact",
    "redacted": "redact",
    "redaction": "redact",
    "exclude": "exclude",
    "excluded": "exclude",
    "exclusion": "exclude",
    "remove": "exclude",
    "drop": "exclude",
    "block": "exclude",
}


def _normalize_class_name(raw: str) -> str | None:
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    # Collapse repeated underscores from inputs like "e - mail".
    while "__" in key:
        key = key.replace("__", "_")
    return _CLASS_ALIASES.get(key)


def _normalize_action(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return _ACTION_ALIASES.get(raw.strip().lower())


class PrivacyFilter(BasePlugin):
    """Exclusion / redaction filter over title/snippet/url/metadata.

    Mandatory (fail closed). Per-class config chooses ``exclude``
    (drop the hit) vs ``redact`` (mask matches, keep the hit, append
    a reason). Redaction never writes ``original_score`` /
    ``final_score``.
    """

    @property
    def name(self) -> str:
        return "privacy-filter"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    @property
    def mandatory(self) -> bool:
        return True

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        self._simulate_outage = bool(self._config.get("simulate_outage", False))

        # Start from defaults; config overrides per class.
        patterns_by_class: dict[str, list[str]] = {
            cls: list(DEFAULT_PATTERNS.get(cls, ())) for cls in PII_CLASSES
        }
        actions_by_class: dict[str, str] = {cls: _DEFAULT_ACTION for cls in PII_CLASSES}

        def _apply_patterns_dict(source: dict[Any, Any], origin: str) -> None:
            for raw_key, raw_val in source.items():
                if not isinstance(raw_key, str):
                    raise ConfigError(
                        f"PrivacyFilter {origin} keys must be strings, got {raw_key!r}."
                    )
                canonical = _normalize_class_name(raw_key)
                if canonical is None:
                    raise ConfigError(
                        f"PrivacyFilter unknown PII class '{raw_key}' in "
                        f"{origin}. Known: {', '.join(PII_CLASSES)}."
                    )
                patterns_by_class[canonical] = self._coerce_patterns(
                    raw_val, canonical, origin
                )

        def _apply_actions_dict(source: dict[Any, Any], origin: str) -> None:
            for raw_key, raw_val in source.items():
                if not isinstance(raw_key, str):
                    raise ConfigError(
                        f"PrivacyFilter {origin} keys must be strings, got {raw_key!r}."
                    )
                canonical = _normalize_class_name(raw_key)
                if canonical is None:
                    raise ConfigError(
                        f"PrivacyFilter unknown PII class '{raw_key}' in "
                        f"{origin}. Known: {', '.join(PII_CLASSES)}."
                    )
                action = _normalize_action(raw_val)
                if action is None:
                    raise ConfigError(
                        f"PrivacyFilter action for '{raw_key}' must be "
                        f"'redact' or 'exclude', got {raw_val!r}."
                    )
                actions_by_class[canonical] = action

        def _apply_class_entry(canonical: str, entry: Any, origin: str) -> None:
            if isinstance(entry, dict):
                if "patterns" in entry:
                    patterns_by_class[canonical] = self._coerce_patterns(
                        entry["patterns"], canonical, origin
                    )
                if "pattern" in entry:
                    patterns_by_class[canonical] = self._coerce_patterns(
                        entry["pattern"], canonical, origin
                    )
                action_raw = entry.get("action", entry.get("mode"))
                if action_raw is not None:
                    action = _normalize_action(action_raw)
                    if action is None:
                        raise ConfigError(
                            f"PrivacyFilter action for '{canonical}' must be "
                            f"'redact' or 'exclude', got {action_raw!r}."
                        )
                    actions_by_class[canonical] = action
            elif isinstance(entry, list):
                patterns_by_class[canonical] = self._coerce_patterns(
                    entry, canonical, origin
                )
            elif isinstance(entry, str):
                action = _normalize_action(entry)
                if action is None:
                    raise ConfigError(
                        f"PrivacyFilter entry for '{canonical}' must be "
                        f"'redact' or 'exclude', got {entry!r}."
                    )
                actions_by_class[canonical] = action
            else:
                raise ConfigError(
                    f"PrivacyFilter entry for '{canonical}' in {origin} "
                    f"must be a mapping, list, or action string, "
                    f"got {type(entry).__name__}."
                )

        # --- pattern sources -------------------------------------------------
        for key in ("patterns", "pii_patterns", "pattern_sets"):
            if key in self._config:
                val = self._config[key]
                if not isinstance(val, dict):
                    raise ConfigError(
                        f"PrivacyFilter '{key}' must be a mapping of "
                        f"PII class to regex list."
                    )
                _apply_patterns_dict(val, f"'{key}'")

        # --- action sources --------------------------------------------------
        for key in ("actions", "pii_actions", "modes", "class_actions"):
            if key in self._config:
                val = self._config[key]
                if not isinstance(val, dict):
                    raise ConfigError(
                        f"PrivacyFilter '{key}' must be a mapping of "
                        f"PII class to 'redact'/'exclude'."
                    )
                _apply_actions_dict(val, f"'{key}'")

        # --- nested per-class mapping ---------------------------------------
        if "classes" in self._config:
            classes_val = self._config["classes"]
            if not isinstance(classes_val, dict):
                raise ConfigError(
                    "PrivacyFilter 'classes' must be a mapping of "
                    "PII class to {patterns, action}."
                )
            for raw_key, entry in classes_val.items():
                if not isinstance(raw_key, str):
                    raise ConfigError(
                        f"PrivacyFilter 'classes' keys must be strings, "
                        f"got {raw_key!r}."
                    )
                canonical = _normalize_class_name(raw_key)
                if canonical is None:
                    raise ConfigError(
                        f"PrivacyFilter unknown PII class '{raw_key}' in "
                        f"'classes'. Known: {', '.join(PII_CLASSES)}."
                    )
                _apply_class_entry(canonical, entry, "'classes'")

        # --- direct per-class top-level keys --------------------------------
        reserved = {
            "simulate_outage",
            "patterns",
            "pii_patterns",
            "pattern_sets",
            "actions",
            "pii_actions",
            "modes",
            "class_actions",
            "classes",
            "redact",
            "exclude",
            "redact_classes",
            "exclude_classes",
        }
        for raw_key, entry in self._config.items():
            if raw_key in reserved:
                continue
            if not isinstance(raw_key, str):
                continue
            # Suffix forms: "<class>_patterns" / "<class>_action" / "<class>_mode"
            handled_suffix = False
            for suffix, kind in (
                ("_patterns", "patterns"),
                ("_pattern", "patterns"),
                ("_action", "action"),
                ("_mode", "action"),
            ):
                if raw_key.endswith(suffix):
                    prefix = raw_key[: -len(suffix)]
                    canonical = _normalize_class_name(prefix)
                    if canonical is not None:
                        handled_suffix = True
                        if kind == "patterns":
                            patterns_by_class[canonical] = self._coerce_patterns(
                                entry, canonical, f"'{raw_key}'"
                            )
                        else:
                            action = _normalize_action(entry)
                            if action is None:
                                raise ConfigError(
                                    f"PrivacyFilter '{raw_key}' must be "
                                    f"'redact' or 'exclude', got {entry!r}."
                                )
                            actions_by_class[canonical] = action
                    break
            if handled_suffix:
                continue
            canonical = _normalize_class_name(raw_key)
            if canonical is not None:
                _apply_class_entry(canonical, entry, f"'{raw_key}'")

        # --- redact/exclude class lists --------------------------------------
        for key, forced in (
            ("redact_classes", "redact"),
            ("redact", "redact"),
            ("exclude_classes", "exclude"),
            ("exclude", "exclude"),
        ):
            if key in self._config:
                val = self._config[key]
                if isinstance(val, list) and all(isinstance(v, str) for v in val):
                    for item in val:
                        canonical = _normalize_class_name(item)
                        if canonical is None:
                            raise ConfigError(
                                f"PrivacyFilter unknown PII class '{item}' in '{key}'."
                            )
                        actions_by_class[canonical] = forced

        # --- caps + compile (one alternation per class) ----------------------
        self._actions_by_class: dict[str, str] = actions_by_class
        self._patterns_by_class: dict[str, list[str]] = patterns_by_class
        self._compiled: dict[str, re.Pattern[str] | None] = {}
        for cls in PII_CLASSES:
            pats = patterns_by_class.get(cls, [])
            if len(pats) > MAX_PATTERNS_PER_CLASS:
                raise ConfigError(
                    f"PrivacyFilter class '{cls}' has {len(pats)} patterns; "
                    f"cap is {MAX_PATTERNS_PER_CLASS} (ReDoS guard)."
                )
            for pat in pats:
                if len(pat) > MAX_PATTERN_LENGTH:
                    raise ConfigError(
                        f"PrivacyFilter pattern for '{cls}' exceeds "
                        f"{MAX_PATTERN_LENGTH} chars (ReDoS guard): "
                        f"{pat[:40]!r}..."
                    )
                if not pat:
                    raise ConfigError(
                        f"PrivacyFilter pattern for '{cls}' must not be empty."
                    )
            if not pats:
                self._compiled[cls] = None
                continue
            combined = "|".join(f"(?:{p})" for p in pats)
            try:
                self._compiled[cls] = re.compile(combined, re.IGNORECASE)
            except re.error as exc:
                raise ConfigError(
                    f"PrivacyFilter invalid regex for class '{cls}': {exc}"
                ) from exc

    def _coerce_patterns(self, raw: Any, cls: str, origin: str) -> list[str]:
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, (list, tuple)):
            if not all(isinstance(p, str) for p in raw):
                raise ConfigError(
                    f"PrivacyFilter patterns for '{cls}' in {origin} "
                    f"must be a list of strings."
                )
            return [str(p) for p in raw]
        raise ConfigError(
            f"PrivacyFilter patterns for '{cls}' in {origin} must be "
            f"a list of regex strings."
        )

    # -- scanning helpers ----------------------------------------------------

    def _metadata_strings(self, value: Any) -> list[str]:
        out: list[str] = []
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                out.extend(self._metadata_strings(v))
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                out.extend(self._metadata_strings(v))
        elif isinstance(value, (int, float)):
            out.append(str(value))
        return out

    def _matches(self, result: SearchResult, compiled: re.Pattern[str]) -> bool:
        if compiled.search(result.title):
            return True
        if compiled.search(result.snippet):
            return True
        if result.url and compiled.search(result.url):
            return True
        for text in self._metadata_strings(result.metadata):
            if compiled.search(text):
                return True
        return False

    def _redact_value(self, value: Any, compiled: re.Pattern[str]) -> Any:
        if isinstance(value, str):
            return compiled.sub(REDACTION_PLACEHOLDER, value)
        if isinstance(value, dict):
            return {k: self._redact_value(v, compiled) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(v, compiled) for v in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(v, compiled) for v in value)
        return value

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_outage:
            raise PluginError(
                "privacy-filter backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=True,
            )
        try:
            kept: list[SearchResult] = []
            removed = 0
            for result in context.results:
                # Exclude check first (first-match short-circuit).
                excluded = False
                for cls in PII_CLASSES:
                    if self._actions_by_class.get(cls) != "exclude":
                        continue
                    compiled = self._compiled.get(cls)
                    if compiled is None:
                        continue
                    if self._matches(result, compiled):
                        excluded = True
                        break
                if excluded:
                    removed += 1
                    continue

                # Redaction for remaining hits.
                redacted_classes: list[str] = []
                for cls in PII_CLASSES:
                    if self._actions_by_class.get(cls) != "redact":
                        continue
                    compiled = self._compiled.get(cls)
                    if compiled is None:
                        continue
                    if not self._matches(result, compiled):
                        continue
                    result.title = compiled.sub(REDACTION_PLACEHOLDER, result.title)
                    result.snippet = compiled.sub(REDACTION_PLACEHOLDER, result.snippet)
                    if result.url:
                        result.url = compiled.sub(REDACTION_PLACEHOLDER, result.url)
                    result.metadata = self._redact_value(result.metadata, compiled)
                    redacted_classes.append(cls)

                if redacted_classes:
                    for cls in redacted_classes:
                        result.reasons.append(f"privacy-filter: redacted {cls} PII")
                kept.append(result)

            context.results = kept
            context.applied_filters.append(self.name)
            context.metadata["privacy-filter-removed"] = removed
            return context
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(
                str(exc),
                plugin_name=self.name,
                stage=self.stage,
                mandatory=True,
            ) from exc
