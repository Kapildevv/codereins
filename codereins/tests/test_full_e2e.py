"""End-to-end tests against the real VS Code.

One VS Code is launched for the run with a throwaway --user-data-dir and
--extensions-dir, so the user's own profile and windows are never touched. VS Code is a
hard dependency: if it is missing these tests fail, they do not skip.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from codereins.core import bridge as bridge_mod
from codereins.core import export as export_mod
from codereins.utils import bridge_client as bc
from codereins.utils import vscode_backend as backend

TEST_SETTINGS = {
    "security.workspace.trust.enabled": False,
    "workbench.startupEditor": "none",
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "extensions.autoUpdate": False,
    "extensions.ignoreRecommendations": True,
    "window.restoreWindows": "none",
    "files.hotExit": "onExitAndWindowClose",
    "git.openRepositoryInParentFolders": "never",
    "chat.commandCenter.enabled": False,
}

APP_JS = """function greet(name) {
  return 'hi ' + name;
}
const out = greet('a');
console.log(greet('b'), out);
"""


def _resolve_cli(name):
    """Resolve installed CLI command; falls back to python -m for dev.

    Set env CODEREINS_FORCE_INSTALLED=1 to require the installed command.
    """
    force = os.environ.get("CODEREINS_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = "codereins"  # codereins/__main__.py
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


def poll(fn, timeout=60.0, interval=0.5, what="condition"):
    """Call ``fn`` until it returns something truthy; fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except bc.BridgeError as e:
            last = e
        time.sleep(interval)
    pytest.fail(f"Timed out after {timeout:g}s waiting for {what}; last value: {last!r}")


def _kill_instance(user_data_dir, exe="Code.exe"):
    """Last resort: stop only processes whose command line names this run's profile folder."""
    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + str(user_data_dir)
                  + "*' -and $_.Name -eq '" + exe + "' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60)
    else:
        subprocess.run(["pkill", "-f", str(user_data_dir)], capture_output=True, timeout=60)


def _instance_alive(user_data_dir, exe):
    """Whether a process named ``exe`` runs with this run's profile folder (Windows only; True elsewhere)."""
    if os.name != "nt":
        return True
    script = ("@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + str(user_data_dir)
              + "*' -and $_.Name -eq '" + exe + "' }).Count")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60)
    return out.stdout.strip() not in ("", "0")


@pytest.fixture(scope="session")
def vscode(tmp_path_factory, _isolated_home):
    """Launch an isolated VS Code with the bridge installed; yield its paths and a client."""
    root = tmp_path_factory.mktemp("vsc")
    udd, ext, ws = root / "udd", root / "ext", root / "ws"
    (udd / "User").mkdir(parents=True)
    ext.mkdir()
    (ws / ".vscode").mkdir(parents=True)
    (udd / "User" / "settings.json").write_text(json.dumps(TEST_SETTINGS, indent=4), encoding="utf-8")
    (ws / "notes.txt").write_text("line one\nline two\nline three\n", encoding="utf-8", newline="\n")
    (ws / "data.json").write_text('{"b":1,"a":[1,2]}', encoding="utf-8")
    (ws / "broken.json").write_text('{"a": 1,, "b": }\n', encoding="utf-8")
    (ws / "app.js").write_text(APP_JS, encoding="utf-8", newline="\n")
    (ws / "wait.js").write_text("setTimeout(() => {}, 60000);\n", encoding="utf-8")
    (ws / ".vscode" / "tasks.json").write_text(json.dumps({"version": "2.0.0", "tasks": [
        {"label": "say-ok", "type": "shell", "command": "echo task-ok", "problemMatcher": []}]}), encoding="utf-8")

    env = {"udd": str(udd), "ext": str(ext), "ws": str(ws), "root": str(root), "home": str(_isolated_home)}
    info = bridge_mod.install(str(udd), str(ext))
    print(f"\n  bridge vsix: {info['vsix']}")
    backend.run_code(["--new-window", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes", str(ws)],
                     user_data_dir=str(udd), extensions_dir=str(ext), capture=False, timeout=90)
    try:
        windows = poll(bc.live_windows, timeout=120, what="the VS Code window to register its bridge")
        env["window"] = windows[0]
        env["client"] = bc.BridgeClient(windows[0], timeout=30)
        yield env
    finally:
        try:
            for w in bc.live_windows():
                bc.BridgeClient(w, timeout=5).call("window.close")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and bc.live_windows():
                time.sleep(0.5)
        except bc.BridgeError:
            pass
        _kill_instance(udd)


@pytest.fixture
def client(vscode):
    return vscode["client"]


def disk(path):
    return Path(path).read_text(encoding="utf-8")


# ── the real `code` launcher ─────────────────────────────────────────────
class TestBackendReal:
    def test_code_version(self):
        v = backend.version()
        assert re.match(r"^\d+\.\d+\.\d+", v["version"]) and re.match(r"^[0-9a-f]{40}$", v["commit"])

    def test_code_lists_the_bridge_as_installed(self, vscode):
        exts = backend.list_extensions(vscode["udd"], vscode["ext"])
        assert {"id": bridge_mod.extension_id(), "version": bridge_mod.manifest()["version"]} in exts
        assert bridge_mod.status(vscode["udd"], vscode["ext"])["live_windows"] == 1

    def test_vsix_is_a_valid_package(self, tmp_dir):
        vsix = bridge_mod.build_vsix(tmp_dir)
        with zipfile.ZipFile(vsix) as z:
            assert z.testzip() is None and "extension/extension.js" in z.namelist()
        print(f"\n  VSIX: {vsix} ({vsix.stat().st_size:,} bytes)")


# ── a live window through the bridge ─────────────────────────────────────
class TestLiveWindow:
    def test_info_matches_the_launcher(self, client, vscode):
        info = client.call("info")
        assert info["vscodeVersion"] == backend.version()["version"]
        assert [os.path.normcase(f["path"]) for f in info["workspace"]["folders"]] == [os.path.normcase(vscode["ws"])]
        assert info["allowEval"] is False

    def test_command_catalogue(self, client):
        ids = client.call("commands.list")
        assert len(ids) > 1000 and "workbench.action.files.save" in ids
        filtered = client.call("commands.list", {"filter": "reloadWindow"})
        assert "workbench.action.reloadWindow" in filtered and all("reloadwindow" in c.lower() for c in filtered)

    def test_open_insert_replace_save_is_on_disk(self, client, vscode):
        path = os.path.join(vscode["ws"], "notes.txt")
        st = client.call("editor.open", {"path": path, "line": 2})
        assert os.path.normcase(st["path"]) == os.path.normcase(path) and st["selections"][0]["active"]["line"] == 2
        client.call("doc.edit", {"path": path, "edits": [{"insert": [2, 1], "text": "INSERTED "}], "save": True})
        assert disk(path) == "line one\nINSERTED line two\nline three\n"
        client.call("doc.edit", {"path": path, "edits": [{"range": [3, 1, 3, 1_000_000], "text": "third"}], "save": True})
        assert disk(path) == "line one\nINSERTED line two\nthird\n"
        assert client.call("editor.state")["isDirty"] is False

    def test_unsaved_buffer_differs_from_disk_until_saved(self, client, vscode):
        path = os.path.join(vscode["ws"], "notes.txt")
        before = disk(path)
        doc = client.call("doc.edit", {"path": path, "edits": [{"append": True, "text": "unsaved tail\n"}]})
        assert doc["isDirty"] is True and disk(path) == before
        assert client.call("doc.read", {"path": path})["text"] == before + "unsaved tail\n"
        assert client.call("doc.read", {"path": path, "startLine": 1, "endLine": 1})["text"] == "line one"
        assert client.call("doc.save", {"path": path})["saved"] is True
        assert disk(path) == before + "unsaved tail\n"

    def test_substitute_plain_and_regex(self, client, vscode):
        path = os.path.join(vscode["ws"], "sub.txt")
        Path(path).write_text("alpha beta alpha\nvalue=12\n", encoding="utf-8", newline="\n")
        assert client.call("doc.replace", {"path": path, "find": "alpha", "replace": "A", "save": True})["replacements"] == 1
        assert disk(path) == "A beta alpha\nvalue=12\n"
        res = client.call("doc.replace", {"path": path, "find": r"(\w+)=(\d+)", "replace": "$2=$1", "regex": True,
                                          "all": True, "save": True})
        assert res["replacements"] == 1 and disk(path) == "A beta alpha\n12=value\n"
        assert client.call("doc.replace", {"path": path, "find": "zzz", "replace": "x"})["replacements"] == 0

    def test_json_formatter_rewrites_the_file(self, client, vscode):
        path = os.path.join(vscode["ws"], "data.json")
        client.call("editor.open", {"path": path})
        res = poll(lambda: client.call("lang.format", {"path": path, "save": True}), timeout=60,
                   what="the JSON formatter")
        text = disk(path)
        assert res["edits"] >= 1 and text.count("\n") >= 4 and json.loads(text) == {"b": 1, "a": [1, 2]}

    def test_json_syntax_error_is_reported(self, client, vscode):
        path = os.path.join(vscode["ws"], "broken.json")
        client.call("editor.open", {"path": path})
        diags = poll(lambda: client.call("lang.diagnostics", {"path": path}), timeout=60, what="JSON diagnostics")
        assert any(d["severity"] == "error" for d in diags)
        assert all(d["range"]["start"]["line"] == 1 for d in diags)
        assert client.call("lang.diagnostics", {"path": path, "severity": "hint"}) == []
        assert any(os.path.normcase(d["path"]) == os.path.normcase(path) for d in client.call("lang.diagnostics"))

    def test_javascript_outline_definition_references_rename(self, client, vscode):
        path = os.path.join(vscode["ws"], "app.js")
        client.call("editor.open", {"path": path})
        symbols = poll(lambda: client.call("lang.documentSymbols", {"path": path}), timeout=120,
                       what="the JavaScript language server")
        assert {"greet", "out"} <= {s["name"] for s in symbols}
        definition = client.call("lang.definition", {"path": path, "line": 4, "column": 13})
        assert definition and definition[0]["range"]["start"] == {"line": 1, "column": 10}
        refs = client.call("lang.references", {"path": path, "line": 1, "column": 10})
        assert sorted(r["range"]["start"]["line"] for r in refs) == [1, 4, 5]
        hover = client.call("lang.hover", {"path": path, "line": 4, "column": 13})
        assert any("greet" in c for h in hover for c in h["contents"])
        preview = client.call("lang.rename", {"path": path, "line": 1, "column": 10, "newName": "welcome", "apply": False})
        assert preview["applied"] is False and preview["files"][0]["edits"] == 3 and "greet" in disk(path)
        done = client.call("lang.rename", {"path": path, "line": 1, "column": 10, "newName": "welcome", "save": True})
        assert done["applied"] is True
        text = disk(path)
        assert text.count("welcome(") == 3 and "greet" not in text

    def test_completions_come_from_the_language_server(self, client, vscode):
        path = os.path.join(vscode["ws"], "app.js")
        items = poll(lambda: client.call("lang.completions", {"path": path, "line": 5, "column": 9, "limit": 200}),
                     timeout=60, what="completions")
        assert "log" in {i["label"] for i in items}

    def test_file_create_rename_delete(self, client, vscode):
        a, b = os.path.join(vscode["ws"], "made.txt"), os.path.join(vscode["ws"], "moved.txt")
        client.call("files.apply", {"ops": [{"create": a, "content": "made by vscode\n"}]})
        assert disk(a) == "made by vscode\n"
        with pytest.raises(bc.BridgeError):
            client.call("files.apply", {"ops": [{"create": a, "content": "again"}]})
        client.call("files.apply", {"ops": [{"rename": a, "to": b}]})
        assert not os.path.exists(a) and disk(b) == "made by vscode\n"
        client.call("files.apply", {"ops": [{"delete": b}]})
        assert not os.path.exists(b)

    def test_find_files(self, client, vscode):
        found = [os.path.basename(p) for p in client.call("workspace.findFiles", {"include": "**/*.json"})]
        assert "data.json" in found and "broken.json" in found and "app.js" not in found

    def test_tabs_list_and_close(self, client, vscode):
        path = os.path.join(vscode["ws"], "wait.js")
        client.call("editor.open", {"path": path})
        labels = [t["label"] for g in client.call("tabs.list") for t in g["tabs"]]
        assert "wait.js" in labels
        closed = client.call("tabs.close", {"path": path})
        assert closed["ok"] and [t["label"] for t in closed["closed"]] == ["wait.js"]
        assert "wait.js" not in [t["label"] for g in client.call("tabs.list") for t in g["tabs"]]

    def test_selection_round_trip(self, client, vscode):
        path = os.path.join(vscode["ws"], "notes.txt")
        client.call("editor.open", {"path": path})
        st = client.call("editor.select", {"ranges": [[1, 1, 1, 5]]})
        assert st["selections"][0]["text"] == "line" and st["selections"][0]["end"] == {"line": 1, "column": 5}

    def test_terminal_keeps_its_requested_name_from_creation(self, client):
        # Seen 2026-11-17 on VS Code 1.146: for about 2 s after createTerminal({name}), Terminal.name
        # reads the shell's title ("powershell"), so a lookup by name straight after create missed it.
        created = client.call("terminal.create", {"name": "e2e-named", "show": False})
        assert created["name"] == "e2e-named"
        client.call("terminal.send", {"name": "e2e-named", "text": "echo named"})
        seen = set()
        for _ in range(60):  # the wrong name lasts well under 3 s, so sample densely
            names = [t["name"] for t in client.call("terminal.list")]
            seen.update(names)
            assert "e2e-named" in names, f"named terminal missing from {names}; names seen so far: {sorted(seen)}"
            time.sleep(0.05)
        client.call("terminal.kill", {"name": "e2e-named"})

    def test_terminal_create_send_kill(self, client):
        client.call("terminal.create", {"name": "e2e-term", "show": False})
        assert "e2e-term" in [t["name"] for t in client.call("terminal.list")]
        client.call("terminal.send", {"name": "e2e-term", "text": "echo hi"})
        client.call("terminal.kill", {"name": "e2e-term"})
        poll(lambda: "e2e-term" not in [t["name"] for t in client.call("terminal.list")], timeout=15,
             what="the terminal to close")

    def test_terminal_exec_captures_output_and_exit_code(self, client):
        res = client.call("terminal.exec", {"command": "echo codereins-marker-4711", "new": True,
                                            "name": "e2e-exec", "timeoutMs": 60000}, timeout=100)
        print(f"\n  terminal output: {res['output']!r} exit={res['exitCode']}")
        assert "codereins-marker-4711" in res["output"] and res["timedOut"] is False
        assert res["exitCode"] in (0, None)
        client.call("terminal.kill", {"name": "e2e-exec"})

    def test_task_run_returns_exit_code(self, client):
        tasks = poll(lambda: [t for t in client.call("tasks.list", timeout=60) if t["name"] == "say-ok"], timeout=60,
                     what="the workspace task")
        assert tasks[0]["type"] == "shell"
        res = client.call("tasks.run", {"name": "say-ok", "wait": True, "timeoutMs": 90000}, timeout=120)
        assert res["exitCode"] == 0 and res["timedOut"] is False

    def test_breakpoints(self, client, vscode):
        path = os.path.join(vscode["ws"], "app.js")
        client.call("debug.breakpoints.remove", {"all": True})
        bp = client.call("debug.breakpoints.add", {"path": path, "line": 2, "condition": "name === 'a'"})
        assert bp["line"] == 2 and bp["condition"] == "name === 'a'"
        listed = client.call("debug.breakpoints.list")
        assert len(listed) == 1 and os.path.normcase(listed[0]["path"]) == os.path.normcase(path)
        assert client.call("debug.breakpoints.remove", {"path": path, "line": 2})["removed"] == 1
        assert client.call("debug.breakpoints.list") == []

    def test_debug_session_start_request_stop(self, client, vscode):
        config = {"type": "node", "request": "launch", "name": "e2e", "program": os.path.join(vscode["ws"], "wait.js"),
                  "console": "internalConsole"}
        assert client.call("debug.start", {"config": config}, timeout=90)["started"] is True
        active = poll(lambda: client.call("debug.state")["active"], timeout=60, what="the debug session")
        assert active["name"] == "e2e"
        threads = poll(lambda: client.call("debug.request", {"command": "threads"}).get("threads"), timeout=30,
                       what="debug threads")
        assert len(threads) >= 1
        client.call("debug.stop")
        poll(lambda: client.call("debug.state")["active"] is None, timeout=30, what="the debug session to end")

    def test_settings_file_edit_reaches_the_running_vscode_and_undo_reverts(self, client, vscode):
        from codereins.core import settings as settings_mod
        from codereins.core.session import Session

        f = settings_mod.settings_file("user", user_data_dir=vscode["udd"])
        sess = Session(os.path.join(vscode["root"], "session.json"))
        assert client.call("config.get", {"key": "editor.tabSize"})["user"] is None
        settings_mod.set_value(f, "editor.tabSize", 3, session=sess)
        seen = poll(lambda: client.call("config.get", {"key": "editor.tabSize"})["user"] == 3
                    and client.call("config.get", {"key": "editor.tabSize"}), timeout=30,
                    what="VS Code to pick up settings.json")
        assert seen["value"] == 3 and seen["default"] == 4
        sess.restore_last()
        poll(lambda: client.call("config.get", {"key": "editor.tabSize"})["user"] is None, timeout=30,
             what="VS Code to see the revert")

    def test_live_config_update(self, client):
        res = client.call("config.update", {"key": "editor.wordWrap", "value": "on", "target": "user"})
        assert res["user"] == "on" and res["value"] == "on"
        assert client.call("config.update", {"key": "editor.wordWrap", "remove": True, "target": "user"})["user"] is None

    def test_extensions_and_git_views(self, client):
        ours = client.call("ext.info", {"id": bridge_mod.extension_id()})
        assert ours["isActive"] is True and ours["commands"][0]["command"] == "codereinsBridge.showStatus"
        assert bridge_mod.extension_id() in [e["id"] for e in client.call("ext.list")]
        assert len(client.call("ext.list", {"builtin": True})) > 20
        assert isinstance(client.call("git.status", timeout=60), list)

    def test_ui_calls_that_do_not_block(self, client):
        assert client.call("ui.statusBar", {"text": "codereins e2e", "timeoutMs": 1000}) == {"shown": True}
        assert client.call("ui.message", {"message": "codereins e2e run"}) == {"shown": True}
        assert "javascript" in client.call("lang.languages")

    def test_safety_wrong_token_unknown_method_eval_off(self, client, vscode):
        with pytest.raises(bc.BridgeError) as e:
            bc.BridgeClient({**vscode["window"], "token": "0" * 64}).call("ping")
        assert e.value.code == "unauthorized"
        with pytest.raises(bc.BridgeError) as e:
            client.call("constructor")
        assert e.value.code == "unknown_method"
        with pytest.raises(bc.BridgeError) as e:
            client.call("eval", {"code": "return 1"})
        assert e.value.code == "eval_disabled"
        with pytest.raises(bc.BridgeError) as e:
            client.call("doc.edit", {})
        assert e.value.code == "bad_params"

    def test_exports_are_real_files(self, client, vscode):
        broken = os.path.join(vscode["ws"], "broken.json")
        client.call("editor.open", {"path": broken})
        poll(lambda: client.call("lang.diagnostics", {"path": broken}), timeout=60, what="diagnostics")
        md = os.path.join(vscode["root"], "out", "problems.md")
        res = export_mod.render("diagnostics-md", md, client=client)
        text = disk(md)
        assert res["file_size"] > 100 and text.startswith("# VS Code problems") and "broken.json" in text
        assert "| error |" in text
        state = os.path.join(vscode["root"], "out", "state.json")
        res2 = export_mod.render("state-json", state, client=client)
        data = json.loads(disk(state))
        assert data["info"]["vscodeVersion"] and data["problems"].get("error", 0) >= 1 and res2["items"] >= 1
        ext_txt = os.path.join(vscode["root"], "out", "extensions.txt")
        export_mod.render("extensions-txt", ext_txt, user_data_dir=vscode["udd"], extensions_dir=vscode["ext"])
        assert disk(ext_txt).strip() == f"{bridge_mod.extension_id()}@{bridge_mod.manifest()['version']}"
        for p in (md, state, ext_txt):
            print(f"\n  export: {p} ({os.path.getsize(p):,} bytes)")


# ── the installed command, as a user or agent runs it ────────────────────
class TestCLISubprocess:
    CLI_BASE = _resolve_cli("codereins")

    def _run(self, args, check=True):
        return subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, encoding="utf-8", check=check)

    def _json(self, vscode, *args):
        base = ["--json", "--user-data-dir", vscode["udd"], "--extensions-dir", vscode["ext"]]
        return json.loads(self._run(base + list(args)).stdout)

    def test_help(self):
        result = self._run(["--help"])
        assert result.returncode == 0 and "terminal" in result.stdout

    def test_app_version_json(self, vscode):
        data = self._json(vscode, "app", "version")
        assert data["version"] == backend.version()["version"] and data["cli"]

    def test_window_list_and_current(self, vscode):
        rows = self._json(vscode, "window", "list")
        assert len(rows) == 1 and os.path.normcase(rows[0]["folders"][0]) == os.path.normcase(vscode["ws"])
        assert "token" not in rows[0]
        assert self._json(vscode, "window", "current")["id"] == rows[0]["id"]
        assert self._json(vscode, "bridge", "ping")["pong"] is True
        assert self._json(vscode, "bridge", "status")["installed_version"] == bridge_mod.manifest()["version"]

    def test_fix_and_save_workflow(self, vscode):
        path = os.path.join(vscode["ws"], "cli-made.py")
        self._json(vscode, "file", "create", path, "--content", "x = 1\ny = 2\n")
        self._json(vscode, "file", "open", f"{path}:2:1")
        assert self._json(vscode, "editor", "state")["selections"][0]["active"] == {"line": 2, "column": 1}
        self._json(vscode, "editor", "insert", "# added by the CLI\n", "--file", path, "--at", "1:1", "--save")
        self._json(vscode, "editor", "replace", "3", "y = 20", "--file", path, "--save")
        res = self._json(vscode, "editor", "sub", "x = 1", "x = 10", "--file", path, "--save")
        assert res["replacements"] == 1
        assert disk(path) == "# added by the CLI\nx = 10\ny = 20\n"
        doc = self._json(vscode, "file", "read", path, "--lines", "2-3")
        assert doc["text"] == "x = 10\ny = 20" and doc["isDirty"] is False
        plain = self._run(["file", "read", path]).stdout
        assert plain.replace("\r\n", "\n").rstrip("\n") == "# added by the CLI\nx = 10\ny = 20"
        self._json(vscode, "file", "write", path, "rewritten\n")
        assert disk(path) == "rewritten\n"
        self._json(vscode, "file", "close", path)
        self._json(vscode, "file", "delete", path)
        assert not os.path.exists(path)
        print(f"\n  workflow file (deleted at the end): {path}")

    def test_command_run_with_typed_uri_argument(self, vscode):
        path = os.path.join(vscode["ws"], "notes.txt")
        self._json(vscode, "command", "run", "vscode.open", json.dumps({"$uri": path}))
        st = self._json(vscode, "editor", "state")
        assert os.path.normcase(st["path"]) == os.path.normcase(path)
        ids = self._json(vscode, "command", "list", "--filter", "workbench.action.files.save")
        assert "workbench.action.files.save" in ids
        recent = self._json(vscode, "window", "recent")
        assert isinstance(recent, list)

    def test_lang_and_export_from_the_command_line(self, vscode):
        broken = os.path.join(vscode["ws"], "broken.json")
        self._json(vscode, "file", "open", broken)
        deadline = time.monotonic() + 60
        diags = []
        while time.monotonic() < deadline and not diags:
            diags = self._json(vscode, "lang", "diagnostics", broken, "--severity", "error")
            time.sleep(0.5)
        assert diags and diags[0]["severity"] == "error"
        out = os.path.join(vscode["root"], "out", "cli-problems.json")
        res = self._json(vscode, "export", "render", out, "-p", "diagnostics-json", "--overwrite")
        assert res["file_size"] > 0 and any(d["path"].lower().endswith("broken.json") for d in json.loads(disk(out)))
        print(f"\n  export: {out} ({res['file_size']:,} bytes)")

    def test_terminal_exec_from_the_command_line(self, vscode):
        res = self._json(vscode, "terminal", "exec", "echo from-cli-9001", "--new", "--name", "cli-exec", "--wait", "60")
        assert "from-cli-9001" in res["output"]
        self._json(vscode, "terminal", "kill", "--name", "cli-exec")

    def test_project_workflow(self, vscode):
        ws_file = os.path.join(vscode["root"], "team.code-workspace")
        self._json(vscode, "project", "new", "-o", ws_file, "--folder", vscode["ws"])
        self._json(vscode, "--project", ws_file, "project", "add-folder", "docs", "--name", "Docs")
        self._json(vscode, "--project", ws_file, "project", "set", "editor.tabSize", "2")
        before = disk(ws_file)
        dry = self._json(vscode, "--dry-run", "--project", ws_file, "project", "add-folder", "extra")
        assert dry["folder_count"] == 3 and disk(ws_file) == before
        data = json.loads(before)
        assert len(data["folders"]) == 2 and data["settings"] == {"editor.tabSize": 2}
        assert self._json(vscode, "session", "undo")["action"] == "restored"
        assert json.loads(disk(ws_file)).get("settings") == {}
        print(f"\n  workspace file: {ws_file}")

    def test_dry_run_sends_nothing_to_the_window(self, vscode):
        path = os.path.join(vscode["ws"], "notes.txt")
        before = disk(path)
        res = self._json(vscode, "--dry-run", "editor", "sub", "line one", "CHANGED", "--file", path, "--save")
        assert res["dry_run"] is True and res["method"] == "doc.replace" and disk(path) == before

    def test_errors_have_exit_codes_and_json(self, vscode):
        bad = self._run(["--json", "--window", "999999", "editor", "state"], check=False)
        assert bad.returncode == 1 and json.loads(bad.stdout)["type"] == "no_such_window"
        usage = self._run(["--json", "lang", "hover", "nofile.py"], check=False)
        assert usage.returncode == 2
        gone = self._run(["--json", "file", "read", os.path.join(vscode["ws"], "does-not-exist.txt")], check=False)
        assert gone.returncode == 1 and "error" in json.loads(gone.stdout)


# ── a window the tests may end: the host guard, folder changes, reload, close ──
@pytest.fixture(scope="class")
def scratch(tmp_path_factory):
    """A second isolated VS Code, opened on folder ``a``, that these tests reload, re-folder
    and close. It registers in its own CLI home, so the session window's tests never see
    it, and only its profile sets ``codereinsBridge.allowEval`` (used to start the CLI
    from inside its extension host, where an agent in a window runs)."""
    root = tmp_path_factory.mktemp("scratch")
    udd, ext, home = root / "udd", root / "ext", root / "cli-home"
    for d in (udd / "User", ext, root / "a", root / "b", root / "c"):
        d.mkdir(parents=True)
    (udd / "User" / "settings.json").write_text(json.dumps({**TEST_SETTINGS, "codereinsBridge.allowEval": True},
                                                           indent=4), encoding="utf-8")
    old_home = os.environ.get("CODEREINS_HOME")
    os.environ["CODEREINS_HOME"] = str(home)
    try:
        bridge_mod.install(str(udd), str(ext))
        backend.run_code(["--new-window", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes",
                          str(root / "a")], user_data_dir=str(udd), extensions_dir=str(ext), capture=False, timeout=90)
        poll(bc.live_windows, timeout=120, what="the scratch VS Code window to register its bridge")
        yield {name: str(root / name) for name in ("a", "b", "c")}
    finally:
        try:
            for w in bc.live_windows():
                bc.BridgeClient(w, timeout=5).call("window.close")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and bc.live_windows():
                time.sleep(0.5)
        except bc.BridgeError:
            pass
        _kill_instance(udd)
        if old_home is None:
            os.environ.pop("CODEREINS_HOME", None)
        else:
            os.environ["CODEREINS_HOME"] = old_home


class TestWindowLifecycle:
    """In order: the guard (window still single-folder), folder changes, reload, close.

    The window id is its extension host's pid, so a new id means the host restarted."""
    CLI_BASE = _resolve_cli("codereins")

    def _cli(self, *args):
        res = subprocess.run(self.CLI_BASE + ["--json", *args], capture_output=True, text=True, encoding="utf-8")
        assert res.returncode == 0, res.stdout + res.stderr
        return json.loads(res.stdout) if res.stdout.strip() else None

    def _window(self, not_id=None, timeout=60):
        return poll(lambda: next((w for w in bc.live_windows() if w["id"] != not_id), None), timeout=timeout,
                    what=f"a scratch window other than {not_id}")

    @staticmethod
    def _names(window):
        return [os.path.basename(f) for f in window["folders"]]

    def _stays(self, wid, names):
        """The change landed in window ``wid`` and its extension host did not restart."""
        poll(lambda: [w for w in bc.live_windows() if w["id"] == wid and self._names(w) == names], timeout=30,
             what=f"window {wid} to list {names}")
        time.sleep(5)  # a restart takes 2-3 s to show; the probe on 2026-11-17 waited 15 s and saw none
        assert [w["id"] for w in bc.live_windows()] == [wid]

    def test_the_cli_refuses_to_end_the_window_it_runs_inside(self, scratch):
        w = self._window()
        cases = [["window", "info"], ["window", "reload"], ["window", "close"],
                 ["command", "run", "workbench.action.reloadWindow"], ["workspace", "add-folder", scratch["b"]],
                 ["--dry-run", "window", "open", "--reuse", scratch["b"]]]
        code = ("const cp = require('child_process');"
                f"const argv = {json.dumps(self.CLI_BASE)}, cases = {json.dumps(cases)}, id = {json.dumps(w['id'])};"
                "const run = (args) => new Promise((resolve) => cp.execFile(argv[0],"
                " argv.slice(1).concat(['--json', '--window', id], args), { timeout: 60000, windowsHide: true },"
                " (err, stdout, stderr) => resolve({ code: err ? err.code : 0, stdout, stderr })));"
                "const out = []; for (const c of cases) out.push(await run(c)); return out;")
        info, *refused = bc.BridgeClient(w, timeout=240).call("eval", {"code": code})
        assert info["code"] == 0, info
        assert json.loads(info["stdout"])["pid"] == int(w["id"])  # the CLI reached the window it runs in
        for case, res in zip(cases[1:], refused):
            assert res["code"] == 1 and json.loads(res["stdout"])["type"] == "hosting_window", (case, res)
        assert [x["id"] for x in bc.live_windows()] == [w["id"]] and self._names(self._window()) == ["a"]
        print(f"\n  refused inside window {w['id']}: {json.loads(refused[0]['stdout'])['error']}")

    def test_folder_changes_restart_the_extension_host_only_when_the_first_folder_changes(self, scratch):
        w0 = self._window()
        self._cli("--window", w0["id"], "workspace", "add-folder", scratch["b"])
        w1 = self._window(not_id=w0["id"])  # a single-folder window became a workspace: restarted
        assert self._names(w1) == ["a", "b"] and (w1["workspaceFile"] or "").startswith("untitled:")
        self._cli("--window", w1["id"], "workspace", "add-folder", scratch["c"])
        self._stays(w1["id"], ["a", "b", "c"])
        self._cli("--window", w1["id"], "workspace", "remove-folder", scratch["c"])
        self._stays(w1["id"], ["a", "b"])
        self._cli("--window", w1["id"], "workspace", "remove-folder", scratch["a"])
        w2 = self._window(not_id=w1["id"])  # the first folder changed: restarted
        assert self._names(w2) == ["b"]
        assert [f["name"] for f in self._cli("--window", w2["id"], "workspace", "folders")] == ["b"]
        print(f"\n  extension host pids: {w0['id']} -> add 2nd -> {w1['id']} -> add/remove 3rd -> {w1['id']} "
              f"-> remove 1st -> {w2['id']}")

    def test_reload_from_outside_restarts_the_extension_host(self, scratch):
        w = self._window()
        self._cli("--window", w["id"], "window", "reload")
        after = self._window(not_id=w["id"])
        assert self._names(after) == self._names(w)

    def test_close_from_outside(self, scratch):
        w = self._window()
        self._cli("--window", w["id"], "window", "close")
        poll(lambda: not bc.live_windows(), timeout=30, what="the scratch window to close")


# ── an editor built on VS Code (--app) ───────────────────────────────────
AGY = "antigravity-ide"


@pytest.fixture(scope="module")
def antigravity(tmp_path_factory):
    """An isolated Antigravity IDE with the bridge installed, registering in its own CLI home.

    Optional: skipped when Antigravity IDE is not installed (VS Code stays the hard
    dependency). The user's own Antigravity IDE windows are never touched: a separate
    --user-data-dir starts a separate instance, and only windows in this run's registry
    are closed.
    """
    if not backend.find_launcher(AGY):
        pytest.skip("Antigravity IDE is not installed")
    root = tmp_path_factory.mktemp("agy")
    udd, ext, ws, home = root / "udd", root / "ext", root / "ws", root / "cli-home"
    (udd / "User").mkdir(parents=True)
    ext.mkdir()
    ws.mkdir()
    (udd / "User" / "settings.json").write_text(json.dumps(TEST_SETTINGS, indent=4), encoding="utf-8")
    (ws / "broken.json").write_text('{"a": 1,, "b": }\n', encoding="utf-8")
    old_home = os.environ.get("CODEREINS_HOME")
    os.environ["CODEREINS_HOME"] = str(home)
    backend.select_app(AGY)
    try:
        info = bridge_mod.install(str(udd), str(ext))
        backend.run_code(["--new-window", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes",
                          str(ws)], user_data_dir=str(udd), extensions_dir=str(ext), capture=False, timeout=90)
        deadline = time.monotonic() + 120
        windows = []
        while time.monotonic() < deadline and not windows:
            windows = bc.live_windows()
            if not windows and time.monotonic() > deadline - 100 and not _instance_alive(udd, "Antigravity IDE.exe"):
                # Seen on the first two launches on 2026-11-17, never since; cause unknown.
                pytest.fail(f"The isolated Antigravity IDE exited at start without opening a window. "
                            f"See {udd / 'logs'} (main.log stops after the startup migrations); re-run.")
            time.sleep(1)
        if not windows:
            pytest.fail("Timed out after 120s waiting for the Antigravity IDE window to register its bridge")
        yield {"udd": str(udd), "ext": str(ext), "ws": str(ws), "home": str(home), "install": info,
               "client": bc.BridgeClient(windows[0], timeout=30)}
    finally:
        try:
            for w in bc.live_windows():
                bc.BridgeClient(w, timeout=5).call("window.close")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and bc.live_windows():
                time.sleep(0.5)
        except bc.BridgeError:
            pass
        _kill_instance(udd, "Antigravity IDE.exe")
        backend.select_app(None)
        if old_home is None:
            os.environ.pop("CODEREINS_HOME", None)
        else:
            os.environ["CODEREINS_HOME"] = old_home


class TestOtherEditor:
    """The same CLI and bridge, driving Antigravity IDE through --app."""
    CLI_BASE = _resolve_cli("codereins")

    def _json(self, *args):
        out = subprocess.run(self.CLI_BASE + ["--json"] + list(args), capture_output=True, text=True,
                             encoding="utf-8", check=True).stdout
        return json.loads(out)

    def test_bridge_installs_with_the_editors_own_launcher(self, antigravity):
        assert antigravity["install"]["name"] == "Antigravity IDE"
        assert bridge_mod.installed_version(antigravity["udd"], antigravity["ext"]) == bridge_mod.manifest()["version"]
        assert backend.version()["name"] == "Antigravity IDE"

    def test_window_reports_its_editor(self, antigravity):
        info = antigravity["client"].call("info")
        assert info["appName"] == "Antigravity IDE"
        assert re.match(r"^\d+\.\d+\.\d+", info["vscodeVersion"])

    def test_app_option_sees_only_that_editors_windows(self, antigravity):
        rows = self._json("--app", AGY, "window", "list")
        assert len(rows) == 1 and rows[0]["app"] == "Antigravity IDE"
        assert os.path.normcase(rows[0]["folders"][0]) == os.path.normcase(antigravity["ws"])
        assert self._json("--app", "vscode", "window", "list") == []
        err = subprocess.run(self.CLI_BASE + ["--json", "--app", "vscode", "window", "info"],
                             capture_output=True, text=True, encoding="utf-8")
        assert err.returncode == 1 and "Antigravity IDE" in json.loads(err.stdout)["error"]

    def test_language_server_answers_through_the_cli(self, antigravity):
        path = os.path.join(antigravity["ws"], "broken.json")
        self._json("--app", AGY, "file", "open", path)
        diags = poll(lambda: self._json("--app", AGY, "lang", "diagnostics", path), timeout=60,
                     what="JSON diagnostics in Antigravity IDE")
        assert any(d["severity"] == "error" for d in diags)

    def test_app_list_and_paths_name_the_editor(self, antigravity):
        rows = {r["app"]: r for r in self._json("app", "list")}
        assert rows[AGY]["installed"] and rows[AGY]["name"] == "Antigravity IDE" and rows[AGY]["live_windows"] == 1
        paths = self._json("--app", AGY, "app", "paths")
        assert paths["extensions_dir"].endswith(os.path.join(".antigravity-ide", "extensions"))
        assert os.path.basename(paths["user_data_dir"]) == "Antigravity IDE"
