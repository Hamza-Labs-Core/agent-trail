"""The batched git reads behind the snapshot checks: one `git cat-file --batch` for every tree,
blob and commit lookup, a blob read once per run, and one `git log` for every snapshot's
history. They must give exactly what the per-file `git show` reads gave."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_trail import snapshot

from agent_trail import core as trail
from agent_trail.config import Config
from conftest import GOOD, g, merge_pr, write

ODD = {
    "docs/trail/events/0700-a.md": GOOD,
    "docs/trail/events/with space/note one.md": "a file\nwith a space\n",
    "docs/trail/events/ünïcødé/événement – 1.md": "unicode: ☃ and العربية\n",
    "docs/trail/events/crlf.md": "one\r\ntwo\rthree\n",
    "docs/trail/events/not-markdown.txt": "skipped\n",
}


def shown(repo: Path, rev: str, path: str) -> str:
    """What the old reader got: `git show rev:path`, read as text."""
    return subprocess.run(["git", "show", f"{rev}:{path}"], cwd=repo, check=True, capture_output=True, text=True).stdout


def test_read_objects_reads_paths_with_spaces_and_unicode_and_reports_a_missing_one(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, ODD)
    names = [f"HEAD:{p}" for p in ODD]
    got = trail.read_objects([*names, "HEAD:docs/trail/events/no-such.md", "0" * 40, "nosuchref:x.md"], cfg)
    for p in ODD:
        assert got[f"HEAD:{p}"] == ODD[p].encode()
    assert got["HEAD:docs/trail/events/no-such.md"] is None
    assert got["0" * 40] is None
    assert got["nosuchref:x.md"] is None
    blob = g(repo, "rev-parse", "HEAD:docs/trail/events/0700-a.md")
    assert trail.read_objects([blob], cfg) == {blob: GOOD.encode()}
    assert trail.read_objects([], cfg) == {}


def test_an_object_name_with_a_line_break_is_refused(cfg: Config) -> None:
    with pytest.raises(trail.TrailError, match="line break"):
        trail.read_objects(["HEAD:a\nb.md"], cfg)


def test_files_at_a_commit_is_what_git_show_gives(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, ODD)
    got = trail.files_at("HEAD", "docs/trail/events", cfg)
    md = [p for p in ODD if p.endswith(".md")]
    assert sorted(got) == sorted(md)
    for p in md:
        assert got[p] == shown(repo, "HEAD", p)
    assert got["docs/trail/events/crlf.md"] == "one\ntwo\nthree\n"  # universal newlines, as before
    # in git ls-tree's order, so the errors come out in the same order
    listed = g(repo, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", "HEAD", "--", "docs/trail/events")
    assert list(got) == [p for p in listed.split("\n") if p.endswith(".md")]
    # a trailing slash on the prefix, as git ls-tree takes it
    assert trail.files_at("HEAD", "docs/trail/events/", cfg) == got


def test_files_at_a_missing_prefix_or_commit_is_empty(repo: Path, cfg: Config) -> None:
    assert trail.files_at("HEAD", "docs/trail/events", cfg) == {}
    assert trail.files_at("nosuchref", "docs/trail/events", cfg) == {}
    assert trail.files_at("0" * 40, "docs/trail/events", cfg) == {}


def test_a_blob_is_read_once_per_run(repo: Path, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    merge_pr(repo, 700, {"docs/trail/events/0700-a.md": GOOD})
    first = g(repo, "rev-parse", "HEAD")
    merge_pr(repo, 701, {"docs/trail/events/0701-b.md": GOOD.replace("700", "701")})
    asked: list[str] = []
    real = trail.ObjectReader.get

    def spy(self: trail.ObjectReader, name: str) -> tuple[str, str, bytes] | None:
        asked.append(name)
        return real(self, name)

    monkeypatch.setattr(trail.ObjectReader, "get", spy)
    trail._BLOBS.clear()
    with trail.ObjectReader(cfg) as reader:
        trail.files_at(first, "docs/trail/events", cfg, reader)
        trail.files_at("HEAD", "docs/trail/events", cfg, reader)
    blob_a = g(repo, "rev-parse", "HEAD:docs/trail/events/0700-a.md")
    blob_b = g(repo, "rev-parse", "HEAD:docs/trail/events/0701-b.md")
    assert asked.count(blob_a) == 1 and asked.count(blob_b) == 1


def test_a_reader_left_open_is_closed(cfg: Config) -> None:
    reader = trail.ObjectReader(cfg)
    assert reader.get("HEAD") is not None
    proc = reader.proc
    assert proc is not None and proc.poll() is None
    reader.close()
    assert reader.proc is None and proc.returncode is not None


def test_check_runs_no_git_per_event_or_per_snapshot(repo: Path, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cost that made `trail check` take minutes: a `git show` per event per snapshot."""
    snaps = []
    for n in range(700, 706):
        merge_pr(repo, n, {f"f{n}.txt": "x", f"docs/trail/events/0{n}-e.md": GOOD.replace("700", str(n))})
        merge_pr(repo, n + 100, {f"g{n}.txt": "y"})  # Missing in every later snapshot
        tag = f"v0.4.{n - 700}"
        g(repo, "tag", tag)
        snaps.append(tag)
    for i, tag in enumerate(snaps):
        text = snapshot(cfg, tag, {}, built="full" if i == 0 else snaps[i - 1])
        write(repo, f"docs/trail/snapshots/{tag}.md", text)
    write(repo, "docs/trail/current.md", text)
    calls: list[list[str]] = []
    real_popen = subprocess.Popen  # subprocess.run starts one too

    def popen(args: list[str], *a: object, **k: object) -> object:
        calls.append(list(args))
        return real_popen(args, *a, **k)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "Popen", popen)
    for cache in (trail._BLOBS, trail._CHANGED, trail._COMMITS, trail._EVENTS):
        cache.clear()
    assert trail.check(cfg) == []
    assert not [c for c in calls if c[1] in ("show", "ls-tree", "rev-parse")]
    # one cat-file, one log for every snapshot's history, a diff-tree per snapshot at most
    assert len(calls) <= 2 + len(snaps)


def test_a_changed_snapshot_still_fails_the_same_way(repo: Path, cfg: Config) -> None:
    merge_pr(repo, 700, {"a.txt": "a", "docs/trail/events/0700-a.md": GOOD})
    g(repo, "tag", "v0.4.0")
    merge_pr(repo, 701, {"b.txt": "b"}, "Feature B")
    g(repo, "tag", "v0.4.1")
    first = snapshot(cfg, "v0.4.0", {"Keep doing": "- Key caches by id. (#700)"})
    second = snapshot(cfg, "v0.4.1", {"Keep doing": "- Key caches by id. (#700)"}, built="v0.4.0")
    assert "#701" in second  # Missing
    write(repo, "docs/trail/snapshots/v0.4.0.md", first)
    write(repo, "docs/trail/snapshots/v0.4.1.md", second.replace("| #701 |", "| #799 |"))
    write(repo, "docs/trail/current.md", second)
    errs = trail.check(cfg)
    assert any("v0.4.1.md: `## Missing events` is not what git says" in e for e in errs)
    assert any("current.md: is not a copy" in e for e in errs)
