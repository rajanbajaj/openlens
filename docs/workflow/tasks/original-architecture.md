# OpenLens — Modular AI Search Engine Architecture

> Source: the author's original plan text, preserved verbatim as the
> traceability baseline for the sub-plans review. One paste artifact
> fixed: `r[118;1:3uanking/` → `ranking/` in §4.

## 1. Objective

Build a modular, extensible, AI-powered search engine with a basic search capability at its core. All advanced features should be implemented as independent, configurable plugins so they can be added, removed, enabled, or disabled without modifying the core search engine.

The search engine should also support multiple configurable search providers, including:

- OpenSearch API / cluster
- Authenticated external search APIs, such as Bing where an appropriate API is available
- A custom search index built and maintained by OpenLens

The architecture should allow developers to customize search sources, ranking behavior, filtering policies, and AI capabilities.

The initial implementation should be a Python modular monolith, not a microservices architecture.

## 2. Core Architectural Principles

1. **Plugin-based architecture:** Features should be independently implemented and configurable.
2. **Provider independence:** The search engine should work with different search backends without changing its core logic.
3. **Separation of concerns:** Retrieval, filtering, ranking, trust analysis, and result presentation should remain separate.
4. **Configurable behavior:** Providers, plugins, ranking profiles, and search modes should be controlled through configuration.
5. **AI as an optional capability:** AI models should be integrated through interfaces rather than embedded into the core search engine.
6. **Safety by design:** Mandatory safety and privacy policies must be enforced independently of generative AI.
7. **Explainability:** The system should be able to explain why results were ranked, filtered, or flagged.
8. **Developer extensibility:** New providers and plugins should be straightforward to implement.

## 3. High-Level Architecture

User query
|
v
Search API / CLI
|
v
Query Processor
|
v
Search Provider Manager
|
+-------------------+-------------------+
| | |
v v v
OpenSearch Provider External API Custom Index Provider
Provider |
v
Custom Crawler
Custom Indexer
|
v
Common Result Normalizer
|
v
Candidate Deduplication
|
v
Search Policy Pipeline
|
+--> Mandatory Safety Filters
+--> Privacy Filters
+--> Website Trust Analysis
+--> Relevance Scoring
+--> Fairness / Perspective Reranking
+--> Result Enrichment
|
v
Result Assembly and Explanation
|
v
Search Response

The provider manager selects and invokes the configured search provider or providers. All retrieved results are normalized into a common format before being passed to OpenLens filtering and ranking logic.

Safety and privacy restrictions must be able to exclude results, rather than merely lowering their ranking scores.

## 4. Recommended Project Structure

openlens/
├── pyproject.toml
├── README.md
├── configs/
│ ├── default.yaml
│ ├── providers.yaml
│ └── ranking.yaml
├── src/
│ └── openlens/
│ ├── main.py
│ ├── api/
│ │ ├── routes.py
│ │ └── schemas.py
│ ├── core/
│ │ ├── engine.py
│ │ ├── pipeline.py
│ │ ├── context.py
│ │ ├── interfaces.py
│ │ └── registry.py
│ ├── providers/
│ │ ├── base.py
│ │ ├── manager.py
│ │ ├── capabilities.py
│ │ ├── opensearch.py
│ │ ├── bing.py
│ │ └── custom_index.py
│ ├── retrieval/
│ │ ├── query_parser.py
│ │ ├── normalizer.py
│ │ ├── deduplicator.py
│ │ └── fusion.py
│ ├── ranking/
│ │ ├── ranker.py
│ │ └── profiles.py
│ ├── indexing/
│ │ ├── crawler.py
│ │ └── indexer.py
│ ├── storage/
│ │ ├── documents.py
│ │ └── repository.py
│ └── plugins/
│ ├── base.py
│ ├── safety/
│ ├── privacy/
│ ├── trust/
│ ├── fairness/
│ └── explanations/
└── tests/
├── unit/
├── integration/
└── fixtures/

This structure is a target layout. Only create modules when they are needed for the current implementation.

## 5. Common Data Models

All providers and plugins should use a shared document and result schema.

### Document

Each indexed or retrieved document should contain:

- id
- url
- title
- content
- source
- metadata

The metadata field should support source-specific attributes, such as publication date, product price, hotel location, content category, or domain information.

### SearchRequest

Fields:

- query
- mode
- limit
- offset
- filters
- provider preferences

The search mode identifies the intended experience, such as default search, child-safe search, news search, shopping search, or travel search.

### SearchResult

Fields:

- document
- original_score
- final_score
- reasons
- warnings
- provider metadata

The original score and final score should remain separate so that the system can preserve the provider's ranking signal while calculating its own ranking.

### SearchContext

The shared execution context should contain:

- Search request
- Retrieved candidates
- Applied filters
- Ranking signals
- Plugin execution metadata
- Warnings and errors
- Search diagnostics

Plugins should modify only the fields they are authorized to modify.

## 6. Search Provider Architecture

A search provider is responsible for retrieving candidate documents from a particular backend or service.

Every provider should implement a common interface.

### SearchProvider Interface

Required operations:

- search(query, limit, cursor, filters)
- health_check()
- configure(config)

The provider should return a standardized response containing:

- Normalized results
- Provider name
- Pagination information
- Provider-specific metadata

The provider manager should handle provider selection, configuration validation, timeouts, and error handling.

### Provider 1: OpenSearch

Responsibilities:

- Connect to a configured OpenSearch cluster.
- Translate OpenLens queries into OpenSearch Query DSL.
- Retrieve indexed documents and relevance scores.
- Support filtering and pagination.
- Support configurable index names and mappings.
- Return normalized results.

### Provider 2: Authenticated External Search API

Responsibilities:

- Authenticate with the configured external provider.
- Translate OpenLens queries into the provider's request format.
- Normalize response data.
- Handle rate limits, quotas, timeouts, and provider errors.
- Preserve source attribution and usage restrictions.
- Support pagination where available.

Bing is an example of a potential provider, subject to current API availability, access terms, and permitted use of returned data.

### Provider 3: Custom Index

Responsibilities:

- Use the OpenLens crawler and indexer.
- Search the custom indexed corpus.
- Support custom indexing and crawling policies.
- Provide relevance scores and document metadata.
- Support incremental indexing and reindexing.
- Integrate with custom ranking and website trust signals.

The custom index is the primary long-term opportunity for OpenLens to differentiate itself.

## 7. Provider Configuration

Providers should be enabled and configured without changing the search engine's source code.

Example providers.yaml:

providers:
opensearch:
enabled: true
type: opensearch
endpoint: ${OPENSEARCH_URL}
index: web-pages
timeout_seconds: 3

bing:
enabled: false
type: bing
api_key: ${BING_API_KEY}
timeout_seconds: 5

custom:
enabled: true
type: custom
index_path: ./data/index
timeout_seconds: 2

search:
strategy: single
default_provider: custom
fallback_providers: - opensearch

The configuration values above are illustrative.

Secrets must be resolved securely from environment variables or a secrets manager. Credentials must never be exposed in frontend configuration or application logs.

The provider registry should instantiate only enabled providers and validate their configuration at startup.

## 8. Search Strategies

The provider manager should support three search strategies.

### Strategy A: Single Provider

A single configured provider handles every query.

Example:

Query → Custom Index → Normalize → Filter → Rank → Results

This should be the default strategy for the initial MVP.

### Strategy B: Fallback Providers

The primary provider is used first. A secondary provider is invoked if the primary provider encounters a qualifying failure.

Example:

Query → Custom Index → Provider Failure → OpenSearch → Results

Fallback behavior should distinguish service failures from legitimate empty results. Timeouts and retry policies should be bounded.

### Strategy C: Federated Search

Multiple providers are queried, and their results are merged.

Example:

Query → Custom Index + OpenSearch + External API → Normalize → Deduplicate → Rerank

Federated search offers broader coverage but introduces additional latency, API costs, quota management, and cross-provider ranking challenges.

When combining providers, use rank-based fusion, such as reciprocal rank fusion, as a baseline rather than directly combining uncalibrated raw scores.

## 9. Plugin Architecture

Plugins implement specific search capabilities. They should be independently configurable and should not require modifications to the core engine.

Recommended plugin categories:

### A. Query Plugins

- Query normalization
- Intent classification
- Query expansion
- Semantic query understanding

### B. Filter Plugins

- Child-safe content filtering
- Privacy-sensitive content filtering
- Blocklists and allowlists
- User-defined restrictions

Filters can remove results from the candidate set. Mandatory safety and privacy restrictions must fail closed if their enforcement cannot be verified.

### C. Scoring Plugins

- Relevance scoring
- Website trust scoring
- Freshness scoring
- User-preference scoring

Scoring plugins should produce named signals rather than silently overwriting one another's scores.

### D. Reranking Plugins

- News perspective diversity
- Source diversity
- Shopping relevance
- Travel relevance
- Category-specific ranking profiles

### E. Enrichment Plugins

- Source credibility information
- Website warnings
- Article metadata
- Product or hotel attributes
- Search result explanations

### F. AI Plugins

- Semantic retrieval
- Content classification
- Article summarization
- Perspective analysis
- Privacy detection
- Trust analysis

AI integrations should be optional and replaceable.

## 10. Plugin Lifecycle and Execution Pipeline

The pipeline should use explicit, deterministic stages.

1. Query processing
2. Provider selection
3. Candidate retrieval
4. Result normalization
5. Mandatory filtering
6. Candidate deduplication
7. Scoring
8. Reranking
9. Result enrichment
10. Response assembly

Each plugin should declare:

- Unique name
- Plugin type
- Execution stage
- Configuration schema
- Dependencies, if any
- Whether failure is optional or mandatory
- Supported capabilities

The pipeline should validate dependencies and stage ordering before accepting requests.

Optional plugin failures may be isolated and logged. Failures in mandatory safety enforcement must not silently allow unrestricted results.

## 11. Ranking Architecture

OpenLens should own its final ranking logic instead of blindly trusting provider scores.

The ranking engine should combine configurable signals, such as:

- Text relevance
- Semantic similarity
- Source credibility
- Website trust
- Freshness
- User-selected preferences
- Source diversity
- Category-specific attributes

Different search modes should use different ranking profiles.

Examples:

- Default search: relevance and quality
- Child-safe search: mandatory safety constraints, then relevance
- News search: relevance, credibility, freshness, and appropriate perspective coverage
- Shopping search: product relevance, price, quality, and user requirements
- Travel search: geographic constraints, price, facilities, and user preferences

The ranking system should explain why a result was selected or excluded.

Gender neutrality should be implemented as fair, evidence-aware ranking rather than artificially giving all viewpoints equal credibility.

## 12. Search API

The engine should expose a stable API that can later support a web frontend and third-party developers.

Example:

POST /api/v1/search

Request:

{
"query": "best dresses under 3000",
"mode": "shopping",
"limit": 10,
"offset": 0,
"filters": {
"exclude_sponsored": true
}
}

Response:

{
"query": "best dresses under 3000",
"results": [
{
"id": "doc_123",
"title": "Example dress",
"url": "https://example.com/dress",
"score": 0.92,
"reasons": [
"Matches your query",
"Within your budget"
],
"warnings": []
}
],
"total": 1,
"applied_filters": [
"exclude_sponsored"
]
}

The response values are illustrative. Actual scores and counts must come from the search execution.

The API should support:

- Query validation
- Search modes
- Provider preferences
- Filters
- Pagination
- Result explanations
- Applied-filter reporting
- Consistent error responses

FastAPI is a suitable framework for the initial implementation.

## 13. Storage and Indexing

Storage should be abstracted behind a repository interface so the core engine is not coupled to a specific database or index implementation.

Recommended initial approach:

- Use the existing custom inverted index if it already works.
- Alternatively, use SQLite with FTS5 for a lightweight local MVP.
- Add OpenSearch as an optional backend.
- Keep crawling, indexing, and querying separate.
- Support document updates, deduplication, and reindexing.
- Keep source metadata and content hashes for trust and duplicate detection.

The initial implementation does not require a distributed database or microservices.

## 14. Testing Requirements

The architecture should guarantee:

- Providers can be enabled or disabled through configuration.
- A new provider can be added without changing the core engine.
- Plugins can be enabled or disabled independently.
- A new plugin can be added without rewriting the search pipeline.
- Provider responses are normalized consistently.
- Safety and privacy policies work across every provider.
- Mandatory restrictions cannot be bypassed by provider failures.
- Optional plugin failures do not crash all searches.
- Ranking profiles are configurable.
- Duplicate results can be detected across providers.
- API keys are not exposed to users.
- Search quality can be measured using a repeatable test corpus.
- Provider latency, failures, and plugin execution are observable.

## 15. Implementation Roadmap

### Phase 1: Working Search Core

- Refactor the existing Python search engine.
- Separate crawler, indexer, storage, retrieval, and ranking.
- Define common document and result models.
- Implement basic search through a single provider.
- Preserve current functionality with automated tests.

### Phase 2: Provider Abstraction

- Define the SearchProvider interface.
- Wrap the custom index in CustomIndexProvider.
- Add a provider registry and configuration loader.
- Implement provider health checks and timeouts.
- Add the OpenSearch adapter.

### Phase 3: Plugin Pipeline

- Define SearchContext and plugin interfaces.
- Implement plugin registration and lifecycle management.
- Add configurable execution stages.
- Implement baseline relevance scoring.
- Add a simple trust-warning plugin and content-filtering plugin.

### Phase 4: API and Observability

- Expose the search API through FastAPI.
- Add validation, pagination, and structured error handling.
- Add logs and metrics for providers and plugins.
- Implement provider fallback behavior.

### Phase 5: AI Search Features

- Add AI-based query understanding.
- Add website trust and duplicate-domain analysis.
- Add content safety and privacy classification.
- Add transparent ranking explanations.
- Add news grouping and perspective-aware reranking.

### Phase 6: Federated Search

- Add external search providers where available.
- Implement multi-provider retrieval.
- Add cross-provider deduplication.
- Implement rank fusion and final reranking.
- Evaluate quality, latency, and cost.

## 16. Definition of Done for the Initial Skeleton

The skeleton is complete when:

1. A user can perform a working search.
2. Search is executed through a configured provider.
3. The custom index works through the common provider interface.
4. Providers can be selected through configuration.
5. Search results use a shared data model.
6. The pipeline has defined stages.
7. A plugin can be enabled or disabled without changing core engine code.
8. A filter plugin can exclude results.
9. A scoring plugin can adjust ranking.
10. The API returns normalized results and diagnostics.
11. Unit and integration tests cover the provider and plugin contracts.

## Final Architectural Principle

OpenLens should be a search platform with two independent extension systems:

**Search providers determine where candidate results come from.**

**Search plugins determine how those results are evaluated, filtered, ranked, and explained.**

The core engine coordinates both through stable interfaces. This lets OpenLens begin as a small local Python search engine and gradually evolve into a customizable search platform supporting external APIs, managed indexes, AI-powered capabilities, and developer-defined search behavior.
