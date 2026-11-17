# VS Code: harness analysis and SOP

Software-specific companion to the CLI-Anything `HARNESS.md` (in the plugin, not copied
here). It records how Visual Studio Code maps onto that method and where it had to bend.

## Phase 1: analysis

**Source.** The analysis is from the installed application (VS Code 1.146, Windows) and
its public extension API, not from a clone of `microsoft/vscode`. The CLI depends only
on stable, documented surfaces, and the end-to-end tests check each one against the
real application.

**Backend engine.** VS Code is an Electron app. Its logic runs in three places: the
main process (windows, lifecycle), the renderer (the workbench UI), and one extension
host per window (where extensions and the `vscode` API live).

**Existing CLI.** `code` covers: open folder/file/workspace, `--goto`, `--diff`,
`--merge`, `--new-window`/`--reuse-window`/`--add`, `--wait`, extension install,
uninstall and list, `--status`, `--version`. It cannot query or change a running
window: no editor state, no unsaved text, no diagnostics, no terminals, no commands.

**Data model.** JSONC files: `User/settings.json`, `User/keybindings.json`,
`<folder>/.vscode/settings.json`, `tasks.json`, `launch.json`, and `.code-workspace`
(folders, settings, extension recommendations). Window and UI state lives in SQLite
(`state.vscdb`); its keys are internal and change between versions, so the CLI does not
read it.

**Command system.** Every UI action is a command id (about 3,200 in a plain 1.146
window), run with `vscode.commands.executeCommand`. That is the general-purpose lever:
menu items, palette entries and keybindings all resolve to it.

**Conclusion.** No supported external control channel exists. Full control needs code
inside the extension host. This is the same shape as the harness's OBS (obs-websocket)
and browser (extension + protocol) rows.

## Phase 2: architecture

```
codereins (Python, click + REPL)
 ├─ utils/vscode_backend.py   → the `code` launcher (subprocess)
 ├─ utils/jsonc.py            → VS Code's JSONC files, edited in place
 └─ utils/bridge_client.py    → named pipe / Unix socket, newline-delimited JSON
                                   │
            bridge/extension.js (inside each window's extension host)
                                   └─ the `vscode` extension API
```

**Bridge protocol.** One request per connection: `{id, token, method, params}` then one
reply `{id, ok, result}` or `{id, ok:false, error:{code, message}}`. Each window writes
`<home>/windows/<pid>.json` with its endpoint and a 32-byte random token; the CLI
discovers windows there and prunes entries whose pipe is gone. A request line over
16 MB drops the connection.

**Positions.** 1-based `{line, column}` on the wire and in the CLI; conversion to the
API's 0-based positions happens once, in the extension. Raw `command run` arguments use
`{"$uri"}`, `{"$pos"}`, `{"$range"}`, `{"$sel"}` markers, also 1-based.

**Window selection.** `--window`, then the session's window, then the window whose
workspace holds the working directory, then the most recently focused.

**State model.** The "project" of the generic harness is the `.code-workspace` file
(`core/project.py`), loaded with `--project`, changed in memory with snapshot/undo/redo
and auto-saved after a one-shot command unless `--dry-run`. Settings and keybindings are
edited directly; each write backs the file up so `session undo` works across separate
invocations.

## Command map

| What a person does in the UI | CLI | Backend |
|---|---|---|
| File > Open Folder | `window open <path>` | `code` |
| Click a file, go to a line | `file open f:12:3` | bridge `showTextDocument` (falls back to `code --goto`) |
| Type, select, replace | `editor insert/replace/sub/select` | bridge `WorkspaceEdit` |
| Save, Save All | `file save [--all]` | bridge |
| Command Palette | `command list`, `command run <id>` | bridge `executeCommand` |
| Problems panel | `lang diagnostics`, `export render -p diagnostics-md` | bridge `getDiagnostics` |
| F12, Shift+F12, F2, hover, Ctrl+. | `lang definition/references/rename/hover/actions` | bridge `vscode.execute*Provider` |
| Format Document | `editor format` | bridge formatter provider |
| Terminal panel | `terminal new/send/exec/kill` | bridge terminals + shell integration |
| Run Task | `task list/run/stop` | bridge `vscode.tasks` |
| Run and Debug, breakpoints | `debug start/stop/break/request` | bridge `vscode.debug` + DAP |
| Extensions view | `ext list/install/uninstall` | `code`; `ext info/activate` via bridge |
| Settings editor | `config get/set/unset` | JSONC file; `--effective` / `--live` via bridge |
| Keyboard Shortcuts | `config keys/bind/unbind` | JSONC file |
| File > Save Workspace As | `project new/add-folder/set` | JSONC file |
| Notifications, quick pick, input box | `ui notify/pick/input` | bridge `vscode.window` |
| Source Control view | `git status` | bridge, Git extension API |

## Truthfulness (the "rendering gap" for an editor)

VS Code renders nothing to a file, so the harness's rendering gap becomes: does the CLI
report what VS Code actually holds? Rules followed:

- Live state always comes from the window. `file read` returns the editor buffer,
  unsaved changes included, not the file on disk.
- Diagnostics, symbols and renames come from the language servers VS Code runs. Nothing
  is parsed in Python.
- Edits go through `WorkspaceEdit`, so undo history, dirty flags and save participants
  behave as if a person typed.
- File-layer edits are verified by parsing the result before writing, and VS Code is the
  judge: the end-to-end suite checks that a running window reports the new value.
- Exports (`core/export.py`) only write what the window or `code` returned.

## Editors built on VS Code

Antigravity IDE, VS Code Insiders and VSCodium ship the same launcher (`<exe> cli.js`) and
extension host, so the bridge runs in them unchanged. `--app` picks the launcher from a
small table (`APPS` in `utils/vscode_backend.py`); everything else is read from that
editor's `product.json`: user data under `nameShort`, extensions under `dataFolderName`,
and `nameLong`, which is what `vscode.env.appName` reports in its windows and what the
window filter matches. All editors share one window registry; each entry carries `appName`.

Profiles are read from `User/globalStorage/storage.json`. A profile's `useDefaultFlags` says
which resources it borrows from Default; `bridge install` adds `--profile` only for profiles
that keep their own extensions, and only with names from that file (an unknown name would
make the editor create an empty profile).

Not every "Antigravity" is an editor built on VS Code: the standalone Antigravity agent app
(`Programs\Antigravity`, an Electron app with `app.asar` and the `agy` CLI) has no
extension host, so the bridge cannot run there.

## Known limits and decisions

- **No preview group.** The harness's `preview` commands suit apps with visual
  intermediate output. An editor's intermediate state is text, which `file read`,
  `editor state` and `export render -p state-json` already return. Screenshots would
  mean scraping the UI, which the harness forbids.
- **`eval` is opt-in.** Typed methods plus `command run` cover the API without it.
- **Breakpoint echo.** VS Code echoes breakpoint changes from the main process; the
  bridge waits for that to settle so a list straight after add/remove is correct.
- **`window recent` asks the window** (`_workbench.getRecentlyOpened`), because the
  storage key older write-ups mention is absent in 1.146.
- **Windows launcher.** `code` is a batch file; arguments to a batch file are re-parsed
  by cmd.exe. The backend runs `Code.exe cli.js` directly, as the batch file itself
  does, and refuses unsafe arguments if it ever has to fall back to the batch file.
- **Environment.** `ELECTRON_*` and `VSCODE_*` variables are dropped before launching
  `code`, as VS Code does for its own terminals, so launching from inside an extension
  host (an agent running in VS Code) starts a clean window.
- **The window that hosts the CLI.** An agent in a window's panel runs as a child of that
  window's extension host (seen 2026-11-17: `claude.exe` ← `Antigravity IDE.exe
  --utility-sub-type=node.mojom.NodeService` ← the editor's main process). The bridge
  registers the host's pid as the window id, so `utils/procs.py` walks the CLI's parent
  chain (Toolhelp32 on Windows, `/proc` on Linux, `ps` elsewhere; a parent that started
  after its child is a reused pid and ends the walk) and `windows.refuse_if_hosting`
  refuses `hosting_window` when the target's pid is in it. Whole-editor commands (`quit`,
  `update.restart*`) are also refused for sibling windows: their host's parent is the same
  main process. Checked: `window reload/close`, `command run` (id patterns:
  `reload(window|vscode)`, `restartExtensionHost`, `closeWindow`, `closeFolder`,
  `reopen(folder|in|locally|workspace)`, `vscode.openFolder` without a new window),
  `workspace add-folder` on a window that is not yet a multi-root workspace,
  `workspace remove-folder` of the first folder, and `window open --reuse/--add` of a
  folder into the same editor. Not checked: `bridge eval`, and a command from another
  extension whose id matches none of the patterns. There is no override flag on purpose.
  A terminal's shell descends from the pty host, not the extension host, so a CLI typed
  into an integrated terminal is not protected (reloading keeps the terminal alive anyway).
- **Folder changes that restart the extension host** (measured on VS Code 1.146, e2e test
  `test_folder_changes_restart_the_extension_host_only_when_the_first_folder_changes`):
  adding a folder to a single-folder window (it becomes an untitled workspace) and removing
  the first folder restart it; adding to, or removing a later folder from, a multi-root
  workspace do not.

## Deviations from the generic validation checklist

| Checklist item | Here |
|---|---|
| Source path or repository | installed app + public API (see Phase 1) |
| `core/project.py` `list_profiles` | VS Code profiles |
| `core/export.py` `render` + `EXPORT_PRESETS` | exports of what VS Code reports, not media renders |
| `preview` commands | not applicable (see above) |
| Pixel or audio verification | file bytes on disk, parsed exports, exit codes |
