import sys
from pathlib import Path

# Resolve the openlens package without assuming the runtime layout:
# a repo checkout carries scripts/ beside src/ (host, venv, remote),
# while the backend image mounts the tree at /app (docker).
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (_REPO_ROOT / "src", Path("/app/src")):
    if str(_candidate) not in sys.path and _candidate.is_dir():
        sys.path.insert(0, str(_candidate))
from contextlib import suppress  # noqa: E402

from openlens.core.interfaces import Document  # noqa: E402
from openlens.indexing.crawler import (  # noqa: E402
    CATEGORY_RULES_VERSION,
    SITE_CATEGORIES,
    classify_site_category,
)
from openlens.main import load_app_config  # noqa: E402
from openlens.providers.custom_index import open_store  # noqa: E402

store = open_store(load_app_config())
rewritten = total = offset = 0
try:
    while True:
        page = store.list_documents(limit=500, offset=offset)
        if not page:
            break
        for doc in page:
            total += 1
            meta = dict(doc.metadata or {})
            verdict = classify_site_category(doc.url, meta)
            if (
                meta.get("site_category") in SITE_CATEGORIES
                and meta.get("site_category") == verdict["site_category"]
                and meta.get("site_category_source") == CATEGORY_RULES_VERSION
                and meta.get("site_category_confidence")
                == verdict["site_category_confidence"]
            ):
                continue
            meta.update(verdict)
            store.update_document(
                Document(
                    id=doc.id,
                    title=doc.title,
                    content=doc.content,
                    url=doc.url,
                    metadata=meta,
                )
            )
            rewritten += 1
        offset += len(page)
finally:
    with suppress(Exception):
        store.close()
print(f"backfill done: {rewritten} rewritten of {total} documents")
