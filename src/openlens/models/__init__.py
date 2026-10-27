"""Plug-and-play AI model backends (Muse, local models, Claude).

Mirrors the search-provider pattern: :class:`AIModelProvider` is the
interface, ``registry`` maps type names to classes, ``manager`` instantiates
only enabled backends, and ``fake`` is the offline stand-in for tests.
No live model calls happen in the skeleton.
"""

from openlens.models.backends import (
    ClaudeModelProvider,
    LocalModelProvider,
    MuseModelProvider,
)
from openlens.models.fake import FakeModelProvider
from openlens.models.interfaces import AIModelProvider
from openlens.models.manager import ModelManager

__all__ = [
    "AIModelProvider",
    "ClaudeModelProvider",
    "FakeModelProvider",
    "LocalModelProvider",
    "ModelManager",
    "MuseModelProvider",
]
