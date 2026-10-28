"""Search routes: POST /api/v1/search.

Thin translation layer over the Wave-0 `Engine` interface: validate the
wire format (schemas), build a core `SearchRequest`, delegate to the
engine, and convert the core `SearchResponse` back. Pagination
(`limit`/`offset`) is passed through to the engine, which owns slicing
and the pre-pagination `total`. Error mapping lives in
`openlens.api.errors`; this module never invents error shapes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from openlens.api.errors import EngineUnavailableError
from openlens.api.schemas import SearchRequestSchema, SearchResponseSchema
from openlens.core.interfaces import Engine, SearchRequest

router = APIRouter(prefix="/api/v1", tags=["search"])


def get_engine(request: Request) -> Engine:
    """Resolve the engine attached to the app (overridable in tests)."""
    engine = getattr(request.app.state, "engine", None)
    if not isinstance(engine, Engine):
        raise EngineUnavailableError("search engine is not configured")
    return engine


@router.post("/search", response_model=SearchResponseSchema)
def search(
    body: SearchRequestSchema,
    engine: Engine = Depends(get_engine),
) -> SearchResponseSchema:
    core_request = SearchRequest(
        query=body.query.strip(),
        mode=body.mode,
        limit=body.limit,
        offset=body.offset,
        filters=dict(body.filters),
    )
    core_response = engine.search(core_request)
    return SearchResponseSchema.from_core(core_response)
