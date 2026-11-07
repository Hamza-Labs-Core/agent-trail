"""Every agent's integration: `trail remind` (the one question they all ask), the Codex and
Cursor hook adapters, `trail init --agent`, and the agent-neutral `agent:` field."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_trail import config, core, hook, integrations, remind
from agent_trail.cli import main
from conftest import GOOD, g, merge_pr, write

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(remind, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


def origin(repo: Path) -> None:
    g(repo, "remote", "add", "origin", str(repo))
    g(repo, "fetch", "-q", "origin")


# ---- the event field ------------------------------------------------------------------


def test_agent_is_an_optional_free_text_field(mcfg: config.Config) -> None:
    for agent in ("claude-code", "codex", "Cursor 1.7", "my own agent"):
        _, errs = core.event_from(
            "docs/trail/events/0700-a.md", GOOD.replace("session: local_abc", f"session: s\nagent: {agent}"), mcfg
        )
        assert errs == [], (agent, errs)
    for bad in ("", "[a, b]", "42"):
        _, errs = core.event_from(
            "docs/trail/events/0700-a.md", GOOD.replace("session: local_abc", f"session: s\nagent: {bad}"), mcfg
        )
        assert any("agent names the agent" in e for e in errs), bad


# ---- remind ---------------------------------------------------------------------------


def test_merge_numbers() -> None:
    assert remind.merge_numbers("gh pr merge 702 --merge") == {702}
    assert remind.merge_numbers("gh pr merge --merge --delete-branch 703 && gh pr merge 704") == {703, 704}
    assert remind.merge_numbers("gh pr view 705") == set()
    assert remind.merge_numbers("gh pr merge --repo org/x 706") == {706}


def test_remind_for_given_prs_and_lately_merged_ones(repo: Path, cfg: config.Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    merge_pr(repo, 701, {"b.txt": "b", "docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    merge_pr(repo, 702, {"docs/trail/events/0699-x.md": GOOD.replace("700", "699")})  # trail-only
    origin(repo)
    r = remind.remind(repo, cfg, {700, 701, 702, 12}, tags=False, fetch=False)
    assert r.prs == [700] and "#700" in r.text(cfg)
    r = remind.remind(repo, cfg, None, days=30, tags=False, fetch=False)
    assert [n for n, _, _ in r.recent] == [700]


def test_remind_lists_tags_with_no_snapshot(repo: Path, cfg: config.Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    origin(repo)
    r = remind.remind(repo, cfg, None, days=30, fetch=False)
    assert r.tags == ["v0.4.0"] and "v0.4.0" in r.text(cfg)


def test_the_remind_command(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    origin(repo)
    r = str(repo)
    assert main(["--root", r, "remind", "--no-fetch", "--no-tags", "--pr", "700"]) == 0
    assert "#700" in capsys.readouterr().out
    assert main(["--root", r, "remind", "--no-fetch", "--no-tags", "--pr", "700", "--strict"]) == 1
    capsys.readouterr()
    assert main(["--root", r, "remind", "--no-fetch", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [x["pr"] for x in out["recent"]] == [700] and out["tags"] == []
    write(repo, "docs/trail/events/0700-a.md", GOOD)  # written in the working tree (a worktree)
    assert main(["--root", r, "remind", "--no-fetch", "--strict"]) == 0
    assert "nothing missing" in capsys.readouterr().out


def test_remind_without_a_config_is_an_error(tmp_path: Path) -> None:
    g(tmp_path, "init", "-q")
    assert main(["--root", str(tmp_path), "remind"]) == 2


# ---- the Codex adapter ----------------------------------------------------------------


def codex(repo: Path, event: str, **kw: object) -> dict[str, object] | None:
    return hook.handle({"hook_event_name": event, "session_id": "cx1", "cwd": str(repo), **kw}, repo, agent="codex")


def test_codex_records_the_merge_and_blocks_its_stop_once(repo: Path) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    origin(repo)
    assert codex(repo, "Stop") is None  # nothing merged yet
    assert codex(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "gh pr merge 700 --merge"}) is None
    out = codex(repo, "Stop", stop_hook_active=False)
    assert out is not None and out["decision"] == "block" and "#700" in str(out["reason"])
    assert codex(repo, "Stop", stop_hook_active=True) is None
    resumed = codex(repo, "SessionStart", source="resume")
    assert resumed is not None and "#700" in str(resumed["hookSpecificOutput"])
    assert codex(repo, "SessionEnd") is None
    start = hook.handle({"hook_event_name": "SessionStart", "source": "startup", "session_id": "cx2"}, repo, agent="codex")
    assert start is not None and "#700" in str(start) and "cx1" in str(start)


def test_codex_is_quiet_once_the_event_exists(repo: Path) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    origin(repo)
    codex(repo, "PostToolUse", tool_input={"command": "gh pr merge 700"})
    write(repo, "docs/trail/events/0700-a.md", GOOD)
    assert codex(repo, "Stop") is None


# ---- the Cursor adapter ---------------------------------------------------------------


def cursor(repo: Path, event: str, **kw: object) -> dict[str, object] | None:
    data = {"hook_event_name": event, "conversation_id": "cu1", "workspace_roots": [str(repo)], **kw}
    return hook.handle(data, repo, agent="cursor")


def test_cursor_records_the_merge_and_follows_up_once(repo: Path) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    origin(repo)
    assert cursor(repo, "stop", status="completed", loop_count=0) == {}
    assert cursor(repo, "afterShellExecution", command="gh pr merge 700 --merge", output="", duration=5) == {}
    out = cursor(repo, "stop", status="completed", loop_count=0)
    assert out is not None and "#700" in str(out["followup_message"])
    assert cursor(repo, "stop", status="completed", loop_count=1) == {}
    assert cursor(repo, "sessionEnd") == {}
    start = cursor(repo, "sessionStart", session_id="x")
    assert start is not None and "#700" in str(start["additional_context"])


def test_cursor_hook_always_prints_json(repo: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    for stdin in ("not json", json.dumps({"hook_event_name": "stop", "workspace_roots": [str(repo)], "loop_count": 0})):
        r = subprocess.run(
            [sys.executable, "-m", "agent_trail", "hook", "--agent", "cursor"],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0 and json.loads(r.stdout) == {}


def test_hooks_do_nothing_outside_a_trail_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    g(plain, "init", "-q")
    assert hook.handle({"hook_event_name": "Stop"}, plain, agent="codex") is None
    assert hook.handle({"hook_event_name": "stop"}, plain, agent="cursor") is None


# ---- trail init --agent -----------------------------------------------------------------


def test_init_agent_claude_merges_into_settings(repo: Path) -> None:
    write(repo, ".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    assert main(["--root", str(repo), "init", "--agent", "claude"]) == 0
    data = json.loads((repo / ".claude/settings.json").read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["extraKnownMarketplaces"]["agent-trail"]["source"] == {
        "source": "github",
        "repo": "Hamza-Labs-Core/agent-trail",
        "ref": "v0",
    }
    assert data["enabledPlugins"]["trail@agent-trail"] is True


def test_init_agent_codex_writes_agents_md_and_hooks_and_is_idempotent(repo: Path) -> None:
    write(repo, "AGENTS.md", "# Notes\n\nOur own rules.\n")
    write(repo, ".codex/hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
    for _ in range(2):
        assert main(["--root", str(repo), "init", "--agent", "codex"]) == 0
    text = (repo / "AGENTS.md").read_text()
    assert text.startswith("# Notes\n\nOur own rules.\n") and text.count(integrations.BEGIN) == 1
    assert "docs/trail/events/NNNN-slug.md" in text and "docs/claude-trail.md" in text  # the repo's own settings
    hooks = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"PostToolUse", "Stop", "SessionStart", "SessionEnd"}
    stop = json.dumps(hooks["Stop"])
    assert '"other"' in stop and stop.count("trail hook --agent codex") == 1
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] <= 3  # Codex's limit for SessionEnd


def test_init_agent_cursor_writes_the_rule_and_hooks(repo: Path) -> None:
    assert main(["--root", str(repo), "init", "--agent", "cursor", "--agent", "agents"]) == 0
    rule = (repo / ".cursor/rules/trail.mdc").read_text()
    assert rule.startswith("---\ndescription: ") and "\nalwaysApply: true\n---\n" in rule
    data = json.loads((repo / ".cursor/hooks.json").read_text())
    assert data["version"] == 1
    assert set(data["hooks"]) == {"afterShellExecution", "stop", "sessionStart", "sessionEnd"}
    assert (repo / "AGENTS.md").read_text().count(integrations.BEGIN) == 1


def test_init_agent_in_a_new_repository_writes_the_config_too(tmp_path: Path) -> None:
    assert main(["--root", str(tmp_path), "init", "--agent", "agents"]) == 0
    assert (tmp_path / ".trail.toml").is_file() and (tmp_path / "AGENTS.md").is_file()


def test_the_block_is_replaced_between_its_markers(tmp_path: Path) -> None:
    p = tmp_path / "AGENTS.md"
    integrations.write_block(p, "one")
    p.write_text(p.read_text() + "\nafter\n")
    integrations.write_block(p, "two")
    text = p.read_text()
    assert "one" not in text and "two" in text and text.endswith("after\n")
