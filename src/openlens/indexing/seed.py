"""Small fixture corpus used to seed the custom index in tests and dev."""

from openlens.core.interfaces import Document

SEED_DOCUMENTS: list[Document] = [
    Document(
        id="doc-python-search",
        title="Python full-text search with SQLite",
        content=(
            "SQLite FTS5 gives Python applications a zero-dependency full-text "
            "search engine. This guide covers virtual tables, bm25 ranking, and "
            "tokenizers for a local document index."
        ),
        url="https://example.com/python-sqlite-fts",
        metadata={"topic": "python"},
    ),
    Document(
        id="doc-opensearch-bm25",
        title="OpenSearch BM25 relevance tuning",
        content=(
            "OpenSearch scores results with BM25. Tune k1 and b per field, add "
            "synonyms for product search, and compare against a baseline before "
            "shipping a new ranking profile."
        ),
        url="https://example.com/opensearch-bm25",
        metadata={"topic": "search"},
    ),
    Document(
        id="doc-fastapi-pagination",
        title="Paginating FastAPI search responses",
        content=(
            "A FastAPI search endpoint should accept limit and offset parameters, "
            "return a total count, and keep pagination stable by sorting on a "
            "deterministic key such as document id."
        ),
        url="https://example.com/fastapi-pagination",
        metadata={"topic": "api"},
    ),
    Document(
        id="doc-plugin-pipeline",
        title="Designing a plugin pipeline for search",
        content=(
            "A search plugin pipeline chains filters, scorers, and enrichers. "
            "Mandatory filters fail closed while optional plugins record "
            "diagnostics and never break the request."
        ),
        url="https://example.com/plugin-pipeline",
        metadata={"topic": "architecture"},
    ),
    Document(
        id="doc-python-testing",
        title="Testing Python search code with pytest",
        content=(
            "Write pytest unit tests for the Python indexer, the query parser, "
            "and the result normalizer. Use an in-memory SQLite database so the "
            "suite stays offline and repeatable."
        ),
        url="https://example.com/python-pytest",
        metadata={"topic": "python"},
    ),
    Document(
        id="doc-config-yaml",
        title="Config-driven provider selection",
        content=(
            "List search providers in YAML config and instantiate only the "
            "enabled ones at startup. Switching from the custom index to an "
            "external provider must need no core code edits."
        ),
        url="https://example.com/config-providers",
        metadata={"topic": "config"},
    ),
    Document(
        id="doc-dedup",
        title="Deduplicating federated search results",
        content=(
            "Federated search merges hits from several providers. Deduplicate by "
            "canonical URL, keep the highest score, and preserve the original "
            "provider signal for explanations."
        ),
        url="https://example.com/dedup-search",
        metadata={"topic": "search"},
    ),
    Document(
        id="doc-child-safety",
        title="Child-safe filtering for search results",
        content=(
            "A child-safe filter excludes blocked domains before scoring. It is "
            "mandatory: any filter outage must propagate instead of leaking "
            "unfiltered results to the response."
        ),
        url="https://example.com/child-safe",
        metadata={"topic": "safety"},
    ),
]
