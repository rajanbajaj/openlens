# Task 20 — Project prompt history (`sessions/prompt-history.md`)

## Problem

Session summaries in `docs/workflow/sessions/` capture what was done, but the
actual user prompts — the decisions, approvals, and corrections that drove the
work — are scattered across session JSONL logs and lost when sessions compact.
We want one durable, human-readable prompt history for the project.

## Goal

Maintain `docs/workflow/sessions/prompt-history.md` as the single,
append-only record of every user prompt that directed project work, updated
automatically by the workflow before the current session closes, with no
duplicate entries when a session is resumed.

## Design

1. **File format** — Markdown, one section per session:
   `## <date-time>_<session-slug> (<session-id>)`, followed by a numbered list
   of that session's user prompts in arrival order (verbatim, trimmed of
   pasted tool output; multi-line prompts collapsed to their directive).
   Machine/system reminders excluded — user prompts only.
2. **Workflow hook** — the session-exit step (the same step that writes the
   session summary per `sessions/README.md`) also updates `prompt-history.md`.
   Source of prompts: the session JSONL log, filtered to user turns.
3. **Resume dedupe** — keyed on session id. If a section for the session id
   already exists (resumed session), merge: keep existing entries, append only
   prompts not already recorded (compare normalized text). Never create a
   second section for the same session id, never re-append identical prompts.
4. **Secrets hygiene** — prompts may contain pasted tokens/keys; scrub with
   the same known-secret-field rules as the search-log scrubber before
   writing. If a prompt is nothing but a secret dump, record a placeholder
   (`[redacted credential]`), not the value.
5. **Backfill** — seed the file once from existing session logs listed in
   `docs/workflow/sessions/`, oldest first, so history starts complete.

## Out of scope

- No changes to how session summaries are written (that format stands).
- No prompt history for subagent/workflow-internal prompts — top-level user
  turns only.
- No UI surface; file is the interface (task 18 may surface it later).

## Definition of done

1. `prompt-history.md` exists with the per-session-section format above.
2. Backfilled from all existing session logs; spot-checked against two logs.
3. Exit-hook documented in `sessions/README.md` and the workflow so every
   future session updates it before closing.
4. Resume test: re-running the exit step on an already-recorded session id
   changes zero bytes (prove with `git diff --stat` empty).
5. New-prompt test: one additional user turn + exit step appends exactly one
   entry under the existing session section.

## Verification

- `git diff --stat` empty on idempotent re-run (dedupe proven).
- A synthetic session log with a resumed id merges without duplication.
- `gitleaks detect` clean on the file (scrub proven); full backend suite
  unaffected (docs-only change outside `src/`).
