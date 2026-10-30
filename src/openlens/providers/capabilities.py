"""Static capability descriptors for known provider types.

Capabilities are advisory metadata (used for diagnostics and config
validation), not a second dispatch mechanism: the manager routes by the
``strategy``/``primary`` config, never by these flags.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider type can do and what it needs."""

    name: str
    supports_pagination: bool = True
    supports_filters: bool = False
    requires_network: bool = False
    requires_auth: bool = False


#: Capability table for every provider type the skeleton knows about.
#: Unknown (test fake) types fall back to the default capabilities.
KNOWN_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "custom_index": ProviderCapabilities(
        name="custom_index",
        supports_pagination=True,
        supports_filters=True,
        requires_network=False,
        requires_auth=False,
    ),
}

_DEFAULT_CAPABILITIES = ProviderCapabilities(name="unknown")


def describe(provider_type: str) -> ProviderCapabilities:
    """Return the capabilities for ``provider_type`` (default when unknown)."""
    return KNOWN_CAPABILITIES.get(provider_type, _DEFAULT_CAPABILITIES)
