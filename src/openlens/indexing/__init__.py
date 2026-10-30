"""Local document indexing over seed and caller-supplied documents."""

from openlens.indexing.indexer import DocumentIndexer, run_index
from openlens.indexing.seed import SEED_DOCUMENTS

__all__ = [
    "SEED_DOCUMENTS",
    "CrawlerWorker",
    "DocumentIndexer",
    "new_worker_id",
    "run_index",
    "run_kafka_worker",
    "run_standalone_worker",
]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """Lazily re-export the worker entrypoints.

    ``indexing.worker`` is imported here on first attribute access instead
    of at package import, so ``python -m openlens.indexing.worker`` no
    longer trips the ``found in sys.modules prior to execution``
    RuntimeWarning: the package ``__init__`` must not pre-import the very
    module runpy is about to execute as ``__main__``.
    """
    if name in {
        "CrawlerWorker",
        "new_worker_id",
        "run_kafka_worker",
        "run_standalone_worker",
    }:
        from openlens.indexing import worker as _worker

        return getattr(_worker, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
