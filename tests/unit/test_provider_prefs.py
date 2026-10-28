"""Unit tests for the provider-preferences data path (federated step 3).

Covers the core `ProviderPrefs` dataclass defaults/immutability and the
API schema pass-through (`ProviderPrefsSchema` -> core). Malformed shapes
must fail Pydantic validation (422 at the API layer). No manager
selection logic, fusion, or engine behavior is tested here.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from openlens.api.schemas import ProviderPrefsSchema, SearchRequestSchema
from openlens.core.interfaces import ProviderPrefs, SearchRequest


def test_provider_prefs_defaults() -> None:
    prefs = ProviderPrefs()
    assert prefs.include == ()
    assert prefs.exclude == ()
    assert prefs.primary is None


def test_provider_prefs_is_frozen() -> None:
    prefs = ProviderPrefs(include=("a",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        prefs.primary = "a"  # type: ignore[misc]


def test_search_request_default_prefs_and_positional_compat() -> None:
    req = SearchRequest("q", "default", 10, 0, {})
    assert req.provider_prefs == ProviderPrefs()
    req2 = SearchRequest(query="q")
    assert req2.provider_prefs == ProviderPrefs()
    req3 = SearchRequest(
        query="q", provider_prefs=ProviderPrefs(include=("a",), primary="a")
    )
    assert req3.provider_prefs.include == ("a",)
    assert req3.provider_prefs.primary == "a"


def test_api_schema_defaults_to_none() -> None:
    body = SearchRequestSchema(query="q")
    assert body.provider_prefs is None


def test_api_schema_prefs_pass_through() -> None:
    body = SearchRequestSchema(
        query="q",
        provider_prefs={"include": ["a", "b"], "exclude": ["c"], "primary": "a"},
    )
    assert body.provider_prefs is not None
    assert tuple(body.provider_prefs.include) == ("a", "b")
    assert tuple(body.provider_prefs.exclude) == ("c",)
    assert body.provider_prefs.primary == "a"
    core = ProviderPrefs(
        include=tuple(body.provider_prefs.include),
        exclude=tuple(body.provider_prefs.exclude),
        primary=body.provider_prefs.primary,
    )
    assert core == ProviderPrefs(include=("a", "b"), exclude=("c",), primary="a")


def test_api_schema_prefs_partial_shape() -> None:
    schema = ProviderPrefsSchema(primary="a")
    assert schema.include == ()
    assert schema.exclude == ()
    assert schema.primary == "a"


@pytest.mark.parametrize(
    "prefs",
    [
        {"include": "a"},  # wrong type: str instead of list/tuple
        {"include": [1, 2]},  # wrong item type
        {"exclude": [None]},  # wrong item type
        {"primary": 123},  # wrong type: int instead of str
        {"primary": ["a"]},  # wrong type: list instead of str
    ],
)
def test_malformed_prefs_shapes_rejected(prefs: dict) -> None:
    with pytest.raises(ValidationError):
        SearchRequestSchema(query="q", provider_prefs=prefs)
