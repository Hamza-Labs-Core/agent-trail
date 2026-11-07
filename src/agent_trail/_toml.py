"""Read `.trail.toml`: the standard library's tomllib on Python 3.11+, else a small reader.

The fallback reads the subset a `.trail.toml` uses: `[table]` and `[table.sub]` headers,
`key = value` lines, comments, and values that are basic or literal strings, integers,
booleans, or arrays of those (an array may span lines). Anything else is an error, never a
silent guess. It exists so the hook and the checker run on the python3 a machine already has
(macOS ships 3.9), with no install step.
"""

from __future__ import annotations

import re
from typing import Any

try:  # Python 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10 in CI
    _tomllib = None  # type: ignore[assignment,unused-ignore]


class TomlError(ValueError):
    pass


def loads(text: str) -> dict[str, Any]:
    if _tomllib is not None:
        try:
            out: dict[str, Any] = _tomllib.loads(text)
            return out
        except _tomllib.TOMLDecodeError as e:
            raise TomlError(str(e)) from e
    return loads_lite(text)


_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def loads_lite(text: str) -> dict[str, Any]:
    """The fallback reader (also tested on 3.11+, against tomllib)."""
    root: dict[str, Any] = {}
    table = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        n = i + 1
        line = _strip_comment(lines[i]).strip()
        i += 1
        if not line:
            continue
        if line.startswith("["):
            if line.startswith("[[") or not line.endswith("]"):
                raise TomlError(f"line {n}: only [table] headers are supported")
            table = root
            for part in line[1:-1].strip().split("."):
                part = part.strip()
                if not _KEY.fullmatch(part):
                    raise TomlError(f"line {n}: bad table name {part!r}")
                nxt = table.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise TomlError(f"line {n}: {part} is already a value")
                table = nxt
            continue
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            raise TomlError(f"line {n}: expected key = value")
        raw = raw.strip()
        # an array may continue over the next lines until its brackets balance
        while raw.startswith("[") and not _balanced(raw):
            if i >= len(lines):
                raise TomlError(f"line {n}: unterminated array")
            raw += " " + _strip_comment(lines[i]).strip()
            i += 1
        if key in table:
            raise TomlError(f"line {n}: {key} is set twice")
        value, rest = _value(raw, n)
        if rest.strip():
            raise TomlError(f"line {n}: unexpected {rest.strip()!r} after the value")
        table[key] = value
    return root


def _strip_comment(line: str) -> str:
    quote = ""
    k = 0
    while k < len(line):
        c = line[k]
        if quote:
            if c == "\\" and quote == '"':
                k += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "#":
            return line[:k]
        k += 1
    return line


def _balanced(raw: str) -> bool:
    depth = 0
    quote = ""
    k = 0
    while k < len(raw):
        c = raw[k]
        if quote:
            if c == "\\" and quote == '"':
                k += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        k += 1
    return depth == 0


def _value(raw: str, n: int) -> tuple[Any, str]:
    raw = raw.lstrip()
    if not raw:
        raise TomlError(f"line {n}: missing value")
    c = raw[0]
    if c == '"':
        out = []
        k = 1
        while k < len(raw):
            ch = raw[k]
            if ch == "\\":
                esc = raw[k + 1 : k + 2]
                if esc not in _ESCAPES:
                    raise TomlError(f"line {n}: unsupported escape \\{esc}")
                out.append(_ESCAPES[esc])
                k += 2
                continue
            if ch == '"':
                return "".join(out), raw[k + 1 :]
            out.append(ch)
            k += 1
        raise TomlError(f"line {n}: unterminated string")
    if c == "'":
        end = raw.find("'", 1)
        if end < 0:
            raise TomlError(f"line {n}: unterminated string")
        return raw[1:end], raw[end + 1 :]
    if c == "[":
        items: list[Any] = []
        rest = raw[1:].lstrip()
        while True:
            if rest.startswith("]"):
                return items, rest[1:]
            item, rest = _value(rest, n)
            items.append(item)
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
            elif not rest.startswith("]"):
                raise TomlError(f"line {n}: expected , or ] in an array")
    m = re.match(r"(true|false)(?![A-Za-z0-9_])", raw)
    if m:
        return m.group(1) == "true", raw[m.end() :]
    m = re.match(r"[+-]?\d[\d_]*(?![A-Za-z0-9.])", raw)
    if m:
        return int(m.group(0).replace("_", "")), raw[m.end() :]
    raise TomlError(f"line {n}: unsupported value {raw[:30]!r}")
