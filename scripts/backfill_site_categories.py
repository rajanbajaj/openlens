import sys

sys.path.insert(0, "/app/src")
from contextlib import suppress

from openlens.core.interfaces import Document
from openlens.indexing.crawler import (
    CATEGORY_RULES_VERSION,
    SITE_CATEGORIES,
    classify_site_category,
)
from openlens.main import load_app_config
from openlens.providers.custom_index import open_store

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
