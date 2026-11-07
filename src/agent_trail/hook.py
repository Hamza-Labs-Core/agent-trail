"""Session hooks for coding agents: did this session write a trail event for each pull request
it merged? One handler, an adapter per agent; the question itself is remind.py's.

    trail hook --agent claude   Claude Code (the plugin's hooks/hooks.json): Stop, PreCompact,
                                SessionStart, SessionEnd. Finds `gh pr merge N` in the
                                session's transcript and its subagents' transcripts.
    trail hook --agent codex    Codex (.codex/hooks.json): PostToolUse records `gh pr merge N`
                                from tool_input.command (Codex's transcript format is not a
                                stable interface); Stop, SessionStart, SessionEnd.
    trail hook --agent cursor   Cursor (.cursor/hooks.json): afterShellExecution records the
                                merge; stop, sessionStart, sessionEnd.

What each does:
  stop             once per stop (stop_hook_active, Cursor's loop_count), asks the session to
                   write the missing events now
  session start    at startup, lists what earlier sessions in this repository left
                   unwritten; after a compact or resume (Claude, Codex), reminds the session
  pre-compact      (Claude) tells the user, before the details are compacted away
  session end      logs what is still unwritten, for the next session's start

It does nothing in a repository with no .trail.toml. It never fails the session: any error
prints nothing (Cursor: `{}`) and exits 0, and every git call has a timeout.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import remind as R
from .config import Config

AGENTS = ("claude", "codex", "cursor")


# ---- Claude Code: the transcript -------------------------------------------------------


def transcripts(transcript: Path) -> list[Path]:
    out = [transcript] if transcript.is_file() else []
    sub = transcript.with_suffix("") / "subagents"
    if sub.is_dir():
        out += sorted(sub.glob("*.jsonl"))
    return out


def merge_commands(paths: list[Path]) -> set[int]:
    """Pull request numbers the session (or its subagents) ran `gh pr merge` on."""
    found: set[int] = set()
    for p in paths:
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "gh pr merge" not in line or '"tool_use"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    for block in (rec.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            found |= R.merge_numbers(str((block.get("input") or {}).get("command", "")))
        except OSError:
            continue
    return found


# ---- the handler ----------------------------------------------------------------------


def _start_context(repo: Path, cfg: Config) -> str:
    left = R.earlier_unwritten(repo, cfg)
    if not left:
        return ""
    rows = "; ".join(f"{', '.join('#' + str(n) for n in prs)} (session {s})" for s, prs in left)
    return (
        f"Trail: earlier sessions merged pull requests with no trail event: {rows}. "
        "Write their events from the PRs and their diffs if this session has the context."
    )


def _command(data: dict[str, Any]) -> str:
    ti = data.get("tool_input")
    cmd = ti.get("command", "") if isinstance(ti, dict) else data.get("command", "")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    return str(cmd or "")


def handle(data: dict[str, Any], repo: Path, cfg: Config | None = None, agent: str = "claude") -> dict[str, Any] | None:
    if cfg is None:
        root = config_mod.find_root(repo)
        cfg = config_mod.load(root)
        repo = root
    if not cfg.found:
        return None  # not a trail repository
    event = str(data.get("hook_event_name", ""))
    if agent == "claude":
        return _claude(data, repo, cfg, event)
    if agent == "codex":
        return _codex(data, repo, cfg, event)
    if agent == "cursor":
        return _cursor(data, repo, cfg, event)
    return None


def _claude(data: dict[str, Any], repo: Path, cfg: Config, event: str) -> dict[str, Any] | None:
    session = str(data.get("session_id", ""))
    if event == "SessionStart" and data.get("source") == "startup":
        ctx = _start_context(repo, cfg)
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}} if ctx else None
    prs = R.unwritten(repo, cfg, merge_commands(transcripts(Path(str(data.get("transcript_path", ""))))))
    if not prs:
        return None
    msg = R.describe(prs, cfg)
    if event == "Stop":
        return None if data.get("stop_hook_active") else {"decision": "block", "reason": msg}
    if event == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}
    if event == "SessionEnd":
        R.remember_unwritten(repo, session, prs, "claude")
    return {"systemMessage": msg}


def _codex(data: dict[str, Any], repo: Path, cfg: Config, event: str) -> dict[str, Any] | None:
    session = str(data.get("session_id", ""))
    if event == "PostToolUse":
        R.record_merges(repo, session, R.merge_numbers(_command(data)))
        return None
    if event == "SessionStart":
        if data.get("source") == "startup":
            ctx = _start_context(repo, cfg)
        else:
            prs = R.unwritten(repo, cfg, R.recorded_merges(repo, session))
            ctx = R.describe(prs, cfg) if prs else ""
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}} if ctx else None
    if event == "Stop":
        if data.get("stop_hook_active"):
            return None
        prs = R.unwritten(repo, cfg, R.recorded_merges(repo, session))
        return {"decision": "block", "reason": R.describe(prs, cfg)} if prs else None
    if event == "SessionEnd":
        # Codex gives SessionEnd at most 3 seconds: no fetch
        prs = R.unwritten(repo, cfg, R.recorded_merges(repo, session), fetch=False)
        if prs:
            R.remember_unwritten(repo, session, prs, "codex")
    return None


def _cursor(data: dict[str, Any], repo: Path, cfg: Config, event: str) -> dict[str, Any]:
    session = str(data.get("conversation_id") or data.get("session_id") or "")
    if event == "afterShellExecution":
        R.record_merges(repo, session, R.merge_numbers(_command(data)))
        return {}
    if event == "sessionStart":
        ctx = _start_context(repo, cfg)
        return {"additional_context": ctx} if ctx else {}
    if event == "stop":
        if int(data.get("loop_count") or 0) > 0:
            return {}
        prs = R.unwritten(repo, cfg, R.recorded_merges(repo, session))
        return {"followup_message": R.describe(prs, cfg)} if prs else {}
    if event == "sessionEnd":
        prs = R.unwritten(repo, cfg, R.recorded_merges(repo, session), fetch=False)
        if prs:
            R.remember_unwritten(repo, session, prs, "cursor")
    return {}


def _repo(data: dict[str, Any]) -> Path:
    roots = data.get("workspace_roots")
    if isinstance(roots, list) and roots:
        return Path(str(roots[0]))
    for var in ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var])
    return Path(str(data.get("cwd") or "."))


def main(agent: str = "claude") -> int:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            data = {}
        out = handle(data, _repo(data), agent=agent)
        if out is not None and (out or agent == "cursor"):
            print(json.dumps(out))
        elif agent == "cursor":
            print("{}")
    except Exception:  # a hook never breaks the session
        if agent == "cursor":
            print("{}")
        return 0
    return 0
