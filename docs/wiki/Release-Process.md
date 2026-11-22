# Release Process (product)

1. Land work on `main` — CI (gates per [Testing and CI](Testing-and-CI)
   plus gitleaks) must be green.
2. Write or update the plan in `docs/releases/`.
3. Set `project.version` in `pyproject.toml` to `X.Y.Z` (no `v`
   prefix) in the release commit, so the tree always names the
   release it ships.
4. Tag: `git tag vX.Y.Z && git push origin main vX.Y.Z`.
   Tags mark releases but never deploy by themselves; `main` and
   tag pushes never trigger a deploy. Tags never move: a missed
   bump is fixed by a follow-up sync commit, never a retag.
5. Deploy by hand: Actions -> release -> Run workflow, with
   `services` set to `all` for the full stack (or named services
   for a selective deploy) and `ref` set to the release tag
   (default `main`). The release workflow (self-hosted runner)
   rebuilds compose, waits for `/healthz`, then gates on frontier
   status, a live search, and both frontends. Concurrency is
   serialized; unused images are pruned on success.

Versioning is `vMAJOR.MINOR.PATCH` (`pyproject.toml` mirrors the
line). Rollback is the previous tag. Server specifics (hosts,
paths, domains) live only in the server-side secrets file and
never in the repo or its docs.
