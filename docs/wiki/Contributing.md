# Contributing (developers)

1. Read the area first: interfaces, existing tests, and call sites
   define the contract — not the issue text.
2. Smallest correct fix at the root cause; cover error/edge inputs,
   not just the happy path.
3. Keep the gates green before handing off: backend
   (`pytest`, `ruff`, `mypy`), frontend (`test`, `lint`,
   `format:check`, `build`), orval regen after route/schema edits.
4. Wire changes are additive with degrade-to-absent readers; update
   the pinned fixture/expectation only when the contract changed.
5. One concern per commit; name the files you touched. Never
   commit, push, or tag unless asked — finishing work is not
   authorization.
6. Docs are deliverables: update README/`docs/` with behavior
   changes, add `docs/troubleshoots/` entries for production
   incidents, and **redact infrastructure specifics** (no IPs,
   server paths, or domains) from everything committed.
