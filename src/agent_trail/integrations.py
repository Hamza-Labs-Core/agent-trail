"""`trail init --agent <name>`: the files that make a coding agent follow the trail.

    claude    .claude/settings.json: the agent-trail marketplace and the `trail` plugin (its
              skill, commands and hooks)
    codex     AGENTS.md: the trail block; .codex/hooks.json: PostToolUse, Stop, SessionStart
              and SessionEnd run `trail hook --agent codex`
    cursor    .cursor/rules/trail.mdc: the instructions as an always-applied rule;
              .cursor/hooks.json: afterShellExecution, stop, sessionStart and sessionEnd run
              `trail hook --agent cursor`
    agents    AGENTS.md: the trail block, for any agent that reads AGENTS.md

Every writer merges into what is there: JSON files keep their other keys and hooks, and the
AGENTS.md block is replaced between its markers (or appended), never duplicated. The Codex
and Cursor hooks need `trail` on the PATH (`uv tool install agent-trail`, or from git); a
missing `trail` makes them do nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config

AGENTS = ("claude", "codex", "cursor", "agents")
REPO = "Hamza-Labs-Core/agent-trail"
MARKETPLACE = "agent-trail"
PLUGIN = "trail"
BEGIN = "<!-- agent-trail:begin (written by `trail init --agent`; edit outside the markers) -->"
END = "<!-- agent-trail:end -->"


def instructions(cfg: Config) -> str:
    """The trail, for an agent: the same rules the Claude Code plugin's skill gives."""
    n = "N" * cfg.pr_digits
    legacy = (
        f"\n`{cfg.legacy}` is a frozen trail from before the events: search it with `grep -n` when a topic "
        "needs history; don't read it whole.\n"
        if cfg.legacy
        else ""
    )
    return f"""## The agent trail

This repository keeps what coding-agent sessions learned in its trail, so the next session
starts from it (https://github.com/{REPO}). The settings are in `.trail.toml`.

**At the start of a session**, read `{cfg.current}` (the newest snapshot of the lessons), then
the events added since its `up_to_commit`:

```bash
git fetch -q {cfg.remote} {cfg.main_branch}
git diff --name-only --diff-filter=A "$(sed -n 's/^up_to_commit: //p' {cfg.current})" {cfg.main_ref} -- {cfg.events}
```

Before the first snapshot exists, `trail index` lists every event; read the ones that touch
your task.
{legacy}
**For each pull request you merge, write its event** in that pull request, before it merges:
`{cfg.events}/{n}-slug.md`, named for the PR's number (zero-padded). Write what only this
session knows, not what the diff shows:

```markdown
---
schema: 1
kind: pr                # pr | incident | decision | correction | release | migration
pr: 634
title: One line on what changed
date: 2026-11-04
session: <this session's id>
agent: <the agent: claude-code, codex, cursor, ...>
---

## What happened
The change, the root cause, the numbers (required).

## What we learned
The gotcha, the wrong first guess and why, what a tool or the code really does.

## Keep doing
## Stop doing
## Open
What is left, with its issue number.
```

Leave out a section with nothing in it. Something learned after the merge goes in a second
event (`{n}-after-merge.md`) in a trail-only pull request; work with no pull request is named
`{cfg.issue_prefix}{n}-slug.md` (an issue) or `{cfg.day_prefix}YYYYMMDD-slug.md` (a day). Events are
append-only: a later fix is a `kind: correction` event with `corrects:`. Use real PR, issue and
sha numbers only; never write secrets; state decisions neutrally, without quoting anyone.

**Check before you push:** `trail check`. **Before you finish:** `trail remind` lists merged
pull requests with no event and tags with no snapshot.

**After a `{cfg.tag_prefix}X.Y.Z` tag**, a session builds its snapshot:
`trail skeleton --tag {cfg.tag_prefix}X.Y.Z > {cfg.snapshots}/{cfg.tag_prefix}X.Y.Z.md`. Fold each new
event the skeleton lists into Keep doing, Stop doing, What we learned and Open, citing it
(`#634`, `{cfg.issue_prefix}649`, `{cfg.day_prefix}20261104-slug`; `issue #649` names an issue without
citing it); leave `Missing events` and `Index` as printed; copy it to `{cfg.current}`; run
`trail check`; merge it as a trail-only pull request.

`trail` is the agent-trail CLI: `uv tool install git+https://github.com/{REPO}` (or
`uvx --from git+https://github.com/{REPO} trail ...`).
"""


# ---- writers --------------------------------------------------------------------------


def _read_json(p: Path) -> dict[str, Any]:
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{p}: not a JSON object")
    return data


def _write_json(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_block(path: Path, body: str) -> None:
    """Put body between the agent-trail markers in path: replaced if they are there, else appended."""
    block = f"{BEGIN}\n\n{body.strip()}\n\n{END}\n"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if BEGIN in text and END in text:
        head, _, rest = text.partition(BEGIN)
        _, _, tail = rest.partition(END)
        text = head + block.rstrip("\n") + tail
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _add_hook(groups: list[Any], entry: dict[str, Any], marker: str) -> list[Any]:
    """groups with entry, replacing any earlier agent-trail entry (its command holds marker)."""
    kept = [g for g in groups if marker not in json.dumps(g)]
    return [*kept, entry]


def claude(cfg: Config) -> list[str]:
    p = cfg.root / ".claude" / "settings.json"
    data = _read_json(p)
    data.setdefault("extraKnownMarketplaces", {})[MARKETPLACE] = {"source": {"source": "github", "repo": REPO, "ref": "v0"}}
    data.setdefault("enabledPlugins", {})[f"{PLUGIN}@{MARKETPLACE}"] = True
    _write_json(p, data)
    return [".claude/settings.json"]


CODEX_CMD = "trail hook --agent codex 2>/dev/null || true"
CURSOR_CMD = "trail hook --agent cursor 2>/dev/null || echo '{}'"


def codex(cfg: Config) -> list[str]:
    write_block(cfg.root / "AGENTS.md", instructions(cfg))
    p = cfg.root / ".codex" / "hooks.json"
    data = _read_json(p)
    hooks = data.setdefault("hooks", {})
    timeouts = {"PostToolUse": 10, "Stop": 30, "SessionStart": 30, "SessionEnd": 3}
    for event, timeout in timeouts.items():
        entry = {"hooks": [{"type": "command", "command": CODEX_CMD, "timeout": timeout, "statusMessage": "Checking the trail"}]}
        hooks[event] = _add_hook(hooks.get(event, []), entry, "trail hook --agent codex")
    _write_json(p, data)
    return ["AGENTS.md", ".codex/hooks.json"]


def cursor(cfg: Config) -> list[str]:
    rule = cfg.root / ".cursor" / "rules" / "trail.mdc"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(
        "---\n"
        "description: The agent trail - read the lessons at the start, write an event for each merged pull request, "
        "build snapshots after tags\n"
        "alwaysApply: true\n"
        "---\n\n" + instructions(cfg),
        encoding="utf-8",
    )
    p = cfg.root / ".cursor" / "hooks.json"
    data = _read_json(p)
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    timeouts = {"afterShellExecution": 10, "stop": 30, "sessionStart": 30, "sessionEnd": 10}
    for event, timeout in timeouts.items():
        hooks[event] = _add_hook(hooks.get(event, []), {"command": CURSOR_CMD, "timeout": timeout}, "trail hook --agent cursor")
    _write_json(p, data)
    return [".cursor/rules/trail.mdc", ".cursor/hooks.json"]


def agents(cfg: Config) -> list[str]:
    write_block(cfg.root / "AGENTS.md", instructions(cfg))
    return ["AGENTS.md"]


WRITERS = {"claude": claude, "codex": codex, "cursor": cursor, "agents": agents}


def init_agent(cfg: Config, agent: str) -> list[str]:
    return WRITERS[agent](cfg)
