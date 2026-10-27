# Session Summaries

Every session ends with a summary file here, named
`<YYYY-MM-DD-HHMM>_<session-slug>.md` (datetime = when the session ended).
It is the handoff to the next session: enough to resume without rereading
the whole repo. Writing it is part of ending a session, not optional.

## Format

Each summary has exactly these sections, in order. Keep every section
tight — facts and file paths, no deliberation logs.

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
3. Add/refresh it in the same commit as any other session-end commit, or
   leave it uncommitted and say so in the final reply.
4. The final reply names the summary file and the single most important
   open thread, if any.
