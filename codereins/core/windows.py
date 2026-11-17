"""Choosing which running VS Code window a command talks to."""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Iterable, Optional

from codereins.utils import procs
from codereins.utils import vscode_backend as backend
from codereins.utils.bridge_client import BridgeError, Transport, live_windows

NO_WINDOW_HELP = (
    "No VS Code window is reachable. Run `codereins bridge install` (add --app NAME, "
    "or --all, for an editor built on VS Code), then open a new window (or reload an open one)."
)


def for_app(windows: list[dict], app_name: Optional[str] = None, *, strict: bool = True) -> list[dict]:
    """Keep the windows of one editor. ``app_name`` is its ``nameLong`` (default: the editor
    chosen with --app); with no editor chosen every window is kept. ``strict`` raises when
    only other editors' windows are reachable, naming them."""
    name = app_name if app_name is not None else backend.app_name()
    if not name:
        return windows
    kept = [w for w in windows if (w.get("appName") or "").lower() == name.lower()]
    if strict and windows and not kept:
        others = sorted({w.get("appName") or "?" for w in windows})
        raise BridgeError("no_windows", f"No {name} window is reachable ({len(windows)} window(s) of "
                                        f"{', '.join(others)} are). {NO_WINDOW_HELP}")
    return kept


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _contains(window: dict, path: str) -> int:
    """Length of the longest workspace root of ``window`` that holds ``path``, or -1."""
    target = _norm(path)
    best = -1
    roots = list(window.get("folders") or [])
    if window.get("workspaceFile"):
        roots.append(window["workspaceFile"])
    for root in roots:
        if "://" in root:
            continue
        n = _norm(root)
        if target == n or target.startswith(n.rstrip(os.sep) + os.sep):
            best = max(best, len(n))
    return best


def describe(window: dict) -> dict:
    """The public view of a registry entry (no token, no endpoint)."""
    return {
        "id": window.get("id"),
        "app": window.get("appName"),
        "name": window.get("name"),
        "folders": window.get("folders") or [],
        "workspaceFile": window.get("workspaceFile"),
        "focused": bool(window.get("focused")),
        "vscodeVersion": window.get("vscodeVersion"),
        "bridgeVersion": window.get("bridgeVersion"),
    }


def select(windows: list[dict], selector: Optional[str] = None, preferred: Optional[str] = None,
           cwd: Optional[str] = None) -> dict:
    """Pick a window.

    Order: an explicit ``selector`` (id, ``focused``, a path inside the window's
    workspace, or the window name); else the session's ``preferred`` window; else the
    window whose workspace holds ``cwd``; else the most recently focused one.
    ``windows`` must already be sorted newest focus first.
    """
    if not windows:
        raise BridgeError("no_windows", NO_WINDOW_HELP)
    if selector:
        if selector in ("focused", "last"):
            return windows[0]
        for w in windows:
            if str(w.get("id")) == str(selector):
                return w
        scored = [(_contains(w, selector), w) for w in windows]
        best = max(scored, key=lambda s: s[0])
        if best[0] >= 0:
            return best[1]
        for w in windows:
            if w.get("name") and w["name"].lower() == selector.lower():
                return w
        raise BridgeError("no_such_window", f"No VS Code window matches {selector!r}. Run `window list`.")
    if preferred:
        for w in windows:
            if str(w.get("id")) == str(preferred):
                return w
    if cwd:
        scored = [(_contains(w, cwd), w) for w in windows]
        best = max(scored, key=lambda s: s[0])
        if best[0] >= 0:
            return best[1]
    return windows[0]


def resolve(selector: Optional[str] = None, preferred: Optional[str] = None,
            transport: Optional[Transport] = None, hint: Optional[str] = None) -> dict:
    """Return the live window a command should use. ``hint`` is a path that, like the
    current directory, breaks ties when nothing was selected."""
    return select(for_app(live_windows(transport=transport)), selector, preferred, hint or os.getcwd())


# ── the window that hosts this CLI ───────────────────────────────────────
# An agent running inside an editor window (Claude Code in its panel, say) descends from
# that window's extension host, whose pid the bridge registers. Ending that host (reload,
# close, quit, or a folder change that restarts it) ends the agent's own session.

# Command ids that end the target window's extension host, or the whole editor. Matched by
# pattern, because extensions add their own (antigravity.reloadWindow, python.reloadVSCode).
_ENDS_WINDOW = re.compile(r"reload(window|vscode)|restartextensionhost|closewindow|closefolder"
                          r"|reopen(folder|in|locally|workspace)", re.I)
_ENDS_APP = re.compile(r"(^|\.)quit$|^update\.restart", re.I)


def _pid(window: dict) -> Optional[int]:
    try:
        return int(window.get("pid") or window.get("id"))
    except (TypeError, ValueError):
        return None


def hosting(windows: Iterable[dict], ancestors: Optional[Iterable[int]] = None) -> Optional[dict]:
    """The window whose extension host is an ancestor of this process, if any."""
    anc = set(procs.ancestors() if ancestors is None else ancestors)
    return next((w for w in windows if _pid(w) in anc), None)


def refuse_if_hosting(window: dict, action: str, reach: str = "window", *,
                      ancestors: Optional[Iterable[int]] = None,
                      parent_of: Optional[Callable[[int], Optional[int]]] = None) -> None:
    """Raise ``hosting_window`` when ``action`` on ``window`` would end this CLI's own host.

    ``reach`` "window": the action ends that window's extension host. "app": it ends the
    whole editor, so any window of the editor process that runs the host is refused too.
    """
    anc = set(procs.ancestors() if ancestors is None else ancestors)
    pid = _pid(window)
    if pid is None:
        return
    if pid in anc:
        why = "this command runs inside it (its extension host is an ancestor of this process)"
    elif reach == "app" and (parent_of or procs.parent)(pid) in anc - {None}:
        why = "it belongs to the editor process this command runs inside"
    else:
        return
    raise BridgeError("hosting_window",
                      f"Refused to {action} window {window.get('id')} ({window.get('name') or 'no folder'}): {why}, "
                      "so it would end the session that started this command, such as an agent in that "
                      "window. Do it by hand in the editor, or target another window with --window.")


def command_reach(command_id: str, args: Iterable[Any] = ()) -> Optional[str]:
    """"window" if the command ends the target window's extension host, "app" if it ends
    the whole editor, else None."""
    if _ENDS_APP.search(command_id):
        return "app"
    if _ENDS_WINDOW.search(command_id):
        return "window"
    if command_id == "vscode.openFolder":
        args = list(args)
        opt = args[1] if len(args) > 1 else None
        new_window = opt is True or (isinstance(opt, dict) and bool(opt.get("forceNewWindow")))
        return None if new_window else "window"
    return None


def folder_add_restarts(window: dict) -> bool:
    """Whether adding a folder restarts the window's extension host: it does unless the
    window is already a workspace with a first folder (measured on VS Code 1.146)."""
    return not (window.get("workspaceFile") and window.get("folders"))


def folder_remove_restarts(window: dict, path: str) -> bool:
    """Whether removing ``path`` restarts the extension host: it does for the first folder."""
    roots = window.get("folders") or []
    if not roots:
        return False
    if "://" in roots[0] or "://" in path:  # a remote folder is registered as its URI
        return roots[0].rstrip("/").lower() == path.rstrip("/").lower()
    return _norm(roots[0]) == _norm(path)
