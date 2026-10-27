"""Repeatable fixture corpus for ranking and contract assertions.

Every document and request here is fixed: the same corpus seeds every
test run, so ranking assertions are deterministic. Providers under test
are seeded via :func:`seed_provider`.
"""

from __future__ import annotations

from openlens.core.interfaces import Document, SearchRequest


def fixture_documents() -> list[Document]:
    """Eight fixed documents with distinct topics and stable URLs."""
    return [
        Document(
            id="doc-shopping-1",
            title="Best running shoes for flat feet",
            content="A shopping guide comparing cushioned running shoes with arch support.",
            url="https://shop.example.com/running-shoes",
            metadata={"category": "shopping"},
        ),
        Document(
            id="doc-shopping-2",
            title="Affordable trail shoes review",
            content="Trail running shoes tested for grip and durability on rough terrain.",
            url="https://shop.example.com/trail-shoes",
            metadata={"category": "shopping"},
        ),
        Document(
            id="doc-news-1",
            title="City council approves new park funding",
            content="Local news: the council voted to fund three new parks downtown.",
            url="https://news.example.com/park-funding",
            metadata={"category": "news"},
        ),
        Document(
            id="doc-tech-1",
            title="How SQLite FTS5 full-text search works",
            content="FTS5 tokenizes documents into an inverted index for fast queries.",
            url="https://dev.example.com/sqlite-fts5",
            metadata={"category": "tech"},
        ),
        Document(
            id="doc-tech-2",
            title="Python dataclasses field guide",
            content="Dataclasses reduce boilerplate for plain data containers in Python.",
            url="https://dev.example.com/python-dataclasses",
            metadata={"category": "tech"},
        ),
        Document(
            id="doc-travel-1",
            title="Weekend trip to the coast",
            content="A short travel itinerary with beaches, seafood, and lighthouse views.",
            url="https://travel.example.com/coast-trip",
            metadata={"category": "travel"},
        ),
        Document(
            id="doc-unsafe-1",
            title="Free malware download explicit",
            content="An unsafe page offering malware downloads and phishing links.",
            url="https://shady.example.com/free-stuff",
            metadata={"category": "unsafe"},
        ),
        Document(
            id="doc-untrusted-1",
            title="Miracle cure claims",
            content="Unverified health claims from an unknown wellness blog.",
            url="https://rumor.example.net/miracle-cure",
            metadata={"category": "health"},
        ),
    ]


def fixture_requests() -> list[SearchRequest]:
    """Fixed requests reused by ranking and integration assertions."""
    return [
        SearchRequest(query="running shoes", mode="shopping", limit=10),
        SearchRequest(query="park funding", mode="news", limit=10),
        SearchRequest(query="SQLite FTS5 search", mode="default", limit=10),
        SearchRequest(query="coast trip", mode="travel", limit=5),
    ]


def seed_provider(provider) -> int:
    """Index the full fixture corpus into a provider. Returns doc count."""
    return provider.index_documents(fixture_documents())
