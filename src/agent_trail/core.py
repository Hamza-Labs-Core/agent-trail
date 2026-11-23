"""The agent trail: events, snapshots and the missing-events list.

    <events>/       one file per piece of work, written by the session that did it
    <snapshots>/    vX.Y.Z.md, one per tag, built by a session from the last snapshot plus the
                    events since
    <current>       a copy of the newest snapshot

The paths and the naming come from `.trail.toml` (config.py). Standard library only; git is
the only outside program.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

KINDS = {"pr", "incident", "decision", "correction", "release", "migration"}
SECTIONS = ["What happened", "What we learned", "Keep doing", "Stop doing", "Open"]
REQUIRED_KEYS = {"schema", "kind", "title", "date", "session"}
OPTIONAL_KEYS = {"pr", "issue", "prs", "corrects", "source", "agent"}
SNAPSHOT_KEYS = {"schema", "tag", "up_to_commit", "built_from"}
SNAPSHOT_SECTIONS = ["Keep doing", "Stop doing", "What we learned", "Open", "Missing events", "Index"]
CITED_SECTIONS = {"Keep doing", "Stop doing", "What we learned", "Open"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MERGE_RE = re.compile(r"^Merge pull request #(\d+) ")

# Patterns that must never appear in trail text (it is read by every session, and leaves the
# machine with the repository). Not a full secret scanner: the common shapes.
SECRET_RES = [
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "a GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "a GitHub token"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "an Anthropic API key"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"), "an OpenAI API key"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"X-Plex-Token=[A-Za-z0-9_-]{8,}"), "a Plex token"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}"), "a bearer token"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^:/@\s]*:[^@/\s]+@"), "a URL with a password"),
]
# `password=…`, `"secret": "…"`, `APP_PG_PASSWORD=…`: the key, then a value of 8 or more
# characters. The key may end a longer name (POSTGRES_PASSWORD, client_secret), so it is not
# preceded by a letter rather than by a word boundary.
CREDENTIAL_RE = re.compile(
    r"(?i)(?:^|[^a-z])(?:password|passwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*(?P<q>[\"']?)(?P<v>[^\s\"'`]{8,})"
)
# Never quote or attribute the owner's words in repo text (checks.owner_quotes).
OWNER_RE = re.compile(r"\((?:the )?owner,\s*\d{4}-\d{2}-\d{2}\)|\bthe owner (?:said|wrote|asked)\s*[:,]?\s*[\"“]")

DEFAULT = Config()


class TrailError(Exception):
    pass


@dataclass
class Doc:
    path: str
    meta: dict[str, object]
    sections: dict[str, str]
    order: list[str]
    body: str


@dataclass
class Event:
    path: str
    stem: str
    kind: str
    title: str
    date: str
    pr: int | None = None
    issue: int | None = None
    prs: list[int] = field(default_factory=list)
    issue_prefix: str = "i"
    day_prefix: str = "d"

    @property
    def cites(self) -> set[str]:
        out = {self.stem}
        if self.pr is not None:
            out.add(f"#{self.pr}")
        out |= {f"#{p}" for p in self.prs}
        if self.issue is not None:
            out.add(f"{self.issue_prefix}{self.issue}")
        return out


# ---- parsing --------------------------------------------------------------------------


def _value(raw: str) -> object:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [_value(x) for x in raw[1:-1].split(",") if x.strip()]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def parse(path: str, text: str) -> Doc:
    """Front matter (`key: value` lines between `---` fences) and `## ` sections."""
    if not text.startswith("---\n"):
        raise TrailError(f"{path}: no front matter (the file must start with a --- line)")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise TrailError(f"{path}: the front matter has no closing --- line")
    meta: dict[str, object] = {}
    for n, line in enumerate(text[4:end].splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep or not re.fullmatch(r"[a-z_]+", key.strip()):
            raise TrailError(f"{path}:{n}: front matter lines are `key: value`")
        if key.strip() in meta:
            raise TrailError(f"{path}:{n}: {key.strip()} appears twice")
        meta[key.strip()] = _value(raw)
    body = text[end + 5 :]
    sections: dict[str, str] = {}
    order: list[str] = []
    current: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^## (.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = m.group(1)
            if current in sections:
                raise TrailError(f"{path}: section `## {current}` appears twice")
            order.append(current)
            lines = []
        elif current is not None:
            lines.append(line)
        elif line.strip() and not line.startswith("# "):
            raise TrailError(f"{path}: text before the first `## ` section")
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return Doc(path, meta, sections, order, body)


def _credential(m: re.Match[str]) -> bool:
    """A credential's value, not prose about one (`password: regenerated`, `token=$TOKEN`).

    A quoted value is a value. An unquoted one is a value unless it reads as a word: letters
    only, maybe ending in punctuation, or a reference (`$X`, `<x>`, `${x}`, `%x%`)."""
    v = m.group("v")
    if v[0] in "$<{%":
        return False
    if m.group("q"):
        return True
    return not re.fullmatch(r"[A-Za-z][a-z]*(?:-[a-z]+)*[.,;:!?)]*", v)


def scan_text(path: str, text: str, cfg: Config = DEFAULT) -> list[str]:
    errs = []
    if cfg.default_secrets:
        for rx, what in SECRET_RES:
            if rx.search(text):
                errs.append(f"{path}: looks like it holds {what}; never write secrets in the trail")
        if any(_credential(m) for m in CREDENTIAL_RE.finditer(text)):
            errs.append(f"{path}: looks like it holds a credential value; never write secrets in the trail")
    for pattern in cfg.extra_secrets:
        if re.search(pattern, text):
            errs.append(f"{path}: matches checks.extra_secrets {pattern!r}; never write secrets in the trail")
    if cfg.owner_quotes and OWNER_RE.search(text):
        errs.append(f"{path}: quotes or attributes the owner's words; state it neutrally")
    return errs


def event_from(path: str, text: str, cfg: Config = DEFAULT) -> tuple[Event | None, list[str]]:
    name = Path(path).name
    m = cfg.file_re.match(name)
    if not m:
        n, i, d = cfg.pr_digits, cfg.issue_prefix, cfg.day_prefix
        return None, [
            f"{path}: an event is named {'N' * n}-slug.md (its pull request, zero-padded to {n} digits), "
            f"{i}{'N' * n}-slug.md (an issue) or {d}YYYYMMDD-slug.md (neither); the slug is lowercase words joined by -"
        ]
    try:
        doc = parse(path, text)
    except TrailError as e:
        return None, [str(e)]
    errs = scan_text(path, text, cfg)
    meta = doc.meta
    missing = REQUIRED_KEYS - set(meta)
    if missing:
        errs.append(f"{path}: front matter lacks {sorted(missing)}")
    unknown = set(meta) - REQUIRED_KEYS - OPTIONAL_KEYS
    if unknown:
        errs.append(f"{path}: unknown front matter keys {sorted(unknown)}")
    if meta.get("schema") != 1:
        errs.append(f"{path}: schema must be 1")
    kind = str(meta.get("kind", ""))
    if kind not in KINDS:
        errs.append(f"{path}: kind must be one of {sorted(KINDS)}")
    date = str(meta.get("date", ""))
    if not DATE_RE.match(date):
        errs.append(f"{path}: date must be YYYY-MM-DD")
    if not str(meta.get("title", "")).strip():
        errs.append(f"{path}: title is empty")
    if not str(meta.get("session", "")).strip():
        errs.append(f"{path}: session is empty (the session id, or `unknown` for a migrated entry)")
    if "agent" in meta and (not isinstance(meta["agent"], str) or not meta["agent"].strip()):
        # free text: the agent that wrote it (claude-code, codex, cursor, ...)
        errs.append(f"{path}: agent names the agent that wrote the event (claude-code, codex, cursor, ...)")
    ev = Event(
        path=path,
        stem=name[:-3],
        kind=kind,
        title=str(meta.get("title", "")),
        date=date,
        issue_prefix=cfg.issue_prefix,
        day_prefix=cfg.day_prefix,
    )
    for group in ("pr", "issue"):
        num = m.group(group)
        if num and len(num) > cfg.pr_digits and num.startswith("0"):
            errs.append(f"{path}: pad the number to {cfg.pr_digits} digits, no more")
    if m.group("pr"):
        if meta.get("pr") != int(m.group("pr")):
            errs.append(f"{path}: the file is named for pull request {int(m.group('pr'))} but pr is {meta.get('pr')!r}")
        ev.pr = int(m.group("pr"))
    elif "pr" in meta:
        errs.append(f"{path}: pr is set, so name the file {'N' * cfg.pr_digits}-slug.md")
    if m.group("issue"):
        if meta.get("issue") != int(m.group("issue")):
            errs.append(f"{path}: the file is named for issue {int(m.group('issue'))} but issue is {meta.get('issue')!r}")
        ev.issue = int(m.group("issue"))
    elif "issue" in meta:
        # an issue the work also belongs to (a PR event that closes an issue, say)
        if isinstance(meta["issue"], int):
            ev.issue = meta["issue"]
        else:
            errs.append(f"{path}: issue must be an issue number")
    if m.group("day") and m.group("day") != date.replace("-", ""):
        errs.append(f"{path}: the file is named for {m.group('day')} but date is {date}")
    prs = meta.get("prs", [])
    if not isinstance(prs, list) or not all(isinstance(p, int) for p in prs):
        errs.append(f"{path}: prs must be a list of pull request numbers, like [608, 635]")
    else:
        ev.prs = list(prs)
    if kind == "pr" and ev.pr is None:
        errs.append(f"{path}: kind pr needs a {'N' * cfg.pr_digits}-slug.md name and pr")
    if kind == "correction" and not meta.get("corrects"):
        errs.append(f"{path}: kind correction needs corrects: <the event file it corrects, without .md>")
    bad = [s for s in doc.order if s not in SECTIONS]
    if bad:
        errs.append(f"{path}: unknown sections {bad}; use {SECTIONS}")
    if not doc.sections.get("What happened"):
        errs.append(f"{path}: `## What happened` is required and must not be empty")
    known = [s for s in doc.order if s in SECTIONS]
    if known != [s for s in SECTIONS if s in known]:
        errs.append(f"{path}: sections must come in the order {SECTIONS}")
    return ev, errs


# ---- git ------------------------------------------------------------------------------


def git(cfg: Config, *args: str, timeout: float | None = None) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cfg.root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise TrailError(f"git {' '.join(args)}: timed out") from e
    if r.returncode != 0:
        raise TrailError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


# What git stores never changes, so a run reads each object once and caches what it built
# from it, keyed by the repository (one process may read several): most event files are the
# same blob at every snapshot's commit, and every snapshot's history repeats the one before.
_BLOBS: dict[tuple[str, str], str] = {}  # blob sha: its text
_CHANGED: dict[tuple[str, str], list[str]] = {}  # commit sha: the files it changed from its first parent
_COMMITS: dict[tuple[str, str], tuple[str, str, str, str]] = {}  # sha: %P, %cs, %s, %b
# An event file's check depends only on its path, its text and the settings.
_EVENTS: dict[tuple[Config, str, str], tuple[Event | None, list[str]]] = {}


def _text(data: bytes) -> str:
    """A file's bytes as `git show` read in text mode gave them: UTF-8, universal newlines."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


class ObjectReader:
    """One `git cat-file --batch` for a run: ask for any object by name (a sha, `<rev>:<path>`,
    `<tag>^{commit}`), one at a time. close() it, or use it in a `with`."""

    def __init__(self, cfg: Config = DEFAULT) -> None:
        self.root = cfg.root
        self.proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> ObjectReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.proc is not None:
            proc, self.proc = self.proc, None
            for f in (proc.stdin, proc.stdout):
                if f is not None:
                    f.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def get(self, name: str) -> tuple[str, str, bytes] | None:
        """(sha, type, content), or None for a name git has no object for (or an ambiguous one)."""
        if not name or "\n" in name or "\r" in name:
            raise TrailError(f"git cat-file: no object name can hold a line break: {name!r}")
        if self.proc is None:
            try:
                self.proc = subprocess.Popen(
                    ["git", "cat-file", "--batch"],
                    cwd=self.root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as e:
                raise TrailError(f"git cat-file --batch: {e}") from e
        proc = self.proc
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(name.encode("utf-8") + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline()
        except OSError as e:
            self.close()
            raise TrailError(f"git cat-file --batch: {e}") from e
        if not header.endswith(b"\n"):
            self.close()
            raise TrailError(f"git cat-file --batch: it stopped at {name!r} (is this a git repository?)")
        parts = header[:-1].decode("utf-8", "replace").rsplit(" ", 2)
        if len(parts) == 3 and parts[2].isdigit() and FULL_SHA_RE.match(parts[0]):
            size = int(parts[2])
            data = proc.stdout.read(size + 1)  # the content, then a newline
            if len(data) != size + 1:
                self.close()
                raise TrailError(f"git cat-file --batch: {name!r} was cut short")
            return parts[0], parts[1], data[:size]
        if header.endswith((b" missing\n", b" ambiguous\n")):
            return None
        self.close()
        raise TrailError(f"git cat-file --batch: unexpected output for {name!r}: {header[:80]!r}")


def read_objects(objects: list[str], cfg: Config = DEFAULT) -> dict[str, bytes | None]:
    """Each object's content, from one `git cat-file --batch`: an object is a sha or
    `<rev>:<path>` (a path may hold spaces or any unicode). None for one git does not have."""
    with ObjectReader(cfg) as r:
        out: dict[str, bytes | None] = {}
        for o in objects:
            got = r.get(o)
            out[o] = None if got is None else got[2]
        return out


def _tree_entries(data: bytes) -> list[tuple[str, str, str]]:
    """A raw tree object's (mode, name, sha) entries, in the tree's order."""
    out = []
    i = 0
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        out.append((data[i:sp].decode(), data[sp + 1 : nul].decode("utf-8", "replace"), data[nul + 1 : nul + 21].hex()))
        i = nul + 21
    return out


def files_at(ref: str | None, prefix: str, cfg: Config = DEFAULT, reader: ObjectReader | None = None) -> dict[str, str]:
    """{path: text} under prefix, at a commit, or in the working tree when ref is None.

    At a commit, the trees and blobs come from one `git cat-file --batch` (the reader given, or
    one of its own), and a blob this run has read before is not read again."""
    out: dict[str, str] = {}
    if ref is None:
        base = cfg.root / prefix
        if base.is_dir():
            for p in sorted(base.rglob("*.md")):
                out[p.relative_to(cfg.root).as_posix()] = p.read_text(encoding="utf-8")
        return out
    if reader is None:
        with ObjectReader(cfg) as own:
            return files_at(ref, prefix, cfg, own)
    prefix = prefix.strip("/")
    top = reader.get(f"{ref}:{prefix}")
    if top is None:  # no such commit, or nothing at prefix (git ls-tree listed nothing)
        return out
    blobs: list[tuple[str, str]] = []

    def walk(path: str, sha: str, kind: str, data: bytes | None) -> None:
        if kind == "tree":
            if data is None:
                got = reader.get(sha)
                if got is None:
                    raise TrailError(f"git cat-file: {ref} holds tree {sha}, which git does not have")
                data = got[2]
            for mode, name, child in _tree_entries(data):
                if mode == "160000":
                    continue  # a submodule: no file here
                walk(f"{path}/{name}", child, "tree" if mode == "40000" else "blob", None)
        elif kind == "blob" and path.endswith(".md"):
            blobs.append((path, sha))

    walk(prefix, top[0], top[1], top[2])
    root = str(cfg.root)
    for path, sha in blobs:
        if (root, sha) not in _BLOBS:
            got = reader.get(sha)
            if got is None:
                raise TrailError(f"git cat-file: {ref} holds blob {sha}, which git does not have")
            _BLOBS[(root, sha)] = _text(got[2])
        out[path] = _BLOBS[(root, sha)]
    return out


def _load_history(refs: list[str], cfg: Config) -> None:
    """Cache every commit on the first-parent lines of refs, from one `git log`."""
    if not refs:
        return
    try:
        log = git(cfg, "log", "-z", "--first-parent", "--format=%H%x09%P%x09%cs%x09%s%x09%b", *refs, "--")
    except TrailError:
        return  # a ref git doesn't know: each snapshot asks git itself, as before
    root = str(cfg.root)
    for rec in log.split("\0"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, rest = (rec.split("\t", 2) + ["", ""])[:3]
        day, subject, body = (rest.split("\t", 2) + ["", "", ""])[:3]
        _COMMITS[(root, sha)] = (parents, day, subject, body)


def merged_prs(ref: str, cfg: Config = DEFAULT) -> list[tuple[int, str, str, str]]:
    """(number, merge sha, date, title) for each pull request merged into ref's first-parent line."""
    return [(n, sha, day, title) for n, sha, _parent, day, title in _merges(ref, cfg)]


def _merges(ref: str, cfg: Config) -> list[tuple[int, str, str, str, str]]:
    """merged_prs, with each merge's first parent after its sha."""
    root = str(cfg.root)
    if FULL_SHA_RE.match(ref) and (root, ref) in _COMMITS:
        cached: list[tuple[int, str, str, str, str]] = []
        sha = ref
        while sha:
            c = _COMMITS.get((root, sha))
            if c is None:
                break  # history not cached: ask git
            parents, day, subject, body = c
            parent_list = parents.split(" ") if parents else []
            m = MERGE_RE.match(subject)
            if len(parent_list) > 1 and m:
                title = body.strip().splitlines()[0] if body.strip() else subject
                cached.append((int(m.group(1)), sha, parent_list[0], day, title))
            sha = parent_list[0] if parent_list else ""
        else:
            return cached
    log = git(cfg, "log", "--first-parent", "--merges", "--format=%H%x09%P%x09%cs%x09%s%x09%b%x1e", ref)
    out = []
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, day, subject, body = (rec.split("\t", 4) + ["", "", "", "", ""])[:5]
        m = MERGE_RE.match(subject)
        if m:
            title = body.strip().splitlines()[0] if body.strip() else subject
            out.append((int(m.group(1)), sha, parents.split(" ")[0], day, title))
    return out


def _prefetch_changed(pairs: list[tuple[str, str]], cfg: Config) -> None:
    """Fill the changed-files cache for (commit, first parent) pairs: one `git diff-tree`."""
    root = str(cfg.root)
    todo = [(c, p) for c, p in dict(pairs).items() if (root, c) not in _CHANGED and FULL_SHA_RE.match(c) and FULL_SHA_RE.match(p)]
    if not todo:
        return
    try:
        r = subprocess.run(
            ["git", "diff-tree", "--stdin", "--always", "-r", "--name-only", "--no-renames", "-z"],
            cwd=cfg.root,
            input="".join(f"{c} {p}\n" for c, p in todo).encode(),
            capture_output=True,
        )
    except OSError:
        return  # changed_files asks git one commit at a time instead
    if r.returncode != 0:
        return
    fields = r.stdout.decode("utf-8", "surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    found: dict[str, list[str]] = {}
    i = 0
    for k, (c, _p) in enumerate(todo):
        if i >= len(fields) or fields[i] != c:
            return  # not the shape expected: no cache, no guess
        i += 1
        nxt = todo[k + 1][0] if k + 1 < len(todo) else None
        names = []
        while i < len(fields) and fields[i] != nxt:
            names.append(fields[i])
            i += 1
        found[c] = names
    for c, names in found.items():
        _CHANGED[(root, c)] = names


def changed_files(rev_range: str, cfg: Config = DEFAULT) -> list[str]:
    """Paths a range changes. `A..B` (or `A...B`) as git diff reads it; a single commit is its
    change from its first parent (a merge commit: what the pull request brought)."""
    if ".." in rev_range:
        return _diff_names(cfg, rev_range)
    if not FULL_SHA_RE.match(rev_range):  # a branch or tag moves: no cache
        return _diff_names(cfg, f"{rev_range}^1", rev_range)
    key = (str(cfg.root), rev_range)
    if key not in _CHANGED:
        _CHANGED[key] = _diff_names(cfg, f"{rev_range}^1", rev_range)
    return list(_CHANGED[key])


def _diff_names(cfg: Config, *args: str) -> list[str]:
    return [f for f in git(cfg, "diff", "--name-only", "--no-renames", *args, "--").split("\n") if f]


def trail_only(sha: str, cfg: Config = DEFAULT) -> bool:
    """A commit that changed only the trail (events, snapshots, current or the legacy file)."""
    files = changed_files(sha, cfg)
    return bool(files) and all(cfg.is_trail_only(f) for f in files)


# ---- events, index, missing -----------------------------------------------------------


def load_events(ref: str | None, cfg: Config = DEFAULT, reader: ObjectReader | None = None) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    errs: list[str] = []
    files = files_at(ref, cfg.events, cfg, reader)
    for path, text in files.items():
        if Path(path).name.lower() == "readme.md":
            continue
        key = (cfg, path, text)
        if key not in _EVENTS:
            _EVENTS[key] = event_from(path, text, cfg)
        ev, e = _EVENTS[key]
        errs += e
        if ev:
            events.append(ev)
    stems = [e.stem for e in events]
    for s in sorted({s for s in stems if stems.count(s) > 1}):
        errs.append(f"{cfg.events}/{s}.md: two events share this name")
    by_stem = {e.stem for e in events}
    for path, text in files.items():
        m = re.search(r"^corrects:\s*(\S+)\s*$", text, re.M)
        if m and m.group(1) not in by_stem:
            errs.append(f"{path}: corrects {m.group(1)}, which is no event")
    return sorted(events, key=lambda e: (e.date, e.stem)), errs


def index_table(events: list[Event]) -> str:
    rows = ["| Event | Date | Kind | Title |", "| --- | --- | --- | --- |"]
    for e in events:
        rows.append(f"| {e.stem} | {e.date} | {e.kind} | {e.title.replace('|', '/')} |")
    return "\n".join(rows)


def covered_prs(events: list[Event]) -> set[int]:
    return {p for e in events for p in ([e.pr] if e.pr else []) + e.prs}


def missing(ref: str, events: list[Event], cfg: Config = DEFAULT) -> list[tuple[int, str, str]]:
    covered = covered_prs(events)
    merges = _merges(ref, cfg)
    _prefetch_changed([(sha, parent) for n, sha, parent, _, _ in merges if n > cfg.missing_after and n not in covered], cfg)
    out = []
    for n, sha, _parent, day, title in merges:
        if n > cfg.missing_after and n not in covered and not trail_only(sha, cfg):
            out.append((n, day, title))
    return sorted(out)


def missing_table(rows: list[tuple[int, str, str]]) -> str:
    if not rows:
        return "None."
    out = ["| PR | Merged | Title |", "| --- | --- | --- |"]
    out += [f"| #{n} | {day} | {title.replace('|', '/')} |" for n, day, title in rows]
    return "\n".join(out)


# ---- snapshots ------------------------------------------------------------------------


def check_snapshot(
    path: str, text: str, cfg: Config = DEFAULT, verify_git: bool = True, reader: ObjectReader | None = None
) -> list[str]:
    try:
        doc = parse(path, text)
    except TrailError as e:
        return [str(e)]
    errs = scan_text(path, text, cfg)
    meta = doc.meta
    tag_re = cfg.tag_re
    shape = f"{cfg.tag_prefix}X.Y.Z"
    if set(meta) != SNAPSHOT_KEYS:
        errs.append(f"{path}: snapshot front matter is exactly {sorted(SNAPSHOT_KEYS)}")
    if meta.get("schema") != 1:
        errs.append(f"{path}: schema must be 1")
    tag = str(meta.get("tag", ""))
    if not tag_re.match(tag):
        errs.append(f"{path}: tag must be {shape}")
    if path != cfg.current and Path(path).name != f"{tag}.md":
        errs.append(f"{path}: a snapshot is named for its tag ({tag}.md)")
    built = str(meta.get("built_from", ""))
    if built != "full" and not tag_re.match(built):
        errs.append(f"{path}: built_from is the previous snapshot's tag, or full for a rebuild from every event")
    if doc.order != SNAPSHOT_SECTIONS:
        errs.append(f"{path}: a snapshot has exactly the sections {SNAPSHOT_SECTIONS}, in that order")
    sha = str(meta.get("up_to_commit", ""))
    if not SHA_RE.match(sha):
        errs.append(f"{path}: up_to_commit must be a commit sha")
        return errs
    if not verify_git:
        return errs
    if reader is None:
        with ObjectReader(cfg) as own:
            return check_snapshot(path, text, cfg, verify_git, own)
    got = reader.get(f"{sha}^{{commit}}")
    if got is None:
        return errs + [f"{path}: up_to_commit {sha} is no commit here (fetch with history)"]
    full = got[0]
    tagged_obj = reader.get(f"{tag}^{{commit}}") if tag else None
    if tagged_obj is None:
        errs.append(f"{path}: tag {tag} is not here (fetch tags)")
    elif tagged_obj[0] != full:
        errs.append(f"{path}: up_to_commit {sha} is not {tag}'s commit ({tagged_obj[0][:9]})")
    events, load_errs = load_events(full, cfg, reader)
    errs += [f"{path}: the events at {sha} do not check: {x}" for x in load_errs[:3]]
    cites = set().union(*(ev.cites for ev in events)) if events else set()
    cite_re = cfg.cite_re
    for name in CITED_SECTIONS:
        for line in doc.sections.get(name, "").splitlines():
            if not line.lstrip().startswith("- "):
                continue
            found = [f"#{n}" if n else (i or d) for n, i, d in cite_re.findall(line)]
            if not found:
                errs.append(f"{path}: `## {name}` item cites no event: {line.strip()[:80]}")
            for c in found:
                if c not in cites:
                    errs.append(f"{path}: `## {name}` cites {c}, which no event at {sha} has")
    if doc.sections.get("Index", "") != index_table(events):
        errs.append(f"{path}: `## Index` is not the events at {sha}; print it with: trail index --ref {sha}")
    if doc.sections.get("Missing events", "") != missing_table(missing(full, events, cfg)):
        errs.append(f"{path}: `## Missing events` is not what git says; print it with: trail missing --ref {sha}")
    return errs


def _version(tag: str, cfg: Config = DEFAULT) -> tuple[int, ...]:
    return tuple(int(x) for x in tag[len(cfg.tag_prefix) :].split("."))


def snapshots_at(cfg: Config = DEFAULT) -> tuple[dict[str, str], list[str]]:
    """The snapshots ({path: text}, named vX.Y.Z.md) and an error for each other file there."""
    snaps: dict[str, str] = {}
    errs: list[str] = []
    for path, text in files_at(None, cfg.snapshots, cfg).items():
        if cfg.tag_re.match(Path(path).stem) and Path(path).parent.as_posix() == cfg.snapshots:
            snaps[path] = text
        else:
            errs.append(f"{path}: {cfg.snapshots}/ holds only snapshots, named {cfg.tag_prefix}X.Y.Z.md")
    return snaps, errs


def check(cfg: Config = DEFAULT, verify_git: bool = True) -> list[str]:
    _events, errs = load_events(None, cfg)
    snaps, e = snapshots_at(cfg)
    errs += e
    if verify_git and snaps:
        with ObjectReader(cfg) as reader:
            shas = []
            for text in snaps.values():
                m = re.search(r"^up_to_commit:\s*([0-9a-f]{7,40})\s*$", text, re.M)
                got = reader.get(f"{m.group(1)}^{{commit}}") if m else None
                if got is not None:
                    shas.append(got[0])
            _load_history(sorted(set(shas)), cfg)
            for path, text in snaps.items():
                errs += check_snapshot(path, text, cfg, verify_git, reader)
    else:
        for path, text in snaps.items():
            errs += check_snapshot(path, text, cfg, verify_git)
    cur = cfg.root / cfg.current
    if snaps:
        newest = max(snaps, key=lambda p: _version(Path(p).stem, cfg))
        if not cur.is_file():
            errs.append(f"{cfg.current}: missing; it is a copy of {newest}")
        elif cur.read_text(encoding="utf-8") != snaps[newest]:
            errs.append(f"{cfg.current}: is not a copy of the newest snapshot, {newest}")
    return errs


def tags(cfg: Config = DEFAULT) -> list[str]:
    """The repository's snapshot tags (vX.Y.Z), oldest first."""
    names = git(cfg, "tag", "--list", f"{cfg.tag_prefix}*").split()
    return sorted((t for t in names if cfg.tag_re.match(t)), key=lambda t: _version(t, cfg))


def tags_without_snapshot(cfg: Config = DEFAULT) -> list[str]:
    """Tags newer than the oldest snapshot with no snapshot, oldest first. Before the first
    snapshot, only the newest tag: the trail starts there, not at every old release."""
    snaps, _ = snapshots_at(cfg)
    have = {Path(p).stem for p in snaps}
    all_tags = tags(cfg)
    if not have:
        return all_tags[-1:]
    oldest = min(_version(t, cfg) for t in have)
    return [t for t in all_tags if _version(t, cfg) > oldest and t not in have]


def skeleton(tag: str, cfg: Config = DEFAULT) -> str:
    """A new snapshot to finish by hand: carried-forward lessons plus the events to fold in."""
    if not cfg.tag_re.match(tag):
        raise TrailError(f"--tag {tag}: a tag is {cfg.tag_prefix}X.Y.Z")
    sha = git(cfg, "rev-parse", f"{tag}^{{commit}}").strip()
    snaps, _ = snapshots_at(cfg)
    # the newest snapshot older than this tag: a snapshot for a skipped or older tag builds on
    # the one before it, not on a newer one
    older = [p for p in snaps if _version(Path(p).stem, cfg) < _version(tag, cfg)]
    prev = None
    if older:
        prev_path = max(older, key=lambda p: _version(Path(p).stem, cfg))
        prev = parse(prev_path, snaps[prev_path])
    events, _ = load_events(sha, cfg)
    since: list[Event] = events
    if prev:
        old, _ = load_events(str(prev.meta["up_to_commit"]), cfg)
        seen = {e.stem for e in old}
        since = [e for e in events if e.stem not in seen]
    head = [
        "---",
        "schema: 1",
        f"tag: {tag}",
        f"up_to_commit: {sha[:9]}",
        f"built_from: {prev.meta['tag'] if prev else 'full'}",
        "---",
        "",
        f"# Trail snapshot: {tag}",
        "",
        "<!-- Fold each new event below into the sections, citing it, then delete this comment.",
        *[f"     new: {e.path} ({e.title})" for e in since],
        "-->",
        "",
    ]
    body = []
    for name in ["Keep doing", "Stop doing", "What we learned", "Open"]:
        body += [f"## {name}", "", (prev.sections.get(name, "") if prev else ""), ""]
    body += ["## Missing events", "", missing_table(missing(sha, events, cfg)), ""]
    body += ["## Index", "", index_table(events), ""]
    return "\n".join(head + body)
