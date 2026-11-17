"""Unit tests: synthetic data only. VS Code is not needed and never started."""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import click
import pytest
from click.testing import CliRunner

from codereins import vscode_cli as cli_mod
from codereins.core import bridge as bridge_mod
from codereins.core import export as export_mod
from codereins.core import project as proj_mod
from codereins.core import settings as settings_mod
from codereins.core import windows as windows_mod
from codereins.core.session import MAX_HISTORY, MAX_UNDO, Session
from codereins.utils import bridge_client as bc
from codereins.utils import jsonc
from codereins.utils import procs
from codereins.utils import vscode_backend as backend

SETTINGS = """// user settings
{
    // font
    "editor.fontSize": 14, // trailing note
    "files.exclude": {
        "**/.git": true, /* keep */
    },
    "url": "http://example.com/a//b",
}
"""


# ── jsonc ────────────────────────────────────────────────────────────────
class TestJsonc:
    def test_loads_comments_and_trailing_commas(self):
        data = jsonc.loads(SETTINGS)
        assert data["editor.fontSize"] == 14
        assert data["files.exclude"] == {"**/.git": True}

    def test_double_slash_inside_string_is_not_a_comment(self):
        assert jsonc.loads(SETTINGS)["url"] == "http://example.com/a//b"

    def test_empty_document(self):
        assert jsonc.loads("") == {}
        assert jsonc.loads("  // only a comment\n") == {}
        assert jsonc.loads("", default=[]) == []

    def test_invalid_document_raises(self):
        with pytest.raises(jsonc.JsoncError):
            jsonc.loads('{"a": }')
        with pytest.raises(jsonc.JsoncError):
            jsonc.scan('{"a": [1, 2')

    def test_set_existing_key_keeps_comments(self):
        out = jsonc.set_key(SETTINGS, "editor.fontSize", 16)
        assert jsonc.loads(out)["editor.fontSize"] == 16
        for note in ("// user settings", "// font", "// trailing note", "/* keep */"):
            assert note in out
        assert out.replace("16", "14") == SETTINGS

    def test_set_new_key_after_trailing_comma(self):
        out = jsonc.set_key(SETTINGS, "editor.tabSize", 2)
        data = jsonc.loads(out)
        assert data["editor.tabSize"] == 2 and data["editor.fontSize"] == 14
        assert '    "editor.tabSize": 2\n}' in out

    def test_set_new_key_adds_missing_comma(self):
        out = jsonc.set_key('{\n  "a": 1\n}\n', "b", True)
        assert out == '{\n  "a": 1,\n  "b": true\n}\n'

    def test_set_key_in_empty_object_and_empty_file(self):
        assert jsonc.loads(jsonc.set_key("{}", "a", 1)) == {"a": 1}
        assert jsonc.loads(jsonc.set_key("", "a", {"b": [1, 2]})) == {"a": {"b": [1, 2]}}

    def test_set_nested_value_is_indented(self):
        out = jsonc.set_key('{\n    "a": 1\n}\n', "b", {"x": 1})
        assert '    "b": {\n        "x": 1\n    }' in out

    def test_crlf_is_preserved(self):
        out = jsonc.set_key('{\r\n    "a": 1\r\n}\r\n', "b", 2)
        assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
        assert jsonc.loads(out) == {"a": 1, "b": 2}

    def test_duplicate_key_edits_the_one_json_uses(self):
        out = jsonc.set_key('{"a": 1, "a": 2}', "a", 3)
        assert jsonc.loads(out)["a"] == 3

    def test_remove_key_middle_and_absent(self):
        out, removed = jsonc.remove_key(SETTINGS, "files.exclude")
        assert removed and "files.exclude" not in jsonc.loads(out)
        assert "// font" in out and jsonc.loads(out)["editor.fontSize"] == 14
        same, removed = jsonc.remove_key(SETTINGS, "nope")
        assert not removed and same == SETTINGS

    def test_remove_last_key_drops_the_dangling_comma(self):
        out, removed = jsonc.remove_key('{\n  "a": 1,\n  "b": 2\n}\n', "b")
        assert removed and out == '{\n  "a": 1\n}\n'

    def test_array_append_and_remove(self):
        text = '// keys\n[\n    { "key": "ctrl+a", "command": "x" }\n]\n'
        out = jsonc.append_item(text, {"key": "ctrl+b", "command": "y"})
        assert [r["key"] for r in jsonc.loads(out)] == ["ctrl+a", "ctrl+b"] and "// keys" in out
        back = jsonc.remove_item(out, 0)
        assert [r["key"] for r in jsonc.loads(back)] == ["ctrl+b"]
        assert jsonc.loads(jsonc.append_item("", {"key": "f1"})) == [{"key": "f1"}]

    def test_wrong_container_kind(self):
        with pytest.raises(jsonc.JsoncError):
            jsonc.set_key("[1]", "a", 1)
        with pytest.raises(jsonc.JsoncError):
            jsonc.remove_item("[1]", 5)

    def test_file_round_trip_keeps_line_endings(self, tmp_path):
        f = tmp_path / "a.json"
        jsonc.write_file(f, '{\r\n}\r\n')
        assert f.read_bytes() == b'{\r\n}\r\n' and jsonc.read_file(f) == '{\r\n}\r\n'
        assert jsonc.read_file(tmp_path / "missing.json") == ""


# ── argument parsers ─────────────────────────────────────────────────────
class TestParsers:
    def test_location_plain_and_positions(self):
        assert cli_mod.parse_location("a/b.py") == ("a/b.py", None, None)
        assert cli_mod.parse_location("a/b.py:12") == ("a/b.py", 12, None)
        assert cli_mod.parse_location("a/b.py:12:5") == ("a/b.py", 12, 5)

    def test_location_windows_drive(self):
        assert cli_mod.parse_location(r"C:\x\b.py") == (r"C:\x\b.py", None, None)
        assert cli_mod.parse_location(r"C:\x\b.py:7:3") == (r"C:\x\b.py", 7, 3)

    def test_range_forms(self):
        assert cli_mod.parse_range("12") == [12, 1, 12, cli_mod.END_OF_LINE]
        assert cli_mod.parse_range("3-5") == [3, 1, 5, cli_mod.END_OF_LINE]
        assert cli_mod.parse_range("3:4") == [3, 4, 3, 4]
        assert cli_mod.parse_range("3:4-5:6") == [3, 4, 5, 6]

    def test_range_and_position_reject_garbage(self):
        with pytest.raises(click.UsageError):
            cli_mod.parse_range("a-b")
        with pytest.raises(click.UsageError):
            cli_mod.parse_position("1:2:3")
        assert cli_mod.parse_position("9") == [9, 1]

    def test_value_is_json_or_string(self):
        assert cli_mod.parse_value("true") is True
        assert cli_mod.parse_value("14") == 14
        assert cli_mod.parse_value('{"a": [1]}') == {"a": [1]}
        assert cli_mod.parse_value("Fira Code") == "Fira Code"

    def test_target_absolutises_paths_but_not_uris(self):
        assert os.path.isabs(cli_mod.target("x.txt"))
        assert cli_mod.target("untitled:Untitled-1") == "untitled:Untitled-1"
        assert cli_mod.target("vscode-remote://ssh/x") == "vscode-remote://ssh/x"


# ── project ──────────────────────────────────────────────────────────────
class TestProject:
    def test_create_add_remove_folder(self):
        proj = proj_mod.create_project(["api"])
        proj_mod.add_folder(proj, "web", name="Web")
        assert [f["path"] for f in proj["folders"]] == ["api", "web"]
        assert proj_mod.remove_folder(proj, "Web")["path"] == "web"
        with pytest.raises(RuntimeError):
            proj_mod.remove_folder(proj, "nope")

    def test_duplicate_folder_is_refused(self):
        proj = proj_mod.create_project(["api"])
        with pytest.raises(RuntimeError):
            proj_mod.add_folder(proj, "./api")

    def test_settings_and_recommendations(self):
        proj = proj_mod.create_project()
        proj_mod.set_setting(proj, "editor.tabSize", 2)
        assert proj_mod.unset_setting(proj, "editor.tabSize") is True
        assert proj_mod.unset_setting(proj, "editor.tabSize") is False
        assert proj_mod.recommend(proj, "a.b") == ["a.b"] and proj_mod.recommend(proj, "a.b") == ["a.b"]

    def test_save_new_and_reopen(self, tmp_path):
        path = str(tmp_path / "w.code-workspace")
        proj = proj_mod.create_project(["src"], {"a": 1})
        proj_mod.save_project(proj, path)
        assert proj_mod.open_project(path) == proj

    def test_save_existing_keeps_comments(self, tmp_path):
        path = tmp_path / "w.code-workspace"
        path.write_text('{\n    // my folders\n    "folders": [{"path": "a"}],\n    "settings": {} // none yet\n}\n',
                        encoding="utf-8")
        proj = proj_mod.open_project(str(path))
        proj_mod.set_setting(proj, "editor.tabSize", 2)
        proj_mod.save_project(proj, str(path))
        text = path.read_text(encoding="utf-8")
        assert "// my folders" in text and "// none yet" in text
        assert proj_mod.open_project(str(path))["settings"] == {"editor.tabSize": 2}

    def test_info_resolves_folders(self, tmp_path):
        (tmp_path / "src").mkdir()
        path = str(tmp_path / "w.code-workspace")
        info = proj_mod.get_project_info(proj_mod.create_project(["src", "gone"]), path)
        assert info["name"] == "w" and info["folder_count"] == 2
        assert [f["exists"] for f in info["folders"]] == [True, False]

    def test_open_rejects_missing_and_non_object(self, tmp_path):
        with pytest.raises(RuntimeError):
            proj_mod.open_project(str(tmp_path / "missing.code-workspace"))
        bad = tmp_path / "bad.code-workspace"
        bad.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(RuntimeError):
            proj_mod.open_project(str(bad))

    def test_list_profiles(self, tmp_path):
        store = tmp_path / "User" / "globalStorage"
        store.mkdir(parents=True)
        (store / "storage.json").write_text(json.dumps({"userDataProfiles": [{"name": "Work", "location": "abc"}]}),
                                            encoding="utf-8")
        profiles = proj_mod.list_profiles(str(tmp_path))
        assert [p["name"] for p in profiles] == ["Default", "Work"]
        assert profiles[1]["settings"].endswith(os.path.join("profiles", "abc", "settings.json"))
        assert len(proj_mod.list_profiles(str(tmp_path / "empty"))) == 1

    def test_profile_borrowing_default_resources(self, tmp_path):
        store = tmp_path / "User" / "globalStorage"
        store.mkdir(parents=True)
        flags = {k: True for k in ("settings", "keybindings", "extensions", "snippets", "tasks")}
        (store / "storage.json").write_text(json.dumps({"userDataProfiles": [
            {"name": "Agents", "location": "builtin/agents", "useDefaultFlags": flags},
            {"name": "Py", "location": "x1", "useDefaultFlags": {"keybindings": True}},
        ]}), encoding="utf-8")
        default, agents, py = proj_mod.list_profiles(str(tmp_path), str(tmp_path / "ext"))
        assert agents["settings"] == default["settings"] and agents["keybindings"] == default["keybindings"]
        assert agents["own_extensions"] is False and "extensions" in agents["shares_default"]
        assert py["settings"].endswith(os.path.join("profiles", "x1", "settings.json"))
        assert py["keybindings"] == default["keybindings"] and py["own_extensions"] is True


# ── session ──────────────────────────────────────────────────────────────
class TestSession:
    def _session(self, tmp_path):
        return Session(str(tmp_path / "session.json"))

    def test_snapshot_undo_redo(self, tmp_path):
        s = self._session(tmp_path)
        s.set_project(proj_mod.create_project(), None)
        s.snapshot("add a")
        proj_mod.add_folder(s.get_project(), "a")
        assert s.undo() == "add a" and s.get_project()["folders"] == []
        assert s.redo() == "add a" and s.get_project()["folders"] == [{"path": "a"}]

    def test_undo_redo_on_empty_stack(self, tmp_path):
        s = self._session(tmp_path)
        s.set_project(proj_mod.create_project(), None)
        with pytest.raises(RuntimeError):
            s.undo()
        with pytest.raises(RuntimeError):
            s.redo()
        with pytest.raises(RuntimeError):
            self._session(tmp_path).get_project()

    def test_undo_stack_is_capped(self, tmp_path):
        s = self._session(tmp_path)
        s.set_project(proj_mod.create_project(), None)
        for i in range(MAX_UNDO + 10):
            s.snapshot(str(i))
        assert len(s._undo) == MAX_UNDO

    def test_backup_and_restore_file(self, tmp_path):
        s = self._session(tmp_path)
        f = tmp_path / "settings.json"
        f.write_text("old", encoding="utf-8")
        backup = s.backup_file(str(f))
        f.write_text("new", encoding="utf-8")
        assert Path(backup).read_text(encoding="utf-8") == "old"
        assert s.restore_last()["action"] == "restored" and f.read_text(encoding="utf-8") == "old"
        with pytest.raises(RuntimeError):
            s.restore_last()

    def test_restore_removes_a_file_that_did_not_exist(self, tmp_path):
        s = self._session(tmp_path)
        f = tmp_path / "new.json"
        assert s.backup_file(str(f)) is None
        f.write_text("{}", encoding="utf-8")
        s.restore_last()
        assert not f.exists()

    def test_state_persists_across_instances(self, tmp_path):
        s = self._session(tmp_path)
        s.set_window("123")
        s.record("terminal send")
        again = self._session(tmp_path)
        assert again.state["window"] == "123"
        assert again.state["history"][-1]["command"] == "terminal send"

    def test_history_is_capped_and_corrupt_state_is_tolerated(self, tmp_path):
        s = self._session(tmp_path)
        for i in range(MAX_HISTORY + 5):
            s.record(f"c{i}")
        assert len(s.state["history"]) == MAX_HISTORY
        (tmp_path / "session.json").write_text("{not json", encoding="utf-8")
        assert self._session(tmp_path).state == {"window": None, "history": [], "backups": []}

    def test_save_session_writes_and_backs_up(self, tmp_path):
        s = self._session(tmp_path)
        path = str(tmp_path / "w.code-workspace")
        proj_mod.save_project(proj_mod.create_project(["a"]), path)
        s.set_project(proj_mod.open_project(path), path)
        s.snapshot("add b")
        proj_mod.add_folder(s.get_project(), "b")
        assert s.status()["modified"] is True
        s.save_session()
        assert len(proj_mod.open_project(path)["folders"]) == 2 and s.status()["modified"] is False
        s.restore_last()
        assert len(proj_mod.open_project(path)["folders"]) == 1


# ── settings and keybindings ─────────────────────────────────────────────
class TestSettings:
    def test_set_get_unset(self, tmp_path):
        f = tmp_path / "User" / "settings.json"
        res = settings_mod.set_value(f, "editor.fontSize", 15)
        assert res["changed"] and res["old"] is None
        assert settings_mod.get_value(f, "editor.fontSize") == {"key": "editor.fontSize", "found": True, "value": 15,
                                                                "path": str(f)}
        assert settings_mod.unset_value(f, "editor.fontSize")["removed"] is True
        assert settings_mod.get_value(f, "editor.fontSize")["found"] is False

    def test_dry_run_writes_nothing(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(SETTINGS, encoding="utf-8")
        res = settings_mod.set_value(f, "editor.fontSize", 99, dry_run=True)
        assert res["dry_run"] and res["new"] == 99 and f.read_text(encoding="utf-8") == SETTINGS
        settings_mod.unset_value(f, "editor.fontSize", dry_run=True)
        assert f.read_text(encoding="utf-8") == SETTINGS

    def test_write_is_undoable_through_the_session(self, tmp_path):
        s = Session(str(tmp_path / "session.json"))
        f = tmp_path / "settings.json"
        f.write_text(SETTINGS, encoding="utf-8")
        settings_mod.set_value(f, "editor.fontSize", 20, session=s)
        assert settings_mod.read(f)["editor.fontSize"] == 20
        s.restore_last()
        assert f.read_text(encoding="utf-8") == SETTINGS

    def test_scope_paths(self, tmp_path):
        user = settings_mod.settings_file("user", user_data_dir=str(tmp_path))
        assert user == tmp_path / "User" / "settings.json"
        ws = settings_mod.settings_file("workspace", folder=str(tmp_path))
        assert ws == tmp_path / ".vscode" / "settings.json"
        with pytest.raises(RuntimeError):
            settings_mod.settings_file("machine")

    def test_keybindings_add_and_remove(self, tmp_path):
        f = tmp_path / "keybindings.json"
        settings_mod.add_keybinding(f, "ctrl+alt+t", "workbench.action.terminal.new")
        settings_mod.add_keybinding(f, "ctrl+alt+y", "x", when="editorTextFocus", args={"a": 1})
        rules = settings_mod.keybindings(f)
        assert [r["key"] for r in rules] == ["ctrl+alt+t", "ctrl+alt+y"] and rules[1]["args"] == {"a": 1}
        res = settings_mod.remove_keybinding(f, command="x")
        assert len(res["removed"]) == 1 and [r["key"] for r in settings_mod.keybindings(f)] == ["ctrl+alt+t"]
        with pytest.raises(RuntimeError):
            settings_mod.remove_keybinding(f)

    def test_non_object_settings_file_is_rejected(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text("[1]", encoding="utf-8")
        with pytest.raises(RuntimeError):
            settings_mod.read(f)


# ── window selection ─────────────────────────────────────────────────────
def _win(wid, folders=(), name=None, ws_file=None):
    return {"id": wid, "folders": list(folders), "name": name, "workspaceFile": ws_file, "token": "t", "endpoint": "e"}


class TestWindows:
    ROOT = os.path.abspath(os.sep + "work")

    def _windows(self):
        return [_win("1", [os.path.join(self.ROOT, "a")], "a"),
                _win("2", [os.path.join(self.ROOT, "a", "nested")], "nested"),
                _win("3", [], "Other", os.path.join(self.ROOT, "o", "o.code-workspace"))]

    def test_by_id_and_focused(self):
        ws = self._windows()
        assert windows_mod.select(ws, "2")["id"] == "2"
        assert windows_mod.select(ws, "focused")["id"] == "1"

    def test_by_path_longest_root_wins(self):
        ws = self._windows()
        assert windows_mod.select(ws, os.path.join(self.ROOT, "a", "nested", "f.py"))["id"] == "2"
        assert windows_mod.select(ws, os.path.join(self.ROOT, "a", "f.py"))["id"] == "1"
        assert windows_mod.select(ws, os.path.join(self.ROOT, "o", "o.code-workspace"))["id"] == "3"

    def test_sibling_folder_with_shared_prefix_does_not_match(self):
        with pytest.raises(bc.BridgeError) as e:
            windows_mod.select(self._windows()[:1], os.path.join(self.ROOT, "ab", "f.py"))
        assert e.value.code == "no_such_window"

    def test_by_name(self):
        assert windows_mod.select(self._windows(), "other")["id"] == "3"

    def test_default_order_preferred_then_cwd_then_newest(self):
        ws = self._windows()
        assert windows_mod.select(ws, None, "3")["id"] == "3"
        assert windows_mod.select(ws, None, "gone", os.path.join(self.ROOT, "a", "nested"))["id"] == "2"
        assert windows_mod.select(ws, None, None, os.path.abspath(os.sep + "elsewhere"))["id"] == "1"

    def test_for_app_filters_and_names_the_other_editors(self):
        ws = [{"id": "1", "appName": "Visual Studio Code"}, {"id": "2", "appName": "Antigravity IDE"}]
        assert [w["id"] for w in windows_mod.for_app(ws, "antigravity ide")] == ["2"]
        assert windows_mod.for_app(ws[:1], "Antigravity IDE", strict=False) == []
        with pytest.raises(bc.BridgeError) as e:
            windows_mod.for_app(ws[:1], "Antigravity IDE")
        assert e.value.code == "no_windows" and "Visual Studio Code" in e.value.message
        assert windows_mod.describe(ws[1])["app"] == "Antigravity IDE"

    def test_for_app_keeps_everything_when_no_editor_was_chosen(self, monkeypatch):
        monkeypatch.setattr(backend, "app_name", lambda: None)
        ws = [{"id": "1", "appName": "A"}, {"id": "2"}]
        assert windows_mod.for_app(ws) == ws

    def test_no_windows(self):
        with pytest.raises(bc.BridgeError) as e:
            windows_mod.select([])
        assert e.value.code == "no_windows" and "bridge install" in e.value.message

    def test_describe_hides_secrets(self):
        d = windows_mod.describe(self._windows()[0])
        assert "token" not in d and "endpoint" not in d and d["id"] == "1"


# ── bridge client ────────────────────────────────────────────────────────
class FakeTransport:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.sent = reply, error, []

    def __call__(self, endpoint, payload, timeout):
        self.sent.append((endpoint, json.loads(payload), timeout))
        if self.error:
            raise self.error
        return json.dumps(self.reply).encode() if not isinstance(self.reply, bytes) else self.reply


class TestBridgeClient:
    WINDOW = {"id": "1", "endpoint": "pipe-x", "token": "secret"}

    def test_request_shape_and_result(self):
        t = FakeTransport({"id": 1, "ok": True, "result": {"pong": True}})
        c = bc.BridgeClient(self.WINDOW, timeout=7, transport=t)
        assert c.call("ping", {"a": 1, "b": None}) == {"pong": True}
        endpoint, req, timeout = t.sent[0]
        assert endpoint == "pipe-x" and timeout == 7
        assert req == {"id": 1, "token": "secret", "method": "ping", "params": {"a": 1}}

    def test_error_reply_becomes_bridge_error(self):
        t = FakeTransport({"id": 1, "ok": False, "error": {"code": "no_active_editor", "message": "none"}})
        with pytest.raises(bc.BridgeError) as e:
            bc.BridgeClient(self.WINDOW, transport=t).call("editor.state")
        assert e.value.code == "no_active_editor" and e.value.message == "none"

    def test_dry_run_does_not_send_mutations(self):
        t = FakeTransport({"id": 1, "ok": True, "result": 1})
        c = bc.BridgeClient(self.WINDOW, dry_run=True, transport=t)
        out = c.call("doc.edit", {"path": "x"}, mutating=True)
        assert out == {"dry_run": True, "method": "doc.edit", "params": {"path": "x"}} and t.sent == []
        assert c.call("editor.state") == 1 and len(t.sent) == 1

    def test_empty_and_unreadable_replies(self):
        for raw, code in ((b"", "no_reply"), (b"not json", "bad_reply")):
            with pytest.raises(bc.BridgeError) as e:
                bc.BridgeClient(self.WINDOW, transport=FakeTransport(raw)).call("ping")
            assert e.value.code == code

    def test_per_call_timeout_overrides_default(self):
        t = FakeTransport({"ok": True, "result": None})
        bc.BridgeClient(self.WINDOW, timeout=5, transport=t).call("x", timeout=99)
        assert t.sent[0][2] == 99

    def test_live_windows_prunes_dead_registrations(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEREINS_HOME", str(tmp_path))
        reg = tmp_path / "windows"
        reg.mkdir()
        (reg / "1.json").write_text(json.dumps({"id": "1", "endpoint": "e", "token": "t"}), encoding="utf-8")
        (reg / "junk.json").write_text("{", encoding="utf-8")
        assert [w["id"] for w in bc.read_registry()] == ["1"]
        dead = FakeTransport(error=bc.BridgeError("not_running", "gone"))
        assert bc.live_windows(transport=dead) == [] and not (reg / "1.json").exists()

    def test_live_windows_keeps_slow_windows_registered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEREINS_HOME", str(tmp_path))
        reg = tmp_path / "windows"
        reg.mkdir()
        (reg / "1.json").write_text(json.dumps({"id": "1", "endpoint": "e", "token": "t"}), encoding="utf-8")
        slow = FakeTransport(error=bc.BridgeError("timeout", "busy"))
        assert bc.live_windows(transport=slow) == [] and (reg / "1.json").exists()

    def test_registry_sorts_newest_focus_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEREINS_HOME", str(tmp_path))
        reg = tmp_path / "windows"
        reg.mkdir()
        for wid, focused, at in (("1", False, 50), ("2", True, 10), ("3", False, 90)):
            (reg / f"{wid}.json").write_text(json.dumps({"id": wid, "endpoint": "e", "token": "t", "focused": focused,
                                                         "lastFocusedAt": at}), encoding="utf-8")
        assert [w["id"] for w in bc.read_registry()] == ["2", "3", "1"]


# ── bridge packaging ─────────────────────────────────────────────────────
class TestVsix:
    def test_vsix_entries(self, tmp_path):
        vsix = bridge_mod.build_vsix(str(tmp_path))
        with zipfile.ZipFile(vsix) as z:
            assert z.testzip() is None
            assert set(z.namelist()) == {"[Content_Types].xml", "extension.vsixmanifest", "extension/package.json",
                                         "extension/extension.js", "extension/README.md"}
            assert json.loads(z.read("extension/package.json")) == bridge_mod.manifest()

    def test_manifest_identity_matches_package_json(self, tmp_path):
        m = bridge_mod.manifest()
        with zipfile.ZipFile(bridge_mod.build_vsix(str(tmp_path))) as z:
            root = ElementTree.fromstring(z.read("extension.vsixmanifest"))
        ns = {"v": "http://schemas.microsoft.com/developer/vsx-schema/2011"}
        ident = root.find("v:Metadata/v:Identity", ns)
        assert ident.get("Id") == m["name"] and ident.get("Version") == m["version"]
        assert ident.get("Publisher") == m["publisher"]
        assert bridge_mod.extension_id() == f"{m['publisher']}.{m['name']}"

    def test_bridge_declares_its_security_settings_at_application_scope(self):
        props = bridge_mod.manifest()["contributes"]["configuration"]["properties"]
        assert props["codereinsBridge.allowEval"] == {**props["codereinsBridge.allowEval"], "default": False,
                                                        "scope": "application"}
        assert props["codereinsBridge.enabled"]["scope"] == "application"


# ── backend ──────────────────────────────────────────────────────────────
class TestBridgeProfiles:
    def test_install_covers_profiles_with_their_own_extensions(self, tmp_path, monkeypatch):
        store = tmp_path / "User" / "globalStorage"
        store.mkdir(parents=True)
        (store / "storage.json").write_text(json.dumps({"userDataProfiles": [
            {"name": "Agents", "location": "builtin/agents", "useDefaultFlags": {"extensions": True}},
            {"name": "Work", "location": "w1"},
        ]}), encoding="utf-8")
        installed: list = []
        monkeypatch.setattr(backend, "install_extension",
                            lambda target, **kw: installed.append(kw["profile"]) or "ok")
        monkeypatch.setattr(backend, "list_extensions",
                            lambda udd, ext, profile=None: [{"id": bridge_mod.extension_id(), "version": "0.1.0"}]
                            if profile in installed else [])
        monkeypatch.setattr(backend, "product", lambda *a: {})
        res = bridge_mod.install(str(tmp_path), str(tmp_path / "ext"))
        assert installed == [None, "Work"]  # Agents borrows Default's extensions: no --profile needed
        assert [p["profile"] for p in res["profiles"]] == ["Default", "Work"]


class TestBackend:
    def test_extension_id_validation(self):
        for good in ("ms-python.python", "ms-python.python@2024.1.0", "a.b-c_d"):
            assert backend.EXTENSION_ID.match(good)
        for bad in ("python", "a.b & calc", "--force", "a.b;rm -rf", "../x.y", "a.b@1 2"):
            assert not backend.EXTENSION_ID.match(bad)

    def test_install_and_uninstall_refuse_bad_targets_before_running_code(self, monkeypatch):
        monkeypatch.setattr(backend, "run_code", lambda *a, **k: pytest.fail("code must not run"))
        with pytest.raises(RuntimeError):
            backend.install_extension("not an id")
        with pytest.raises(RuntimeError):
            backend.install_extension("missing-file.vsix")
        with pytest.raises(RuntimeError):
            backend.uninstall_extension("a.b --force")

    def test_env_cleanup_pattern(self):
        for drop in ("ELECTRON_RUN_AS_NODE", "VSCODE_PID", "VSCODE_IPC_HOOK", "VSCODE_NLS_CONFIG"):
            assert backend._ENV_DROP.match(drop)
        for keep in ("PATH", "VSCODE_PORTABLE", "VSCODE_IPC_HOOK_CLI", "CODEREINS_HOME", "ELECTRON"):
            assert not backend._ENV_DROP.match(keep)

    def test_paths_layout(self, tmp_path):
        p = backend.paths(str(tmp_path / "udd"), str(tmp_path / "ext"))
        assert p["settings"] == tmp_path / "udd" / "User" / "settings.json"
        assert p["keybindings"].name == "keybindings.json" and p["extensions_dir"] == tmp_path / "ext"

    def test_batch_launcher_is_bypassed(self, tmp_path):
        if os.name != "nt":
            assert backend.launcher("/usr/bin/code") == (["/usr/bin/code"], {}, False)
            return
        (tmp_path / "bin").mkdir()
        (tmp_path / "abc" / "resources" / "app" / "out").mkdir(parents=True)
        (tmp_path / "Code.exe").write_bytes(b"")
        (tmp_path / "abc" / "resources" / "app" / "out" / "cli.js").write_text("", encoding="utf-8")
        cmd = tmp_path / "bin" / "code.cmd"
        cmd.write_text('@echo off\n"%~dp0..\\Code.exe" "%~dp0..\\abc\\resources\\app\\out\\cli.js" %*\n', encoding="utf-8")
        prefix, env, via_batch = backend.launcher(str(cmd))
        assert [Path(p).name for p in prefix] == ["Code.exe", "cli.js"] and not via_batch
        assert env == {"ELECTRON_RUN_AS_NODE": "1"}
        odd = tmp_path / "bin" / "other.cmd"
        odd.write_text("@echo off\n", encoding="utf-8")
        assert backend.launcher(str(odd)) == ([str(odd)], {}, True)

    @pytest.fixture
    def fake_ide(self, tmp_path, monkeypatch):
        """A fake 'Antigravity IDE' user install: launcher batch file, exe, cli.js, product.json."""
        if os.name != "nt":
            pytest.skip("the fake install uses the Windows layout")
        local, roaming, user_home, empty = (tmp_path / n for n in ("local", "roaming", "home", "empty"))
        app = local / "Programs" / "Antigravity IDE"
        (app / "bin").mkdir(parents=True)
        (app / "resources" / "app" / "out").mkdir(parents=True)
        empty.mkdir()
        (app / "Antigravity IDE.exe").write_bytes(b"")
        (app / "resources" / "app" / "out" / "cli.js").write_text("", encoding="utf-8")
        (app / "resources" / "app" / "product.json").write_text(json.dumps(
            {"nameShort": "Antigravity IDE", "nameLong": "Antigravity IDE", "dataFolderName": ".antigravity-ide",
             "version": "1.107.0"}), encoding="utf-8")
        cmd = app / "bin" / "antigravity-ide.cmd"
        cmd.write_text('@echo off\n"%~dp0..\\Antigravity IDE.exe" "%~dp0..\\resources\\app\\out\\cli.js" %*\n',
                       encoding="utf-8")
        for var, val in (("LOCALAPPDATA", local), ("ProgramFiles", empty), ("APPDATA", roaming),
                         ("USERPROFILE", user_home), ("PATH", empty)):
            monkeypatch.setenv(var, str(val))
        monkeypatch.delenv("CODEREINS_BIN", raising=False)
        monkeypatch.delenv(backend.APP_ENV, raising=False)
        yield {"cmd": cmd, "roaming": roaming, "home": user_home}
        backend.select_app(None)

    def test_app_paths_come_from_its_product_json(self, fake_ide):
        backend.select_app("antigravity-ide")
        assert backend.find_code() == str(fake_ide["cmd"])
        assert backend.app_name() == "Antigravity IDE"
        p = backend.paths()
        assert p["settings"] == fake_ide["roaming"] / "Antigravity IDE" / "User" / "settings.json"
        assert p["extensions_dir"] == fake_ide["home"] / ".antigravity-ide" / "extensions"
        info = backend.app_info("antigravity-ide")
        assert info["installed"] and info["version"] == "1.107.0" and info["name"] == "Antigravity IDE"

    def test_default_app_is_the_first_installed(self, fake_ide):
        assert backend.current_app() is None and backend.app_name() is None
        assert backend.find_code() == str(fake_ide["cmd"])  # VS Code is absent on the fake machine
        assert backend.app_info("vscode") == {"app": "vscode", "installed": False}

    def test_chosen_app_that_is_missing_or_unknown_fails_clearly(self, fake_ide, monkeypatch):
        backend.select_app("vscode")
        with pytest.raises(RuntimeError, match="vscode was not found"):
            backend.find_code()
        with pytest.raises(RuntimeError, match="vscode was not found"):
            backend.paths()  # a chosen editor must exist: no silent fall back to VS Code's folders
        with pytest.raises(RuntimeError, match="Unknown app"):
            backend.select_app("notepad")
        backend.select_app(None)
        monkeypatch.setenv(backend.APP_ENV, "notepad")
        with pytest.raises(RuntimeError, match="not a known app"):
            backend.current_app()

    def test_find_code_honours_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEREINS_BIN", str(tmp_path / "missing"))
        with pytest.raises(RuntimeError):
            backend.find_code()
        real = tmp_path / "code"
        real.write_text("", encoding="utf-8")
        monkeypatch.setenv("CODEREINS_BIN", str(real))
        assert backend.find_code() == str(real)


# ── export ───────────────────────────────────────────────────────────────
DIAGS = [{"path": "a.json", "range": {"start": {"line": 3, "column": 2}, "end": {"line": 3, "column": 4}},
          "severity": "error", "message": "Value | expected", "source": "json", "code": 516}]


class TestExport:
    def test_presets_are_complete(self):
        for name, spec in export_mod.EXPORT_PRESETS.items():
            assert set(spec) == {"format", "needs", "description"} and spec["needs"] in ("window", "code"), name

    def test_diagnostics_markdown(self, tmp_path):
        client = bc.BridgeClient({"endpoint": "e", "token": "t"},
                                 transport=FakeTransport({"ok": True, "result": DIAGS}))
        out = tmp_path / "sub" / "problems.md"
        res = export_mod.render("diagnostics-md", str(out), client=client)
        text = out.read_text(encoding="utf-8")
        assert res["items"] == 1 and res["file_size"] == out.stat().st_size > 0
        assert "## a.json" in text and "| error | 3 | Value \\| expected | json |" in text

    def test_guards(self, tmp_path):
        out = tmp_path / "x.json"
        out.write_text("keep", encoding="utf-8")
        client = bc.BridgeClient({"endpoint": "e", "token": "t"}, transport=FakeTransport({"ok": True, "result": []}))
        with pytest.raises(RuntimeError, match="Unknown preset"):
            export_mod.render("pdf", str(out), client=client)
        with pytest.raises(RuntimeError, match="exists"):
            export_mod.render("diagnostics-json", str(out), client=client)
        with pytest.raises(RuntimeError, match="needs a running"):
            export_mod.render("diagnostics-json", str(tmp_path / "y.json"))
        assert out.read_text(encoding="utf-8") == "keep"
        export_mod.render("diagnostics-json", str(out), client=client, overwrite=True)
        assert json.loads(out.read_text(encoding="utf-8")) == []


# ── CLI through click (no VS Code) ───────────────────────────────────────
@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEREINS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli_mod, "_session", None)
    monkeypatch.setattr(cli_mod, "_repl_mode", False)

    def _run(*args):
        monkeypatch.setattr(cli_mod, "_session", None)
        return CliRunner().invoke(cli_mod.cli, ["--json", "--user-data-dir", str(tmp_path / "udd"), *args])
    return _run


class TestCLI:
    def test_help_lists_every_group(self):
        res = CliRunner().invoke(cli_mod.cli, ["--help"])
        assert res.exit_code == 0
        for group in ("app", "window", "project", "file", "editor", "command", "workspace", "lang", "terminal",
                      "task", "debug", "ext", "config", "ui", "git", "export", "bridge", "session", "repl"):
            assert group in res.output

    def test_app_option_is_validated_and_listed(self):
        res = CliRunner().invoke(cli_mod.cli, ["--app", "notepad", "app", "paths"])
        assert res.exit_code == 2 and "notepad" in res.output
        res = CliRunner().invoke(cli_mod.cli, ["--help"])
        assert "--app" in res.output and "antigravity-ide" in res.output

    def test_every_command_has_help(self):
        for name, group in cli_mod.cli.commands.items():
            for sub in getattr(group, "commands", {}).values():
                assert sub.help, f"{name} {sub.name} has no help text"

    def test_project_workflow(self, run, tmp_path):
        ws = str(tmp_path / "team.code-workspace")
        assert run("project", "new", "-o", ws, "--folder", "api").exit_code == 0
        assert run("--project", ws, "project", "add-folder", "web", "--name", "Web").exit_code == 0
        assert run("--project", ws, "project", "set", "editor.tabSize", "2").exit_code == 0
        info = json.loads(run("--project", ws, "project", "info").output)
        assert [f["path"] for f in info["folders"]] == ["api", "web"] and info["settings"] == {"editor.tabSize": 2}
        dup = run("--project", ws, "project", "add-folder", "api")
        assert dup.exit_code == 1 and json.loads(dup.output)["type"] == "RuntimeError"
        assert run("project", "new", "-o", ws).exit_code == 1

    def test_project_dry_run_leaves_file_alone(self, run, tmp_path):
        ws = tmp_path / "team.code-workspace"
        run("project", "new", "-o", str(ws), "--folder", "api")
        before = ws.read_text(encoding="utf-8")
        res = run("--dry-run", "--project", str(ws), "project", "add-folder", "web")
        assert res.exit_code == 0 and json.loads(res.output)["folder_count"] == 2
        assert ws.read_text(encoding="utf-8") == before

    def test_config_workflow_and_undo(self, run, tmp_path):
        settings = tmp_path / "udd" / "User" / "settings.json"
        assert json.loads(run("config", "set", "editor.fontSize", "15").output)["new"] == 15
        assert json.loads(run("config", "get", "editor.fontSize").output)["value"] == 15
        assert json.loads(run("config", "list").output) == {"editor.fontSize": 15}
        run("config", "set", "editor.fontSize", "18")
        assert json.loads(run("session", "undo").output)["action"] == "restored"
        assert json.loads(settings.read_text(encoding="utf-8")) == {"editor.fontSize": 15}
        assert json.loads(run("config", "unset", "editor.fontSize").output)["removed"] is True
        history = [h["command"] for h in json.loads(run("session", "history").output)]
        assert history == ["config set", "config set", "config unset"]

    def test_config_dry_run_and_workspace_scope(self, run, tmp_path):
        res = run("--dry-run", "config", "set", "a.b", "true")
        assert json.loads(res.output)["dry_run"] is True and not (tmp_path / "udd" / "User" / "settings.json").exists()
        run("config", "set", "a.b", "true", "--scope", "workspace", "--folder", str(tmp_path / "repo"))
        assert json.loads((tmp_path / "repo" / ".vscode" / "settings.json").read_text(encoding="utf-8")) == {"a.b": True}

    def test_keybinding_commands(self, run):
        run("config", "bind", "ctrl+alt+t", "workbench.action.terminal.new")
        assert json.loads(run("config", "keys").output) == [{"key": "ctrl+alt+t",
                                                              "command": "workbench.action.terminal.new"}]
        assert len(json.loads(run("config", "unbind", "--key", "ctrl+alt+t").output)["removed"]) == 1

    def test_no_window_is_a_clear_json_error(self, run):
        res = run("editor", "state")
        assert res.exit_code == 1
        err = json.loads(res.output)
        assert err["type"] == "no_windows" and "bridge install" in err["error"]

    def test_usage_errors_exit_2(self, run):
        assert run("lang", "hover", "file-without-line.py").exit_code == 2
        assert run("editor", "select", "x-y").exit_code == 2
        assert run("ui", "open-url", "file:///etc/passwd").exit_code == 1

    def test_session_status_and_export_presets(self, run):
        status = json.loads(run("session", "status").output)
        assert status["window"] is None and status["undo_depth"] == 0
        presets = json.loads(run("export", "presets").output)
        assert {p["preset"] for p in presets} == set(export_mod.EXPORT_PRESETS)


# ── the window that hosts this CLI ───────────────────────────────────────
class TestProcs:
    def test_chain_of_this_process_starts_at_its_parent(self):
        anc = procs.ancestors()
        assert anc[0] == os.getppid() and os.getpid() not in anc

    def test_a_child_process_counts_this_process_as_an_ancestor(self):
        code = "import json; from codereins.utils import procs; print(json.dumps(procs.ancestors()))"
        anc = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                        check=True).stdout)
        assert os.getpid() in anc and os.getppid() in anc

    def test_parent_of_this_process(self):
        assert procs.parent(os.getpid()) == os.getppid()

    def test_chain_stops_at_a_pid_that_was_reused(self):
        # pid 5 started after pid 10, so it cannot be 10's parent: 10's real parent exited.
        assert procs.chain(10, {10: 5, 5: 1}.get, {10: 100.0, 5: 200.0, 1: 1.0}.get) == []
        assert procs.chain(10, {10: 5, 5: 1}.get, {10: 3.0, 5: 2.0, 1: 1.0}.get) == [5, 1]

    def test_chain_keeps_links_it_cannot_date_and_ends_on_a_cycle(self):
        # Unknown start times keep the link: a refused command is cheap, a missed host is not.
        assert procs.chain(10, {10: 5, 5: 10}.get, lambda pid: None) == [5]


class TestHostGuard:
    HOST, OTHER = "/w/site", "/w/other"

    def test_refuses_the_window_whose_extension_host_started_this_cli(self):
        with pytest.raises(bc.BridgeError) as e:
            windows_mod.refuse_if_hosting({**_win("100", [self.HOST], "site"), "pid": 100}, "reload",
                                          ancestors=[7, 100, 1])
        assert e.value.code == "hosting_window" and "100" in e.value.message and "site" in e.value.message

    def test_lets_other_windows_through(self):
        windows_mod.refuse_if_hosting({**_win("200", [self.OTHER]), "pid": 200}, "reload", ancestors=[7, 100, 1])

    def test_whole_editor_commands_refuse_every_window_of_the_hosting_editor(self):
        parent = {100: 50, 200: 50, 300: 60}.get  # 100 and 200: one editor process; 300: another
        with pytest.raises(bc.BridgeError):
            windows_mod.refuse_if_hosting(_win("200"), "quit", reach="app", ancestors=[100, 50, 1], parent_of=parent)
        windows_mod.refuse_if_hosting(_win("300"), "quit", reach="app", ancestors=[100, 50, 1], parent_of=parent)

    def test_hosting_picks_the_ancestor_window(self):
        ws = [_win("200"), _win("100")]
        assert windows_mod.hosting(ws, ancestors=[100, 1])["id"] == "100"
        assert windows_mod.hosting(ws, ancestors=[1]) is None

    @pytest.mark.parametrize("command_id,args,reach", [
        ("workbench.action.reloadWindow", [], "window"),
        ("workbench.action.reloadWindowWithExtensionsDisabled", [], "window"),
        ("antigravity.reloadWindow", [], "window"),
        ("python.reloadVSCode", [], "window"),
        ("workbench.action.restartExtensionHost", [], "window"),
        ("workbench.action.closeWindow", [], "window"),
        ("workbench.action.closeFolder", [], "window"),
        ("antigravity-dev-containers.reopenFolderLocally", [], "window"),
        ("vscode.openFolder", [{"$uri": "/x"}], "window"),
        ("vscode.openFolder", [{"$uri": "/x"}, True], None),
        ("vscode.openFolder", [{"$uri": "/x"}, {"forceNewWindow": True}], None),
        ("workbench.action.quit", [], "app"),
        ("update.restartToUpdate", [], "app"),
        ("notebook.cell.quitEdit", [], None),
        ("workbench.action.reopenClosedEditor", [], None),
        ("workbench.action.webview.reloadWebviewAction", [], None),
        ("typescript.restartTsServer", [], None),
        ("editor.action.formatDocument", [], None),
    ])
    def test_command_reach(self, command_id, args, reach):
        assert windows_mod.command_reach(command_id, args) == reach

    def test_which_folder_changes_restart_the_extension_host(self):
        # Measured on VS Code 1.146 (TEST.md Part 4): the first folder changing restarts it, and so does
        # turning a single-folder window into a workspace.
        single = _win("1", [self.HOST])
        multi = _win("1", [self.HOST, self.OTHER], ws_file="untitled:1")
        assert windows_mod.folder_add_restarts(single) and windows_mod.folder_add_restarts(_win("1"))
        assert not windows_mod.folder_add_restarts(multi)
        assert windows_mod.folder_remove_restarts(multi, self.HOST)
        assert not windows_mod.folder_remove_restarts(multi, self.OTHER)


@pytest.fixture
def two_windows(tmp_path, monkeypatch):
    """Two registered windows behind a fake transport; window 100's extension host is this
    CLI's ancestor. Returns the requests that reached a window (pings left out)."""
    folder = tmp_path / "home" / "windows"
    folder.mkdir(parents=True)
    for wid, name, focused in (("100", "site", True), ("200", "other", False)):
        entry = {"id": wid, "pid": int(wid), "endpoint": f"pipe-{wid}", "token": "t", "name": name,
                 "appName": "Visual Studio Code", "folders": [str(tmp_path / name)], "workspaceFile": None,
                 "focused": focused}
        (folder / f"{wid}.json").write_text(json.dumps(entry), encoding="utf-8")
    sent = []

    def transport(endpoint, payload, timeout):
        req = json.loads(payload)
        if req["method"] != "ping":
            sent.append((endpoint, req["method"], req["params"]))
        return json.dumps({"id": req["id"], "ok": True, "result": {"done": True}}).encode()
    monkeypatch.delenv(backend.APP_ENV, raising=False)
    monkeypatch.setattr(bc, "default_transport", transport)
    monkeypatch.setattr(procs, "ancestors", lambda pid=None: [7, 100, 50, 1])
    monkeypatch.setattr(procs, "parent", {100: 50, 200: 50}.get)
    monkeypatch.setattr(backend, "launcher_name", lambda: "Visual Studio Code")
    monkeypatch.setattr(backend, "run_code", lambda *a, **k: pytest.fail("the launcher must not run"))
    return sent


def _refused(res):
    return res.exit_code == 1 and json.loads(res.output)["type"] == "hosting_window"


class TestHostGuardCLI:
    def test_reload_and_close_refuse_the_hosting_window(self, run, two_windows):
        # With no --window the focused window (the host) is the target; naming it changes nothing.
        for args in (["window", "reload"], ["window", "close"], ["--window", "100", "window", "reload"],
                     ["--dry-run", "window", "close"]):
            assert _refused(run(*args)), args
        assert two_windows == []

    def test_reload_and_close_of_another_window_go_through(self, run, two_windows):
        assert run("--window", "200", "window", "reload").exit_code == 0
        assert run("--window", "other", "window", "close").exit_code == 0
        assert two_windows == [("pipe-200", "window.reload", {}), ("pipe-200", "window.close", {})]

    def test_command_run_refuses_commands_that_end_the_host(self, run, two_windows):
        for cid in ("workbench.action.reloadWindow", "python.reloadVSCode", "workbench.action.restartExtensionHost"):
            assert _refused(run("command", "run", cid)), cid
        # quitting ends every window of the editor process, the host's included
        assert _refused(run("--window", "200", "command", "run", "workbench.action.quit"))
        assert run("--window", "200", "command", "run", "workbench.action.reloadWindow").exit_code == 0
        assert run("command", "run", "editor.action.formatDocument").exit_code == 0
        assert [m[2]["command"] for m in two_windows] == ["workbench.action.reloadWindow",
                                                         "editor.action.formatDocument"]

    def test_folder_changes_that_restart_the_host_are_refused(self, run, two_windows, tmp_path):
        assert _refused(run("workspace", "add-folder", str(tmp_path / "extra")))
        assert _refused(run("workspace", "remove-folder", str(tmp_path / "site")))
        assert run("--window", "200", "workspace", "add-folder", str(tmp_path / "extra")).exit_code == 0
        assert [m[1] for m in two_windows] == ["workspace.addFolder"]

    def test_window_open_into_the_hosting_window_is_refused(self, run, two_windows, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        assert _refused(run("--dry-run", "window", "open", "--reuse", str(folder)))
        assert _refused(run("--dry-run", "window", "open", "--add", str(folder)))
        assert json.loads(run("--dry-run", "window", "open", "--new", str(folder)).output)["dry_run"] is True
        assert json.loads(run("--dry-run", "window", "open", "--reuse", str(tmp_path / "f.txt")).output)["dry_run"]

    def test_window_open_into_another_editor_is_allowed(self, run, two_windows, tmp_path, monkeypatch):
        monkeypatch.setattr(backend, "launcher_name", lambda: "Antigravity IDE")
        folder = tmp_path / "proj"
        folder.mkdir()
        assert json.loads(run("--dry-run", "window", "open", "--reuse", str(folder)).output)["dry_run"] is True
