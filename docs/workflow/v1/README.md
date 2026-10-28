# Workflow v1 — Run Guide

## Contents

- `workflow.js` — executable root script for the workflow tool.
- `context.md` — shared conventions injected into every agent prompt.

## Run

Launch the workflow tool with `docs/workflow/v1/workflow.js` as the script
source and the contents of `docs/workflow/v1/skeleton_spec.json` as its input.
The script is generic: it executes whatever waves the input describes, so the
skeleton run is:

1. **contracts** (solo): hygiene + core contracts.
2. **build** (4 parallel): retrieval, providers, pipeline+ranking, API.
3. **seams-and-tests** (2 parallel): AI seams, conformance/quality tests.
4. **integrate** (solo) + **synthesis**: wiring, full suite, smoke test, verdict.

## Reuse for a Future Plan

1. Write the plan as `docs/workflow/tasks/<YYYY-MM-DD-HHMM>_<slug>_plan.md`
   (datetime = date and time it was written).
2. Copy `skeleton_spec.json` to `<plan>_spec.json`.
3. Point `plan_path` at the new plan; rewrite `waves`/`integrate` briefs
   (inline `owns`/`task`/`done`, or `brief_path` files for long briefs).
4. Run `workflow.js` unchanged with the new spec as input.

Existing specs: `skeleton_spec.json` (backend, with `context.md`),
`frontend_spec.json` (frontend in `frontend/`, with `frontend_context.md`).

## Resume

If a wave fails, relaunch the script with the same spec plus
`only_waves: ["<failed-wave-names>"]` and `prior_refs` copied from the prior
report, so relaunched agents see earlier evidence as context. Skipped waves
are listed in the report — a report with skips is never a done verdict.
(Alternatively the platform can resume the interrupted run by id, which reuses
recorded results directly.)

## Interpret Results

The workflow returns a JSON summary: per-agent status (ok / failed / gap),
the integration verdict, and the Definition-of-Done checklist with
pass/fail per item. Any item not marked pass with a cited test command is
unfinished — do not treat the plan as done.

A large structured submission is retained by result ref (`spilled: true`)
instead of inlined. That is a successful handoff, not a failed agent: the
synthesis agent receives the refs and inspects them before judging. Only a
missing ref, an error result, or gaps the synthesis itself reports count as
unfinished for that agent.
