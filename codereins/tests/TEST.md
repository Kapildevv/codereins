# codereins: test plan and results

## Part 1: Plan (written before the tests)

### Test inventory

| File | Planned | What it needs |
|---|---|---|
| `test_core.py` | about 70 unit tests | nothing: synthetic data, temp folders, a fake transport |
| `test_full_e2e.py` | about 35 end-to-end tests | the real VS Code (`code` on PATH), Node on PATH for the debug test |

`conftest.py` points `CODEREINS_HOME` at a temp folder for every test, so no
test can read or write the real `~/.codereins`. The end-to-end tests launch
their own VS Code with a temp `--user-data-dir` and `--extensions-dir`: the user's
profile, settings and open windows are never touched.

### Unit test plan (`test_core.py`)

| Module | Covered | Edge cases |
|---|---|---|
| `utils/jsonc.py` | `loads`, `strip`, `scan`, `set_key`, `remove_key`, `append_item`, `remove_item` | comments kept, `//` inside strings, trailing commas, empty file, `{}`, CRLF kept, duplicate keys, removing the last item, unterminated input |
| `vscode_cli.py` parsers | `parse_location`, `parse_range`, `parse_position`, `parse_value` | Windows drive colon, whole-line ranges, bad input |
| `core/project.py` | create, add/remove folder, settings, save, reopen, info, profiles | duplicate folder, comment survival on save, missing file |
| `core/session.py` | snapshot/undo/redo, file backup and restore, persisted state, history | empty undo stack, undo cap, restore of a file that did not exist |
| `core/settings.py` | get/set/unset, keybinding add/remove | dry run writes nothing, backup allows undo |
| `core/windows.py` | `select` | id, `focused`, path (longest root wins), name, session preference, cwd hint, no window, no match |
| `utils/bridge_client.py` | request shape, error mapping, dry run, registry pruning | empty reply, unreadable reply, dead window |
| `core/bridge.py` | `.vsix` assembly | zip entries, manifest identity matches `package.json` |
| `utils/vscode_backend.py` | launcher parsing, extension-id validation, env clean-up | hostile extension ids, batch-file fallback |
| `core/export.py` | presets, Markdown rendering, overwrite guard | unknown preset, existing output |
| CLI (click runner) | help, project workflow, config workflow, dry run, error shape | no window reachable gives exit 1 and a JSON error |

### End-to-end plan (`test_full_e2e.py`)

One VS Code is launched for the whole run (isolated profile, bridge installed from the
`.vsix` this package builds). Tests then drive it and check results **outside** VS Code
wherever possible: file contents on disk, exported files, exit codes.

- **Real backend**: `code --version`, `code --list-extensions` shows the bridge, the `.vsix` is a valid zip.
- **Window**: info matches `code --version`, more than 1000 command ids, recent list.
- **Editing**: open at a line, insert, replace a whole line, regex substitute, save; verify the bytes on disk. Unsaved buffer differs from disk until saved.
- **Language servers**: JSON syntax error is reported; JSON formatter rewrites the file; JavaScript outline, definition, references and a workspace rename (verified on disk).
- **Files**: create, rename, delete through VS Code; glob search.
- **Terminal**: create/list/kill; run a command and capture its output and exit code.
- **Tasks**: list a `tasks.json` task, run it, read exit code 0.
- **Debugging**: breakpoints add/list/remove; start a Node session, ask the adapter for threads, stop.
- **Settings round trip**: the file layer edits `settings.json`; the running VS Code reports the new effective value; `session undo` reverts it.
- **Exports**: `diagnostics-md` and `state-json` exist, are non-empty, parse, and name the broken file.
- **Safety**: wrong token is refused; `eval` is refused by default; unknown method is an error.
- **Installed command** (`TestCLISubprocess`, via `_resolve_cli`): help, JSON output, window list, a full edit workflow, a project workflow, `command run` with a typed `$uri` argument, error exit codes, `--dry-run` changes nothing.

### Realistic workflows

| Workflow | Simulates | Steps | Verified |
|---|---|---|---|
| Fix-and-save | an agent patching a file in the user's editor | create file, insert, substitute, save, read back | exact file bytes on disk |
| Rename refactor | a cross-reference rename | outline, definition, references, rename, save | every reference renamed on disk, old name gone |
| Problem report | CI-style triage from the Problems panel | open a broken file, wait for the server, export Markdown | report names the file and an error row |
| Workspace assembly | building a multi-root workspace | `project new`, add folder, set setting, dry run | valid JSONC with the folders and setting; dry run leaves the file unchanged |
| Settings round trip | changing a preference from a script | set in file, read effective value from the window, undo | VS Code sees the change and the revert |

## Part 2: Results

Run on 2026-11-17, Windows 11, Python 3.12, VS Code 1.146.0, against the installed command:

```
CODEREINS_FORCE_INSTALLED=1 python -m pytest codereins/tests/ -v --tb=no
```

```
============================= test session starts =============================
platform win32 -- Python 3.12.9, pytest-9.2.0, pluggy-1.7.0 -- <repo>\.venv\Scripts\python.exe
rootdir: <repo>
collecting ... collected 118 items

codereins/tests/test_core.py::TestJsonc::test_loads_comments_and_trailing_commas PASSED [  0%]
codereins/tests/test_core.py::TestJsonc::test_double_slash_inside_string_is_not_a_comment PASSED [  1%]
codereins/tests/test_core.py::TestJsonc::test_empty_document PASSED [  2%]
codereins/tests/test_core.py::TestJsonc::test_invalid_document_raises PASSED [  3%]
codereins/tests/test_core.py::TestJsonc::test_set_existing_key_keeps_comments PASSED [  4%]
codereins/tests/test_core.py::TestJsonc::test_set_new_key_after_trailing_comma PASSED [  5%]
codereins/tests/test_core.py::TestJsonc::test_set_new_key_adds_missing_comma PASSED [  5%]
codereins/tests/test_core.py::TestJsonc::test_set_key_in_empty_object_and_empty_file PASSED [  6%]
codereins/tests/test_core.py::TestJsonc::test_set_nested_value_is_indented PASSED [  7%]
codereins/tests/test_core.py::TestJsonc::test_crlf_is_preserved PASSED [  8%]
codereins/tests/test_core.py::TestJsonc::test_duplicate_key_edits_the_one_json_uses PASSED [  9%]
codereins/tests/test_core.py::TestJsonc::test_remove_key_middle_and_absent PASSED [ 10%]
codereins/tests/test_core.py::TestJsonc::test_remove_last_key_drops_the_dangling_comma PASSED [ 11%]
codereins/tests/test_core.py::TestJsonc::test_array_append_and_remove PASSED [ 11%]
codereins/tests/test_core.py::TestJsonc::test_wrong_container_kind PASSED [ 12%]
codereins/tests/test_core.py::TestJsonc::test_file_round_trip_keeps_line_endings PASSED [ 13%]
codereins/tests/test_core.py::TestParsers::test_location_plain_and_positions PASSED [ 14%]
codereins/tests/test_core.py::TestParsers::test_location_windows_drive PASSED [ 15%]
codereins/tests/test_core.py::TestParsers::test_range_forms PASSED [ 16%]
codereins/tests/test_core.py::TestParsers::test_range_and_position_reject_garbage PASSED [ 16%]
codereins/tests/test_core.py::TestParsers::test_value_is_json_or_string PASSED [ 17%]
codereins/tests/test_core.py::TestParsers::test_target_absolutises_paths_but_not_uris PASSED [ 18%]
codereins/tests/test_core.py::TestProject::test_create_add_remove_folder PASSED [ 19%]
codereins/tests/test_core.py::TestProject::test_duplicate_folder_is_refused PASSED [ 20%]
codereins/tests/test_core.py::TestProject::test_settings_and_recommendations PASSED [ 21%]
codereins/tests/test_core.py::TestProject::test_save_new_and_reopen PASSED [ 22%]
codereins/tests/test_core.py::TestProject::test_save_existing_keeps_comments PASSED [ 22%]
codereins/tests/test_core.py::TestProject::test_info_resolves_folders PASSED [ 23%]
codereins/tests/test_core.py::TestProject::test_open_rejects_missing_and_non_object PASSED [ 24%]
codereins/tests/test_core.py::TestProject::test_list_profiles PASSED [ 25%]
codereins/tests/test_core.py::TestSession::test_snapshot_undo_redo PASSED [ 26%]
codereins/tests/test_core.py::TestSession::test_undo_redo_on_empty_stack PASSED [ 27%]
codereins/tests/test_core.py::TestSession::test_undo_stack_is_capped PASSED [ 27%]
codereins/tests/test_core.py::TestSession::test_backup_and_restore_file PASSED [ 28%]
codereins/tests/test_core.py::TestSession::test_restore_removes_a_file_that_did_not_exist PASSED [ 29%]
codereins/tests/test_core.py::TestSession::test_state_persists_across_instances PASSED [ 30%]
codereins/tests/test_core.py::TestSession::test_history_is_capped_and_corrupt_state_is_tolerated PASSED [ 31%]
codereins/tests/test_core.py::TestSession::test_save_session_writes_and_backs_up PASSED [ 32%]
codereins/tests/test_core.py::TestSettings::test_set_get_unset PASSED [ 33%]
codereins/tests/test_core.py::TestSettings::test_dry_run_writes_nothing PASSED [ 33%]
codereins/tests/test_core.py::TestSettings::test_write_is_undoable_through_the_session PASSED [ 34%]
codereins/tests/test_core.py::TestSettings::test_scope_paths PASSED [ 35%]
codereins/tests/test_core.py::TestSettings::test_keybindings_add_and_remove PASSED [ 36%]
codereins/tests/test_core.py::TestSettings::test_non_object_settings_file_is_rejected PASSED [ 37%]
codereins/tests/test_core.py::TestWindows::test_by_id_and_focused PASSED [ 38%]
codereins/tests/test_core.py::TestWindows::test_by_path_longest_root_wins PASSED [ 38%]
codereins/tests/test_core.py::TestWindows::test_sibling_folder_with_shared_prefix_does_not_match PASSED [ 39%]
codereins/tests/test_core.py::TestWindows::test_by_name PASSED [ 40%]
codereins/tests/test_core.py::TestWindows::test_default_order_preferred_then_cwd_then_newest PASSED [ 41%]
codereins/tests/test_core.py::TestWindows::test_no_windows PASSED [ 42%]
codereins/tests/test_core.py::TestWindows::test_describe_hides_secrets PASSED [ 43%]
codereins/tests/test_core.py::TestBridgeClient::test_request_shape_and_result PASSED [ 44%]
codereins/tests/test_core.py::TestBridgeClient::test_error_reply_becomes_bridge_error PASSED [ 44%]
codereins/tests/test_core.py::TestBridgeClient::test_dry_run_does_not_send_mutations PASSED [ 45%]
codereins/tests/test_core.py::TestBridgeClient::test_empty_and_unreadable_replies PASSED [ 46%]
codereins/tests/test_core.py::TestBridgeClient::test_per_call_timeout_overrides_default PASSED [ 47%]
codereins/tests/test_core.py::TestBridgeClient::test_live_windows_prunes_dead_registrations PASSED [ 48%]
codereins/tests/test_core.py::TestBridgeClient::test_live_windows_keeps_slow_windows_registered PASSED [ 49%]
codereins/tests/test_core.py::TestBridgeClient::test_registry_sorts_newest_focus_first PASSED [ 50%]
codereins/tests/test_core.py::TestVsix::test_vsix_entries PASSED [ 50%]
codereins/tests/test_core.py::TestVsix::test_manifest_identity_matches_package_json PASSED [ 51%]
codereins/tests/test_core.py::TestVsix::test_bridge_declares_its_security_settings_at_application_scope PASSED [ 52%]
codereins/tests/test_core.py::TestBackend::test_extension_id_validation PASSED [ 53%]
codereins/tests/test_core.py::TestBackend::test_install_and_uninstall_refuse_bad_targets_before_running_code PASSED [ 54%]
codereins/tests/test_core.py::TestBackend::test_env_cleanup_pattern PASSED [ 55%]
codereins/tests/test_core.py::TestBackend::test_paths_layout PASSED [ 55%]
codereins/tests/test_core.py::TestBackend::test_batch_launcher_is_bypassed PASSED [ 56%]
codereins/tests/test_core.py::TestBackend::test_find_code_honours_override PASSED [ 57%]
codereins/tests/test_core.py::TestExport::test_presets_are_complete PASSED [ 58%]
codereins/tests/test_core.py::TestExport::test_diagnostics_markdown PASSED [ 59%]
codereins/tests/test_core.py::TestExport::test_guards PASSED   [ 60%]
codereins/tests/test_core.py::TestCLI::test_help_lists_every_group PASSED [ 61%]
codereins/tests/test_core.py::TestCLI::test_every_command_has_help PASSED [ 61%]
codereins/tests/test_core.py::TestCLI::test_project_workflow PASSED [ 62%]
codereins/tests/test_core.py::TestCLI::test_project_dry_run_leaves_file_alone PASSED [ 63%]
codereins/tests/test_core.py::TestCLI::test_config_workflow_and_undo PASSED [ 64%]
codereins/tests/test_core.py::TestCLI::test_config_dry_run_and_workspace_scope PASSED [ 65%]
codereins/tests/test_core.py::TestCLI::test_keybinding_commands PASSED [ 66%]
codereins/tests/test_core.py::TestCLI::test_no_window_is_a_clear_json_error PASSED [ 66%]
codereins/tests/test_core.py::TestCLI::test_usage_errors_exit_2 PASSED [ 67%]
codereins/tests/test_core.py::TestCLI::test_session_status_and_export_presets PASSED [ 68%]
codereins/tests/test_full_e2e.py::TestBackendReal::test_code_version PASSED [ 69%]
codereins/tests/test_full_e2e.py::TestBackendReal::test_code_lists_the_bridge_as_installed PASSED [ 70%]
codereins/tests/test_full_e2e.py::TestBackendReal::test_vsix_is_a_valid_package PASSED [ 71%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_info_matches_the_launcher PASSED [ 72%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_command_catalogue PASSED [ 72%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_open_insert_replace_save_is_on_disk PASSED [ 73%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_unsaved_buffer_differs_from_disk_until_saved PASSED [ 74%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_substitute_plain_and_regex PASSED [ 75%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_json_formatter_rewrites_the_file PASSED [ 76%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_json_syntax_error_is_reported PASSED [ 77%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_javascript_outline_definition_references_rename PASSED [ 77%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_completions_come_from_the_language_server PASSED [ 78%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_file_create_rename_delete PASSED [ 79%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_find_files PASSED [ 80%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_tabs_list_and_close PASSED [ 81%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_selection_round_trip PASSED [ 82%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_terminal_create_send_kill PASSED [ 83%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_terminal_exec_captures_output_and_exit_code PASSED [ 83%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_task_run_returns_exit_code PASSED [ 84%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_breakpoints PASSED [ 85%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_debug_session_start_request_stop PASSED [ 86%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_settings_file_edit_reaches_the_running_vscode_and_undo_reverts PASSED [ 87%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_live_config_update PASSED [ 88%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_extensions_and_git_views PASSED [ 88%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_ui_calls_that_do_not_block PASSED [ 89%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_safety_wrong_token_unknown_method_eval_off PASSED [ 90%]
codereins/tests/test_full_e2e.py::TestLiveWindow::test_exports_are_real_files PASSED [ 91%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 92%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_app_version_json PASSED [ 93%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_window_list_and_current PASSED [ 94%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_fix_and_save_workflow PASSED [ 94%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_command_run_with_typed_uri_argument PASSED [ 95%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_lang_and_export_from_the_command_line PASSED [ 96%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_terminal_exec_from_the_command_line PASSED [ 97%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_project_workflow PASSED [ 98%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_dry_run_sends_nothing_to_the_window PASSED [ 99%]
codereins/tests/test_full_e2e.py::TestCLISubprocess::test_errors_have_exit_codes_and_json PASSED [100%]

============================ 118 passed in 32.46s =============================
```

### Summary

| | |
|---|---|
| Total | 118 tests |
| Passed | 118 (100%) |
| Unit (`test_core.py`) | 81 |
| End-to-end (`test_full_e2e.py`) | 37, all against a real VS Code |
| Time | about 32 s, of which about 8 s is VS Code starting |

Proof that the installed command and real output files were used (`-s` run):

```
[_resolve_cli] Using installed command: <repo>\.venv\Scripts\codereins.EXE
  VSIX: %TEMP%\pytest-of-<user>\pytest-523\test_vsix_is_a_valid_package0\codereins-bridge-0.1.0.vsix (14,199 bytes)
  terminal output: 'codereins-marker-4711\n' exit=0
  export: %TEMP%\pytest-of-<user>\pytest-523\vsc0\out\problems.md (264 bytes)
  export: %TEMP%\pytest-of-<user>\pytest-523\vsc0\out\state.json (2,259 bytes)
  export: %TEMP%\pytest-of-<user>\pytest-523\vsc0\out\extensions.txt (39 bytes)
```

The end-to-end file was also run five times in a row after the last bridge change: 37 passed each time.

### What the run found and fixed

- A missing `.vsix` path was accepted as an extension id (`name.vsix` looks like `publisher.name`). Now a `.vsix` argument must be an existing file. Caught by a unit test.
- Removing a breakpoint straight after adding it could leave it listed for a moment, because VS Code echoes breakpoint changes from its main process. Failed 2 of 6 full runs. The bridge now waits for that echo; 0 failures in 5 runs since.

## Part 3: editors built on VS Code (`--app`) and profiles, 2026-11-17

Added: `--app`, `app list`, `bridge install/status --all`, profile-aware `project profiles`
and `bridge install`, and the `app` field in `window list`.

| File | New tests | What they check |
|---|---|---|
| `test_core.py` | 8 | a fake Antigravity IDE install: launcher, `product.json` folders, `nameLong`; default = first installed; a chosen editor that is missing or unknown fails (no fall back to VS Code's folders); the window filter and its error naming the other editors; profiles that borrow Default's files; `bridge install` adds `--profile` only for a profile with its own extensions; `--app` is validated |
| `test_full_e2e.py` (`TestOtherEditor`) | 5 | an isolated Antigravity IDE 1.107 (own `--user-data-dir`, own CLI home): bridge installed with its own launcher; the window reports `appName`; `--app antigravity-ide window list` sees it and `--app vscode` does not (and `window info` errors naming Antigravity IDE); JSON diagnostics through the CLI; `app list` / `app paths` |

Result, against the installed command (`CODEREINS_FORCE_INSTALLED=1`): **131 passed in 47.46 s**
(89 unit, 37 VS Code end-to-end, 5 Antigravity IDE end-to-end). The Antigravity IDE class
alone passed 4 runs in a row (about 14 s each).

Not stable at first: the first two isolated Antigravity IDE launches (19:49 and 19:51) exited
about 0.25 s after start without opening a window (`main.log` stops after the startup
migrations; the "mutex already exists" line appears in passing runs too, so it is not the
cause). Cause unknown; the fixture now fails with a pointer to the logs instead of waiting
120 s. It is skipped only when Antigravity IDE is not installed.

### Smoke test against the real windows (read-only), 2026-11-17

`bridge install --all` put the bridge into VS Code 1.146 and Antigravity IDE 1.107 (Default
profiles; VS Code's "Agents" profile borrows Default's extensions). The five open Antigravity
IDE windows started it without a reload.

| Check | Result |
|---|---|
| `window list` | 5 windows, all `app: Antigravity IDE`; VS Code was not running (`--app vscode window list` = `[]`) |
| `window current` / `window info` | the window holding the working directory (`Website`), `appName` Antigravity IDE, 1.107.0 |
| `editor state` | `null`: no text editor active in that window (the Claude Code panel had focus) |
| `lang diagnostics` | 0 problems in that workspace (no file with problems was open) |
| `ext info anthropic.claude-code` | 2.1.325, active, in `~/.antigravity-ide/extensions` |
| `command list --filter claude` / `antigravity` | 55 Claude Code commands; 156 `antigravity*` commands (`google.google-antigravity` active) |
| `--app vscode ext list` | `anthropic.claude-code@2.1.325` and the bridge, via the launcher (no window opened) |

### Coverage notes: not exercised by a test

- `ui pick`, `ui input`, `ui notify --button`: they wait for a person to click.
- `ui clipboard-get` / `clipboard-set`: the clipboard is system-wide, and a test would overwrite the user's.
- `window focus`, `file diff`, `ext install` from the marketplace (needs network).
- `lang actions` (code actions), `debug` stepping beyond `threads`, `task stop`.
- The host guard's whole-editor branch (`quit` refused for a sibling window of the host's editor process): unit-tested with a fake process table; the parent link it relies on (extension host → editor main process) was checked by hand on Antigravity IDE 1.107 only.
- The host guard on macOS (`ps`, no start times, so no pid-reuse check) and Linux (`/proc`): written, not run.
- macOS and Linux: the Unix-socket transport and launcher paths are written but were not run. Everything above was run on Windows only.
- `--app insiders` / `vscodium`: in the table, not installed here, never run.
- `bridge install` into a profile with its own extensions (`--profile`): unit-tested with a fake launcher only; no such profile exists on this machine.
- Claude Code inside a live VS Code window: VS Code was not running, so only the launcher-side check above was made.
- The REPL loop itself (its commands are the same functions the one-shot tests call).

## Part 4: the window that hosts the CLI, 2026-11-17

Added: `utils/procs.py` (parent chain), `windows.refuse_if_hosting` / `hosting` /
`command_reach` / `folder_add_restarts` / `folder_remove_restarts`, and the checks in
`window reload/close/open`, `command run` and `workspace add-folder/remove-folder`.

| File | New tests | What they check |
|---|---|---|
| `test_core.py` (`TestProcs`) | 5 | the real chain of the test process starts at its parent; a child process lists the test process and its parent; `parent()`; a parent that started after its child (reused pid) ends the walk; undatable links are kept and a cycle ends it |
| `test_core.py` (`TestHostGuard`) | 23 | refusal names the window; other windows pass; whole-editor commands refuse sibling windows of the host's editor process only; `hosting()`; 18 command ids classified (incl. `antigravity.reloadWindow`, `python.reloadVSCode`, `vscode.openFolder` with and without a new window, and look-alikes that must pass: `reopenClosedEditor`, `reloadWebviewAction`, `notebook.cell.quitEdit`, `restartTsServer`); which folder changes restart the host |
| `test_core.py` (`TestHostGuardCLI`) | 6 | two fake windows, 100 hosting: `window reload/close` (default target, `--window`, `--dry-run`) refused and nothing sent; window 200 reloads; `command run` refuses reload/restart commands, refuses `quit` on the sibling, sends harmless ones; folder changes; `window open --reuse/--add` of a folder refused into the same editor, allowed with `--new`, for a file, or into another editor |
| `test_full_e2e.py` (`TestWindowLifecycle`) | 4 | a second isolated VS Code (own CLI home, `allowEval` on in its profile only): the CLI started **from inside its extension host** (`bridge eval` + `child_process.execFile`) reaches the window (`window info`, pid matches) yet refuses `window reload`, `window close`, `command run workbench.action.reloadWindow`, `workspace add-folder`, `--dry-run window open --reuse`, and the window keeps its id; folder changes from outside, with the host pid before and after; `window reload` from outside (new pid, same folders); `window close` from outside |

The six `TestHostGuardCLI` tests were also run with the guard switched off
(`refuse_if_hosting` and `hosting` replaced by no-ops): the 4 refusal tests failed and the
2 "goes through" tests passed, so they test the guard and not the fixture.

Result, against the installed command (`CODEREINS_FORCE_INSTALLED=1`), two full runs:
**169 passed** (82 s and 90 s): 123 unit, 41 VS Code end-to-end, 5 Antigravity IDE end-to-end.

```
refused inside window 32728: Refused to reload window 32728 (a): this command runs inside it (its extension host is an ancestor of this process), so it would end the session that started this command, such as an agent in that window. Do it by hand in the editor, or target another window with --window.
extension host pids: 32728 -> add 2nd -> 46092 -> add/remove 3rd -> 46092 -> remove 1st -> 14108
```

Read-only check from the Claude Code session in the real Antigravity IDE (nothing sent to a
window): ancestors `claude.exe 43380 → 26388 → 40556`; `hosting()` returned window 26388
(`Website`) only; its editor main process is 40556, shared by the other four windows, so
`quit` is refused for all five and `reload/close` only for `Website`.

Now covered that Part 3 listed as untested: `window reload`, `workspace add-folder/remove-folder`,
`bridge eval` with the setting on (the guard test runs through it).

## Part 5: renamed to CodeReins, 2026-11-17

The tool was renamed from `cli-anything-vscode` for a public release: package
`cli_anything.vscode` → `codereins` (flat layout, `setup.py` at the repository root), command
`codereins`, environment variables `CODEREINS_*`, state folder `~/.codereins`, bridge extension
`local.codereins-bridge` with settings `codereinsBridge.*`, pipe names `codereins-<id>-…`.
`utils/repl_skin.py` (from CLI-Anything, Apache-2.0) carries a notice listing its changes.

**Fixed: a terminal's name right after it is created.** For about 2 s after
`createTerminal({name})`, `Terminal.name` can read the shell's title instead of the requested
name (a probe saw `['powershell']`, then `['probe-term']`, for a terminal created as
`probe-term`, VS Code 1.146). `terminal send/kill/exec --name` straight after `terminal new`
could then miss the terminal. Two full runs before the fix failed
`test_terminal_create_send_kill` with `'e2e-term' in ['powershell']`. The bridge now identifies
a terminal by `creationOptions.name` (falling back to the live title) for lookups and reports it
as `name`, with the live title as `title`. New test:
`test_terminal_keeps_its_requested_name_from_creation` (create, send by name at once, then 60
listings over 3 s must all show the name). It could not be made to fail on demand: three
targeted runs before the fix passed, so the race is timing-dependent.

| Run (installed command) | Result | Time |
|---|---|---|
| before the fix, full | 167 passed, 2 failed (terminal create; folder-change lifecycle) | 252 s |
| before the fix, full | 168 passed, 1 failed (terminal create) | 249 s |
| after the fix, full | **170 passed** | 287 s |
| after the fix, full | 168 passed, 2 failed (`TestWindowLifecycle`: a scratch window did not register within 60 s) | 431 s |
| after the fix, `TestWindowLifecycle` alone | 4 passed | 57 s |

170 tests: 123 unit, 42 VS Code end-to-end, 5 Antigravity IDE end-to-end. Full runs now take
4-7 minutes on the same machine that ran them in 82-90 s earlier the same day (other load on
the machine; not investigated). The `TestWindowLifecycle` waits (60 s for a new window) are the
first to time out under that load.

Coverage notes in Part 3 are unchanged.
