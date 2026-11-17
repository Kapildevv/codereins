"""Export: write what the real VS Code reports to a file.

Every preset gets its data from VS Code itself, either a live window (through the
bridge) or the ``code`` command line, never from a reimplementation.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from codereins.core import settings as settings_mod
from codereins.utils import vscode_backend as backend
from codereins.utils.bridge_client import BridgeClient

EXPORT_PRESETS = {
    "diagnostics-json": {"format": "json", "needs": "window",
                         "description": "Every problem VS Code reports (errors, warnings, hints) as JSON"},
    "diagnostics-md": {"format": "markdown", "needs": "window",
                       "description": "The same problems as a Markdown report grouped by file"},
    "state-json": {"format": "json", "needs": "window",
                   "description": "Window snapshot: workspace, tabs, active editor, terminals, problem counts"},
    "commands-json": {"format": "json", "needs": "window",
                      "description": "Every command id the window can run"},
    "extensions-txt": {"format": "text", "needs": "code",
                       "description": "Installed extensions as id@version, one per line"},
    "setup-json": {"format": "json", "needs": "code",
                   "description": "User settings, keybindings and installed extensions in one file"},
}


def _markdown(diags: list[dict]) -> str:
    counts = Counter(d["severity"] for d in diags)
    lines = ["# VS Code problems", "",
             f"{len(diags)} problem(s): " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"), ""]
    by_file: dict[str, list[dict]] = {}
    for d in diags:
        by_file.setdefault(d["path"], []).append(d)
    for path in sorted(by_file):
        lines += [f"## {path}", "", "| Severity | Line | Message | Source |", "|---|---|---|---|"]
        for d in sorted(by_file[path], key=lambda x: (x["range"]["start"]["line"], x["range"]["start"]["column"])):
            msg = str(d["message"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {d['severity']} | {d['range']['start']['line']} | {msg} | {d.get('source') or ''} |")
        lines.append("")
    return "\n".join(lines)


def render(preset: str, output: str, *, client: Optional[BridgeClient] = None,
           user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None,
           overwrite: bool = False) -> dict:
    """Produce the export for ``preset`` at ``output`` and describe the file written."""
    if preset not in EXPORT_PRESETS:
        raise RuntimeError(f"Unknown preset {preset!r}. Choose one of: {', '.join(EXPORT_PRESETS)}")
    spec = EXPORT_PRESETS[preset]
    if os.path.exists(output) and not overwrite:
        raise RuntimeError(f"Output exists (pass --overwrite): {output}")
    if spec["needs"] == "window" and client is None:
        raise RuntimeError(f"Preset {preset} needs a running VS Code window")

    items = 0
    if preset in ("diagnostics-json", "diagnostics-md"):
        diags = client.call("lang.diagnostics")
        items = len(diags)
        text = _markdown(diags) if preset == "diagnostics-md" else json.dumps(diags, indent=2, ensure_ascii=False)
        method = "bridge:lang.diagnostics"
    elif preset == "state-json":
        diags = client.call("lang.diagnostics")
        data = {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "info": client.call("info"),
            "tabs": client.call("tabs.list"),
            "editor": client.call("editor.state"),
            "terminals": client.call("terminal.list"),
            "problems": dict(Counter(d["severity"] for d in diags)),
        }
        items = sum(len(g["tabs"]) for g in data["tabs"])
        text = json.dumps(data, indent=2, ensure_ascii=False)
        method = "bridge:info+tabs.list+editor.state+terminal.list+lang.diagnostics"
    elif preset == "commands-json":
        commands = client.call("commands.list")
        items = len(commands)
        text = json.dumps(commands, indent=2)
        method = "bridge:commands.list"
    elif preset == "extensions-txt":
        exts = backend.list_extensions(user_data_dir, extensions_dir)
        items = len(exts)
        text = "".join(f"{e['id']}@{e['version']}\n" for e in exts)
        method = "code --list-extensions --show-versions"
    else:
        p = backend.paths(user_data_dir, extensions_dir)
        exts = backend.list_extensions(user_data_dir, extensions_dir)
        data = {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "vscode": backend.version(),
            "settings": settings_mod.read(p["settings"]),
            "keybindings": settings_mod.keybindings(p["keybindings"]),
            "extensions": exts,
        }
        items = len(exts)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        method = "code --version + --list-extensions, settings.json, keybindings.json"

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return {"output": os.path.abspath(output), "preset": preset, "format": spec["format"],
            "file_size": os.path.getsize(output), "items": items, "method": method}
