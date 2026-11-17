"""Settings and keybindings as files: VS Code's own JSONC documents, edited in place.

Works with no window open. A running VS Code picks the change up by watching the file.
Every write is backed up through the session, so ``session undo`` reverts it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from codereins.core.session import Session
from codereins.utils import jsonc
from codereins.utils.vscode_backend import paths

_MISSING = object()


def settings_file(scope: str = "user", *, user_data_dir: Optional[str] = None,
                  folder: Optional[str] = None) -> Path:
    """Path of the settings file for ``scope`` (``user`` or ``workspace``)."""
    if scope == "user":
        return paths(user_data_dir)["settings"]
    if scope == "workspace":
        return Path(folder or os.getcwd()) / ".vscode" / "settings.json"
    raise RuntimeError("scope must be 'user' or 'workspace'")


def read(path: Path) -> dict:
    """All settings in ``path`` (an absent file is empty)."""
    data = jsonc.loads(jsonc.read_file(path))
    if not isinstance(data, dict):
        raise RuntimeError(f"Settings file is not a JSON object: {path}")
    return data


def _write(path: Path, text: str, session: Optional[Session], dry_run: bool) -> Optional[str]:
    if dry_run:
        return None
    jsonc.loads(text)
    backup = session.backup_file(str(path)) if session else None
    jsonc.write_file(path, text)
    return backup


def get_value(path: Path, key: str) -> dict:
    """Look ``key`` up in ``path``."""
    value = read(path).get(key, _MISSING)
    return {"key": key, "found": value is not _MISSING, "value": None if value is _MISSING else value,
            "path": str(path)}


def set_value(path: Path, key: str, value: Any, *, session: Optional[Session] = None,
              dry_run: bool = False) -> dict:
    """Set ``key`` to ``value``, preserving comments and layout."""
    text = jsonc.read_file(path)
    old = read(path).get(key, _MISSING)
    backup = _write(path, jsonc.set_key(text, key, value), session, dry_run)
    return {"key": key, "old": None if old is _MISSING else old, "new": value, "path": str(path),
            "changed": old is _MISSING or old != value, "backup": backup, "dry_run": dry_run}


def unset_value(path: Path, key: str, *, session: Optional[Session] = None, dry_run: bool = False) -> dict:
    """Remove ``key``."""
    new_text, removed = jsonc.remove_key(jsonc.read_file(path), key)
    backup = _write(path, new_text, session, dry_run) if removed else None
    return {"key": key, "removed": removed, "path": str(path), "backup": backup, "dry_run": dry_run}


def keybindings(path: Path) -> list:
    """User keybinding rules."""
    data = jsonc.loads(jsonc.read_file(path), default=[])
    if not isinstance(data, list):
        raise RuntimeError(f"Keybindings file is not a JSON array: {path}")
    return data


def add_keybinding(path: Path, key: str, command: str, *, when: Optional[str] = None, args: Any = None,
                   session: Optional[Session] = None, dry_run: bool = False) -> dict:
    """Append a keybinding rule."""
    rule: dict[str, Any] = {"key": key, "command": command}
    if when:
        rule["when"] = when
    if args is not None:
        rule["args"] = args
    backup = _write(path, jsonc.append_item(jsonc.read_file(path), rule), session, dry_run)
    return {"added": rule, "path": str(path), "backup": backup, "dry_run": dry_run}


def remove_keybinding(path: Path, *, key: Optional[str] = None, command: Optional[str] = None,
                      session: Optional[Session] = None, dry_run: bool = False) -> dict:
    """Remove every rule matching ``key`` and/or ``command``."""
    if not key and not command:
        raise RuntimeError("Pass --key and/or --command")
    text = jsonc.read_file(path)
    rules = keybindings(path)
    hit = [i for i, r in enumerate(rules) if isinstance(r, dict)
           and (not key or r.get("key") == key) and (not command or r.get("command") == command)]
    for i in reversed(hit):
        text = jsonc.remove_item(text, i)
    backup = _write(path, text, session, dry_run) if hit else None
    return {"removed": [rules[i] for i in hit], "path": str(path), "backup": backup, "dry_run": dry_run}
