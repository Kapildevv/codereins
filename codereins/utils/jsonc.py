"""JSONC (JSON with comments and trailing commas), the format of VS Code's settings.json,
keybindings.json and .code-workspace files.

``loads`` parses it. ``set_key``, ``remove_key``, ``append_item`` and ``remove_item`` edit
the top-level object or array in place, so comments and layout elsewhere in the file
survive an edit.
"""
from __future__ import annotations

import json
import os
from typing import Any


class JsoncError(ValueError):
    """Raised when a JSONC document cannot be parsed or edited."""


def read_file(path: Any) -> str:
    """Read a text file without translating its line endings. A missing file reads as ''."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write_file(path: Any, text: str) -> None:
    """Write a text file atomically, keeping the line endings in ``text``."""
    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _skip_ws(text: str, i: int) -> int:
    """Return the index of the next token at or after ``i`` (skips space and comments)."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n﻿":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise JsoncError("unterminated block comment")
            i = j + 2
        else:
            break
    return i


def _skip_string(text: str, i: int) -> int:
    """``text[i]`` is an opening quote; return the index just past the closing quote."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "\n":
            break
        else:
            i += 1
    raise JsoncError("unterminated string")


def _skip_value(text: str, i: int) -> int:
    """Return the index just past the JSON value that starts at ``i``."""
    n = len(text)
    if i >= n:
        raise JsoncError("unexpected end of document")
    c = text[i]
    if c == '"':
        return _skip_string(text, i)
    if c in "{[":
        depth = 0
        while i < n:
            c = text[i]
            if c == '"':
                i = _skip_string(text, i)
                continue
            if text.startswith("//", i) or text.startswith("/*", i):
                i = _skip_ws(text, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise JsoncError("unterminated object or array")
    j = i
    while j < n and text[j] not in ",}] \t\r\n" and not text.startswith("//", j) and not text.startswith("/*", j):
        j += 1
    if j == i:
        raise JsoncError(f"unexpected character {c!r} at offset {i}")
    return j


def strip(text: str) -> str:
    """Return ``text`` as plain JSON: comments and trailing commas removed."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise JsoncError("unterminated block comment")
            i = j + 2
        else:
            out.append(c)
            i += 1
    plain = "".join(out)
    res: list[str] = []
    i, n = 0, len(plain)
    while i < n:
        c = plain[i]
        if c == '"':
            j = _skip_string(plain, i)
            res.append(plain[i:j])
            i = j
            continue
        if c == ",":
            j = i + 1
            while j < n and plain[j] in " \t\r\n":
                j += 1
            if j < n and plain[j] in "}]":
                i += 1
                continue
        res.append(c)
        i += 1
    return "".join(res)


def loads(text: str, default: Any = None) -> Any:
    """Parse JSONC. An empty document yields ``default`` (``{}`` when not given)."""
    text = text.lstrip("﻿")
    if not text.strip() or not strip(text).strip():
        return {} if default is None else default
    try:
        return json.loads(strip(text))
    except json.JSONDecodeError as e:
        raise JsoncError(f"invalid JSONC: {e}") from e


def scan(text: str) -> dict:
    """Locate the top-level container and its items.

    Returns ``{"kind": "object"|"array"|None, "open", "close", "items"}`` where each item
    is ``(key_or_None, item_start, value_start, value_end)``.
    """
    i = _skip_ws(text, 0)
    if i >= len(text):
        return {"kind": None, "open": -1, "close": -1, "items": []}
    opener = text[i]
    if opener not in "{[":
        raise JsoncError("top-level value must be an object or an array")
    kind = "object" if opener == "{" else "array"
    closer = "}" if kind == "object" else "]"
    items: list[tuple] = []
    j = _skip_ws(text, i + 1)
    while True:
        if j >= len(text):
            raise JsoncError("unterminated object or array")
        if text[j] == closer:
            break
        start, key = j, None
        if kind == "object":
            if text[j] != '"':
                raise JsoncError(f"expected a key at offset {j}")
            k_end = _skip_string(text, j)
            key = json.loads(text[j:k_end])
            j = _skip_ws(text, k_end)
            if j >= len(text) or text[j] != ":":
                raise JsoncError(f"expected ':' at offset {j}")
            j = _skip_ws(text, j + 1)
        v_end = _skip_value(text, j)
        items.append((key, start, j, v_end))
        j = _skip_ws(text, v_end)
        if j < len(text) and text[j] == ",":
            j = _skip_ws(text, j + 1)
        elif j < len(text) and text[j] != closer:
            raise JsoncError(f"expected ',' or {closer!r} at offset {j}")
    return {"kind": kind, "open": i, "close": j, "items": items}


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _indent(text: str, sc: dict) -> str:
    if sc["items"]:
        start = sc["items"][0][1]
        line_start = text.rfind("\n", 0, start) + 1
        lead = text[line_start:start]
        if line_start > 0 and lead.strip() == "":
            return lead
    return "    "


def _render(value: Any, indent: str, nl: str) -> str:
    return json.dumps(value, indent=4, ensure_ascii=False).replace("\n", nl + indent)


def _append(text: str, sc: dict, entry: str, indent: str, nl: str) -> str:
    close = sc["close"]
    line_start = text.rfind("\n", 0, close) + 1
    if line_start > sc["open"] and text[line_start:close].strip() == "":
        text = text[:line_start] + indent + entry + nl + text[line_start:]
    else:
        text = text[:close] + nl + indent + entry + nl + text[close:]
    if sc["items"]:
        last_end = sc["items"][-1][3]
        j = _skip_ws(text, last_end)
        if text[j] != ",":
            text = text[:last_end] + "," + text[last_end:]
    return text


def _container(text: str, kind: str) -> tuple[str, dict]:
    if not text.strip():
        text = ("{" if kind == "object" else "[") + _newline(text) + ("}" if kind == "object" else "]") + _newline(text)
    sc = scan(text)
    if sc["kind"] is None:
        text = text.rstrip() + _newline(text) + ("{}" if kind == "object" else "[]") + _newline(text)
        sc = scan(text)
    if sc["kind"] != kind:
        raise JsoncError(f"top-level value is not an {kind}")
    return text, sc


def set_key(text: str, key: str, value: Any) -> str:
    """Set ``key`` in the top-level object, keeping every other byte of the file."""
    text, sc = _container(text, "object")
    indent, nl = _indent(text, sc), _newline(text)
    rendered = _render(value, indent, nl)
    for k, _start, v_start, v_end in reversed(sc["items"]):
        if k == key:
            return text[:v_start] + rendered + text[v_end:]
    return _append(text, sc, f"{json.dumps(key, ensure_ascii=False)}: {rendered}", indent, nl)


def append_item(text: str, value: Any) -> str:
    """Append ``value`` to the top-level array."""
    text, sc = _container(text, "array")
    indent, nl = _indent(text, sc), _newline(text)
    return _append(text, sc, _render(value, indent, nl), indent, nl)


def _remove(text: str, sc: dict, n: int) -> str:
    _key, start, _v_start, v_end = sc["items"][n]
    end = v_end
    j = _skip_ws(text, v_end)
    prev_comma = None
    if j < len(text) and text[j] == ",":
        end = j + 1
    elif n > 0:
        pj = _skip_ws(text, sc["items"][n - 1][3])
        if text[pj] == ",":
            prev_comma = pj
    line_end = text.find("\n", end)
    rest = text[end:] if line_end < 0 else text[end:line_end]
    if rest.strip() == "":
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].strip() == "":
            start = line_start
            end = len(text) if line_end < 0 else line_end + 1
    text = text[:start] + text[end:]
    if prev_comma is not None:
        text = text[:prev_comma] + text[prev_comma + 1:]
    return text


def remove_key(text: str, key: str) -> tuple[str, bool]:
    """Remove ``key`` from the top-level object. Returns ``(new_text, removed)``."""
    if not text.strip():
        return text, False
    removed = False
    while True:
        sc = scan(text)
        if sc["kind"] != "object":
            raise JsoncError("top-level value is not an object")
        hit = [n for n, item in enumerate(sc["items"]) if item[0] == key]
        if not hit:
            return text, removed
        text = _remove(text, sc, hit[-1])
        removed = True


def remove_item(text: str, index: int) -> str:
    """Remove the element at ``index`` from the top-level array."""
    sc = scan(text)
    if sc["kind"] != "array":
        raise JsoncError("top-level value is not an array")
    if not 0 <= index < len(sc["items"]):
        raise JsoncError(f"no element at index {index}")
    return _remove(text, sc, index)
