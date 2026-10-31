# Release plan: search gaps (commit `3a1adf7`)

Target: release server (self-hosted runner `openlens-release`) → public
the public search and admin hostnames (served through the existing
tunnel — fill in from the tunnel dashboard when verifying). Mechanism: pushing a version tag (`v*`, e.g. `v1.0.0`) triggers
[.github/workflows/release.yml](../../.github/workflows/release.yml);
no manual server steps.

## What's in this release

- **UI**: every filter/mode change re-runs search; sort presets; prefer-domain
  lists; chips for all 13 filters; sponsored text-fallback removed;
  `site_category` dropdown.
- **Ranking**: sponsored truthiness end-to-end; shopping demotion 0.25→1.0;
  brand/style/size/occasion and `site_category` are real filters.
- **Indexing**: head-only SVG-blind titles + cascade + cleanup; `site_category`
  family of columns with automatic `_migrate` ALTER + backfill path;
  new `configs/category_domains.yaml` (baked into the backend image via
  `COPY configs/`, no compose change needed).
- **Retrieval**: Unicode tokenizer + casefold, normalize-before-parse,
  code-token preservation, suggest upgrades.
- **Tests**: 103 new regression tests; backend 1224 passed, frontend 192
  passed, ruff/mypy clean (verified pre-commit).

## Pre-release checklist

- [ ] `git status` clean except this plan file (commit `3a1adf7` contains all
  27 implementation files; nothing else pending).
- [ ] No new env vars: the server-side secrets file needs no changes.
  Rebaked `configs/` ships `category_domains.yaml` automatically.
- [ ] No `-v` volume wipes: DB migration runs in-code on boot and is
  additive (`site_category` defaults to `'other'`).
- [ ] Crawler scale stays at 3 (`CRAWLER_SCALE` default in the server-side secrets file).

## Release steps

1. Commit this plan file, then release with `git tag v1.0.0 && git push origin v1.0.0` (tags never move — a fix is a new tag).
2. Watch the `release` workflow (queued behind any running release;
   `concurrency: release-production`, one at a time).
3. Workflow gates must pass: `/healthz`, frontier status, search probe,
   both frontends `:3000`/`:3001`.
4. Confirm completion in the workflow log (`docker compose ps` step).

## Post-release verification (prod)

Run from anywhere with network + admin token:

- [ ] `GET /healthz` → 200; search UI loads at
  the public search hostname.
- [ ] Toggle **Exclude sponsored** on a shopping query → result count drops,
  no organic article about sponsored content disappears.
- [ ] Set brand/style/size filters → results narrow (previously no-ops).
- [ ] `site_category=shopping` filter → 200 with shopping hits only
  (previously would 500 — backend key is new this release).
- [ ] New crawls store clean titles: check `frontier/status` → `New URLs`,
  then search a freshly crawled page and confirm no icon-text concatenation.
- [ ] `configs/category_domains.yaml` effective: crawl a known shopping
  domain, confirm `site_category=shopping` in result metadata/diagnostics.
- [ ] Admin dashboard at the public admin hostname shows
  workers alive and queue draining.

## Rollback

Rollback: a new patch tag from the last good commit (e.g. `git tag v1.0.1 5b7d5fa && git push origin v1.0.1`) redeploys the pre-gap stack. Data is safe: volumes persist, migration is additive, and the old
code ignores the new columns. Alternatively re-run the workflow from the
last good commit. Never `docker compose down -v` on the server.

## Known follow-ups (not in this release)

FTS index-side tokenizer change; admin-frontend category management UI;
integration suites needing live Postgres/MySQL.
