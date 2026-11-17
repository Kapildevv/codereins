"""Backend: finds and runs the real VS Code command line (``code``).

VS Code is a hard dependency. Nothing here reimplements it: opening, diffing and
extension management all go through the application's own launcher.

Editors built on VS Code (Antigravity IDE, VS Code Insiders, VSCodium) ship the same
launcher under another name. ``--app`` picks one; each app's own ``product.json`` says
where it keeps user data (``nameShort``) and extensions (``dataFolderName``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

INSTALL_HELP = (
    "VS Code was not found. Install it, then make sure `code` is on PATH:\n"
    "  Windows: winget install Microsoft.VisualStudioCode\n"
    "  macOS:   brew install --cask visual-studio-code  (then 'Shell Command: Install code command in PATH')\n"
    "  Linux:   https://code.visualstudio.com/docs/setup/linux\n"
    "Or point CODEREINS_BIN at the launcher, or pick another editor with --app."
)

# Editors that ship VS Code's launcher. ``win`` is the install folder under
# %LOCALAPPDATA%\Programs (user install) or %ProgramFiles% (system install).
APPS: dict[str, dict] = {
    "vscode": {"launchers": ("code",), "win": "Microsoft VS Code",
               "mac": "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
               "linux": ("/usr/bin/code", "/snap/bin/code")},
    "insiders": {"launchers": ("code-insiders",), "win": "Microsoft VS Code Insiders"},
    "vscodium": {"launchers": ("codium",), "win": "VSCodium"},
    "antigravity-ide": {"launchers": ("antigravity-ide",), "win": "Antigravity IDE"},
}
APP_ENV = "CODEREINS_APP"
_selected_app: Optional[str] = None

_BATCH_LINE = re.compile(r'"%~dp0([^"]+?\.exe)"\s+"%~dp0([^"]+?cli\.js)"', re.IGNORECASE)
_BATCH_UNSAFE = re.compile(r'[&|<>^%"\r\n]')
_ENV_DROP = re.compile(r"^(ELECTRON_.+|VSCODE_(?!PORTABLE$|IPC_HOOK_CLI$).+)$")
EXTENSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\.[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9._-]+)?$")


def select_app(name: Optional[str]) -> None:
    """Make later calls use editor ``name`` (a key of ``APPS``); None restores the default."""
    if name is not None and name not in APPS:
        raise RuntimeError(f"Unknown app {name!r}. Known: {', '.join(APPS)}")
    global _selected_app
    _selected_app = name


def current_app() -> Optional[str]:
    """The editor chosen with --app or $CODEREINS_APP, or None (first one installed)."""
    name = _selected_app or os.environ.get(APP_ENV) or None
    if name is not None and name not in APPS:
        raise RuntimeError(f"{APP_ENV}={name!r} is not a known app. Known: {', '.join(APPS)}")
    return name


def find_launcher(app: str) -> Optional[str]:
    """Path of ``app``'s launcher, or None when it is not installed."""
    spec = APPS[app]
    for name in spec["launchers"]:
        found = shutil.which(name)
        if found:
            return found
    candidates: list[Path] = []
    if os.name == "nt":
        for var, sub in (("LOCALAPPDATA", Path("Programs")), ("ProgramFiles", Path())):
            base = os.environ.get(var)
            if base:
                candidates += [Path(base) / sub / spec["win"] / "bin" / f"{n}.cmd" for n in spec["launchers"]]
    elif sys.platform == "darwin":
        candidates += [Path(spec["mac"])] if spec.get("mac") else []
    else:
        candidates += [Path(p) for p in spec.get("linux", ())]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def find_code() -> str:
    """Return the path of the launcher to use, or raise with install instructions."""
    explicit = os.environ.get("CODEREINS_BIN")
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise RuntimeError(f"CODEREINS_BIN points to a missing file: {explicit}")
    app = current_app()
    if app:
        found = find_launcher(app)
        if found:
            return found
        raise RuntimeError(f"{app} was not found (looked for {', '.join(APPS[app]['launchers'])} on PATH "
                           f"and in the default install folder). Run `app list`.")
    for name in APPS:
        found = find_launcher(name)
        if found:
            return found
    raise RuntimeError(INSTALL_HELP)


def product(code_path: Optional[str] = None) -> dict:
    """The editor's ``product.json`` (name, version, data folder names), or {} if unreadable."""
    try:
        code_path = code_path or find_code()
    except RuntimeError:
        return {}
    prefix, _, _ = launcher(code_path)
    candidates = []
    if len(prefix) == 2:  # [Electron exe, .../resources/app/out/cli.js]
        candidates.append(Path(prefix[1]).parent.parent / "product.json")
    bin_dir = Path(code_path).resolve().parent
    candidates += [bin_dir.parent / "resources" / "app" / "product.json", bin_dir.parent / "product.json"]
    for c in candidates:
        try:
            data = json.loads(c.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def launcher(code_path: str) -> tuple[list[str], dict[str, str], bool]:
    """Return ``(argv_prefix, env_overrides, via_batch)`` for running the VS Code CLI.

    On Windows ``code`` is a batch file, and arguments handed to a batch file are parsed
    again by cmd.exe: a path holding ``&`` or ``%`` could run something else. The batch
    file only sets ELECTRON_RUN_AS_NODE and runs ``Code.exe cli.js``, so do that directly.
    """
    if os.name == "nt" and code_path.lower().endswith((".cmd", ".bat")):
        try:
            text = Path(code_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = _BATCH_LINE.search(text)
        if m:
            base = Path(code_path).parent
            exe, cli = (base / m.group(1)).resolve(), (base / m.group(2)).resolve()
            if exe.exists() and cli.exists():
                return [str(exe), str(cli)], {"ELECTRON_RUN_AS_NODE": "1"}, False
        return [code_path], {}, True
    return [code_path], {}, False


def run_code(args: list[str], *, user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None,
             profile: Optional[str] = None, timeout: float = 180, check: bool = True,
             capture: bool = True) -> subprocess.CompletedProcess:
    """Run ``code <args>``. ``capture=False`` is for commands that open a window."""
    prefix, extra, via_batch = launcher(find_code())
    full = [str(a) for a in args]
    if user_data_dir:
        full += ["--user-data-dir", str(user_data_dir)]
    if extensions_dir:
        full += ["--extensions-dir", str(extensions_dir)]
    if profile:
        full += ["--profile", profile]
    if via_batch:
        bad = [a for a in full if _BATCH_UNSAFE.search(a)]
        if bad:
            raise RuntimeError(f"Refusing to pass {bad[0]!r} through code.cmd (cmd.exe would re-parse it)")
    # Same clean-up VS Code does for its own terminals: a launcher started from inside an
    # extension host must not hand that host's ELECTRON_*/VSCODE_* variables to a new window.
    env = {k: v for k, v in os.environ.items() if not _ENV_DROP.match(k)}
    env.update(extra)
    stream = subprocess.PIPE if capture else subprocess.DEVNULL
    proc = subprocess.run(prefix + full, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)
    if check and proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip() or "no output"
        raise RuntimeError(f"`code {' '.join(full)}` failed with exit code {proc.returncode}: {detail}")
    return proc


def default_user_data_dir(prod: Optional[dict] = None) -> Path:
    """The user data folder the editor uses when --user-data-dir is not given:
    the per-user config folder plus ``product.json``'s ``nameShort`` (VS Code: ``Code``)."""
    name = (prod or {}).get("nameShort") or "Code"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / name


def default_extensions_dir(prod: Optional[dict] = None) -> Path:
    """``~/<dataFolderName>/extensions`` (VS Code: ``~/.vscode/extensions``)."""
    return Path.home() / ((prod or {}).get("dataFolderName") or ".vscode") / "extensions"


def _product_for_defaults() -> dict:
    """product.json for default paths. With no --app and no editor found, VS Code's names
    apply (the file layer works without an editor installed); a chosen app must exist."""
    try:
        return product(find_code())
    except RuntimeError:
        if current_app():
            raise
        return {}


def paths(user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None) -> dict[str, Path]:
    """Every file location the data layer reads or writes."""
    prod = _product_for_defaults() if not (user_data_dir and extensions_dir) else {}
    udd = Path(user_data_dir) if user_data_dir else default_user_data_dir(prod)
    user = udd / "User"
    return {
        "user_data_dir": udd,
        "settings": user / "settings.json",
        "keybindings": user / "keybindings.json",
        "snippets": user / "snippets",
        "storage": user / "globalStorage" / "storage.json",
        "extensions_dir": Path(extensions_dir) if extensions_dir else default_extensions_dir(prod),
    }


def app_info(app: str) -> dict:
    """What is installed for editor ``app``: launcher, product name/version and default folders.
    Reads files only; nothing is launched."""
    launcher_path = find_launcher(app)
    if not launcher_path:
        return {"app": app, "installed": False}
    prod = product(launcher_path)
    return {
        "app": app, "installed": True, "name": prod.get("nameLong"), "version": prod.get("version"),
        "launcher": launcher_path, "user_data_dir": str(default_user_data_dir(prod)),
        "extensions_dir": str(default_extensions_dir(prod)),
    }


def launcher_name() -> Optional[str]:
    """``nameLong`` of the editor the launcher talks to: the chosen one, else the default."""
    return product(find_code()).get("nameLong") or None


def app_name() -> Optional[str]:
    """``nameLong`` of the editor chosen with --app (what ``vscode.env.appName`` reports in
    its windows), or None when no app was chosen."""
    if not current_app():
        return None
    return launcher_name()


def version() -> dict:
    """``code --version`` as a dict."""
    lines = run_code(["--version"]).stdout.split()
    return {
        "app": current_app(),
        "name": product().get("nameLong"),
        "version": lines[0] if lines else None,
        "commit": lines[1] if len(lines) > 1 else None,
        "arch": lines[2] if len(lines) > 2 else None,
        "binary": find_code(),
    }


def list_extensions(user_data_dir: Optional[str] = None, extensions_dir: Optional[str] = None,
                    profile: Optional[str] = None) -> list[dict]:
    """Installed extensions, from ``code --list-extensions --show-versions``."""
    out = run_code(["--list-extensions", "--show-versions"], user_data_dir=user_data_dir,
                   extensions_dir=extensions_dir, profile=profile).stdout
    res = []
    for line in out.splitlines():
        line = line.strip()
        if "@" in line and EXTENSION_ID.match(line):
            ext_id, _, ver = line.rpartition("@")
            res.append({"id": ext_id, "version": ver})
    return res


def install_extension(target: str, *, force: bool = False, user_data_dir: Optional[str] = None,
                      extensions_dir: Optional[str] = None, profile: Optional[str] = None) -> str:
    """Install an extension by marketplace id or from a .vsix file (into ``profile`` if given)."""
    if target.lower().endswith(".vsix"):
        if not os.path.isfile(target):
            raise RuntimeError(f"No such .vsix file: {target}")
    elif not EXTENSION_ID.match(target):
        raise RuntimeError(f"Not an extension id (publisher.name) or a .vsix file: {target}")
    args = ["--install-extension", target] + (["--force"] if force else [])
    return run_code(args, user_data_dir=user_data_dir, extensions_dir=extensions_dir, profile=profile,
                    timeout=600).stdout.strip()


def uninstall_extension(ext_id: str, *, user_data_dir: Optional[str] = None,
                        extensions_dir: Optional[str] = None, profile: Optional[str] = None) -> str:
    """Uninstall an extension by id (from ``profile`` if given)."""
    if not EXTENSION_ID.match(ext_id):
        raise RuntimeError(f"Not an extension id (publisher.name): {ext_id}")
    return run_code(["--uninstall-extension", ext_id], user_data_dir=user_data_dir,
                    extensions_dir=extensions_dir, profile=profile, timeout=300).stdout.strip()
