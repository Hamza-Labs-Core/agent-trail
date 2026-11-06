"""The `trail` command: exit codes and output, run in-process and as `python -m claude_trail`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_trail import __version__, config
from claude_trail.cli import main
from conftest import GOOD, g, merge_pr, write

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = SRC.parent


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run([sys.executable, "-m", "claude_trail", *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_init_writes_the_config_and_folders_and_refuses_to_overwrite(tmp_path: Path) -> None:
    assert main(["--root", str(tmp_path), "init"]) == 0
    assert (tmp_path / ".trail.toml").read_text(encoding="utf-8") == config.TEMPLATE
    assert (tmp_path / "docs/trail/events").is_dir() and (tmp_path / "docs/trail/snapshots").is_dir()
    assert main(["--root", str(tmp_path), "init"]) == 1
    assert main(["--root", str(tmp_path), "init", "--force"]) == 0
    assert main(["--root", str(tmp_path), "check"]) == 0


def test_check_exit_codes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo, "docs/trail/events/0700-a.md", GOOD)
    assert main(["--root", str(repo), "check"]) == 0
    assert "trail: ok (1 events)" in capsys.readouterr().out
    write(repo, "docs/trail/events/0701-b.md", GOOD)  # named 701, says 700
    assert main(["--root", str(repo), "check"]) == 1
    assert "named for pull request 701" in capsys.readouterr().err
    write(repo, ".trail.toml", "[nope]\n")
    assert main(["--root", str(repo), "check"]) == 2


def test_is_trail_only(repo: Path) -> None:
    code = merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    events = merge_pr(repo, 701, {"docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    legacy = merge_pr(repo, 702, {"docs/claude-trail.md": "old"})
    r = str(repo)
    assert main(["--root", r, "is-trail-only", code]) == 1
    assert main(["--root", r, "is-trail-only", events]) == 0
    assert main(["--root", r, "is-trail-only", legacy]) == 0  # the legacy path counts
    assert main(["--root", r, "is-trail-only", f"{code}..HEAD"]) == 0
    assert main(["--root", r, "is-trail-only", f"{code}^1..HEAD"]) == 1
    assert main(["--root", r, "is-trail-only", "HEAD..HEAD"]) == 0  # nothing: nothing to tag
    assert main(["--root", r, "is-trail-only", "no-such-rev"]) == 2


def test_is_trail_only_reads_the_configured_paths(repo: Path) -> None:
    write(repo, ".trail.toml", "[trail_only]\npaths = ['docs/trail/**', 'NOTES.md']\n")
    g(repo, "commit", "-qam", "config")
    sha = merge_pr(repo, 700, {"NOTES.md": "n"})
    assert main(["--root", str(repo), "is-trail-only", sha]) == 0
    sha = merge_pr(repo, 701, {"docs/claude-trail.md": "x"})  # no longer listed
    assert main(["--root", str(repo), "is-trail-only", sha]) == 1


def test_missing_with_pr(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    merge_pr(repo, 700, {"a.txt": "a"}, "Feature A")
    merge_pr(repo, 701, {"b.txt": "b", "docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    merge_pr(repo, 650, {"c.txt": "c"})  # missing_after = 653
    r = str(repo)
    assert main(["--root", r, "missing"]) == 0
    assert "| #700 |" in capsys.readouterr().out
    assert main(["--root", r, "missing", "--pr", "700"]) == 1
    assert main(["--root", r, "missing", "--pr", "701"]) == 0
    assert main(["--root", r, "missing", "--pr", "650"]) == 0


def test_unsnapshotted(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for n, tag in [(700, "v0.4.0"), (701, "v0.4.1"), (702, "v0.4.2")]:
        merge_pr(repo, n, {f"{n}.txt": "x"})
        g(repo, "tag", tag)
    g(repo, "tag", "nightly")
    assert main(["--root", str(repo), "unsnapshotted"]) == 0
    assert capsys.readouterr().out.split() == ["v0.4.2"]  # before the first snapshot: the newest
    write(repo, "docs/trail/snapshots/v0.4.0.md", "x")
    assert main(["--root", str(repo), "unsnapshotted"]) == 0
    assert capsys.readouterr().out.split() == ["v0.4.1", "v0.4.2"]


def test_the_module_runs_from_a_subfolder(repo: Path) -> None:
    write(repo, "docs/trail/events/0700-a.md", GOOD)
    (repo / "deep" / "er").mkdir(parents=True)
    r = run("check", cwd=repo / "deep" / "er")
    assert r.returncode == 0 and "1 events" in r.stdout, r.stderr


def test_the_hook_command_reads_stdin_and_exits_0(repo: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(repo)}
    r = subprocess.run(
        [sys.executable, "-m", "claude_trail", "hook"],
        input=json.dumps({"hook_event_name": "Stop", "transcript_path": "/nonexistent"}),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and r.stdout == ""


def test_the_bin_wrapper_runs(repo: Path) -> None:
    r = subprocess.run([str(ROOT / "bin" / "trail"), "--root", str(repo), "check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
