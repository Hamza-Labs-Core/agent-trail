"""trail: the Claude trail's checker and tools.

Commands:
    check                     validate every event and snapshot (CI runs this)
    missing [--ref R] [--pr N]
                              merged pull requests with no event, up to R (default HEAD);
                              with --pr, exit 1 when N is one of them
    index [--ref R]           print the Index table for the events at R (default: the tree)
    skeleton --tag vX.Y.Z     print a new snapshot: the last snapshot's lessons carried
                              forward, the events since it to fold in, the computed tables
    unsnapshotted             print the tags that have no snapshot yet, oldest first
    init                      write .trail.toml and the trail's folders
    is-trail-only RANGE       exit 0 when RANGE (A..B, or one commit: its change from its
                              first parent) changes only trail paths, 1 when it changes others
    hook                      the Claude Code hook (reads the hook's JSON on stdin)

Settings come from .trail.toml at the repository root (`trail init` writes one).
Standard library only; git is the only outside program.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, core
from . import config as config_mod
from .config import CONFIG_NAME, Config, ConfigError
from .core import TrailError


def _cfg(a: argparse.Namespace) -> Config:
    root = Path(a.root).resolve() if a.root else config_mod.find_root()
    return config_mod.load(root)


def cmd_init(cfg: Config, force: bool) -> int:
    path = cfg.root / CONFIG_NAME
    if path.exists() and not force:
        print(f"trail: {path} exists (pass --force to overwrite it)", file=sys.stderr)
        return 1
    path.write_text(config_mod.TEMPLATE, encoding="utf-8")
    fresh = config_mod.load(cfg.root)
    for d in (fresh.events, fresh.snapshots):
        p = cfg.root / d
        p.mkdir(parents=True, exist_ok=True)
        keep = p / ".gitkeep"
        if not any(p.iterdir()):
            keep.write_text("", encoding="utf-8")
    print(f"trail: wrote {CONFIG_NAME}, {fresh.events}/ and {fresh.snapshots}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trail", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"claude-trail {__version__}")
    ap.add_argument("--root", help="the repository root (default: the nearest folder with a .trail.toml, else git's top level)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate every event and snapshot")
    c.add_argument("--no-git", action="store_true", help="skip the checks that need tags and history")
    m = sub.add_parser("missing", help="merged pull requests with no event")
    m.add_argument("--ref", default="HEAD")
    m.add_argument("--pr", type=int, help="exit 1 when this pull request is missing its event")
    i = sub.add_parser("index", help="print the Index table")
    i.add_argument("--ref", default=None)
    s = sub.add_parser("skeleton", help="print a new snapshot to finish")
    s.add_argument("--tag", required=True)
    sub.add_parser("unsnapshotted", help="tags with no snapshot yet")
    n = sub.add_parser("init", help="write .trail.toml and the folders")
    n.add_argument("--force", action="store_true")
    t = sub.add_parser("is-trail-only", help="exit 0 when a range changes only the trail")
    t.add_argument("range")
    t.add_argument("--quiet", "-q", action="store_true")
    sub.add_parser("hook", help="the Claude Code hook")
    a = ap.parse_args(argv)
    if a.cmd == "hook":
        from .hook import main as hook_main

        return hook_main()
    try:
        cfg = _cfg(a)
        if a.cmd == "init":
            return cmd_init(cfg, a.force)
        if a.cmd == "check":
            errs = core.check(cfg, verify_git=not a.no_git)
            for e in errs:
                print(e, file=sys.stderr)
            events, _ = core.load_events(None, cfg)
            print(f"trail: {'ok' if not errs else f'{len(errs)} problem(s)'} ({len(events)} events)")
            return 1 if errs else 0
        if a.cmd == "missing":
            events, _ = core.load_events(a.ref, cfg)
            rows = core.missing(a.ref, events, cfg)
            if a.pr is not None:
                gone = any(r[0] == a.pr for r in rows)
                print(f"#{a.pr}: {'no event' if gone else 'has an event, or needs none'}")
                return 1 if gone else 0
            print(core.missing_table(rows))
        elif a.cmd == "index":
            events, _ = core.load_events(a.ref, cfg)
            print(core.index_table(events))
        elif a.cmd == "skeleton":
            print(core.skeleton(a.tag, cfg))
        elif a.cmd == "unsnapshotted":
            for tag in core.tags_without_snapshot(cfg):
                print(tag)
        elif a.cmd == "is-trail-only":
            files = core.changed_files(a.range, cfg)
            others = [f for f in files if not cfg.is_trail_only(f)]
            if not a.quiet:
                if not files:
                    print(f"{a.range}: changes nothing")
                elif others:
                    print(f"{a.range}: changes more than the trail, e.g. {', '.join(others[:5])}")
                else:
                    print(f"{a.range}: changes only the trail ({len(files)} file(s))")
            return 1 if others else 0
    except (TrailError, ConfigError) as e:
        print(f"trail: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
