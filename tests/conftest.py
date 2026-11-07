"""Shared fixtures: a scratch git repository with Maktaba's trail settings."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_trail import config

# the settings the trail was built with (Maktaba's): the ported tests assume them
MAKTABA_TOML = """\
[paths]
legacy = "docs/claude-trail.md"
[events]
missing_after = 653
[checks]
owner_quotes = true
"""

GOOD = """---
schema: 1
kind: pr
pr: 700
title: A change
date: 2026-11-06
session: local_abc
---

## What happened
Built the thing; the first guess (a race) was wrong, the cause was a stale cache.

## What we learned
- The cache is keyed by path, not by id.
"""


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def merge_pr(repo: Path, n: int, files: dict[str, str], title: str = "Some change") -> str:
    g(repo, "checkout", "-q", "-b", f"pr{n}")
    for rel, text in files.items():
        write(repo, rel, text)
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", f"work {n}")
    g(repo, "checkout", "-q", "main")
    g(repo, "merge", "-q", "--no-ff", f"pr{n}", "-m", f"Merge pull request #{n} from org/pr{n}\n\n{title}")
    return g(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    g(r, "init", "-q", "-b", "main")
    g(r, "config", "user.email", "t@example.invalid")
    g(r, "config", "user.name", "t")
    g(r, "config", "commit.gpgsign", "false")
    g(r, "config", "tag.gpgsign", "false")
    write(r, "README.md", "x\n")
    write(r, ".trail.toml", MAKTABA_TOML)
    g(r, "add", "-A")
    g(r, "commit", "-qm", "root")
    return r


@pytest.fixture
def cfg(repo: Path) -> config.Config:
    return config.load(repo)


@pytest.fixture
def mcfg() -> config.Config:
    """Maktaba's settings, for the tests that need no repository."""
    return config.parse(MAKTABA_TOML, Path.cwd())
