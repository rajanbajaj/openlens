"""Build/maintain docs/workflow/sessions/prompt-history.md (Task 20).

Reads top-level user turns from Muse session logs (main ``session.jsonl``
files only -- never ``subagent/`` logs) and maintains one Markdown section
per session::

    ## <date-time>_<session-slug> (<session-id>)

followed by a numbered list of that session's user prompts in arrival
order. Re-running on an already-recorded session changes zero bytes;
a genuinely new user turn appends exactly one entry.

Run from the repo root (or anywhere; paths default sensibly)::

    uv run python docs/workflow/sessions/update-prompt-history.py
    uv run python docs/workflow/sessions/update-prompt-history.py \\
        --session-id <id> --slug 2026-10-28-1648_lime-telescopium

Secrets are scrubbed with the exact rules used by the search-log
scrubber (``openlens.observability.logging.scrub_value``); a prompt that
is nothing but a secret dump becomes ``[redacted credential]``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime


def _repo_root() -> str:
    here = os.path.abspath(__file__)
    for _ in range(4):
        here = os.path.dirname(here)
    if os.path.isdir(os.path.join(here, "src", "openlens")):
        return here
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "src", "openlens")):
        return cwd
    return here


REPO_ROOT = _repo_root()
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from openlens.observability.logging import REDACTED, scrub_value  # noqa: E402

DEFAULT_SUMMARIES_DIR = os.path.join(REPO_ROOT, "docs", "workflow", "sessions")
DEFAULT_OUT = os.path.join(DEFAULT_SUMMARIES_DIR, "prompt-history.md")


def _default_sessions_root() -> str:
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(xdg, "muse", "sessions")


HEADER_RE = re.compile(r"^##\s+(?P<head>.+?)\s+\((?P<sid>[0-9a-f-]{36})\)\s*$")
ENTRY_RE = re.compile(r"^(?P<num>\d+)\.\s+(?P<text>.*)$")


def normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip."""
    return " ".join(text.split())


def clean_prompt(raw: str) -> str | None:
    """Scrub secrets and collapse to one line; None when there is no directive."""
    scrubbed = scrub_value(raw)
    if not isinstance(scrubbed, str):
        return None
    collapsed = normalize(scrubbed)
    if not collapsed:
        return None
    remainder = collapsed.replace(REDACTED, "")
    if not re.search(r"[A-Za-z0-9]", remainder):
        return "[redacted credential]"
    return collapsed


def session_workspace(session_log: str) -> str | None:
    """Workspace root recorded by the session, if any."""
    with open(session_log, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line).get("payload") or {}
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("kind")
            if kind == "metadata":
                root = (payload.get("record") or {}).get("workspace_root")
                if isinstance(root, str):
                    return os.path.realpath(root)
            elif kind == "session_start_intake_v3":
                meta = (payload.get("payload") or {}).get("session_metadata") or {}
                root = meta.get("workspace_root")
                if isinstance(root, str):
                    return os.path.realpath(root)
    return None


def extract_prompts(session_log: str) -> tuple[list[str], str | None, int | None]:
    """Return (prompts, first_prompt_local_dt, first_prompt_ts) in log order.

    Sources, in record order: ``started`` events carrying ``prompt``
    (preferring an immediately following ``user_prompt_display`` text,
    which already stands in for pasted content), plus ``inbox_item_queued``
    events whose source is ``user_steer`` (prompts typed while a run was
    active). Multi-line prompts without a display record collapse to
    their first line (the directive); pasted tool output lives on the
    lines after the first, so it is trimmed.
    """
    with open(session_log, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    events = [
        (rec.get("recorded_at"), (rec.get("payload") or {}).get("event") or {})
        for rec in records
    ]
    prompts: list[str] = []
    first_dt: str | None = None
    first_ts: int | None = None
    index = 0
    while index < len(events):
        recorded_at, event = events[index]
        kind = event.get("kind")
        raw: str | None = None
        if kind == "started" and isinstance(event.get("prompt"), str):
            raw = event["prompt"]
            # A display record normally follows at once, but kind-less
            # envelope records (e.g. publication arming) can sit between.
            display = None
            look = index + 1
            while look < len(events) and look - index <= 5:
                candidate = events[look][1]
                if candidate.get("kind"):
                    if candidate.get("kind") == "user_prompt_display":
                        display = candidate
                    break
                look += 1
            if display is not None and isinstance(display.get("prompt"), str):
                raw = display["prompt"]
                index = look
            else:
                raw = raw.split("\n", 1)[0]
        elif kind == "inbox_item_queued":
            source = event.get("source")
            is_steer = isinstance(source, dict) and source.get("source") == "user_steer"
            if is_steer:
                payload = event.get("payload") or {}
                candidate = payload.get("prompt", event.get("body"))
                if isinstance(candidate, str):
                    raw = candidate.split("\n", 1)[0]
        if raw is not None:
            cleaned = clean_prompt(raw)
            if cleaned is not None:
                prompts.append(cleaned)
                if first_ts is None and recorded_at:
                    first_ts = recorded_at
                    first_dt = datetime.fromtimestamp(recorded_at / 1_000_000).strftime(
                        "%Y-%m-%d-%H%M"
                    )
        index += 1
    return prompts, first_dt, first_ts


def read_existing(path: str) -> tuple[list[str], dict[str, list[str]]]:
    """Parse an existing history file into (headers, entries-by-session-id)."""
    headers: list[str] = []
    entries: dict[str, list[str]] = {}
    current: str | None = None
    if not os.path.exists(path):
        return headers, entries
    with open(path, encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            match = HEADER_RE.match(line)
            if match:
                current = match.group("sid")
                headers.append(line)
                entries.setdefault(current, [])
                continue
            item = ENTRY_RE.match(line)
            if item and current is not None:
                entries[current].append(item.group("text"))
    return headers, entries


def oldest_summary_slug(summaries_dir: str, session_id: str) -> str | None:
    """Oldest summary basename mentioning the session id, if any."""
    hits: list[str] = []
    if not os.path.isdir(summaries_dir):
        return None
    for name in sorted(os.listdir(summaries_dir)):
        if not name.endswith(".md") or name == "prompt-history.md":
            continue
        if name == "README.md":
            continue
        path = os.path.join(summaries_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                if session_id in handle.read():
                    hits.append(name[: -len(".md")])
        except OSError:
            continue
    return hits[0] if hits else None


def session_logs(sessions_root: str) -> list[tuple[str, str]]:
    """All top-level session.jsonl paths as (session_id, path)."""
    found: list[tuple[int, str, str]] = []
    for dirpath, dirnames, filenames in os.walk(sessions_root):
        dirnames[:] = [d for d in dirnames if d != "subagent"]
        if "session.jsonl" in filenames and os.path.basename(dirpath) != "subagent":
            parent = os.path.basename(dirpath)
            if re.fullmatch(r"[0-9a-f-]{36}", parent):
                path = os.path.join(dirpath, "session.jsonl")
                try:
                    first = os.path.getmtime(path)
                except OSError:
                    continue
                found.append((first, parent, path))
    found.sort()
    return [(sid, path) for _, sid, path in found]


def build(
    sessions_root: str,
    summaries_dir: str,
    repo_root: str,
    only_session: str | None,
    slug_override: str | None,
) -> tuple[list[tuple[str, list[str]]], dict[str, int]]:
    """Extract prompts; return ([(header, entries)] oldest-first, stats).

    Only sessions whose recorded workspace root is this repo are
    included. ``slug_override`` applies solely to a targeted
    ``--session-id`` run (the exit-time call that just wrote the
    session's summary).
    """
    sections: list[tuple[int, str, list[str]]] = []
    stats = {"sessions": 0, "prompts": 0, "skipped_other_workspace": 0}
    for session_id, log_path in session_logs(sessions_root):
        if only_session and session_id != only_session:
            continue
        workspace = session_workspace(log_path)
        if workspace != os.path.realpath(repo_root):
            stats["skipped_other_workspace"] += 1
            continue
        prompts, first_dt, first_ts = extract_prompts(log_path)
        if not prompts:
            continue
        slug = oldest_summary_slug(summaries_dir, session_id)
        if slug is None:
            if only_session and slug_override:
                slug = slug_override
            else:
                slug = f"{first_dt or 'unknown'}_{session_id[:8]}"
        header = f"## {slug} ({session_id})"
        sections.append((first_ts or 0, header, prompts))
        stats["sessions"] += 1
        stats["prompts"] += len(prompts)
    sections.sort(key=lambda item: item[0])
    return [(header, prompts) for _, header, prompts in sections], stats


def merge(out_path: str, sections: list[tuple[str, list[str]]]) -> tuple[int, int]:
    """Merge extracted sections into the history file; return (added, sessions)."""
    headers, existing = read_existing(out_path)
    known_headers = {HEADER_RE.match(h).group("sid"): h for h in headers}  # type: ignore[union-attr]
    added = 0
    touched: set[str] = set()
    for header, prompts in sections:
        session_id = HEADER_RE.match(header).group("sid")  # type: ignore[union-attr]
        if session_id not in known_headers:
            known_headers[session_id] = header
            headers.append(header)
        # Occurrence-aware dedupe: the k-th identical prompt in the log
        # maps to the k-th recorded entry, so re-running skips exactly
        # what is recorded while a genuinely repeated user turn appends.
        recorded_counts: dict[str, int] = {}
        for text in existing.get(session_id, []):
            key = normalize(text)
            recorded_counts[key] = recorded_counts.get(key, 0) + 1
        seen_counts: dict[str, int] = {}
        section_entries = existing.setdefault(session_id, [])
        for prompt in prompts:
            key = normalize(prompt)
            occurrence = seen_counts.get(key, 0)
            seen_counts[key] = occurrence + 1
            if occurrence < recorded_counts.get(key, 0):
                continue
            recorded_counts[key] = recorded_counts.get(key, 0) + 1
            section_entries.append(prompt)
            added += 1
        if prompts:
            touched.add(session_id)
    lines = [
        "# Project Prompt History",
        "",
        "Every user prompt that directed project work, one section per session,",
        "oldest first. Source: session JSONL logs; see `sessions/README.md`.",
        "",
    ]
    for header in headers:
        session_id = HEADER_RE.match(header).group("sid")  # type: ignore[union-attr]
        lines.append(header)
        lines.append("")
        for number, text in enumerate(existing.get(session_id, []), 1):
            lines.append(f"{number}. {text}")
        lines.append("")
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return added, len(touched)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-root", default=_default_sessions_root())
    parser.add_argument("--summaries-dir", default=DEFAULT_SUMMARIES_DIR)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--slug", default=None, help="Header slug for a session with no summary yet."
    )
    args = parser.parse_args(argv)
    # The project whose sessions count is the one owning summaries_dir:
    # <root>/docs/workflow/sessions -> <root>. Otherwise fall back to
    # this script's own repo checkout.
    sessions_parent = os.path.dirname(os.path.abspath(args.summaries_dir))
    if os.path.basename(sessions_parent) == "workflow":
        repo_root = os.path.realpath(os.path.dirname(os.path.dirname(sessions_parent)))
    else:
        repo_root = os.path.realpath(REPO_ROOT)
    sections, stats = build(
        args.sessions_root,
        args.summaries_dir,
        repo_root,
        args.session_id,
        args.slug,
    )
    added, touched = merge(args.out, sections)
    print(
        f"sessions with prompts: {stats['sessions']} "
        f"({touched} in file), prompts extracted: {stats['prompts']}, "
        f"entries added: {added} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
