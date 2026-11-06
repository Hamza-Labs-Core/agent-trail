"""Claude Code hook: did this session write a trail event for each pull request it merged?

The plugin wires it for Stop, PreCompact, SessionStart and SessionEnd (hooks/hooks.json). It
reads the hook's JSON on stdin (session_id, transcript_path, cwd, hook_event_name,
stop_hook_active, source), finds `gh pr merge N` in the session's transcript and its
subagents' transcripts, keeps the N that git shows merged into <remote>/<main_branch>, and
looks for an event for each one: an <events>/NNNN-*.md file (or one listing N in prs:) on any
local or remote-tracking branch or in any worktree of the repository.

  Stop          blocks once (stop_hook_active) with the list, so the session writes them now
  PreCompact    tells the user, before the details are compacted away
  SessionStart  after a compact or resume: reminds the session; at startup: lists what
                earlier sessions left unwritten in this repository (from SessionEnd's log)
  SessionEnd    tells the user and logs what is still unwritten

It does nothing in a repository with no .trail.toml. It never fails the session: any error
prints nothing and exits 0, and every git call has a timeout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config as config_mod
from .config import Config
from .core import MERGE_RE

MERGE_CMD_RE = re.compile(r"gh pr merge (\d+)")
FETCH_EVERY = 600  # seconds between fetches of the main branch from this hook, per repository


def state_dir() -> Path:
    for var in ("CLAUDE_TRAIL_HOOK_DIR", "CLAUDE_PLUGIN_DATA"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    return Path.home() / ".claude" / "trail-hook"


STATE_DIR = state_dir()


def git(repo: Path, *args: str, timeout: float = 20) -> str:
    """git's output, or "" when it fails or runs out of time."""
    try:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def repo_key(repo: Path) -> str:
    """One name for a repository across its worktrees: its common git dir."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=5).strip()
    return common or str(repo.resolve())


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
                            cmd = str((block.get("input") or {}).get("command", ""))
                            found |= {int(n) for n in MERGE_CMD_RE.findall(cmd)}
        except OSError:
            continue
    return found


def fetch_main(repo: Path, cfg: Config) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(repo_key(repo).encode()).hexdigest()[:12]
    stamp = STATE_DIR / f"last-fetch-{key}"
    try:
        if time.time() - stamp.stat().st_mtime < FETCH_EVERY:
            return
    except OSError:
        pass
    stamp.touch()  # first: a fetch that hangs is not tried again at every Stop
    git(repo, "fetch", "-q", cfg.remote, cfg.main_branch, timeout=15)


def merged_on_main(repo: Path, cfg: Config) -> set[int]:
    log = git(repo, "log", "--first-parent", "--merges", "--format=%s", cfg.main_ref)
    return {int(m.group(1)) for line in log.splitlines() if (m := MERGE_RE.match(line))}


def covered(repo: Path, cfg: Config) -> set[int]:
    """Pull requests with an event on any local branch, remote branch or worktree."""
    out: set[int] = set()
    file_re = cfg.file_re

    def add(name: str, text: str | None = None) -> None:
        m = file_re.match(Path(name).name)
        if m and m.group("pr"):
            out.add(int(m.group("pr")))
        if text:
            pm = re.search(r"^prs:\s*\[([^\]]*)\]", text, re.M)
            if pm:
                out.update(int(x) for x in re.findall(r"\d+", pm.group(1)))

    for name in git(repo, "log", "--all", "--format=", "--name-only", "--", cfg.events).split("\n"):
        if name:
            add(name)
    # the prs: lists of those events, from every version git has of them (an event on a
    # branch that is not checked out anywhere covers its prs: too)
    patch = git(repo, "log", "--all", "--format=", "-p", "--unified=0", "--", cfg.events)
    for m in re.finditer(r"^\+prs:\s*\[([^\]]*)\]", patch, re.M):
        out.update(int(x) for x in re.findall(r"\d+", m.group(1)))
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            d = Path(line[9:]) / cfg.events
            if d.is_dir():
                for p in d.glob("*.md"):
                    try:
                        add(p.name, p.read_text(encoding="utf-8"))
                    except OSError:
                        pass
    return out


def unwritten(repo: Path, cfg: Config, transcript: Path) -> list[int]:
    asked = {n for n in merge_commands(transcripts(transcript)) if n > cfg.missing_after}
    if not asked:
        return []
    fetch_main(repo, cfg)
    return sorted((asked & merged_on_main(repo, cfg)) - covered(repo, cfg))


def describe(prs: list[int], cfg: Config) -> str:
    names = ", ".join(f"#{n}" for n in prs)
    return (
        f"Trail: this session merged {names} with no trail event yet. Write one per pull request "
        f"now, while you remember what happened (the `trail` skill, or /trail:event: "
        f"{cfg.events}/{'N' * cfg.pr_digits}-slug.md, in the code PR or a trail-only PR)."
    )


def log_path() -> Path:
    return STATE_DIR / "unwritten.jsonl"


def remember(repo: Path, session: str, prs: list[int]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"repo": repo_key(repo), "session": session, "prs": prs, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def earlier(repo: Path, cfg: Config) -> list[tuple[str, list[int]]]:
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    me = repo_key(repo)
    recs = []
    for line in lines[-200:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        # a record from another repository is not this one's to report
        if isinstance(rec, dict) and rec.get("repo", me) == me:
            recs.append(rec)
    if not recs:
        return []
    have = covered(repo, cfg)
    out = []
    for rec in recs[-50:]:
        left = [n for n in rec.get("prs", []) if isinstance(n, int) and n not in have]
        if left:
            out.append((str(rec.get("session", "?")), left))
    return out


def handle(data: dict[str, object], repo: Path, cfg: Config | None = None) -> dict[str, object] | None:
    if cfg is None:
        root = config_mod.find_root(repo)
        cfg = config_mod.load(root)
        repo = root
    if not cfg.found:
        return None  # not a trail repository
    event = str(data.get("hook_event_name", ""))
    transcript = Path(str(data.get("transcript_path", "")))
    session = str(data.get("session_id", ""))
    if event == "SessionStart" and data.get("source") == "startup":
        left = earlier(repo, cfg)
        if not left:
            return None
        rows = "; ".join(f"{', '.join('#' + str(n) for n in prs)} (session {s})" for s, prs in left)
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"Trail: earlier sessions merged pull requests with no trail event: {rows}. "
                "Write their events from the PRs and their diffs if this session has the context.",
            }
        }
    prs = unwritten(repo, cfg, transcript)
    if not prs:
        return None
    msg = describe(prs, cfg)
    if event == "Stop":
        return None if data.get("stop_hook_active") else {"decision": "block", "reason": msg}
    if event == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}
    if event == "SessionEnd":
        remember(repo, session, prs)
    return {"systemMessage": msg}


def main() -> int:
    try:
        data = json.load(sys.stdin)
        repo = Path(os.environ.get("CLAUDE_PROJECT_DIR") or str(data.get("cwd") or "."))
        out = handle(data, repo)
        if out:
            print(json.dumps(out))
    except Exception:  # a hook never breaks the session
        return 0
    return 0
