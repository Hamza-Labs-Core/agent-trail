"""`.trail.toml`: where a repository keeps its trail and how its events are named.

Every key is optional; a repository with no `.trail.toml` gets the defaults below (the layout
the trail was first built with, in Maktaba). `trail init` writes a commented one.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._toml import TomlError, loads

CONFIG_NAME = ".trail.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    root: Path = field(default_factory=Path.cwd)
    # [paths]
    events: str = "docs/trail/events"
    snapshots: str = "docs/trail/snapshots"
    current: str = "docs/trail/current.md"
    legacy: str = ""  # a frozen trail from before the events, if any
    # [git]
    remote: str = "origin"
    main_branch: str = "main"
    # [events]
    pr_digits: int = 4  # 0634-slug.md: the number zero-padded to this many digits
    issue_prefix: str = "i"  # i0649-slug.md
    day_prefix: str = "d"  # d20261104-slug.md
    missing_after: int = 0  # pull requests up to this number are never Missing
    # [snapshots]
    tag_prefix: str = "v"  # snapshots are named <prefix>X.Y.Z.md, one per such tag
    # [checks]
    default_secrets: bool = True
    extra_secrets: tuple[str, ...] = ()
    owner_quotes: bool = False  # refuse text that quotes or attributes the owner's words
    # [trail_only]
    trail_only: tuple[str, ...] = ()  # empty: the events, snapshots, current and legacy paths
    found: bool = False  # a .trail.toml was read

    # ---- derived -------------------------------------------------------------------------

    @property
    def trail_only_paths(self) -> tuple[str, ...]:
        if self.trail_only:
            return self.trail_only
        out = [f"{self.events}/**", f"{self.snapshots}/**", self.current]
        if self.legacy:
            out.append(self.legacy)
        return tuple(out)

    @property
    def main_ref(self) -> str:
        return f"{self.remote}/{self.main_branch}"

    @property
    def file_re(self) -> re.Pattern[str]:
        i, d, n = re.escape(self.issue_prefix), re.escape(self.day_prefix), self.pr_digits
        return re.compile(
            rf"^(?:(?P<pr>\d{{{n},}})|{i}(?P<issue>\d{{{n},}})|{d}(?P<day>\d{{8}}))"
            r"-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
        )

    @property
    def cite_re(self) -> re.Pattern[str]:
        # `#634` cites a pull request's events; `issue #649` names an issue and cites nothing
        i, d = re.escape(self.issue_prefix), re.escape(self.day_prefix)
        return re.compile(rf"(?<![\w/])(?:(?<![Ii]ssue )#(\d+)|\b({i}\d+)\b|\b({d}\d{{8}}-[a-z0-9-]+)\b)")

    @property
    def tag_re(self) -> re.Pattern[str]:
        return re.compile(rf"^{re.escape(self.tag_prefix)}\d+\.\d+\.\d+$")

    def pad(self, n: int) -> str:
        return str(n).zfill(self.pr_digits)

    def is_trail_only(self, path: str) -> bool:
        return any(glob_match(p, path) for p in self.trail_only_paths)


def glob_match(pattern: str, path: str) -> bool:
    """`dir/**` is the folder and everything in it, `*` is one path segment's characters,
    anything else is the exact path."""
    rx = ""
    k = 0
    while k < len(pattern):
        if pattern.startswith("/**", k) and k + 3 == len(pattern):
            rx += "(?:/.*)?"
            k += 3
        elif pattern.startswith("**", k):
            rx += ".*"
            k += 2
        elif pattern[k] == "*":
            rx += "[^/]*"
            k += 1
        elif pattern[k] == "?":
            rx += "[^/]"
            k += 1
        else:
            rx += re.escape(pattern[k])
            k += 1
    return re.fullmatch(rx, path) is not None


_SCHEMA: dict[str, dict[str, type]] = {
    "paths": {"events": str, "snapshots": str, "current": str, "legacy": str},
    "git": {"remote": str, "main_branch": str},
    "events": {"pr_digits": int, "issue_prefix": str, "day_prefix": str, "missing_after": int},
    "snapshots": {"tag_prefix": str},
    "checks": {"default_secrets": bool, "extra_secrets": list, "owner_quotes": bool},
    "trail_only": {"paths": list},
}


def find_root(start: Path | None = None) -> Path:
    """The nearest folder at or above start holding a .trail.toml; else git's top level; else start."""
    start = (start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        if (d / CONFIG_NAME).is_file():
            return d
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return start


def parse(text: str, root: Path, found: bool = True) -> Config:
    try:
        raw = loads(text)
    except TomlError as e:
        raise ConfigError(f"{CONFIG_NAME}: {e}") from e
    kw: dict[str, Any] = {}
    for table, value in raw.items():
        if table not in _SCHEMA or not isinstance(value, dict):
            raise ConfigError(f"{CONFIG_NAME}: unknown table [{table}]; known: {sorted(_SCHEMA)}")
        for key, v in value.items():
            want = _SCHEMA[table].get(key)
            if want is None:
                raise ConfigError(f"{CONFIG_NAME}: unknown key {table}.{key}; known: {sorted(_SCHEMA[table])}")
            if not isinstance(v, want) or (want is int and isinstance(v, bool)):
                raise ConfigError(f"{CONFIG_NAME}: {table}.{key} must be a {want.__name__}")
            name = "trail_only" if table == "trail_only" else key
            if isinstance(v, list):
                if not all(isinstance(x, str) for x in v):
                    raise ConfigError(f"{CONFIG_NAME}: {table}.{key} must be a list of strings")
                kw[name] = tuple(v)
            else:
                kw[name] = v
    cfg = Config(root=root, found=found, **kw)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    for name in ("events", "snapshots", "current"):
        p = getattr(cfg, name)
        if not p or p.startswith("/") or ".." in p.split("/"):
            raise ConfigError(f"{CONFIG_NAME}: paths.{name} must be a path inside the repository")
    for name in ("issue_prefix", "day_prefix"):
        if not re.fullmatch(r"[a-z]+", getattr(cfg, name)):
            raise ConfigError(f"{CONFIG_NAME}: events.{name} must be lowercase letters")
    if cfg.issue_prefix == cfg.day_prefix:
        raise ConfigError(f"{CONFIG_NAME}: events.issue_prefix and events.day_prefix must differ")
    if not 1 <= cfg.pr_digits <= 9:
        raise ConfigError(f"{CONFIG_NAME}: events.pr_digits must be 1 to 9")
    if cfg.missing_after < 0:
        raise ConfigError(f"{CONFIG_NAME}: events.missing_after must be 0 or more")
    if not re.fullmatch(r"[A-Za-z0-9_.-]*", cfg.tag_prefix):
        raise ConfigError(f"{CONFIG_NAME}: snapshots.tag_prefix must be letters, digits, ., _ or -")
    for rx in cfg.extra_secrets:
        try:
            re.compile(rx)
        except re.error as e:
            raise ConfigError(f"{CONFIG_NAME}: checks.extra_secrets {rx!r} is not a regular expression: {e}") from e


def load(root: Path | None = None) -> Config:
    root = (root or find_root()).resolve()
    path = root / CONFIG_NAME
    if not path.is_file():
        return Config(root=root, found=False)
    return parse(path.read_text(encoding="utf-8"), root)


TEMPLATE = """\
# The Claude trail's settings (https://github.com/Hamza-Labs-Core/claude-trail).
# Every key is optional: the values below are the defaults.

[paths]
events = "docs/trail/events"        # one file per piece of work
snapshots = "docs/trail/snapshots"  # vX.Y.Z.md, one per tag
current = "docs/trail/current.md"   # a copy of the newest snapshot
legacy = ""                         # a frozen trail file from before the events, if any

[git]
remote = "origin"
main_branch = "main"

[events]
pr_digits = 4        # 0634-slug.md: pull request numbers zero-padded to this many digits
issue_prefix = "i"   # i0649-slug.md: work with no pull request, named for its issue
day_prefix = "d"     # d20261104-slug.md: work with neither, named for its day
missing_after = 0    # pull requests up to this number are never listed as Missing

[snapshots]
tag_prefix = "v"     # a snapshot per <prefix>X.Y.Z tag

[checks]
default_secrets = true   # refuse tokens, keys, passwords and URLs with passwords
extra_secrets = []       # more regular expressions to refuse
owner_quotes = false     # refuse text that quotes or attributes the owner's words

[trail_only]
# A change of only these paths is trail-only: it is never Missing an event, and
# `trail is-trail-only` says so to tag automation. Empty: the events, snapshots,
# current and legacy paths above.
paths = []
"""
