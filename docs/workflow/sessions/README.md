# Session Summaries

Every session ends with a summary file here, named
`<YYYY-MM-DD-HHMM>_<session-slug>.md` (datetime = when the session ended).
It is the handoff to the next session: enough to resume without rereading
the whole repo. Writing it is part of ending a session, not optional.

## Format

Each summary has exactly these sections, in order. Keep every section
tight — facts and file paths, no deliberation logs.

0. **Session** — session id and log path, so a later session can resume it (`muse resume <id>` or read the log).
1. **Outcome** — what this session produced, in 3–8 bullets.
2. **Decisions** — choices made and why (naming, paths, scope cuts). One
   line each, with the reason attached.
3. **Tree state** — last commits on `main`, what is uncommitted, and what
   the next session must commit or finish first.
4. **Environment quirks** — sandbox/toolchain facts discovered this session
   (cache paths, proxy behavior, daemon needs) that the next session will
   otherwise rediscover painfully. Append-only: never delete a quirk that
   still applies; strike through with a date when it stops applying.
5. **Open threads** — unfinished work with exact resume scope (file, wave,
   or command), and anything explicitly deferred to the user.
6. **Key references** — specs, plans, and entry points touched, as paths.

## Procedure (runs before every session end)

1. `git status --short` + `git log --oneline -5` for tree state.
2. Write the file. Slug = 2–4 words on the session's main work.
3. Update `prompt-history.md` (Task 20) with the session's user prompts:

   ```bash
   UV_CACHE_DIR=/tmp/uv-cache uv run python docs/workflow/sessions/update-prompt-history.py \
     --session-id <session-id> --slug <summary-basename-without-.md>
   ```

   Rules the script enforces (see its docstring): only sessions whose
   log records this repo as workspace root are included; a section
   header is set once from the oldest summary naming the session id
   (or `--slug` for a session with no summary yet) and never renamed —
   the session id in parentheses is the dedupe key, so re-running on
   an already-recorded session changes zero bytes; secrets are scrubbed
   with the search-log scrubber rules before writing.
4. Add/refresh the summary, `prompt-history.md`, and the updater script
   in the same commit as any other session-end commit, or leave them
   uncommitted and say so in the final reply.
5. The final reply names the summary file and the single most important
   open thread, if any.

First-time or repair backfill across every session log (oldest first):

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python docs/workflow/sessions/update-prompt-history.py
```
