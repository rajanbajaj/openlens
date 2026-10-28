"""Local document indexing over seed and caller-supplied documents."""

from openlens.indexing.indexer import DocumentIndexer, run_index
from openlens.indexing.seed import SEED_DOCUMENTS

__all__ = ["SEED_DOCUMENTS", "DocumentIndexer", "run_index"]
