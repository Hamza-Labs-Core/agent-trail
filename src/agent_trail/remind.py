"""What the trail is missing right now: merged pull requests with no event, tags with no snapshot.

Every integration asks the same question through this module: the `trail remind` command,
and the hooks for Claude Code, Codex and Cursor (hook.py). A pull request counts as covered
when an event for it exists anywhere git can see: any local or remote-tracking branch, any
version in history (its `prs:` list too), or any worktree's files. A just-written event on an
unpushed branch counts.

Every git call has a timeout and a failed call reads as "nothing", so a hook built on this
never hangs or fails a session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .core import MERGE_RE, TrailError, tags_without_snapshot

MERGE_CMD_RE = re.compile(r"\bgh\s+pr\s+merge\s+(?:[^\n;&|]*?\s)?#?(\d+)\b")
FETCH_EVERY = 600  # seconds between fetches of the main branch, per repository


def state_dir() -> Path:
    """Where reminders keep their state: AGENT_TRAIL_STATE_DIR, the Claude Code plugin's data
    folder, or the XDG state folder."""
    for var in ("AGENT_TRAIL_STATE_DIR", "CLAUDE_PLUGIN_DATA"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "agent-trail"


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


def _key(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()[:16]


def merge_numbers(command: str) -> set[int]:
    """Pull request numbers a shell command merges (`gh pr merge 702 --merge`)."""
    return {int(n) for n in MERGE_CMD_RE.findall(command)}


# ---- git: what main has, what the events cover ------------------------------------------


def fetch_main(repo: Path, cfg: Config) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = STATE_DIR / f"last-fetch-{_key(repo_key(repo))}"
    try:
        if time.time() - stamp.stat().st_mtime < FETCH_EVERY:
            return
    except OSError:
        pass
    stamp.touch()  # first: a fetch that hangs is not tried again at every stop
    git(repo, "fetch", "-q", cfg.remote, cfg.main_branch, timeout=15)


def merged_on_main(repo: Path, cfg: Config) -> dict[int, tuple[str, str, str]]:
    """{number: (merge sha, date, title)} for the pull requests merged into the main branch."""
    log = git(repo, "log", "--first-parent", "--merges", "--format=%H%x09%cs%x09%s%x09%b%x1e", cfg.main_ref)
    out: dict[int, tuple[str, str, str]] = {}
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, day, subject, body = (rec.split("\t", 3) + ["", "", "", ""])[:4]
        m = MERGE_RE.match(subject)
        if m:
            title = body.strip().splitlines()[0] if body.strip() else subject
            out.setdefault(int(m.group(1)), (sha, day, title))
    return out


def covered(repo: Path, cfg: Config) -> set[int]:
    """Pull requests with an event on any local branch, remote branch, past version or worktree."""
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


def _trail_only(repo: Path, cfg: Config, sha: str) -> bool:
    files = [f for f in git(repo, "diff", "--name-only", "--no-renames", f"{sha}^1", sha, "--").split("\n") if f]
    return bool(files) and all(cfg.is_trail_only(f) for f in files)


def unwritten(repo: Path, cfg: Config, prs: set[int], fetch: bool = True) -> list[int]:
    """Of these pull requests, the ones merged into main with no event yet."""
    asked = {n for n in prs if n > cfg.missing_after}
    if not asked:
        return []
    if fetch:
        fetch_main(repo, cfg)
    main = merged_on_main(repo, cfg)
    left = (asked & set(main)) - covered(repo, cfg)
    return sorted(n for n in left if not _trail_only(repo, cfg, main[n][0]))


def recent_unwritten(repo: Path, cfg: Config, days: int, fetch: bool = True) -> list[tuple[int, str, str]]:
    """(number, date, title) for pull requests merged in the last `days` days with no event."""
    if fetch:
        fetch_main(repo, cfg)
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    have = covered(repo, cfg)
    out = []
    for n, (sha, day, title) in merged_on_main(repo, cfg).items():
        if n > cfg.missing_after and day >= since and n not in have and not _trail_only(repo, cfg, sha):
            out.append((n, day, title))
    return sorted(out)


# ---- per-session memory: what a session merged, what sessions left unwritten -------------


def _session_file(repo: Path, session: str) -> Path:
    return STATE_DIR / "sessions" / f"{_key(repo_key(repo), session)}.json"


def record_merges(repo: Path, session: str, prs: set[int]) -> None:
    """Remember that this session merged these pull requests (from a tool-use hook)."""
    if not prs:
        return
    p = _session_file(repo, session)
    p.parent.mkdir(parents=True, exist_ok=True)
    have = recorded_merges(repo, session)
    p.write_text(json.dumps(sorted(have | prs)), encoding="utf-8")


def recorded_merges(repo: Path, session: str) -> set[int]:
    try:
        data = json.loads(_session_file(repo, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {n for n in data if isinstance(n, int)} if isinstance(data, list) else set()


def log_path() -> Path:
    return STATE_DIR / "unwritten.jsonl"


def remember_unwritten(repo: Path, session: str, prs: list[int], agent: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"repo": repo_key(repo), "session": session, "agent": agent, "prs": prs, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def earlier_unwritten(repo: Path, cfg: Config) -> list[tuple[str, list[int]]]:
    """(session, pull requests) that earlier sessions in this repository left with no event."""
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


# ---- the reminder ---------------------------------------------------------------------


@dataclass
class Reminder:
    prs: list[int] = field(default_factory=list)  # merged by this session, no event
    recent: list[tuple[int, str, str]] = field(default_factory=list)  # merged lately, no event
    tags: list[str] = field(default_factory=list)  # tags with no snapshot

    def __bool__(self) -> bool:
        return bool(self.prs or self.recent or self.tags)

    def text(self, cfg: Config) -> str:
        parts = []
        if self.prs:
            parts.append(describe(self.prs, cfg))
        if self.recent:
            rows = ", ".join(f"#{n} ({day}: {title})" for n, day, title in self.recent)
            parts.append(f"Trail: pull requests merged lately with no trail event: {rows}. {how_to_write(cfg)}")
        if self.tags:
            parts.append(
                f"Trail: tags with no snapshot yet: {', '.join(self.tags)}. Build each with "
                f"`trail skeleton --tag <tag>` in a trail-only pull request (see the trail instructions)."
            )
        return "\n".join(parts)

    def as_json(self) -> dict[str, object]:
        return {
            "prs": self.prs,
            "recent": [{"pr": n, "merged": day, "title": title} for n, day, title in self.recent],
            "tags": self.tags,
        }


def how_to_write(cfg: Config) -> str:
    return (
        f"Write one event per pull request while the work is fresh: {cfg.events}/{'N' * cfg.pr_digits}-slug.md, "
        "in the code PR or a trail-only PR, then run `trail check`."
    )


def describe(prs: list[int], cfg: Config) -> str:
    names = ", ".join(f"#{n}" for n in prs)
    return f"Trail: this session merged {names} with no trail event yet. {how_to_write(cfg)}"


def remind(
    repo: Path,
    cfg: Config,
    prs: set[int] | None = None,
    days: int = 14,
    tags: bool = True,
    fetch: bool = True,
) -> Reminder:
    """The reminder for these pull requests (a session's merges), or for everything merged in
    the last `days` days when none are given; plus the tags with no snapshot."""
    r = Reminder()
    if prs:
        r.prs = unwritten(repo, cfg, prs, fetch)
    else:
        r.recent = recent_unwritten(repo, cfg, days, fetch)
    if tags:
        if fetch:
            git(repo, "fetch", "-q", "--tags", cfg.remote, timeout=15)
        try:
            r.tags = tags_without_snapshot(cfg)
        except TrailError:
            r.tags = []
    return r
