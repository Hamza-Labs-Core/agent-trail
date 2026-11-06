"""The Claude Code plugin and the Action: their files agree with each other and with the docs'
formats, and the hook stays safe (repositories with no trail, other repositories' logs, a
missing python3)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from claude_trail import __version__, hook
from conftest import g, merge_pr

ROOT = Path(__file__).resolve().parents[1]


def load(rel: str) -> dict:  # type: ignore[type-arg]
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_the_manifest_and_the_marketplace_agree() -> None:
    plugin = load(".claude-plugin/plugin.json")
    market = load(".claude-plugin/marketplace.json")
    assert plugin["name"] == "trail"
    # claude-, anthropic- and similar prefixes are reserved for Anthropic's own plugins
    assert not re.match(r"(claude|anthropic|cc-plugin)-", plugin["name"])
    assert plugin["version"] == __version__
    assert market["name"] == "claude-trail" and market["owner"]["name"]
    [entry] = market["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"] == "./"
    assert "version" not in entry  # plugin.json's version is the one


def test_the_python_package_version_matches() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in text


def test_the_hooks_file_wires_the_four_events_to_a_script_that_exists() -> None:
    hooks = load("hooks/hooks.json")["hooks"]
    assert set(hooks) == {"Stop", "PreCompact", "SessionStart", "SessionEnd"}
    for groups in hooks.values():
        [group] = groups
        [h] = group["hooks"]
        assert h["type"] == "command" and h["timeout"] == 30
        assert h["command"] == '"${CLAUDE_PLUGIN_ROOT}"/hooks/run-hook.sh'
    assert os.access(ROOT / "hooks" / "run-hook.sh", os.X_OK)
    assert os.access(ROOT / "bin" / "trail", os.X_OK)


@pytest.mark.parametrize("name", ["event", "snapshot", "read"])
def test_each_command_has_a_description(name: str) -> None:
    text = (ROOT / "commands" / f"{name}.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and re.search(r"^description: .+", text, re.M)


def test_the_skill_has_its_front_matter() -> None:
    text = (ROOT / "skills" / "trail" / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"^name: trail$", text, re.M) and re.search(r"^description: .+", text, re.M)


def test_the_action_runs_the_checker_from_its_own_copy() -> None:
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    assert "using: composite" in text
    assert "${{ github.action_path }}/src" in text
    for name in ("command", "comment-missing", "working-directory", "fetch-tags", "github-token"):
        assert re.search(rf"^  {name}:$", text, re.M), name


# ---- the hook script and the hook's safety -------------------------------------------


def test_the_hook_script_exits_0_without_python(tmp_path: Path) -> None:
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}  # no python3 on this PATH
    r = subprocess.run(["/bin/sh", str(ROOT / "hooks" / "run-hook.sh")], input="{}", env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


def test_the_hook_script_exits_0_on_garbage(tmp_path: Path) -> None:
    r = subprocess.run([str(ROOT / "hooks" / "run-hook.sh")], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


def test_the_hook_is_quiet_in_a_repository_with_no_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "state")
    plain = tmp_path / "plain"
    plain.mkdir()
    g(plain, "init", "-q", "-b", "main")
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"message": {"content": [{"type": "tool_use", "input": {"command": "gh pr merge 9000"}}]}}) + "\n")
    assert hook.handle({"hook_event_name": "Stop", "transcript_path": str(t)}, plain) is None
    assert not (tmp_path / "state").exists()  # not even a fetch


def test_the_hook_finds_the_trail_from_a_subfolder(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "state")
    merge_pr(repo, 700, {"a.txt": "a"})
    g(repo, "remote", "add", "origin", str(repo))
    g(repo, "fetch", "-q", "origin")
    (repo / "sub").mkdir()
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"message": {"content": [{"type": "tool_use", "input": {"command": "gh pr merge 700"}}]}}) + "\n")
    out = hook.handle({"hook_event_name": "Stop", "transcript_path": str(t)}, repo / "sub")
    assert out is not None and "#700" in str(out["reason"])


def test_the_startup_reminder_lists_only_this_repositorys_prs(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    other = {"repo": "/somewhere/else/.git", "session": "x", "prs": [9001]}
    legacy = {"session": "y", "prs": [9002]}  # a record from before records named their repository
    mine = {"repo": hook.repo_key(repo), "session": "z", "prs": [9003]}
    hook.log_path().write_text("\n".join(json.dumps(r) for r in (other, legacy, mine)) + "\n", encoding="utf-8")
    out = hook.handle({"hook_event_name": "SessionStart", "source": "startup"}, repo)
    text = str(out)
    assert "#9003" in text and "#9002" in text and "#9001" not in text


def test_the_state_dir_follows_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_TRAIL_HOOK_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert hook.state_dir() == tmp_path / "data"
    monkeypatch.setenv("CLAUDE_TRAIL_HOOK_DIR", str(tmp_path / "mine"))
    assert hook.state_dir() == tmp_path / "mine"


def test_the_workflows_and_the_action_are_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    for rel in [".github/workflows/ci.yml", ".github/workflows/release.yml", "action.yml"]:
        assert isinstance(yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")), dict), rel
