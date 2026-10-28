# Task 22 — AI digest source citations

## Problem

AI digests (`ai_summary` / `ai_perspectives`) are flat strings with a model
label but no provenance: a reader cannot tell which search hit a statement
came from, and the UI renders no source link. Nothing named "citation"
exists in the repo (verified 2026-10-28: zero `citat` matches across
`src/`, `frontend/src`, plans, specs, configs, README).

## Goal

Every AI digest carries machine-readable source attribution from the
result(s) it was built from, rendered as source links in the Enterprise
UI's AI block — provenance the reader can click, not a truth claim.

## Design

1. **Backend (`ai-summary` plugin only).** For each enriched result
   (the existing top-K per-result loop), attach
   `result.metadata["ai_citations"] = [{"id": ..., "title": ..., "url": ...}]`
   alongside `ai_summary` / `ai_perspectives` / `ai_model`, assigned
   atomically in the same per-result write. One entry per digest today
   (each summary is built from its own result alone); a list so future
   multi-source digests fit without a shape change. No-model path
   unchanged: no digest → no citations, same rule as the AI block.
2. **Wire.** No schema change: `SearchResultSchema.metadata` is a
   free-form dict and already passes through to the frontend. Covered by
   an assertion, not new code.
3. **Frontend.** Extend the enrichment reader (`resultAiDigest`) with
   defensive `citations` parsing (array of string-id/title/url records;
   malformed → dropped, never a crash), mirror the shape in
   `api/types.ts` + `guard.ts` in the same change, and render a
   "Sources:" link list inside the existing AI digest block. The
   established absent-rule holds: no citations → no sources chrome.
4. **Tests.** Backend unit: enriched result carries citations matching
   its own id/title/url; unenriched results carry none. Frontend unit:
   links render with correct hrefs; malformed/absent citations render
   nothing. Fixture: one envelope with AI metadata incl. citations.

## Out of scope

- Inline `[1]`-style claim markers inside summary prose (unreliable with
  rule-based/local backends; source-level attribution only).
- Multi-result fused digests (shape allows it; not built here).
- Judging citation *correctness* — we attest provenance, not truth.
- Any change to ranking, filtering, the model backends, or diagnostics.

## Definition of done

1. Enriched results carry `ai_citations` matching their own id/title/url;
   unenriched results carry none.
2. The wire carries citations with zero schema changes (assertion proves it).
3. The AI block renders clickable source links; malformed/absent
   citations render no sources chrome and never crash.
4. Full backend suite green, ruff/format/mypy clean; frontend
   eslint/prettier/vitest/build/audit clean.

## Verification

- Backend unit: citations present + self-matching on enriched hits,
  absent elsewhere (rule-based and model paths where runnable offline).
- Frontend unit: link hrefs correct; absent/malformed cases render nothing.
- Envelope fixture with AI metadata parses through the guard.
