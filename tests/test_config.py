"""`.trail.toml`: parsing, defaults, validation, and that every setting reaches the checker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_trail import _toml, config, core
from agent_trail.config import Config, ConfigError
from conftest import GOOD, g, merge_pr, write


def test_no_file_gives_the_defaults(tmp_path: Path) -> None:
    cfg = config.load(tmp_path)
    assert not cfg.found
    assert (cfg.events, cfg.snapshots, cfg.current) == ("docs/trail/events", "docs/trail/snapshots", "docs/trail/current.md")
    assert cfg.main_ref == "origin/main" and cfg.missing_after == 0 and not cfg.owner_quotes
    assert cfg.trail_only_paths == ("docs/trail/events/**", "docs/trail/snapshots/**", "docs/trail/current.md")


def test_the_template_parses_to_the_defaults(tmp_path: Path) -> None:
    cfg = config.parse(config.TEMPLATE, tmp_path)
    assert cfg == Config(root=tmp_path, found=True)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("[pathz]\nevents = 'x'\n", "unknown table [pathz]"),
        ("[paths]\nevent = 'x'\n", "unknown key paths.event"),
        ("[events]\npr_digits = '4'\n", "events.pr_digits must be a int"),
        ("[events]\npr_digits = true\n", "events.pr_digits must be a int"),
        ("[checks]\nextra_secrets = [1]\n", "list of strings"),
        ("[checks]\nextra_secrets = ['(']\n", "is not a regular expression"),
        ("[events]\nissue_prefix = 'I'\n", "lowercase letters"),
        ("[events]\nissue_prefix = 'd'\n", "must differ"),
        ("[events]\npr_digits = 0\n", "1 to 9"),
        ("[paths]\nevents = '../x'\n", "inside the repository"),
        ("[paths]\nevents = '/abs'\n", "inside the repository"),
        ("[paths\n", ".trail.toml"),
    ],
)
def test_bad_settings_are_refused_with_a_reason(tmp_path: Path, text: str, needle: str) -> None:
    with pytest.raises(ConfigError, match=None) as e:
        config.parse(text, tmp_path)
    assert needle in str(e.value), str(e.value)


def test_maktabas_settings(mcfg: Config) -> None:
    assert mcfg.missing_after == 653 and mcfg.owner_quotes and mcfg.legacy == "docs/claude-trail.md"
    assert "docs/claude-trail.md" in mcfg.trail_only_paths


SAMPLE = """\
# a comment
[paths]
events = "trail/ev"   # trailing comment
snapshots = 'trail/snap'
current = "trail/now.md"
legacy = "OLD # not a comment.md"

[git]
remote = "upstream"
main_branch = "trunk"

[events]
pr_digits = 5
issue_prefix = "iss"
day_prefix = "day"
missing_after = 1_000

[checks]
default_secrets = false
extra_secrets = [
  "ACME-[0-9]{6}",  # an internal ticket token
  'x\\d',
]
owner_quotes = true

[trail_only]
paths = ["trail/**", "OLD.md"]
"""


def test_the_fallback_reader_matches_tomllib() -> None:
    lite = _toml.loads_lite(SAMPLE)
    assert lite["paths"]["legacy"] == "OLD # not a comment.md"
    assert lite["events"]["missing_after"] == 1000
    assert lite["checks"]["extra_secrets"] == ["ACME-[0-9]{6}", "x\\d"]
    if sys.version_info >= (3, 11):
        import tomllib

        assert lite == tomllib.loads(SAMPLE)


@pytest.mark.parametrize(
    "bad", ["[[a]]\n", "a = 1.5\n", "a = {b = 1}\n", "a = 'x' junk\n", "a = [1,\n", "a = 1\na = 2\n", 'a = "\\q"\n']
)
def test_the_fallback_reader_refuses_what_it_does_not_read(bad: str) -> None:
    with pytest.raises(_toml.TomlError):
        _toml.loads_lite(bad)


def test_the_fallback_reader_is_used_without_tomllib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_toml, "_tomllib", None)
    cfg = config.parse(SAMPLE, tmp_path)
    assert cfg.pr_digits == 5 and cfg.main_ref == "upstream/trunk"


def test_custom_naming_reaches_the_checker(tmp_path: Path) -> None:
    cfg = config.parse(SAMPLE, tmp_path)
    ev, errs = core.event_from("trail/ev/00700-a.md", GOOD, cfg)
    assert errs == [] and ev is not None and ev.pr == 700
    _, errs = core.event_from("trail/ev/0700-a.md", GOOD, cfg)  # four digits: too few now
    assert any("zero-padded to 5 digits" in e for e in errs)
    issue = GOOD.replace("kind: pr\npr: 700", "kind: incident\nissue: 649")
    ev, errs = core.event_from("trail/ev/iss00649-x.md", issue, cfg)
    assert errs == [] and ev is not None and "iss649" in ev.cites
    day = GOOD.replace("kind: pr\npr: 700\n", "kind: decision\n")
    ev, errs = core.event_from("trail/ev/day20261106-x.md", day, cfg)
    assert errs == [] and ev is not None
    assert cfg.cite_re.findall("- a (iss649) and day20261106-x") == [("", "iss649", ""), ("", "", "day20261106-x")]


def test_over_padding_is_refused(mcfg: Config) -> None:
    _, errs = core.event_from("docs/trail/events/00700-a.md", GOOD, mcfg)
    assert any("no more" in e for e in errs)
    ev, errs = core.event_from("docs/trail/events/10700-a.md", GOOD.replace("700", "10700"), mcfg)
    assert errs == [] and ev is not None and ev.pr == 10700


def test_secret_settings(tmp_path: Path) -> None:
    off = config.parse("[checks]\ndefault_secrets = false\nextra_secrets = ['ACME-[0-9]{6}']\n", tmp_path)
    assert core.scan_text("x.md", "password: hunter2hunter2", off) == []
    assert any("extra_secrets" in e for e in core.scan_text("x.md", "ticket ACME-123456", off))
    on = Config(root=tmp_path)
    assert core.scan_text("x.md", "password: hunter2hunter2", on)


def test_owner_quotes_are_refused_only_when_asked(tmp_path: Path, mcfg: Config) -> None:
    text = "Done this way (owner, 2026-11-05)."
    assert core.scan_text("x.md", text, Config(root=tmp_path)) == []
    assert core.scan_text("x.md", text, mcfg)


@pytest.mark.parametrize(
    ("pattern", "path", "hit"),
    [
        ("docs/trail/**", "docs/trail/events/0700-a.md", True),
        ("docs/trail/**", "docs/trail", True),
        ("docs/trail/**", "docs/trailer.md", False),
        ("docs/claude-trail.md", "docs/claude-trail.md", True),
        ("docs/claude-trail.md", "docs/claude-trail.md.bak", False),
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "docs/x/a.md", False),
        ("**/TRAIL.md", "a/b/TRAIL.md", True),
    ],
)
def test_trail_only_globs(pattern: str, path: str, hit: bool) -> None:
    assert config.glob_match(pattern, path) is hit


def test_find_root_walks_up_to_the_config(tmp_path: Path) -> None:
    (tmp_path / ".trail.toml").write_text("", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert config.find_root(deep) == tmp_path.resolve()


def test_custom_paths_and_trail_only_reach_missing_and_check(repo: Path) -> None:
    write(repo, ".trail.toml", "[paths]\nevents = 'notes/ev'\nsnapshots = 'notes/snap'\ncurrent = 'notes/now.md'\n")
    g(repo, "commit", "-qam", "config")
    cfg = config.load(repo)
    merge_pr(repo, 5, {"a.txt": "a"})
    merge_pr(repo, 6, {"notes/ev/0005-a.md": GOOD.replace("700", "5")})  # trail-only under the custom path
    events, errs = core.load_events("HEAD", cfg)
    assert errs == [] and [e.pr for e in events] == [5]
    assert core.missing("HEAD", events, cfg) == []
    write(repo, "notes/snap/notes.txt.md", "x")
    assert any("notes/snap/ holds only snapshots" in e for e in core.check(cfg))
