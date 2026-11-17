"""Session state.

Two kinds of state live here:

* in memory: the loaded project (workspace file) with snapshot / undo / redo, used by
  the REPL and by ``--project`` one-shot commands (auto-saved on exit);
* on disk in ``<home>/session.json``: the selected window, a history of command names,
  and a stack of file backups, so ``session undo`` works across separate invocations.

History stores command names only, never arguments: arguments can carry text typed into
a terminal.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from codereins.core import project as proj_mod
from codereins.utils.bridge_client import home

MAX_UNDO = 50
MAX_HISTORY = 200
MAX_BACKUPS = 50


def _locked_save_json(path: str, data: dict, **dump_kwargs) -> None:
    """Write JSON with exclusive file locking (truncate inside the lock)."""
    try:
        f = open(path, "r+", encoding="utf-8")
    except FileNotFoundError:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        f = open(path, "w", encoding="utf-8")
    with f:
        locked = ""
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            locked = "fcntl"
        except ImportError:
            try:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                locked = "msvcrt"
            except (ImportError, OSError):
                pass
        except OSError:
            pass
        try:
            f.seek(0)
            f.truncate()
            json.dump(data, f, **dump_kwargs)
            f.flush()
        finally:
            if locked == "fcntl":
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            elif locked == "msvcrt":
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    """The CLI's state for one process, backed by ``session.json``."""

    def __init__(self, state_path: Optional[str] = None):
        self.state_path = Path(state_path) if state_path else home() / "session.json"
        self.project: Optional[dict] = None
        self.project_path: Optional[str] = None
        self._modified = False
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self.state = self._load_state()

    # ── persistent state ────────────────────────────────────────────────
    def _load_state(self) -> dict:
        base: dict = {"window": None, "history": [], "backups": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return base
        if isinstance(data, dict):
            base.update({k: data[k] for k in base if k in data})
        return base

    def save_state(self) -> None:
        """Persist the window selection, history and backup stack."""
        _locked_save_json(str(self.state_path), self.state, indent=2)

    def set_window(self, window_id: Optional[str]) -> None:
        """Remember which window later commands target."""
        self.state["window"] = window_id
        self.save_state()

    def record(self, command: str) -> None:
        """Append a command name to the history."""
        self.state["history"].append({"at": _now(), "command": command})
        del self.state["history"][:-MAX_HISTORY]
        self.save_state()

    # ── project with undo / redo ────────────────────────────────────────
    def set_project(self, proj: dict, path: Optional[str]) -> None:
        """Load a project into the session."""
        self.project, self.project_path = proj, path
        self._modified = False
        self._undo.clear()
        self._redo.clear()

    def has_project(self) -> bool:
        """Whether a project is loaded."""
        return self.project is not None

    def get_project(self) -> dict:
        """Return the loaded project or raise."""
        if self.project is None:
            raise RuntimeError("No project loaded. Pass --project <file.code-workspace> or run `project new`.")
        return self.project

    def snapshot(self, description: str = "") -> None:
        """Record the project before a mutation."""
        self._undo.append({"description": description, "project": copy.deepcopy(self.get_project())})
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self._modified = True

    def undo(self) -> str:
        """Undo the last project mutation. Returns its description."""
        if not self._undo:
            raise RuntimeError("Nothing to undo")
        entry = self._undo.pop()
        self._redo.append({"description": entry["description"], "project": copy.deepcopy(self.get_project())})
        self.project = entry["project"]
        self._modified = True
        return entry["description"]

    def redo(self) -> str:
        """Redo the last undone project mutation. Returns its description."""
        if not self._redo:
            raise RuntimeError("Nothing to redo")
        entry = self._redo.pop()
        self._undo.append({"description": entry["description"], "project": copy.deepcopy(self.get_project())})
        self.project = entry["project"]
        self._modified = True
        return entry["description"]

    def save_session(self) -> Optional[str]:
        """Write the loaded project to its file (after backing the file up)."""
        if self.project is None or not self.project_path:
            return None
        self.backup_file(self.project_path)
        out = proj_mod.save_project(self.project, self.project_path)
        self._modified = False
        return out

    # ── file backups: undo across invocations ───────────────────────────
    def backup_file(self, path: str) -> Optional[str]:
        """Copy ``path`` aside before it is rewritten. Returns the backup path."""
        path = os.path.abspath(path)
        backup = None
        if os.path.isfile(path):
            folder = home() / "backups"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = str(folder / f"{stamp}-{os.path.basename(path)}")
            shutil.copy2(path, backup)
        self.state["backups"].append({"at": _now(), "file": path, "backup": backup})
        for old in self.state["backups"][:-MAX_BACKUPS]:
            if old.get("backup"):
                try:
                    os.unlink(old["backup"])
                except OSError:
                    pass
        del self.state["backups"][:-MAX_BACKUPS]
        self.save_state()
        return backup

    def restore_last(self) -> dict:
        """Put back the most recently backed-up file."""
        if not self.state["backups"]:
            raise RuntimeError("Nothing to undo")
        entry = self.state["backups"].pop()
        if entry.get("backup"):
            shutil.copy2(entry["backup"], entry["file"])
            try:
                os.unlink(entry["backup"])
            except OSError:
                pass
            action = "restored"
        else:
            try:
                os.unlink(entry["file"])
            except OSError:
                pass
            action = "removed (the file did not exist before)"
        self.save_state()
        return {"file": entry["file"], "action": action}

    def status(self) -> dict:
        """A summary for ``session status``."""
        return {
            "window": self.state["window"],
            "project": self.project_path,
            "modified": self._modified,
            "undo_depth": len(self._undo),
            "redo_depth": len(self._redo),
            "file_backups": len(self.state["backups"]),
            "history_entries": len(self.state["history"]),
            "state_file": str(self.state_path),
        }
