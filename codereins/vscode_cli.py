"""codereins: drive Visual Studio Code from the command line.

Three backends, all of them the real VS Code:
  * the ``code`` launcher (open, diff, extensions),
  * VS Code's own JSONC files (settings, keybindings, .code-workspace),
  * a bridge extension inside each window (everything live: editors, language features,
    terminals, tasks, debugging, any command id).

Positions are 1-based LINE:COLUMN everywhere, like the editor UI and ``code --goto``.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Optional

import click

from codereins import __version__
from codereins.core import bridge as bridge_mod
from codereins.core import export as export_mod
from codereins.core import project as proj_mod
from codereins.core import settings as settings_mod
from codereins.core import windows as windows_mod
from codereins.core.session import Session
from codereins.utils import vscode_backend as backend
from codereins.utils.bridge_client import BridgeClient, BridgeError, home, live_windows
from codereins.utils.jsonc import JsoncError

_session: Optional[Session] = None
_repl_mode = False
_mutated = False
_opts: dict[str, Any] = {"json": False, "window": None, "app": None, "user_data_dir": None, "extensions_dir": None,
                         "dry_run": False, "timeout": 30.0}
END_OF_LINE = 1_000_000  # VS Code clamps a column past the end of a line to the line end


# ── shared helpers ───────────────────────────────────────────────────────
def get_session() -> Session:
    """The process-wide session."""
    global _session
    if _session is None:
        _session = Session()
    return _session


def client() -> BridgeClient:
    """A client for the window this invocation targets."""
    sess = get_session()
    window = windows_mod.resolve(_opts["window"], sess.state.get("window"), hint=sess.project_path)
    return BridgeClient(window, timeout=_opts["timeout"], dry_run=_opts["dry_run"])


def live(method: str, params: Optional[dict] = None, *, mutating: bool = False,
         timeout: Optional[float] = None, refuse: Optional[str] = None, reach: str = "window") -> Any:
    """Call a bridge method in the target window. ``refuse`` names an action that must not
    reach the window running this CLI (see ``windows.refuse_if_hosting``); it is checked
    under --dry-run too, so a dry run reports the refusal."""
    global _mutated
    c = client()
    if refuse:
        windows_mod.refuse_if_hosting(c.window, refuse, reach)
    if mutating:
        _mutated = True
    return c.call(method, params, mutating=mutating, timeout=timeout)


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    s = s.replace("\n", " ")
    return s if len(s) <= 70 else s[:67] + "..."


def _human(data: Any, pad: str = "") -> None:
    if data is None:
        return
    if isinstance(data, list):
        if not data:
            click.echo(pad + "(none)")
        elif all(isinstance(x, dict) for x in data):
            cols = list(data[0])[:7]
            rows = [[_cell(x.get(c)) for c in cols] for x in data]
            widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
            for r in [cols] + rows:
                click.echo(pad + "  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())
        else:
            for x in data:
                click.echo(pad + _cell(x))
    elif isinstance(data, dict):
        for k, v in data.items():
            nested = (isinstance(v, dict) and v) or (isinstance(v, list) and v and all(isinstance(x, dict) for x in v))
            if nested:
                click.echo(f"{pad}{k}:")
                _human(v, pad + "  ")
            else:
                click.echo(f"{pad}{k}: {_cell(v)}")
    else:
        click.echo(pad + str(data))


def output(data: Any, plain: Optional[str] = None) -> None:
    """Print a result: JSON with --json, else ``plain`` if given, else a readable layout."""
    if _opts["json"]:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif plain is not None:
        click.echo(plain)
    else:
        _human(data)


def _fail(kind: str, message: str) -> None:
    if _opts["json"]:
        click.echo(json.dumps({"error": message, "type": kind}))
    else:
        click.echo(f"Error: {message}", err=True)
    if not _repl_mode:
        sys.exit(1)


def handle_error(func):
    """Turn backend failures into one clear message (JSON under --json) and exit code 1."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _mutated
        _mutated = False
        try:
            result = func(*args, **kwargs)
        except BridgeError as e:
            return _fail(e.code, e.message)
        except JsoncError as e:
            return _fail("bad_jsonc", str(e))
        except subprocess.TimeoutExpired as e:
            return _fail("timeout", f"VS Code launcher did not finish within {e.timeout:g}s")
        except (RuntimeError, OSError, ValueError) as e:
            return _fail(type(e).__name__, str(e))
        if _mutated and not _opts["dry_run"]:
            ctx = click.get_current_context(silent=True)
            if ctx is not None:
                try:
                    get_session().record(" ".join(ctx.command_path.split()[1:]))
                except OSError:
                    pass
        return result
    return wrapper


def target(path: str) -> str:
    """Absolute path for a file argument; URIs pass through."""
    return path if "://" in path or path.startswith("untitled:") else os.path.abspath(path)


def opt_target(path: Optional[str]) -> Optional[str]:
    return target(path) if path else None


_LOCATION = re.compile(r"^(.+?)(?::(\d+)(?::(\d+))?)?$")
_RANGE = re.compile(r"^(\d+)(?::(\d+))?(?:-(\d+)(?::(\d+))?)?$")
_POSITION = re.compile(r"^(\d+)(?::(\d+))?$")


def parse_location(text: str) -> tuple[str, Optional[int], Optional[int]]:
    """Split ``FILE[:LINE[:COLUMN]]`` (a Windows drive colon is not a separator)."""
    m = _LOCATION.match(text)
    path, line, col = m.group(1), m.group(2), m.group(3)
    return path, int(line) if line else None, int(col) if col else None


def parse_range(text: str) -> list[int]:
    """``L`` (whole line), ``L1-L2`` (whole lines), ``L:C`` (a point) or ``L1:C1-L2:C2``."""
    m = _RANGE.match(text)
    if not m:
        raise click.UsageError(f"Not a range: {text!r}. Use L, L1-L2, L:C or L1:C1-L2:C2")
    l1, c1, l2, c2 = m.group(1), m.group(2), m.group(3), m.group(4)
    if l2 is None:
        return [int(l1), int(c1), int(l1), int(c1)] if c1 else [int(l1), 1, int(l1), END_OF_LINE]
    return [int(l1), int(c1) if c1 else 1, int(l2), int(c2) if c2 else END_OF_LINE]


def parse_position(text: str) -> list[int]:
    """``L`` or ``L:C``."""
    m = _POSITION.match(text)
    if not m:
        raise click.UsageError(f"Not a position: {text!r}. Use LINE or LINE:COLUMN")
    return [int(m.group(1)), int(m.group(2)) if m.group(2) else 1]


def parse_value(text: str) -> Any:
    """A JSON value when ``text`` parses as one, else the string itself."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _at(location: str) -> dict:
    path, line, col = parse_location(location)
    if line is None:
        raise click.UsageError("Expected FILE:LINE[:COLUMN]")
    return {"path": target(path), "line": line, "column": col or 1}


def _dirs() -> dict:
    return {"user_data_dir": _opts["user_data_dir"], "extensions_dir": _opts["extensions_dir"]}


def _mark() -> None:
    global _mutated
    _mutated = True


def _text_arg(text: Optional[str], from_file: Optional[str]) -> str:
    if from_file == "-":
        return sys.stdin.read()
    if from_file:
        with open(from_file, "r", encoding="utf-8", newline="") as f:
            return f.read()
    if text is None:
        raise click.UsageError("Pass the text, or --from FILE (use - for stdin)")
    return text


# ── root ─────────────────────────────────────────────────────────────────
@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "use_json", is_flag=True, help="Output as JSON")
@click.option("--project", "project_path", type=str, default=None,
              help="Workspace file (.code-workspace) the project commands operate on")
@click.option("--window", "window", type=str, default=None,
              help="Target window: id, 'focused', a path inside its workspace, or its name")
@click.option("--app", "app_name", type=click.Choice(list(backend.APPS)), default=None, envvar=backend.APP_ENV,
              help="Editor built on VS Code to drive (default: the first installed, VS Code first). "
                   "Also picks whose windows window commands see. Env: CODEREINS_APP")
@click.option("--user-data-dir", type=str, default=None, help="VS Code user data folder (default: the standard one)")
@click.option("--extensions-dir", type=str, default=None, help="VS Code extensions folder (default: the standard one)")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Describe changes without making them (nothing is written or sent)")
@click.option("--timeout", type=float, default=30.0, show_default=True, help="Seconds to wait for a window to answer")
@click.version_option(__version__, prog_name="codereins")
@click.pass_context
def cli(ctx, use_json, project_path, window, app_name, user_data_dir, extensions_dir, dry_run, timeout):
    """Drive Visual Studio Code (or an editor built on it, see --app): windows, files, editors,
    language features, terminals, tasks, debugging, extensions and settings. Run with no
    command for the REPL."""
    _opts.update(json=use_json, window=window, app=app_name, user_data_dir=user_data_dir,
                 extensions_dir=extensions_dir, dry_run=dry_run, timeout=timeout)
    backend.select_app(app_name)
    sess = get_session()
    if project_path and not (_repl_mode and sess.has_project()):
        try:
            if os.path.isfile(project_path):
                sess.set_project(proj_mod.open_project(project_path), project_path)
            else:
                sess.project_path = project_path
        except (RuntimeError, JsoncError, OSError) as e:
            _fail(type(e).__name__, str(e))
            return
    if ctx.invoked_subcommand is None and not _repl_mode:
        ctx.invoke(repl)


@cli.result_callback()
def auto_save_on_exit(result, **kwargs):
    """Auto-save the project after a one-shot command that changed it."""
    if _repl_mode or _opts["dry_run"]:
        return
    sess = get_session()
    if sess.has_project() and sess._modified and sess.project_path:
        try:
            sess.save_session()
        except (RuntimeError, OSError, JsoncError) as e:
            click.echo(f"Warning: auto-save failed: {e}", err=True)


# ── app ──────────────────────────────────────────────────────────────────
@cli.group()
def app():
    """The VS Code installation: version, paths, process status."""


@app.command("list")
@handle_error
def app_list():
    """Editors built on VS Code that this CLI knows, whether each is installed, and how many of
    its windows the bridge reaches. Reads files only; launches nothing."""
    windows = live_windows()
    rows = []
    for name in backend.APPS:
        info = backend.app_info(name)
        if info["installed"]:
            info["profiles"] = [p["name"] for p in proj_mod.list_profiles(info["user_data_dir"], info["extensions_dir"])]
            want = (info.get("name") or "").lower()
            info["live_windows"] = sum(1 for w in windows if want and (w.get("appName") or "").lower() == want)
        rows.append(info)
    output(rows)


@app.command("version")
@handle_error
def app_version():
    """Show the installed VS Code version."""
    output({**backend.version(), "cli": __version__})


@app.command("paths")
@handle_error
def app_paths():
    """Show where VS Code keeps settings, keybindings, extensions and state."""
    data = {k: str(v) for k, v in backend.paths(**_dirs()).items()}
    data.update(binary=backend.find_code(), cli_home=str(home()))
    output(data)


@app.command("status")
@handle_error
def app_status():
    """Process and workspace diagnostics of the running instance (`code --status`)."""
    text = backend.run_code(["--status"], user_data_dir=_opts["user_data_dir"], timeout=60, check=False).stdout
    output({"status": text.strip()}, plain=text.strip())


# ── window ───────────────────────────────────────────────────────────────
@cli.group()
def window():
    """Running VS Code windows: list, choose, open, focus, reload, close."""


@window.command("list")
@handle_error
def window_list():
    """List windows the bridge is running in."""
    current = get_session().state.get("window")
    rows = [{**windows_mod.describe(w), "selected": str(w.get("id")) == str(current)}
            for w in windows_mod.for_app(live_windows(), strict=False)]
    output(rows)


@window.command("use")
@click.argument("selector")
@handle_error
def window_use(selector):
    """Make later commands target a window (id, 'focused', a path, or its name)."""
    w = windows_mod.select(windows_mod.for_app(live_windows()), selector)
    get_session().set_window(str(w["id"]))
    output({"selected": windows_mod.describe(w)})


@window.command("current")
@handle_error
def window_current():
    """Show which window commands target right now."""
    output(windows_mod.describe(client().window))


@window.command("info")
@handle_error
def window_info():
    """Details of the target window: version, workspace, theme, trust."""
    output(live("info"))


@window.command("open")
@click.argument("path", required=False)
@click.option("--new", "new_window", is_flag=True, help="Force a new window")
@click.option("--reuse", is_flag=True, help="Reuse the last active window")
@click.option("--add", is_flag=True, help="Add the folder to the last active window")
@click.option("--wait", "wait_s", type=float, default=0, help="Seconds to wait until the window answers")
@click.option("--code-arg", multiple=True, help="Extra argument for the `code` launcher (repeatable)")
@handle_error
def window_open(path, new_window, reuse, add, wait_s, code_arg):
    """Open a folder, workspace file or file in VS Code."""
    args = list(code_arg)
    if new_window:
        args.append("--new-window")
    if reuse:
        args.append("--reuse-window")
    if add:
        args.append("--add")
    full = os.path.abspath(path) if path else None
    if full:
        args.append(full)
    if (reuse or add) and full and (os.path.isdir(full) or full.lower().endswith(".code-workspace")):
        # The launcher puts it into the editor's last active window; for an agent that is
        # usually its own window, and a new folder there restarts the extension host.
        host = windows_mod.hosting(live_windows())
        if (host and (host.get("appName") or "").lower() == (backend.launcher_name() or "").lower()
                and (reuse or windows_mod.folder_add_restarts(host))):
            raise BridgeError("hosting_window",
                              f"Refused to open {full} with --{'reuse' if reuse else 'add'}: {host.get('appName')} "
                              f"puts it into its last active window, which may be window {host.get('id')} "
                              f"({host.get('name') or 'no folder'}), the one running this command; that restarts "
                              "its extension host and ends this session. Use --new.")
    result: dict[str, Any] = {"opened": full, "args": args}
    if _opts["dry_run"]:
        output({**result, "dry_run": True})
        return
    backend.run_code(args, **_dirs(), capture=False, timeout=60)
    if wait_s and full:
        deadline = time.monotonic() + wait_s
        found = None
        while time.monotonic() < deadline and found is None:
            try:
                found = windows_mod.select(windows_mod.for_app(live_windows()), full)
            except BridgeError:
                time.sleep(0.5)
        if found is None:
            raise BridgeError("timeout", f"VS Code opened {full} but no bridge answered within {wait_s:g}s. "
                                         "Is the bridge installed? Run `bridge status`.")
        result["window"] = windows_mod.describe(found)
    output(result)


@window.command("focus")
@handle_error
def window_focus():
    """Bring the target window to the front."""
    output(live("window.focus", mutating=True))


@window.command("reload")
@handle_error
def window_reload():
    """Reload the target window (restarts its extensions, including the bridge). Refused for
    the window this command runs inside."""
    output(live("window.reload", mutating=True, refuse="reload"))


@window.command("close")
@handle_error
def window_close():
    """Close the target window. Refused for the window this command runs inside."""
    output(live("window.close", mutating=True, refuse="close"))


@window.command("recent")
@click.option("--limit", type=int, default=20, show_default=True)
@handle_error
def window_recent(limit):
    """Recently opened folders, workspaces and files, as the window's File > Open Recent lists them."""
    res = live("commands.execute", {"command": "_workbench.getRecentlyOpened"}) or {}
    rows = []
    for w in res.get("workspaces") or []:
        if w.get("folderUri"):
            rows.append({"kind": "folder", "path": w["folderUri"]})
        elif w.get("workspace"):
            rows.append({"kind": "workspace", "path": w["workspace"].get("configPath")})
    rows += [{"kind": "file", "path": f["fileUri"]} for f in res.get("files") or [] if f.get("fileUri")]
    output(rows[:limit])


# ── project (workspace file) ─────────────────────────────────────────────
@cli.group()
def project():
    """Workspace files (.code-workspace): folders, workspace settings, recommendations."""


@project.command("new")
@click.option("-o", "--output", "out", type=str, default=None, help="File to create (default: --project)")
@click.option("--folder", multiple=True, help="Folder to include (repeatable)")
@click.option("--force", is_flag=True, help="Overwrite an existing file")
@handle_error
def project_new(out, folder, force):
    """Create a workspace file."""
    sess = get_session()
    path = out or sess.project_path
    if not path:
        raise click.UsageError("Pass -o FILE.code-workspace (or --project)")
    if os.path.exists(path) and not force:
        raise RuntimeError(f"File exists (pass --force): {path}")
    proj = proj_mod.create_project(list(folder))
    if not _opts["dry_run"]:
        _mark()
        if os.path.exists(path):
            sess.backup_file(path)
            os.unlink(path)
        proj_mod.save_project(proj, path)
        sess.set_project(proj, path)
    output({**proj_mod.get_project_info(proj, path), "dry_run": _opts["dry_run"]})


@project.command("info")
@handle_error
def project_info():
    """Show the workspace: folders, settings, recommendations."""
    sess = get_session()
    output(proj_mod.get_project_info(sess.get_project(), sess.project_path))


def _project_change(description: str, change) -> None:
    sess = get_session()
    proj = sess.get_project()
    was_modified = sess._modified
    sess.snapshot(description)
    try:
        change(proj)
    except RuntimeError:
        sess.undo()
        sess._redo.clear()
        sess._modified = was_modified
        raise
    _mark()
    output({**proj_mod.get_project_info(proj, sess.project_path), "change": description, "dry_run": _opts["dry_run"]})


@project.command("add-folder")
@click.argument("path")
@click.option("--name", type=str, default=None, help="Display name")
@handle_error
def project_add_folder(path, name):
    """Add a folder (relative paths are relative to the workspace file)."""
    _project_change(f"add folder {path}", lambda p: proj_mod.add_folder(p, path, name))


@project.command("remove-folder")
@click.argument("path_or_name")
@handle_error
def project_remove_folder(path_or_name):
    """Remove a folder by path or display name."""
    _project_change(f"remove folder {path_or_name}", lambda p: proj_mod.remove_folder(p, path_or_name))


@project.command("set")
@click.argument("key")
@click.argument("value")
@handle_error
def project_set(key, value):
    """Set a workspace-level setting (VALUE is JSON, or a plain string)."""
    _project_change(f"set {key}", lambda p: proj_mod.set_setting(p, key, parse_value(value)))


@project.command("unset")
@click.argument("key")
@handle_error
def project_unset(key):
    """Remove a workspace-level setting."""
    _project_change(f"unset {key}", lambda p: proj_mod.unset_setting(p, key))


@project.command("recommend")
@click.argument("extension_id")
@handle_error
def project_recommend(extension_id):
    """Add an extension to the workspace recommendations."""
    _project_change(f"recommend {extension_id}", lambda p: proj_mod.recommend(p, extension_id))


@project.command("save")
@handle_error
def project_save():
    """Write the loaded workspace to its file (the REPL does not auto-save)."""
    if _opts["dry_run"]:
        output({"saved": None, "dry_run": True})
        return
    output({"saved": get_session().save_session()})


@project.command("open")
@click.option("--new", "new_window", is_flag=True, help="Force a new window")
@handle_error
def project_open(new_window):
    """Open the workspace file in VS Code."""
    path = get_session().project_path
    if not path or not os.path.isfile(path):
        raise RuntimeError("No workspace file. Pass --project FILE.code-workspace")
    args = (["--new-window"] if new_window else []) + [os.path.abspath(path)]
    if not _opts["dry_run"]:
        backend.run_code(args, **_dirs(), capture=False, timeout=60)
    output({"opened": os.path.abspath(path), "dry_run": _opts["dry_run"]})


@project.command("profiles")
@handle_error
def project_profiles():
    """List VS Code profiles and their settings files."""
    output(proj_mod.list_profiles(_opts["user_data_dir"], _opts["extensions_dir"]))


# ── file ─────────────────────────────────────────────────────────────────
@cli.group()
def file():
    """Files and documents: open, read, write, save, close, create, rename, delete, diff."""


@file.command("open")
@click.argument("location")
@click.option("--preview", is_flag=True, help="Open as a preview tab")
@click.option("--keep-focus", is_flag=True, help="Do not move keyboard focus to the editor")
@handle_error
def file_open(location, preview, keep_focus):
    """Open FILE[:LINE[:COLUMN]] in an editor. Falls back to `code --goto` with no bridge."""
    path, line, col = parse_location(location)
    try:
        output(live("editor.open", {"path": target(path), "line": line, "column": col, "preview": preview or None,
                                    "preserveFocus": keep_focus or None}, mutating=True))
    except BridgeError as e:
        if e.code != "no_windows":
            raise
        spec = target(path) + (f":{line}" if line else "") + (f":{col}" if line and col else "")
        if not _opts["dry_run"]:
            backend.run_code(["--goto", spec], **_dirs(), capture=False, timeout=60)
        output({"opened": spec, "via": "code --goto", "dry_run": _opts["dry_run"]})


@file.command("new")
@click.option("--content", type=str, default="", help="Initial text")
@click.option("--language", type=str, default=None, help="Language id, e.g. python")
@handle_error
def file_new(content, language):
    """Open a new untitled document."""
    output(live("editor.new", {"content": content, "language": language}, mutating=True))


@file.command("list")
@handle_error
def file_list():
    """Documents VS Code has loaded (including unsaved ones)."""
    output(live("doc.list"))


@file.command("read")
@click.argument("path", required=False)
@click.option("--lines", type=str, default=None, help="Line range A-B")
@handle_error
def file_read(path, lines):
    """Print a document as VS Code sees it, unsaved changes included (default: active editor)."""
    params: dict[str, Any] = {"path": opt_target(path)}
    if lines:
        r = parse_range(lines)
        params.update(startLine=r[0], endLine=r[2])
    doc = live("doc.read", params)
    output(doc, plain=doc["text"])


@file.command("write")
@click.argument("path")
@click.argument("text", required=False)
@click.option("--from", "from_file", type=str, default=None, help="Read the text from a file (- for stdin)")
@click.option("--no-save", is_flag=True, help="Leave the document unsaved")
@handle_error
def file_write(path, text, from_file, no_save):
    """Replace a document's whole text through VS Code (undo history and save hooks apply)."""
    body = _text_arg(text, from_file)
    output(live("doc.edit", {"path": target(path), "edits": [{"all": True, "text": body}], "save": not no_save},
                mutating=True))


@file.command("save")
@click.argument("path", required=False)
@click.option("--all", "save_all", is_flag=True, help="Save every dirty file")
@handle_error
def file_save(path, save_all):
    """Save a document (default: active editor)."""
    output(live("doc.save", {"path": opt_target(path), "all": save_all or None}, mutating=True))


@file.command("close")
@click.argument("path", required=False)
@click.option("--all", "close_all", is_flag=True, help="Close every tab")
@handle_error
def file_close(path, close_all):
    """Close a tab (default: the active one). An unsaved tab makes VS Code ask the user."""
    output(live("tabs.close", {"path": opt_target(path), "all": close_all or None}, mutating=True))


@file.command("create")
@click.argument("path")
@click.option("--content", type=str, default=None, help="Initial text")
@click.option("--overwrite", is_flag=True)
@handle_error
def file_create(path, content, overwrite):
    """Create a file through VS Code."""
    op = {"create": target(path), "content": content, "overwrite": overwrite}
    output({**live("files.apply", {"ops": [op]}, mutating=True), "created": target(path)})


@file.command("rename")
@click.argument("old")
@click.argument("new")
@click.option("--overwrite", is_flag=True)
@handle_error
def file_rename(old, new, overwrite):
    """Rename or move a file through VS Code (open editors follow, import updates may run)."""
    op = {"rename": target(old), "to": target(new), "overwrite": overwrite}
    output({**live("files.apply", {"ops": [op]}, mutating=True), "renamed": target(new)})


@file.command("delete")
@click.argument("path")
@click.option("--recursive", is_flag=True, help="Delete a folder and its contents")
@handle_error
def file_delete(path, recursive):
    """Delete a file or folder through VS Code."""
    op = {"delete": target(path), "recursive": recursive}
    output({**live("files.apply", {"ops": [op]}, mutating=True), "deleted": target(path)})


@file.command("diff")
@click.argument("left")
@click.argument("right")
@handle_error
def file_diff(left, right):
    """Open two files side by side in the diff editor (`code --diff`)."""
    if not _opts["dry_run"]:
        backend.run_code(["--diff", os.path.abspath(left), os.path.abspath(right)], **_dirs(), capture=False, timeout=60)
    output({"diff": [os.path.abspath(left), os.path.abspath(right)], "dry_run": _opts["dry_run"]})


# ── editor ───────────────────────────────────────────────────────────────
@cli.group()
def editor():
    """The active editor: state, tabs, selection, edits, formatting."""


@editor.command("state")
@handle_error
def editor_state():
    """Active editor: file, language, dirty flag, selections, visible range."""
    output(live("editor.state"))


@editor.command("tabs")
@handle_error
def editor_tabs():
    """Every open tab, by editor group."""
    groups = live("tabs.list")
    if _opts["json"]:
        output(groups)
    else:
        _human([{"group": g["viewColumn"], **t} for g in groups for t in g["tabs"]])


@editor.command("select")
@click.argument("ranges", nargs=-1, required=True)
@handle_error
def editor_select(ranges):
    """Set the selection(s): L, L1-L2, L:C or L1:C1-L2:C2 (several for multi-cursor)."""
    output(live("editor.select", {"ranges": [parse_range(r) for r in ranges]}, mutating=True))


@editor.command("insert")
@click.argument("text", required=False)
@click.option("--at", "at", type=str, default=None, help="LINE[:COLUMN] (default: the cursor)")
@click.option("--end", is_flag=True, help="Append at the end of the document")
@click.option("--file", "path", type=str, default=None, help="Document to edit (default: active editor)")
@click.option("--from", "from_file", type=str, default=None, help="Read the text from a file (- for stdin)")
@click.option("--save", is_flag=True, help="Save after the edit")
@handle_error
def editor_insert(text, at, end, path, from_file, save):
    """Insert text at a position, at the cursor, or at the end."""
    body = _text_arg(text, from_file)
    if end:
        edit: dict[str, Any] = {"append": True, "text": body}
    else:
        if at:
            pos = parse_position(at)
        else:
            st = live("editor.state")
            if not st:
                raise BridgeError("no_active_editor", "No active editor; pass --at and --file")
            active = st["selections"][0]["active"]
            pos, path = [active["line"], active["column"]], path or st["uri"]
        edit = {"insert": pos, "text": body}
    output(live("doc.edit", {"path": opt_target(path), "edits": [edit], "save": save or None}, mutating=True))


@editor.command("replace")
@click.argument("range_")
@click.argument("text", required=False)
@click.option("--file", "path", type=str, default=None, help="Document to edit (default: active editor)")
@click.option("--from", "from_file", type=str, default=None, help="Read the text from a file (- for stdin)")
@click.option("--save", is_flag=True, help="Save after the edit")
@handle_error
def editor_replace(range_, text, path, from_file, save):
    """Replace a range (L, L1-L2 or L1:C1-L2:C2) with TEXT."""
    edit = {"range": parse_range(range_), "text": _text_arg(text, from_file)}
    output(live("doc.edit", {"path": opt_target(path), "edits": [edit], "save": save or None}, mutating=True))


@editor.command("sub")
@click.argument("find")
@click.argument("replacement")
@click.option("--regex", is_flag=True, help="FIND is a JavaScript regular expression ($1 works in the replacement)")
@click.option("--all", "all_", is_flag=True, help="Replace every match, not only the first")
@click.option("--ignore-case", is_flag=True)
@click.option("--file", "path", type=str, default=None, help="Document to edit (default: active editor)")
@click.option("--save", is_flag=True, help="Save after the edit")
@handle_error
def editor_sub(find, replacement, regex, all_, ignore_case, path, save):
    """Find and replace inside a document."""
    output(live("doc.replace", {"path": opt_target(path), "find": find, "replace": replacement, "regex": regex or None,
                                "all": all_ or None, "ignoreCase": ignore_case or None, "save": save or None},
                mutating=True))


@editor.command("format")
@click.option("--file", "path", type=str, default=None, help="Document to format (default: active editor)")
@click.option("--range", "range_", type=str, default=None, help="Format only this range")
@click.option("--save", is_flag=True, help="Save after formatting")
@handle_error
def editor_format(path, range_, save):
    """Format a document with the formatter VS Code has for its language."""
    output(live("lang.format", {"path": opt_target(path), "range": parse_range(range_) if range_ else None,
                                "save": save or None}, mutating=True))


@editor.command("language")
@click.argument("language_id")
@click.option("--file", "path", type=str, default=None, help="Document (default: active editor)")
@handle_error
def editor_language(language_id, path):
    """Change a document's language mode."""
    output(live("editor.setLanguage", {"path": opt_target(path), "languageId": language_id}, mutating=True))


@editor.command("undo")
@handle_error
def editor_undo():
    """Undo in the focused editor."""
    live("commands.execute", {"command": "undo"}, mutating=True)
    output(live("editor.state"))


@editor.command("redo")
@handle_error
def editor_redo():
    """Redo in the focused editor."""
    live("commands.execute", {"command": "redo"}, mutating=True)
    output(live("editor.state"))


# ── command ──────────────────────────────────────────────────────────────
@cli.group()
def command():
    """VS Code commands: list every command id, run any of them."""


@command.command("list")
@click.option("--filter", "filter_", type=str, default=None, help="Substring to match")
@click.option("--internal", is_flag=True, help="Include commands starting with an underscore")
@handle_error
def command_list(filter_, internal):
    """List command ids (several thousand; use --filter)."""
    output(live("commands.list", {"filter": filter_, "internal": internal or None}))


@command.command("run")
@click.argument("command_id")
@click.argument("args", nargs=-1)
@handle_error
def command_run(command_id, args):
    """Run a command by id. Each ARG is JSON (or a plain string).

    Typed arguments: {"$uri": "path"}, {"$pos": [line, col]}, {"$range": [l1, c1, l2, c2]}.
    Commands that reload, close or quit the window this command runs inside are refused.
    """
    parsed = [parse_value(a) for a in args]
    reach = windows_mod.command_reach(command_id, parsed)
    result = live("commands.execute", {"command": command_id, "args": parsed}, mutating=True,
                  timeout=max(_opts["timeout"], 60), refuse=reach and f"run {command_id} in", reach=reach or "window")
    output({"command": command_id, "result": result})


# ── workspace ────────────────────────────────────────────────────────────
@cli.group()
def workspace():
    """The window's workspace: folders, file search, symbol search."""


@workspace.command("folders")
@handle_error
def workspace_folders():
    """List the workspace folders of the target window."""
    output(live("workspace.folders"))


@workspace.command("add-folder")
@click.argument("path")
@click.option("--name", type=str, default=None)
@handle_error
def workspace_add_folder(path, name):
    """Add a folder to the open workspace. A single-folder window becomes a workspace, which
    restarts its extension host: refused for the window this command runs inside."""
    c = client()
    if windows_mod.folder_add_restarts(c.window):
        windows_mod.refuse_if_hosting(c.window, "add a folder to")
    _mark()
    output(c.call("workspace.addFolder", {"path": target(path), "name": name}, mutating=True))


@workspace.command("remove-folder")
@click.argument("path")
@handle_error
def workspace_remove_folder(path):
    """Remove a folder from the open workspace. Removing the first one restarts the extension
    host: refused for the window this command runs inside."""
    c = client()
    if windows_mod.folder_remove_restarts(c.window, target(path)):
        windows_mod.refuse_if_hosting(c.window, "remove the first folder of")
    _mark()
    output(c.call("workspace.removeFolder", {"path": target(path)}, mutating=True))


@workspace.command("find")
@click.argument("glob")
@click.option("--exclude", type=str, default=None, help="Glob to exclude (default: files.exclude)")
@click.option("--max", "max_results", type=int, default=1000, show_default=True)
@handle_error
def workspace_find(glob, exclude, max_results):
    """Find files by glob, honouring the workspace's exclude settings."""
    output(live("workspace.findFiles", {"include": glob, "exclude": exclude, "maxResults": max_results}))


@workspace.command("symbols")
@click.argument("query", required=False, default="")
@handle_error
def workspace_symbols(query):
    """Search symbols across the workspace."""
    output(live("lang.workspaceSymbols", {"query": query}))


# ── lang ─────────────────────────────────────────────────────────────────
@cli.group()
def lang():
    """Language features from VS Code's language servers: problems, navigation, rename."""


@lang.command("diagnostics")
@click.argument("path", required=False)
@click.option("--severity", type=click.Choice(["error", "warning", "info", "hint"]), default=None)
@handle_error
def lang_diagnostics(path, severity):
    """Problems VS Code reports (whole workspace, or one file)."""
    output(live("lang.diagnostics", {"path": opt_target(path), "severity": severity}))


@lang.command("definition")
@click.argument("location")
@click.option("--kind", type=click.Choice(["definition", "type", "implementation", "declaration"]),
              default="definition", show_default=True)
@handle_error
def lang_definition(location, kind):
    """Where the symbol at FILE:LINE:COLUMN is defined."""
    method = {"definition": "lang.definition", "type": "lang.typeDefinition",
              "implementation": "lang.implementation", "declaration": "lang.declaration"}[kind]
    output(live(method, _at(location)))


@lang.command("references")
@click.argument("location")
@handle_error
def lang_references(location):
    """Every reference to the symbol at FILE:LINE:COLUMN."""
    output(live("lang.references", _at(location)))


@lang.command("hover")
@click.argument("location")
@handle_error
def lang_hover(location):
    """Hover text (type, docs) for the symbol at FILE:LINE:COLUMN."""
    hovers = live("lang.hover", _at(location))
    output(hovers, plain="\n\n".join(c for h in hovers for c in h["contents"]) or "(no hover)")


@lang.command("symbols")
@click.argument("path", required=False)
@handle_error
def lang_symbols(path):
    """Outline of a document (default: active editor)."""
    output(live("lang.documentSymbols", {"path": opt_target(path)}))


@lang.command("rename")
@click.argument("location")
@click.argument("new_name")
@click.option("--preview", is_flag=True, help="Report the files that would change without changing them")
@click.option("--save", is_flag=True, help="Save the changed files")
@handle_error
def lang_rename(location, new_name, preview, save):
    """Rename the symbol at FILE:LINE:COLUMN across the workspace."""
    params = {**_at(location), "newName": new_name, "apply": not preview, "save": save or None}
    output(live("lang.rename", params, mutating=not preview))


@lang.command("actions")
@click.argument("location")
@click.option("--kind", type=str, default=None, help="Code action kind, e.g. quickfix or source.organizeImports")
@click.option("--apply", "apply_", type=str, default=None, help="Index or exact title of the action to apply")
@handle_error
def lang_actions(location, kind, apply_):
    """Code actions (quick fixes, refactorings) at FILE[:LINE[:COLUMN]]; optionally apply one."""
    path, line, col = parse_location(location)
    params: dict[str, Any] = {"path": target(path), "line": line, "column": col, "kind": kind}
    if apply_ is not None:
        params["apply"] = int(apply_) if apply_.isdigit() else apply_
    output(live("lang.codeActions", params, mutating=apply_ is not None))


@lang.command("completions")
@click.argument("location")
@click.option("--limit", type=int, default=50, show_default=True)
@handle_error
def lang_completions(location, limit):
    """Completion items VS Code would offer at FILE:LINE:COLUMN."""
    output(live("lang.completions", {**_at(location), "limit": limit}))


@lang.command("languages")
@handle_error
def lang_languages():
    """Language ids VS Code knows."""
    output(live("lang.languages"))


# ── terminal ─────────────────────────────────────────────────────────────
@cli.group()
def terminal():
    """Integrated terminals: list, create, type into, run a command and capture its output."""


def _term(index: Optional[int], name: Optional[str]) -> dict:
    return {"index": index, "name": name}


@terminal.command("list")
@handle_error
def terminal_list():
    """List integrated terminals."""
    output(live("terminal.list"))


@terminal.command("new")
@click.option("--name", type=str, default=None)
@click.option("--cwd", type=str, default=None)
@click.option("--shell", type=str, default=None, help="Shell executable")
@click.option("--hidden", is_flag=True, help="Do not reveal the terminal panel")
@handle_error
def terminal_new(name, cwd, shell, hidden):
    """Create a terminal."""
    output(live("terminal.create", {"name": name, "cwd": opt_target(cwd), "shellPath": shell,
                                    "show": not hidden}, mutating=True))


@terminal.command("send")
@click.argument("text")
@click.option("--index", type=int, default=None, help="Terminal index (default: the active one)")
@click.option("--name", type=str, default=None)
@click.option("--no-enter", is_flag=True, help="Type the text without pressing Enter")
@handle_error
def terminal_send(text, index, name, no_enter):
    """Type text into a terminal (does not wait or capture output)."""
    output(live("terminal.send", {**_term(index, name), "text": text, "enter": not no_enter}, mutating=True))


@terminal.command("exec")
@click.argument("command_line")
@click.option("--index", type=int, default=None, help="Terminal index (default: the active one)")
@click.option("--name", type=str, default=None)
@click.option("--new", "new", is_flag=True, help="Run in a fresh terminal")
@click.option("--cwd", type=str, default=None, help="Working directory for a fresh terminal")
@click.option("--wait", "wait_s", type=float, default=60, show_default=True, help="Seconds to wait for the command")
@click.option("--raw", is_flag=True, help="Keep terminal escape sequences in the output")
@handle_error
def terminal_exec(command_line, index, name, new, cwd, wait_s, raw):
    """Run a command in a terminal and return its output and exit code.

    Needs shell integration (PowerShell, bash, zsh, fish), which VS Code turns on by default.
    """
    res = live("terminal.exec", {**_term(index, name), "command": command_line, "new": new or None,
                                 "cwd": opt_target(cwd), "timeoutMs": int(wait_s * 1000), "raw": raw or None},
               mutating=True, timeout=wait_s + 30)
    output(res, plain=res.get("output"))


@terminal.command("show")
@click.option("--index", type=int, default=None)
@click.option("--name", type=str, default=None)
@handle_error
def terminal_show(index, name):
    """Reveal a terminal."""
    output(live("terminal.show", _term(index, name), mutating=True))


@terminal.command("kill")
@click.option("--index", type=int, default=None)
@click.option("--name", type=str, default=None)
@handle_error
def terminal_kill(index, name):
    """Close a terminal."""
    output(live("terminal.kill", _term(index, name), mutating=True))


# ── task ─────────────────────────────────────────────────────────────────
@cli.group()
def task():
    """Tasks (tasks.json and auto-detected): list, run, stop."""


@task.command("list")
@click.option("--type", "type_", type=str, default=None, help="Task type, e.g. npm or shell")
@handle_error
def task_list(type_):
    """List tasks VS Code can run."""
    output(live("tasks.list", {"type": type_}, timeout=max(_opts["timeout"], 60)))


@task.command("run")
@click.argument("name")
@click.option("--source", type=str, default=None, help="Disambiguate tasks with the same name")
@click.option("--wait", "wait_s", type=float, default=0, help="Seconds to wait for the exit code")
@handle_error
def task_run(name, source, wait_s):
    """Run a task; with --wait, return its exit code."""
    output(live("tasks.run", {"name": name, "source": source, "wait": bool(wait_s) or None,
                              "timeoutMs": int(wait_s * 1000) or None},
                mutating=True, timeout=max(_opts["timeout"], wait_s + 30)))


@task.command("running")
@handle_error
def task_running():
    """List running tasks."""
    output(live("tasks.running"))


@task.command("stop")
@click.argument("name")
@handle_error
def task_stop(name):
    """Terminate a running task."""
    output(live("tasks.terminate", {"name": name}, mutating=True))


# ── debug ────────────────────────────────────────────────────────────────
@cli.group()
def debug():
    """Debugging: sessions, breakpoints, raw debug-adapter requests."""


@debug.command("state")
@handle_error
def debug_state():
    """Active debug session and breakpoint count."""
    output(live("debug.state"))


@debug.command("start")
@click.argument("name", required=False)
@click.option("--config", type=str, default=None, help="Launch configuration as JSON, instead of a name")
@click.option("--folder", type=str, default=None, help="Workspace folder holding launch.json")
@click.option("--no-debug", is_flag=True, help="Run without debugging")
@handle_error
def debug_start(name, config, folder, no_debug):
    """Start a launch configuration by NAME, or one given inline with --config."""
    output(live("debug.start", {"name": name, "config": json.loads(config) if config else None,
                                "folder": opt_target(folder), "noDebug": no_debug or None},
                mutating=True, timeout=max(_opts["timeout"], 60)))


@debug.command("stop")
@handle_error
def debug_stop():
    """Stop the active debug session."""
    output(live("debug.stop", mutating=True))


@debug.command("request")
@click.argument("dap_command")
@click.argument("args", required=False)
@handle_error
def debug_request(dap_command, args):
    """Send a Debug Adapter Protocol request (threads, stackTrace, evaluate, continue, next...)."""
    output(live("debug.request", {"command": dap_command, "args": json.loads(args) if args else None}, mutating=True))


@debug.command("breakpoints")
@handle_error
def debug_breakpoints():
    """List breakpoints."""
    output(live("debug.breakpoints.list"))


@debug.command("break")
@click.argument("location")
@click.option("--condition", type=str, default=None)
@click.option("--log", "log_message", type=str, default=None, help="Make it a logpoint")
@handle_error
def debug_break(location, condition, log_message):
    """Add a breakpoint at FILE:LINE."""
    p = _at(location)
    output(live("debug.breakpoints.add", {"path": p["path"], "line": p["line"], "condition": condition,
                                          "logMessage": log_message}, mutating=True))


@debug.command("unbreak")
@click.argument("location", required=False)
@click.option("--all", "all_", is_flag=True, help="Remove every breakpoint")
@handle_error
def debug_unbreak(location, all_):
    """Remove breakpoints at FILE[:LINE], or all of them."""
    if all_:
        output(live("debug.breakpoints.remove", {"all": True}, mutating=True))
        return
    if not location:
        raise click.UsageError("Pass FILE[:LINE] or --all")
    path, line, _col = parse_location(location)
    output(live("debug.breakpoints.remove", {"path": target(path), "line": line}, mutating=True))


# ── ext ──────────────────────────────────────────────────────────────────
@cli.group()
def ext():
    """Extensions: list, install, uninstall, inspect, activate."""


@ext.command("list")
@click.option("--live", "live_", is_flag=True, help="Ask the window (adds activation state) instead of `code`")
@click.option("--builtin", is_flag=True, help="With --live, include built-in extensions")
@handle_error
def ext_list(live_, builtin):
    """List installed extensions."""
    output(live("ext.list", {"builtin": builtin or None}) if live_ else backend.list_extensions(**_dirs()))


@ext.command("install")
@click.argument("id_or_vsix")
@click.option("--force", is_flag=True, help="Reinstall or downgrade without asking")
@handle_error
def ext_install(id_or_vsix, force):
    """Install an extension from the marketplace (publisher.name[@version]) or a .vsix file."""
    if _opts["dry_run"]:
        output({"would_install": id_or_vsix, "dry_run": True})
        return
    _mark()
    output({"installed": id_or_vsix, "output": backend.install_extension(id_or_vsix, force=force, **_dirs())})


@ext.command("uninstall")
@click.argument("extension_id")
@handle_error
def ext_uninstall(extension_id):
    """Uninstall an extension."""
    if _opts["dry_run"]:
        output({"would_uninstall": extension_id, "dry_run": True})
        return
    _mark()
    output({"uninstalled": extension_id, "output": backend.uninstall_extension(extension_id, **_dirs())})


@ext.command("info")
@click.argument("extension_id")
@handle_error
def ext_info(extension_id):
    """An extension's version, state and contributed commands (from the window)."""
    output(live("ext.info", {"id": extension_id}))


@ext.command("activate")
@click.argument("extension_id")
@handle_error
def ext_activate(extension_id):
    """Activate an extension in the target window."""
    output(live("ext.activate", {"id": extension_id}, mutating=True))


# ── config ───────────────────────────────────────────────────────────────
@cli.group()
def config():
    """Settings and keybindings, edited in VS Code's own JSONC files (comments are kept)."""


def _scope_options(func):
    func = click.option("--folder", type=str, default=None, help="Workspace folder for --scope workspace (default: cwd)")(func)
    return click.option("--scope", type=click.Choice(["user", "workspace"]), default="user", show_default=True)(func)


def _settings_path(scope: str, folder: Optional[str]):
    return settings_mod.settings_file(scope, user_data_dir=_opts["user_data_dir"], folder=folder)


@config.command("path")
@_scope_options
@handle_error
def config_path(scope, folder):
    """Show the settings and keybindings file paths."""
    output({"settings": str(_settings_path(scope, folder)),
            "keybindings": str(backend.paths(**_dirs())["keybindings"])})


@config.command("list")
@_scope_options
@handle_error
def config_list(scope, folder):
    """Every setting stored in the file."""
    output(settings_mod.read(_settings_path(scope, folder)))


@config.command("get")
@click.argument("key")
@click.option("--effective", is_flag=True, help="Ask the window: effective value plus default, user and workspace values")
@_scope_options
@handle_error
def config_get(key, effective, scope, folder):
    """Read one setting from the file, or its effective value from a window."""
    output(live("config.get", {"key": key}) if effective else settings_mod.get_value(_settings_path(scope, folder), key))


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--live", "live_", is_flag=True, help="Let the target window write it (uses the window's own workspace)")
@_scope_options
@handle_error
def config_set(key, value, live_, scope, folder):
    """Set a setting. VALUE is JSON (true, 14, "x", {...}) or a plain string."""
    parsed = parse_value(value)
    if live_:
        output(live("config.update", {"key": key, "value": parsed, "target": scope}, mutating=True))
        return
    _mark()
    output(settings_mod.set_value(_settings_path(scope, folder), key, parsed, session=get_session(),
                                  dry_run=_opts["dry_run"]))


@config.command("unset")
@click.argument("key")
@_scope_options
@handle_error
def config_unset(key, scope, folder):
    """Remove a setting from the file."""
    _mark()
    output(settings_mod.unset_value(_settings_path(scope, folder), key, session=get_session(),
                                    dry_run=_opts["dry_run"]))


@config.command("keys")
@handle_error
def config_keys():
    """List user keybinding rules."""
    output(settings_mod.keybindings(backend.paths(**_dirs())["keybindings"]))


@config.command("bind")
@click.argument("key")
@click.argument("command_id")
@click.option("--when", type=str, default=None, help="Context expression")
@click.option("--args", "args", type=str, default=None, help="Command arguments as JSON")
@handle_error
def config_bind(key, command_id, when, args):
    """Add a keybinding, e.g. `config bind ctrl+alt+t workbench.action.terminal.new`."""
    _mark()
    output(settings_mod.add_keybinding(backend.paths(**_dirs())["keybindings"], key, command_id, when=when,
                                       args=json.loads(args) if args else None, session=get_session(),
                                       dry_run=_opts["dry_run"]))


@config.command("unbind")
@click.option("--key", type=str, default=None)
@click.option("--command", "command_id", type=str, default=None)
@handle_error
def config_unbind(key, command_id):
    """Remove keybinding rules matching --key and/or --command."""
    _mark()
    output(settings_mod.remove_keybinding(backend.paths(**_dirs())["keybindings"], key=key, command=command_id,
                                          session=get_session(), dry_run=_opts["dry_run"]))


# ── ui ───────────────────────────────────────────────────────────────────
@cli.group()
def ui():
    """Talk to the person at the keyboard: notifications, pickers, input boxes, clipboard."""


@ui.command("notify")
@click.argument("message")
@click.option("--level", type=click.Choice(["info", "warning", "error"]), default="info", show_default=True)
@click.option("--button", multiple=True, help="Button to offer (repeatable); waits for the choice")
@click.option("--modal", is_flag=True, help="Show as a blocking dialog")
@handle_error
def ui_notify(message, level, button, modal):
    """Show a notification. With --button, wait for the click and return it."""
    blocking = bool(button) or modal
    output(live("ui.message", {"message": message, "level": level, "items": list(button) or None,
                               "modal": modal or None, "wait": blocking or None},
                mutating=True, timeout=max(_opts["timeout"], 300) if blocking else None))


@ui.command("status")
@click.argument("text")
@click.option("--seconds", type=float, default=5, show_default=True)
@handle_error
def ui_status(text, seconds):
    """Show a message in the status bar."""
    output(live("ui.statusBar", {"text": text, "timeoutMs": int(seconds * 1000)}, mutating=True))


@ui.command("pick")
@click.argument("items", nargs=-1, required=True)
@click.option("--title", type=str, default=None)
@click.option("--many", is_flag=True, help="Allow several choices")
@handle_error
def ui_pick(items, title, many):
    """Ask the user to pick from a list; returns the choice (null if dismissed)."""
    output(live("ui.quickPick", {"items": list(items), "title": title, "many": many or None},
                mutating=True, timeout=max(_opts["timeout"], 300)))


@ui.command("input")
@click.option("--prompt", type=str, default=None)
@click.option("--value", type=str, default=None, help="Pre-filled text")
@click.option("--password", is_flag=True, help="Mask what is typed")
@handle_error
def ui_input(prompt, value, password):
    """Ask the user to type something; returns it (null if dismissed)."""
    output(live("ui.inputBox", {"prompt": prompt, "value": value, "password": password or None},
                mutating=True, timeout=max(_opts["timeout"], 300)))


@ui.command("clipboard-get")
@handle_error
def ui_clipboard_get():
    """Read the clipboard."""
    res = live("clipboard.read")
    output(res, plain=res["text"])


@ui.command("clipboard-set")
@click.argument("text")
@handle_error
def ui_clipboard_set(text):
    """Write the clipboard."""
    output(live("clipboard.write", {"text": text}, mutating=True))


@ui.command("open-url")
@click.argument("url")
@handle_error
def ui_open_url(url):
    """Open an http(s) URL in the default browser."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise RuntimeError("Only http and https URLs are opened")
    output(live("ui.openExternal", {"url": url}, mutating=True))


# ── git ──────────────────────────────────────────────────────────────────
@cli.group()
def git():
    """Source control as VS Code's Git extension sees it."""


@git.command("status")
@handle_error
def git_status():
    """Repositories in the window: branch, ahead/behind, staged and unstaged changes."""
    output(live("git.status", timeout=max(_opts["timeout"], 60)))


# ── export ───────────────────────────────────────────────────────────────
@cli.group()
def export():
    """Write what VS Code reports to a file: problems, window state, commands, setup."""


@export.command("presets")
@handle_error
def export_presets():
    """List export presets."""
    output([{"preset": k, **v} for k, v in export_mod.EXPORT_PRESETS.items()])


@export.command("render")
@click.argument("output_path")
@click.option("-p", "--preset", type=click.Choice(list(export_mod.EXPORT_PRESETS)), required=True)
@click.option("--overwrite", is_flag=True)
@handle_error
def export_render(output_path, preset, overwrite):
    """Produce an export file from the real VS Code."""
    if _opts["dry_run"]:
        output({"would_write": os.path.abspath(output_path), "preset": preset, "dry_run": True})
        return
    needs_window = export_mod.EXPORT_PRESETS[preset]["needs"] == "window"
    output(export_mod.render(preset, output_path, client=client() if needs_window else None,
                             overwrite=overwrite, **_dirs()))


# ── bridge ───────────────────────────────────────────────────────────────
@cli.group()
def bridge():
    """The companion extension that lets this CLI reach running windows."""


def _each_app(all_apps: bool, work):
    """Run ``work()`` for the chosen editor, or with --all for every installed one."""
    if not all_apps:
        return work()
    if _opts["user_data_dir"] or _opts["extensions_dir"]:
        raise click.UsageError("--all uses each editor's own folders; drop --user-data-dir/--extensions-dir")
    rows = []
    try:
        for name in backend.APPS:
            if backend.find_launcher(name):
                backend.select_app(name)
                rows.append(work())
    finally:
        backend.select_app(_opts["app"])
    return rows


@bridge.command("install")
@click.option("--all", "all_apps", is_flag=True, help="Install into every installed editor built on VS Code")
@handle_error
def bridge_install(all_apps):
    """Package the bridge extension and install it into VS Code (or --app NAME, or --all)."""
    if _opts["dry_run"]:
        targets = [n for n in backend.APPS if backend.find_launcher(n)] if all_apps else [backend.current_app()]
        output({"would_install": bridge_mod.extension_id(), "apps": targets, "dry_run": True})
        return
    _mark()
    output(_each_app(all_apps, lambda: bridge_mod.install(**_dirs())))


@bridge.command("uninstall")
@handle_error
def bridge_uninstall():
    """Remove the bridge extension from VS Code."""
    if _opts["dry_run"]:
        output({"would_uninstall": bridge_mod.extension_id(), "dry_run": True})
        return
    _mark()
    output(bridge_mod.uninstall(**_dirs()))


@bridge.command("status")
@click.option("--all", "all_apps", is_flag=True, help="Report every installed editor built on VS Code")
@handle_error
def bridge_status(all_apps):
    """Is the bridge installed, and how many windows answer?"""
    output(_each_app(all_apps, lambda: bridge_mod.status(**_dirs())))


@bridge.command("build")
@click.option("-o", "--out-dir", type=str, default=None, help="Folder for the .vsix")
@handle_error
def bridge_build(out_dir):
    """Write the bridge .vsix without installing it."""
    output({"vsix": str(bridge_mod.build_vsix(out_dir))})


@bridge.command("ping")
@handle_error
def bridge_ping():
    """Check that the target window answers."""
    start = time.perf_counter()
    res = live("ping")
    output({**res, "ms": round((time.perf_counter() - start) * 1000, 1)})


@bridge.command("call")
@click.argument("method")
@click.argument("params", required=False)
@handle_error
def bridge_call(method, params):
    """Call a bridge method directly with a JSON params object."""
    output(live(method, json.loads(params) if params else {}, mutating=True, timeout=max(_opts["timeout"], 60)))


@bridge.command("eval")
@click.argument("code", required=False)
@click.option("--from", "from_file", type=str, default=None, help="Read the code from a file (- for stdin)")
@handle_error
def bridge_eval(code, from_file):
    """Run JavaScript against the extension API (`vscode`, `require` in scope; use `return`).

    Off until "codereinsBridge.allowEval": true is set in VS Code user settings.
    """
    output(live("eval", {"code": _text_arg(code, from_file)}, mutating=True, timeout=max(_opts["timeout"], 60)))


# ── session ──────────────────────────────────────────────────────────────
@cli.group()
def session():
    """CLI state: selected window, history, undo."""


@session.command("status")
@handle_error
def session_status():
    """Show the selected window, loaded project and undo depth."""
    output(get_session().status())


@session.command("history")
@click.option("--limit", type=int, default=20, show_default=True)
@handle_error
def session_history(limit):
    """Recent state-changing commands (names only, no arguments)."""
    output(get_session().state["history"][-limit:])


@session.command("undo")
@handle_error
def session_undo():
    """Undo the last project change (REPL), else restore the last file this CLI rewrote."""
    sess = get_session()
    if sess._undo:
        output({"undone": sess.undo()})
    elif _opts["dry_run"]:
        output({"would_restore": (sess.state["backups"] or [None])[-1], "dry_run": True})
    else:
        output(sess.restore_last())


@session.command("redo")
@handle_error
def session_redo():
    """Redo the last undone project change (REPL only)."""
    output({"redone": get_session().redo()})


@session.command("clear-window")
@handle_error
def session_clear_window():
    """Forget the window chosen with `window use`."""
    get_session().set_window(None)
    output({"selected": None})


# ── REPL ─────────────────────────────────────────────────────────────────
@cli.command()
@handle_error
def repl():
    """Start the interactive REPL (the default with no command)."""
    from codereins.utils.repl_skin import ReplSkin

    global _repl_mode
    _repl_mode = True
    base = []
    if _opts["json"]:
        base.append("--json")
    if _opts["dry_run"]:
        base.append("--dry-run")
    for flag, key in (("--window", "window"), ("--app", "app"), ("--user-data-dir", "user_data_dir"),
                      ("--extensions-dir", "extensions_dir")):
        if _opts[key]:
            base += [flag, _opts[key]]
    base += ["--timeout", str(_opts["timeout"])]

    skin = ReplSkin("vscode", version=__version__)
    skin.print_banner()
    pt_session = skin.create_prompt_session()

    def label() -> str:
        sess = get_session()
        if sess.project_path:
            return os.path.basename(sess.project_path)
        return f"window {sess.state['window']}" if sess.state.get("window") else ""

    try:
        while True:
            try:
                line = skin.get_input(pt_session, project_name=label(), modified=get_session()._modified).strip()
            except (EOFError, KeyboardInterrupt):
                skin.print_goodbye()
                break
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                skin.print_goodbye()
                break
            if line.lower() in ("help", "?"):
                skin.help({name: (cmd.get_short_help_str(80) or "") for name, cmd in sorted(cli.commands.items())})
                continue
            try:
                args = shlex.split(line, posix=os.name != "nt")
            except ValueError:
                args = line.split()
            if os.name == "nt":
                args = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a for a in args]
            try:
                cli.main(base + args, standalone_mode=False)
            except SystemExit:
                pass
            except click.exceptions.UsageError as e:
                skin.error(f"Usage error: {e.format_message()}")
            except click.exceptions.Abort:
                pass
            except Exception as e:  # noqa: BLE001 - the REPL must survive any command
                skin.error(str(e))
    finally:
        _repl_mode = False


def main() -> None:
    """Console entry point."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cli()


if __name__ == "__main__":
    main()
