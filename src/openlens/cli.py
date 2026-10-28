"""Operator CLI: ``python -m openlens.cli`` (stdlib ``argparse`` only).

Four command groups, all offline-capable:

- ``search "query"`` — run one search through the real Engine and print
  the response (JSON by default, ``--pretty`` for humans).
- ``index (add|update|reindex|list)`` — delegate ingestion to the
  crawler-index entry point (:func:`openlens.indexing.run_index`); when
  that entry point is unavailable the commands fail loudly instead of
  pretending to succeed.
- ``config validate`` — validate a config file without serving anything.
- ``providers health`` — per-provider reachability table, no network.

The CLI reuses :func:`openlens.main.load_app_config` and
:func:`openlens.main.build_engine` — there is no second boot path.
Module-level names (``build_engine``, ``load_app_config``,
``DocumentRepository``, ``indexing_api``) are the injection seams the
unit tests monkeypatch; they are looked up at call time on purpose.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

import openlens.indexing as indexing_api
from openlens.api.schemas import SearchResponseSchema
from openlens.config import load_config
from openlens.core.interfaces import Engine, ProviderPrefs, SearchRequest
from openlens.main import (
    DEFAULT_CONFIG_PATH,
    _log_search,
    build_engine,
    load_app_config,
)
from openlens.observability.logging import configure_logging
from openlens.observability.request_id import new_request_id
from openlens.storage.repository import DocumentRepository

#: Exit code for operational failures (bad config, failed search, ...).
#: Usage errors come from argparse itself (exit 2).
FAILURE_EXIT = 1

#: Message used when the indexer entry point cannot be resolved.
INDEXER_UNAVAILABLE_MESSAGE = (
    "indexer not available: openlens.indexing.run_index could not be "
    "resolved; install/land the crawler-index entry point and retry"
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser (no side effects)."""
    parser = argparse.ArgumentParser(
        prog="openlens",
        description="Operate and debug an OpenLens instance.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    search_cmd = commands.add_parser(
        "search", help="Run one search and print the response."
    )
    search_cmd.add_argument("query", help="Query text to search for.")
    search_cmd.add_argument("--mode", default="default", help="Ranking mode.")
    search_cmd.add_argument(
        "--limit", type=int, default=10, help="Maximum hits to return."
    )
    search_cmd.add_argument(
        "--provider",
        default=None,
        help="Pin this request to one provider (primary preference).",
    )
    output = search_cmd.add_mutually_exclusive_group()
    output.add_argument(
        "--json", action="store_true", help="Print the response as JSON."
    )
    output.add_argument(
        "--pretty", action="store_true", help="Print a human-readable summary."
    )
    search_cmd.set_defaults(func=_cmd_search)

    index_cmd = commands.add_parser("index", help="Manage the local document index.")
    index_sub = index_cmd.add_subparsers(dest="index_command", required=True)
    for name in ("add", "update", "reindex"):
        sub = index_sub.add_parser(
            name,
            help=(
                f"{name} documents via the incremental indexer entry point "
                "(openlens.indexing.run_index)."
            ),
        )
        sub.add_argument("targets", nargs="+", help="Seed files-or-URLs to ingest.")
        sub.set_defaults(func=_cmd_index)
    list_sub = index_sub.add_parser(
        "list", help="Report how many documents the index holds."
    )
    list_sub.set_defaults(func=_cmd_index)

    config_cmd = commands.add_parser(
        "config", help="Validate configuration without serving."
    )
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    validate_sub = config_sub.add_parser(
        "validate", help="Validate a config file (no server is started)."
    )
    validate_sub.add_argument(
        "--config",
        default=None,
        help="Config file to validate (defaults to configs/default.yaml).",
    )
    validate_sub.set_defaults(func=_cmd_config_validate)

    cache_cmd = commands.add_parser("cache", help="Manage the search-result cache.")
    cache_sub = cache_cmd.add_subparsers(dest="cache_command", required=True)
    purge_sub = cache_sub.add_parser(
        "purge",
        help=(
            "Drop every cached entry from the configured backend. "
            "In-process memory caches of running servers are unaffected "
            "(restart or wait out the TTL); sqlite/redis backends purge "
            "for every process."
        ),
    )
    purge_sub.set_defaults(func=_cmd_cache_purge)

    providers_cmd = commands.add_parser(
        "providers", help="Inspect configured search providers."
    )
    providers_sub = providers_cmd.add_subparsers(
        dest="providers_command", required=True
    )
    health_sub = providers_sub.add_parser(
        "health", help="Per-provider reachability table (offline)."
    )
    health_sub.set_defaults(func=_cmd_providers_health)
    return parser


def _get_engine(config_path: str | None = None) -> Engine:
    """Load config and build the Engine (the single shared boot path)."""
    return build_engine(load_app_config(config_path))


def _resolve_run_index() -> Callable[..., Any] | None:
    """Return the indexer entry point, or None when it is unavailable."""
    candidate = getattr(indexing_api, "run_index", None)
    return candidate if callable(candidate) else None


def _open_repository(config: dict[str, Any]) -> DocumentRepository:
    """Open the document repository named by the app config.

    Uses the repository seam only — never raw SQLite access. Falls back
    to an in-memory repository when no ``db_path`` is configured.
    """
    db_path = ":memory:"
    providers = config.get("providers")
    if isinstance(providers, dict):
        for entry in providers.values():
            if isinstance(entry, dict):
                candidate = entry.get("db_path")
                if isinstance(candidate, str) and candidate:
                    db_path = candidate
                    break
    return DocumentRepository(db_path)


def _provider_prefs(provider: str | None) -> ProviderPrefs:
    """Translate ``--provider`` into per-query preferences (T13 seam)."""
    if provider is None:
        return ProviderPrefs()
    return ProviderPrefs(primary=provider)


def _cmd_search(args: argparse.Namespace) -> int:
    try:
        app_config = load_app_config()
        configure_logging(app_config)
        engine = build_engine(app_config)
    except Exception as exc:
        print(f"error: cannot build search engine: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    request_id = new_request_id()
    request = SearchRequest(
        query=args.query,
        mode=args.mode,
        limit=args.limit,
        provider_prefs=_provider_prefs(args.provider),
    )
    try:
        response = engine.search(request)
    except Exception as exc:
        print(f"error: search failed: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    # Same single tested log path as the serving route (plan falsifiable
    # claim: one JSON INFO record per CLI search, no raw query at INFO).
    _log_search(request, response, request_id)
    schema = SearchResponseSchema.from_core(response)
    if args.pretty:
        _print_pretty(schema)
    else:
        print(schema.model_dump_json(indent=2))
    return 0


def _print_pretty(schema: SearchResponseSchema) -> None:
    """Render the same response payload in human-readable form."""
    print(f"{schema.query!r} ({schema.mode}): {schema.total} result(s)")
    for position, hit in enumerate(schema.results, start=1):
        print(
            f"{position}. [{hit.provider}] {hit.title} "
            f"(final={hit.final_score:.3f}, original={hit.original_score:.3f})"
        )
        if hit.url:
            print(f"   {hit.url}")
        print(f"   id={hit.id}")


def _cmd_index(args: argparse.Namespace) -> int:
    try:
        config = load_app_config()
        repository = _open_repository(config)
    except Exception as exc:
        print(f"error: cannot open index: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    if args.index_command == "list":
        try:
            count = repository.count()
        except Exception as exc:
            print(f"error: cannot list index: {exc}", file=sys.stderr)
            return FAILURE_EXIT
        print(f"{count} document(s) in index")
        return 0
    run_index = _resolve_run_index()
    if run_index is None:
        print(f"error: {INDEXER_UNAVAILABLE_MESSAGE}", file=sys.stderr)
        return FAILURE_EXIT
    indexing_config = config.get("indexing", {})
    if not isinstance(indexing_config, dict):
        indexing_config = {}
    try:
        stats = run_index(list(args.targets), repository, dict(indexing_config))
    except Exception as exc:
        print(f"error: index {args.index_command} failed: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    print(
        f"index {args.index_command}: added={stats.added} "
        f"updated={stats.updated} skipped_unchanged={stats.skipped_unchanged} "
        f"skipped_duplicates={stats.skipped_duplicates}"
    )
    return 0


def _cmd_config_validate(args: argparse.Namespace) -> int:
    if args.config is not None:
        path = Path(args.config)
        label = str(path)
        try:
            config = load_config(path)
        except Exception as exc:
            print(f"error: invalid config '{label}': {exc}", file=sys.stderr)
            return FAILURE_EXIT
    else:
        label = str(DEFAULT_CONFIG_PATH)
        config = load_app_config()
        if not config:
            print(
                f"error: invalid config '{label}': no config found",
                file=sys.stderr,
            )
            return FAILURE_EXIT
    try:
        build_engine(config)
    except Exception as exc:
        print(f"error: invalid config '{label}': {exc}", file=sys.stderr)
        return FAILURE_EXIT
    print(f"config OK: {label}")
    return 0


def _cmd_cache_purge(args: argparse.Namespace) -> int:  # noqa: ARG001
    from openlens.cache.factory import build_cache_provider, validate_cache_section

    try:
        config = load_app_config()
    except Exception as exc:
        print(f"error: cannot load config: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    try:
        section = validate_cache_section(config.get("cache", None))
        provider = build_cache_provider(section or None)
    except Exception as exc:
        print(f"error: cannot build cache backend: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    if provider is None:
        print("cache disabled (cache.enabled: false); nothing to purge")
        return 0
    try:
        removed = provider.clear()
    except Exception as exc:
        print(f"error: cache purge failed: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    finally:
        provider.close()
    print(f"purged {removed} cached entr{'y' if removed == 1 else 'ies'}")
    return 0


def _cmd_providers_health(args: argparse.Namespace) -> int:  # noqa: ARG001
    try:
        engine = _get_engine()
    except Exception as exc:
        print(f"error: cannot build search engine: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    try:
        statuses = engine.health_check()
    except Exception as exc:
        print(f"error: health check failed: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    if not statuses:
        print("no providers configured")
        return 0
    width = max(len(name) for name in statuses)
    print(f"{'PROVIDER'.ljust(width)}  STATUS")
    for name in sorted(statuses):
        state = "reachable" if statuses[name] else "unreachable"
        print(f"{name.ljust(width)}  {state}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
