"""Project = a VS Code workspace file (``.code-workspace``): folders, workspace settings
and extension recommendations. This is VS Code's native multi-root project format.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from codereins.utils import jsonc
from codereins.utils.vscode_backend import paths


def create_project(folders: Optional[list[str]] = None, settings: Optional[dict] = None) -> dict:
    """Return a new, unsaved workspace."""
    proj: dict[str, Any] = {"folders": [], "settings": dict(settings or {})}
    for f in folders or []:
        add_folder(proj, f)
    return proj


def open_project(path: str) -> dict:
    """Load a ``.code-workspace`` file."""
    if not os.path.isfile(path):
        raise RuntimeError(f"Workspace file not found: {path}")
    proj = jsonc.loads(jsonc.read_file(path))
    if not isinstance(proj, dict):
        raise RuntimeError(f"Not a workspace file (top level is not an object): {path}")
    proj.setdefault("folders", [])
    if not isinstance(proj["folders"], list):
        raise RuntimeError(f"Not a workspace file (folders is not a list): {path}")
    return proj


def save_project(proj: dict, path: str) -> str:
    """Write the workspace. An existing file keeps its comments outside the changed keys."""
    text = jsonc.read_file(path)
    if text.strip():
        current = jsonc.loads(text)
        if not isinstance(current, dict):
            raise RuntimeError(f"Not a workspace file: {path}")
        for key, value in proj.items():
            if key not in current or current[key] != value:
                text = jsonc.set_key(text, key, value)
        for key in current:
            if key not in proj:
                text, _ = jsonc.remove_key(text, key)
    else:
        text = json.dumps(proj, indent=4, ensure_ascii=False) + "\n"
    jsonc.write_file(path, text)
    return os.path.abspath(path)


def get_project_info(proj: dict, path: Optional[str] = None) -> dict:
    """Summarise a workspace. Folder paths are resolved against the workspace file."""
    base = Path(path).resolve().parent if path else None
    folders = []
    for f in proj.get("folders", []):
        raw = f.get("path") or f.get("uri") or ""
        resolved = str((base / raw).resolve()) if base and raw and "://" not in raw else raw
        folders.append({"name": f.get("name"), "path": raw, "resolved": resolved,
                        "exists": os.path.isdir(resolved) if "://" not in raw else None})
    return {
        "path": os.path.abspath(path) if path else None,
        "name": Path(path).name.removesuffix(".code-workspace") if path else None,
        "folders": folders,
        "folder_count": len(folders),
        "settings": proj.get("settings", {}),
        "recommendations": (proj.get("extensions") or {}).get("recommendations", []),
        "has_tasks": bool(proj.get("tasks")),
        "has_launch": bool(proj.get("launch")),
    }


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def add_folder(proj: dict, path: str, name: Optional[str] = None) -> dict:
    """Add a folder. ``path`` is stored as given: relative paths are relative to the workspace file."""
    path = path.replace("\\", "/")
    for f in proj.setdefault("folders", []):
        if _same(f.get("path", ""), path):
            raise RuntimeError(f"Folder is already in the workspace: {path}")
    entry = {"path": path}
    if name:
        entry["name"] = name
    proj["folders"].append(entry)
    return entry


def remove_folder(proj: dict, path_or_name: str) -> dict:
    """Remove a folder by path or by display name."""
    for i, f in enumerate(proj.get("folders", [])):
        if f.get("name") == path_or_name or _same(f.get("path", ""), path_or_name):
            return proj["folders"].pop(i)
    raise RuntimeError(f"No such folder in the workspace: {path_or_name}")


def set_setting(proj: dict, key: str, value: Any) -> None:
    """Set a workspace-level setting."""
    proj.setdefault("settings", {})[key] = value


def unset_setting(proj: dict, key: str) -> bool:
    """Remove a workspace-level setting. Returns whether it existed."""
    return proj.get("settings", {}).pop(key, _MISSING) is not _MISSING


def recommend(proj: dict, ext_id: str) -> list[str]:
    """Add an extension id to the workspace recommendations."""
    rec = proj.setdefault("extensions", {}).setdefault("recommendations", [])
    if ext_id not in rec:
        rec.append(ext_id)
    return rec


def list_profiles(user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None) -> list[dict]:
    """Profiles: the default one plus those listed in the application storage.

    A profile can borrow resources from Default (``useDefaultFlags`` in storage.json, e.g.
    the built-in "Agents" profile borrows all of them). ``settings``/``keybindings`` are the
    files that profile really uses, and ``own_extensions`` says whether it keeps its own
    extension set (then an extension must be installed with ``--profile``)."""
    p = paths(user_data_dir, extensions_dir)
    user = p["settings"].parent
    profiles = [{"name": "Default", "location": None, "shares_default": [], "own_extensions": True,
                 "settings": str(p["settings"]), "keybindings": str(p["keybindings"])}]
    try:
        storage = json.loads(p["storage"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return profiles
    for entry in storage.get("userDataProfiles") or []:
        loc = entry.get("location")
        if not loc:
            continue
        flags = entry.get("useDefaultFlags") or {}
        shared = sorted(k for k, v in flags.items() if v)
        own = user / "profiles" / loc
        profiles.append({
            "name": entry.get("name"), "location": loc, "shares_default": shared,
            "own_extensions": not flags.get("extensions"),
            "settings": str(p["settings"] if flags.get("settings") else own / "settings.json"),
            "keybindings": str(p["keybindings"] if flags.get("keybindings") else own / "keybindings.json"),
        })
    return profiles


_MISSING = object()
