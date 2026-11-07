"""The checker (events, snapshots, Missing) and the session hook: every test from Maktaba's
tools/ci/tests/test_trail.py, run with Maktaba's settings (conftest.MAKTABA_TOML)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_trail import core as trail
from agent_trail import hook, remind
from agent_trail.config import Config
from conftest import GOOD, g, merge_pr, write

# ---- events ---------------------------------------------------------------------------


def test_a_good_event_checks(mcfg: Config) -> None:
    ev, errs = trail.event_from("docs/trail/events/0700-a-change.md", GOOD, mcfg)
    assert errs == []
    assert ev is not None and ev.pr == 700 and "#700" in ev.cites


@pytest.mark.parametrize(
    ("name", "text", "needle"),
    [
        ("0701-a-change.md", GOOD, "named for pull request 701"),
        ("700-a-change.md", GOOD, "an event is named"),
        ("0700-A_Change.md", GOOD, "an event is named"),
        ("0700-a.md", GOOD.replace("kind: pr", "kind: chat"), "kind must be one of"),
        ("0700-a.md", GOOD.replace("date: 2026-11-06", "date: 6 Nov"), "date must be"),
        ("0700-a.md", GOOD.replace("session: local_abc\n", ""), "lacks ['session']"),
        ("0700-a.md", GOOD.replace("## What happened\nBuilt", "## What happened\n\n## Notes\nBuilt"), "unknown sections"),
        ("0700-a.md", GOOD + "\nThe token is ghp_" + "a" * 36 + "\n", "a GitHub token"),
        ("0700-a.md", GOOD + "\npassword: hunter2hunter2\n", "a credential value"),
        ("0700-a.md", GOOD + "\nDone this way (owner, 2026-11-05).\n", "owner"),
        ("0700-a.md", GOOD.replace("kind: pr", "kind: correction"), "corrects"),
        (
            "d20261106-x.md",
            GOOD.replace("pr: 700\n", "").replace("kind: pr", "kind: decision").replace("2026-11-06", "2026-11-07"),
            "named for 20261106",
        ),
    ],
)
def test_bad_events_are_refused_with_a_reason(name: str, text: str, needle: str, mcfg: Config) -> None:
    _, errs = trail.event_from(f"docs/trail/events/{name}", text, mcfg)
    assert any(needle in e for e in errs), errs


def test_what_happened_is_required(mcfg: Config) -> None:
    text = GOOD.split("## What happened")[0] + "## What we learned\n- x\n"
    _, errs = trail.event_from("docs/trail/events/0700-a.md", text, mcfg)
    assert any("What happened" in e for e in errs)


def test_sections_must_keep_their_order(mcfg: Config) -> None:
    text = GOOD.replace("## What happened", "## Keep doing\n- x\n\n## What happened")
    _, errs = trail.event_from("docs/trail/events/0700-a.md", text, mcfg)
    assert any("order" in e for e in errs)


def test_issue_and_day_events_cite_by_their_names(mcfg: Config) -> None:
    issue = GOOD.replace("kind: pr\npr: 700", "kind: incident\nissue: 649")
    ev, errs = trail.event_from("docs/trail/events/i0649-shards.md", issue, mcfg)
    assert errs == [] and ev is not None and "i649" in ev.cites
    day = GOOD.replace("kind: pr\npr: 700\n", "kind: decision\nprs: [608, 635]\n")
    ev, errs = trail.event_from("docs/trail/events/d20261106-split.md", day, mcfg)
    assert errs == [] and ev is not None and {"d20261106-split", "#608", "#635"} <= ev.cites


def test_a_correction_must_point_at_an_event(repo: Path, cfg: Config) -> None:
    write(repo, "docs/trail/events/0700-a.md", GOOD)
    fix = GOOD.replace("kind: pr", "kind: correction\ncorrects: 0699-gone").replace("pr: 700", "pr: 701")
    write(repo, "docs/trail/events/0701-fix.md", fix)
    _, errs = trail.load_events(None, cfg)
    assert any("corrects 0699-gone" in e for e in errs)


# ---- missing and snapshots ------------------------------------------------------------


def test_missing_lists_merged_prs_with_no_event_and_skips_trail_only_ones(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a"}, "Feature A")
    merge_pr(repo, 701, {"b.txt": "b", "docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    merge_pr(repo, 702, {"docs/trail/events/0700-a.md": GOOD})  # a trail-only PR: never Missing
    merge_pr(repo, 600, {"c.txt": "c"})  # before trail v2: covered by the frozen trail
    events, errs = trail.load_events("HEAD", cfg)
    assert errs == []
    assert trail.missing("HEAD", events, cfg) == []  # 700 got its event in 702
    merge_pr(repo, 703, {"d.txt": "d"}, "Feature D")
    events, _ = trail.load_events("HEAD", cfg)
    rows = trail.missing("HEAD", events, cfg)
    assert [r[0] for r in rows] == [703] and rows[0][2] == "Feature D"


def snapshot(cfg: Config, tag: str, body: dict[str, str], built: str = "full") -> str:
    sha = g(cfg.root, "rev-parse", f"{tag}^{{commit}}")
    events, _ = trail.load_events(sha, cfg)
    sections = {
        "Keep doing": body.get("Keep doing", ""),
        "Stop doing": body.get("Stop doing", ""),
        "What we learned": body.get("What we learned", ""),
        "Open": body.get("Open", ""),
        "Missing events": trail.missing_table(trail.missing(sha, events, cfg)),
        "Index": trail.index_table(events),
    }
    text = f"---\nschema: 1\ntag: {tag}\nup_to_commit: {sha[:9]}\nbuilt_from: {built}\n---\n\n# Trail snapshot: {tag}\n\n"
    text += "".join(f"## {k}\n\n{v}\n\n" for k, v in sections.items())
    return text


def test_a_snapshot_checks_when_its_citations_resolve(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    text = snapshot(cfg, "v0.4.0", {"What we learned": "- The cache is keyed by path. (#700)"})
    write(repo, "docs/trail/snapshots/v0.4.0.md", text)
    write(repo, "docs/trail/current.md", text)
    assert trail.check(cfg) == []


def test_a_snapshot_refuses_uncited_or_unknown_citations_and_hand_edited_tables(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    text = snapshot(cfg, "v0.4.0", {"Keep doing": "- Something with no source.\n- Another. (#999)"})
    text = text.replace("| 0700-a |", "| 0700-a-edited |")
    errs = trail.check_snapshot("docs/trail/snapshots/v0.4.0.md", text, cfg)
    assert any("cites no event" in e for e in errs)
    assert any("cites #999" in e for e in errs)
    assert any("`## Index` is not" in e for e in errs)


def test_a_snapshot_must_sit_on_its_tags_commit_and_current_must_copy_the_newest(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    merge_pr(repo, 701, {"b.txt": "b", "docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    g(repo, "tag", "v0.4.1")
    old = snapshot(cfg, "v0.4.0", {})
    new = snapshot(cfg, "v0.4.1", {}, built="v0.4.0")
    write(repo, "docs/trail/snapshots/v0.4.0.md", old)
    write(
        repo,
        "docs/trail/snapshots/v0.4.1.md",
        new.replace(g(repo, "rev-parse", "v0.4.1")[:9], g(repo, "rev-parse", "v0.4.0")[:9]),
    )
    write(repo, "docs/trail/current.md", old)
    errs = trail.check(cfg)
    assert any("is not v0.4.1's commit" in e for e in errs)
    assert any("not a copy of the newest snapshot" in e for e in errs)


def test_the_skeleton_carries_the_last_snapshot_forward_and_lists_new_events(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    first = snapshot(cfg, "v0.4.0", {"Keep doing": "- Key caches by id. (#700)"})
    write(repo, "docs/trail/snapshots/v0.4.0.md", first)
    write(repo, "docs/trail/current.md", first)
    merge_pr(repo, 701, {"b.txt": "b", "docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    g(repo, "tag", "v0.4.1")
    sk = trail.skeleton("v0.4.1", cfg)
    assert "built_from: v0.4.0" in sk
    assert "- Key caches by id. (#700)" in sk
    assert "new: docs/trail/events/0701-b.md" in sk and "new: docs/trail/events/0700-a.md" not in sk


# ---- the hook -------------------------------------------------------------------------


def transcript(path: Path, commands: list[str]) -> Path:
    lines = []
    for c in commands:
        lines.append(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def hook_repo(repo: Path) -> None:
    # the hook reads origin/main: point it at the repo itself
    g(repo, "remote", "add", "origin", str(repo))
    g(repo, "fetch", "-q", "origin")


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(remind, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


def test_the_hook_blocks_a_stop_once_for_a_merged_pr_with_no_event(repo: Path, tmp_path: Path, state: Path) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    hook_repo(repo)
    t = transcript(tmp_path / "s.jsonl", ["gh pr merge 700 --merge", "gh pr merge 12 --merge", "echo gh pr merge 701"])
    out = hook.handle({"hook_event_name": "Stop", "transcript_path": str(t), "session_id": "s"}, repo)
    assert out is not None and out["decision"] == "block" and "#700" in str(out["reason"])
    assert "#12" not in str(out["reason"])  # before trail v2
    again = hook.handle({"hook_event_name": "Stop", "transcript_path": str(t), "stop_hook_active": True}, repo)
    assert again is None


def test_the_hook_is_quiet_once_an_event_exists_on_any_branch(repo: Path, tmp_path: Path, state: Path) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    hook_repo(repo)
    g(repo, "checkout", "-q", "-b", "trail-700")
    write(repo, "docs/trail/events/0700-a.md", GOOD)
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "event")
    g(repo, "checkout", "-q", "main")
    t = transcript(tmp_path / "s.jsonl", ["gh pr merge 700 --merge"])
    assert hook.handle({"hook_event_name": "Stop", "transcript_path": str(t)}, repo) is None


def test_the_hook_reads_subagent_transcripts_and_logs_at_session_end(repo: Path, tmp_path: Path, state: Path) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    hook_repo(repo)
    t = transcript(tmp_path / "s.jsonl", [])
    (tmp_path / "s" / "subagents").mkdir(parents=True)
    transcript(tmp_path / "s" / "subagents" / "agent-1.jsonl", ["gh pr merge 700 --merge"])
    end = hook.handle({"hook_event_name": "SessionEnd", "transcript_path": str(t), "session_id": "s"}, repo)
    assert end is not None and "#700" in str(end["systemMessage"])
    start = hook.handle({"hook_event_name": "SessionStart", "source": "startup", "transcript_path": "x"}, repo)
    assert start is not None and "#700" in str(start["hookSpecificOutput"])


def test_the_hook_never_fails_the_session(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


# ---- review fixes (#674): secret shapes, snapshot edges, the hook's coverage and fetch ---

SECRETS = [
    "POSTGRES_PASSWORD=hunter2hunter2",
    'export MAKTABA_PG_PASSWORD="s3cr3tpass"',
    '{"password": "hunter2hunter2"}',
    "client_secret: 9f8e7d6c5b4a3f2e",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
    "token=0a1b2c3d4e5f6a7b",
    "ASIAQWERTYUIOPASDFGH",
    "https://user:pass@example.com/x",
    "redis://:pw@cache:6379/0",
    "password: hunter2hunter2",
    "xoxb-1234567890-abcdefghij",
    "sk-proj-" + "a1B2" * 10,
]

PROSE = [
    "password: regenerated",
    "The password: rotated, then the token: refreshed.",
    "secret: `MAKTABA_PLEX_TOKEN` in the keychain",
    "MAKTABA_PG_PASSWORD=$MAKTABA_PG_PASSWORD from the env",
    "token=<the token> in the URL",
    "https://example.com/x and postgres://localhost/db",
    "the sk-learn style task-runner",
]


@pytest.mark.parametrize("line", SECRETS)
def test_common_secret_shapes_are_refused(line: str) -> None:
    assert trail.scan_text("x.md", line), line


@pytest.mark.parametrize("line", PROSE)
def test_prose_about_secrets_is_not_a_secret(line: str) -> None:
    assert trail.scan_text("x.md", line) == [], line


def test_a_stray_file_in_snapshots_is_refused_cleanly(repo: Path, cfg: Config) -> None:
    write(repo, "docs/trail/snapshots/README.md", "# notes\n")
    errs = trail.check(cfg)
    assert any("snapshots/README.md" in e and "vX.Y.Z.md" in e for e in errs), errs


def test_a_snapshot_may_name_an_issue_without_citing_it(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    text = snapshot(cfg, "v0.4.0", {"Open": "- The cache key (issue #4242). (#700)\n- Only an issue: issue #4243."})
    errs = trail.check_snapshot("docs/trail/snapshots/v0.4.0.md", text, cfg)
    assert not any("cites #4242" in e or "cites #4243" in e for e in errs), errs
    assert any("cites no event" in e and "#4243" in e for e in errs), errs


def test_the_skeleton_for_an_older_tag_builds_on_the_snapshot_before_it(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    merge_pr(repo, 701, {"b.txt": "b", "docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    g(repo, "tag", "v0.4.1")
    merge_pr(repo, 702, {"c.txt": "c", "docs/trail/events/0702-c.md": GOOD.replace("700", "702")})
    g(repo, "tag", "v0.4.2")
    write(repo, "docs/trail/snapshots/v0.4.0.md", snapshot(cfg, "v0.4.0", {}))
    write(repo, "docs/trail/snapshots/v0.4.2.md", snapshot(cfg, "v0.4.2", {}, built="v0.4.0"))
    sk = trail.skeleton("v0.4.1", cfg)
    assert "built_from: v0.4.0" in sk
    assert "new: docs/trail/events/0701-b.md" in sk and "0702-c" not in sk.split("-->")[0]


def test_the_hook_reads_prs_lists_from_events_only_in_git_history(repo: Path, tmp_path: Path, state: Path) -> None:
    merge_pr(repo, 700, {"a.txt": "a"})
    hook_repo(repo)
    g(repo, "checkout", "-q", "-b", "trail-701")
    write(repo, "docs/trail/events/0701-b.md", GOOD.replace("pr: 700", "pr: 701\nprs: [700]"))
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "event")
    g(repo, "checkout", "-q", "main")  # the event is on a branch, not checked out
    from agent_trail import config

    assert 700 in remind.covered(repo, config.load(repo))
    t = transcript(tmp_path / "s.jsonl", ["gh pr merge 700 --merge"])
    assert hook.handle({"hook_event_name": "Stop", "transcript_path": str(t)}, repo) is None


def test_a_hung_fetch_still_stamps_so_the_next_stop_skips_it(
    repo: Path, cfg: Config, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    real = subprocess.run

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if "fetch" in cmd:
            calls.append(cmd)
            raise subprocess.TimeoutExpired(cmd, 15)
        return real(cmd, **kw)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(remind.subprocess, "run", run)
    remind.fetch_main(repo, cfg)  # does not raise
    remind.fetch_main(repo, cfg)  # stamped: no second fetch
    assert len(calls) == 1
    assert list(state.glob("last-fetch-*"))
