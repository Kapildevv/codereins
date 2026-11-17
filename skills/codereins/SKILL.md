---
name: "codereins"
description: "Drive a running Visual Studio Code from the command line: list windows, open and edit files through the editor (unsaved text included), ask the language servers (problems, definition, references, rename, format), run a command in the integrated terminal and capture its output, run tasks, debug, manage extensions, settings and keybindings, and run any VS Code command id. Use when a task needs to read or change what is in the user's VS Code, not only files on disk."
---

# codereins

A command-line interface to the real Visual Studio Code. Three backends: the `code`
launcher, VS Code's own JSONC files (settings, keybindings, `.code-workspace`), and a
bridge extension inside each window for everything live.

## Prerequisites

- VS Code 1.93+ with `code` on PATH, Python 3.10+
- The command `codereins` on PATH
- For live commands, the bridge extension: `codereins bridge install`, then
  open a new window (or reload an open one)

Check before relying on live commands:

```bash
codereins --json bridge status   # installed_version, live_windows
codereins --json window list     # windows that answer
```

`config`, `project`, `app`, `ext list|install|uninstall`, `window open` and `file diff`
work with no window and no bridge.

## Syntax

```bash
codereins [--json] [--window SEL] [--app EDITOR] [--project FILE.code-workspace] [--dry-run] [--timeout S] <group> <command> [args]
codereins            # REPL
```

- Positions are 1-based `LINE:COLUMN`. File locations are `FILE[:LINE[:COLUMN]]`.
- Ranges: `L` (whole line), `L1-L2` (whole lines), `L:C` (a point), `L1:C1-L2:C2`.
- `--window` takes an id from `window list`, `focused`, a path inside the window's
  workspace, or the window name. Default: the window chosen with `window use`, else the
  window whose workspace holds the current directory, else the most recently focused.
- `--app` picks an editor built on VS Code: `vscode`, `insiders`, `vscodium`,
  `antigravity-ide` (env `CODEREINS_APP`). Window commands then see only that
  editor's windows; without it they see all, and `window list` shows each window's `app`.
- Never end the window hosting your own session (`window current` with no `--window` is
  usually that window). The CLI refuses, with `hosting_window`, whatever would end it:
  `window reload/close`, `command run` of a reload, close or quit command, a `workspace
  add-folder/remove-folder` that restarts the extension host, and `window open --reuse/--add`
  of a folder into that editor. Do not try to get around it; ask the user instead.
- Values for `config set`, `project set` and `command run` arguments are JSON, or a
  plain string when they do not parse as JSON.
- `command run` typed arguments: `{"$uri": "path"}`, `{"$pos": [line, col]}`,
  `{"$range": [l1, c1, l2, c2]}`.

## Command Groups

### App

The VS Code installation: version, paths, process status.

| Command | Description |
|---------|-------------|
| `list` | Editors built on VS Code this CLI knows: installed, version, folders, profiles, live windows. |
| `version` | Show the installed VS Code version. |
| `paths` | Show where VS Code keeps settings, keybindings, extensions and state. |
| `status` | Process and workspace diagnostics of the running instance (`code --status`). |

### Window

Running VS Code windows: list, choose, open, focus, reload, close.

| Command | Description |
|---------|-------------|
| `list` | List windows the bridge is running in. |
| `use` | Make later commands target a window (id, 'focused', a path, or its name). |
| `current` | Show which window commands target right now. |
| `info` | Details of the target window: version, workspace, theme, trust. |
| `open` | Open a folder, workspace file or file in VS Code. |
| `focus` | Bring the target window to the front. |
| `reload` | Reload the target window (restarts its extensions, including the bridge). Refused for the window this command runs inside. |
| `close` | Close the target window. Refused for the window this command runs inside. |
| `recent` | Recently opened folders, workspaces and files, as the window's File > Open Recent lists them. |

### Project

Workspace files (.code-workspace): folders, workspace settings, recommendations.

| Command | Description |
|---------|-------------|
| `new` | Create a workspace file. |
| `info` | Show the workspace: folders, settings, recommendations. |
| `add-folder` | Add a folder (relative paths are relative to the workspace file). |
| `remove-folder` | Remove a folder by path or display name. |
| `set` | Set a workspace-level setting (VALUE is JSON, or a plain string). |
| `unset` | Remove a workspace-level setting. |
| `recommend` | Add an extension to the workspace recommendations. |
| `save` | Write the loaded workspace to its file (the REPL does not auto-save). |
| `open` | Open the workspace file in VS Code. |
| `profiles` | List VS Code profiles and their settings files. |

### File

Files and documents: open, read, write, save, close, create, rename, delete, diff.

| Command | Description |
|---------|-------------|
| `open` | Open FILE[:LINE[:COLUMN]] in an editor. Falls back to `code --goto` with no bridge. |
| `new` | Open a new untitled document. |
| `list` | Documents VS Code has loaded (including unsaved ones). |
| `read` | Print a document as VS Code sees it, unsaved changes included (default: active editor). |
| `write` | Replace a document's whole text through VS Code (undo history and save hooks apply). |
| `save` | Save a document (default: active editor). |
| `close` | Close a tab (default: the active one). An unsaved tab makes VS Code ask the user. |
| `create` | Create a file through VS Code. |
| `rename` | Rename or move a file through VS Code (open editors follow, import updates may run). |
| `delete` | Delete a file or folder through VS Code. |
| `diff` | Open two files side by side in the diff editor (`code --diff`). |

### Editor

The active editor: state, tabs, selection, edits, formatting.

| Command | Description |
|---------|-------------|
| `state` | Active editor: file, language, dirty flag, selections, visible range. |
| `tabs` | Every open tab, by editor group. |
| `select` | Set the selection(s): L, L1-L2, L:C or L1:C1-L2:C2 (several for multi-cursor). |
| `insert` | Insert text at a position, at the cursor, or at the end. |
| `replace` | Replace a range (L, L1-L2 or L1:C1-L2:C2) with TEXT. |
| `sub` | Find and replace inside a document. |
| `format` | Format a document with the formatter VS Code has for its language. |
| `language` | Change a document's language mode. |
| `undo` | Undo in the focused editor. |
| `redo` | Redo in the focused editor. |

### Command

VS Code commands: list every command id, run any of them.

| Command | Description |
|---------|-------------|
| `list` | List command ids (several thousand; use --filter). |
| `run` | Run a command by id. Each ARG is JSON (or a plain string). |

### Workspace

The window's workspace: folders, file search, symbol search.

| Command | Description |
|---------|-------------|
| `folders` | List the workspace folders of the target window. |
| `add-folder` | Add a folder to the open workspace. Restarts the extension host of a single-folder window (refused for your own window). |
| `remove-folder` | Remove a folder from the open workspace. Removing the first one restarts the extension host (refused for your own window). |
| `find` | Find files by glob, honouring the workspace's exclude settings. |
| `symbols` | Search symbols across the workspace. |

### Lang

Language features from VS Code's language servers: problems, navigation, rename.

| Command | Description |
|---------|-------------|
| `diagnostics` | Problems VS Code reports (whole workspace, or one file). |
| `definition` | Where the symbol at FILE:LINE:COLUMN is defined. |
| `references` | Every reference to the symbol at FILE:LINE:COLUMN. |
| `hover` | Hover text (type, docs) for the symbol at FILE:LINE:COLUMN. |
| `symbols` | Outline of a document (default: active editor). |
| `rename` | Rename the symbol at FILE:LINE:COLUMN across the workspace. |
| `actions` | Code actions (quick fixes, refactorings) at FILE[:LINE[:COLUMN]]; optionally apply one. |
| `completions` | Completion items VS Code would offer at FILE:LINE:COLUMN. |
| `languages` | Language ids VS Code knows. |

### Terminal

Integrated terminals: list, create, type into, run a command and capture its output.

| Command | Description |
|---------|-------------|
| `list` | List integrated terminals. |
| `new` | Create a terminal. |
| `send` | Type text into a terminal (does not wait or capture output). |
| `exec` | Run a command in a terminal and return its output and exit code. |
| `show` | Reveal a terminal. |
| `kill` | Close a terminal. |

### Task

Tasks (tasks.json and auto-detected): list, run, stop.

| Command | Description |
|---------|-------------|
| `list` | List tasks VS Code can run. |
| `run` | Run a task; with --wait, return its exit code. |
| `running` | List running tasks. |
| `stop` | Terminate a running task. |

### Debug

Debugging: sessions, breakpoints, raw debug-adapter requests.

| Command | Description |
|---------|-------------|
| `state` | Active debug session and breakpoint count. |
| `start` | Start a launch configuration by NAME, or one given inline with --config. |
| `stop` | Stop the active debug session. |
| `request` | Send a Debug Adapter Protocol request (threads, stackTrace, evaluate, continue, next...). |
| `breakpoints` | List breakpoints. |
| `break` | Add a breakpoint at FILE:LINE. |
| `unbreak` | Remove breakpoints at FILE[:LINE], or all of them. |

### Ext

Extensions: list, install, uninstall, inspect, activate.

| Command | Description |
|---------|-------------|
| `list` | List installed extensions. |
| `install` | Install an extension from the marketplace (publisher.name[@version]) or a .vsix file. |
| `uninstall` | Uninstall an extension. |
| `info` | An extension's version, state and contributed commands (from the window). |
| `activate` | Activate an extension in the target window. |

### Config

Settings and keybindings, edited in VS Code's own JSONC files (comments are kept).

| Command | Description |
|---------|-------------|
| `path` | Show the settings and keybindings file paths. |
| `list` | Every setting stored in the file. |
| `get` | Read one setting from the file, or its effective value from a window. |
| `set` | Set a setting. VALUE is JSON (true, 14, "x", {...}) or a plain string. |
| `unset` | Remove a setting from the file. |
| `keys` | List user keybinding rules. |
| `bind` | Add a keybinding, e.g. `config bind ctrl+alt+t workbench.action.terminal.new`. |
| `unbind` | Remove keybinding rules matching --key and/or --command. |

### Ui

Talk to the person at the keyboard: notifications, pickers, input boxes, clipboard.

| Command | Description |
|---------|-------------|
| `notify` | Show a notification. With --button, wait for the click and return it. |
| `status` | Show a message in the status bar. |
| `pick` | Ask the user to pick from a list; returns the choice (null if dismissed). |
| `input` | Ask the user to type something; returns it (null if dismissed). |
| `clipboard-get` | Read the clipboard. |
| `clipboard-set` | Write the clipboard. |
| `open-url` | Open an http(s) URL in the default browser. |

### Git

Source control as VS Code's Git extension sees it.

| Command | Description |
|---------|-------------|
| `status` | Repositories in the window: branch, ahead/behind, staged and unstaged changes. |

### Export

Write what VS Code reports to a file: problems, window state, commands, setup.

| Command | Description |
|---------|-------------|
| `presets` | List export presets. |
| `render` | Produce an export file from the real VS Code. |

### Bridge

The companion extension that lets this CLI reach running windows.

| Command | Description |
|---------|-------------|
| `install` | Package the bridge extension and install it into VS Code (`--app NAME`, or `--all`). |
| `uninstall` | Remove the bridge extension from VS Code. |
| `status` | Is the bridge installed (per profile), and how many windows answer? `--all` for every editor. |
| `build` | Write the bridge .vsix without installing it. |
| `ping` | Check that the target window answers. |
| `call` | Call a bridge method directly with a JSON params object. |
| `eval` | Run JavaScript against the extension API (`vscode`, `require` in scope; use `return`). |

### Session

CLI state: selected window, history, undo.

| Command | Description |
|---------|-------------|
| `status` | Show the selected window, loaded project and undo depth. |
| `history` | Recent state-changing commands (names only, no arguments). |
| `undo` | Undo the last project change (REPL), else restore the last file this CLI rewrote. |
| `redo` | Redo the last undone project change (REPL only). |
| `clear-window` | Forget the window chosen with `window use`. |

## Examples

### Inspect

```bash
codereins --json window list
codereins --json editor state          # active file, selection, dirty flag
codereins --json editor tabs
codereins --json file read src/app.py --lines 10-40   # the editor buffer, unsaved changes included
codereins --json lang diagnostics --severity error
```

### Edit through the editor

```bash
codereins file open src/app.py:42
codereins --json editor replace 42 "    return total" --file src/app.py --save
codereins --json editor sub "old_name" "new_name" --all --file src/app.py --save
codereins --json editor insert "import os
" --at 1:1 --file src/app.py --save
codereins --json editor format --file src/app.py --save
```

### Language servers

```bash
codereins --json lang symbols src/app.py
codereins --json lang definition src/app.py:10:5
codereins --json lang references src/app.py:10:5
codereins --json lang rename src/app.py:10:5 new_name --preview   # files that would change
codereins --json lang rename src/app.py:10:5 new_name --save
```

### Terminal, tasks, debugging

```bash
codereins --json terminal exec "pnpm build" --wait 300   # {output, exitCode, timedOut}
codereins --json task run build --wait 300               # {exitCode}
codereins --json debug break src/app.py:42
codereins --json debug start "Launch Program"
codereins --json debug request stackTrace '{"threadId": 1}'
```

### Any command, settings, exports

```bash
codereins --json command list --filter sidebar
codereins --json command run workbench.action.toggleSidebarVisibility
codereins --json config set editor.fontSize 15
codereins --json config get editor.fontSize --effective
codereins --json session undo                             # restores the last file this CLI rewrote
codereins --json export render problems.md -p diagnostics-md --overwrite
```

## For AI Agents

1. Always pass `--json`. Success prints one JSON document on stdout with exit code 0.
2. Failure is exit code 1 with `{"error": "...", "type": "..."}` on stdout; usage
   mistakes are exit code 2 with a message on stderr. Useful `type` values:
   `no_windows` (bridge missing or no window open), `no_such_window`,
   `no_active_editor`, `timeout`, `no_shell_integration`, `edit_failed`,
   `hosting_window` (the command would end the window this CLI runs inside).
3. Use absolute paths, or run from the workspace folder: relative paths resolve against
   your current directory, and the default window is the one holding that directory.
4. `file read` returns what the editor holds. A file on disk can differ until
   `file save`. Check `isDirty` in `editor state` or `file list`.
5. `--dry-run` shows what would be written or sent without doing it.
6. `terminal exec` returns output and exit code; `terminal send` only types. If `exec`
   reports `no_shell_integration`, fall back to `send`.
7. `ui pick`, `ui input`, `ui notify --button` and `file close` on an unsaved tab wait
   for the person at the keyboard. Raise `--timeout` or avoid them when unattended.
8. Do not end the window your own session runs in. The CLI refuses the commands it can
   recognise (`hosting_window`), but `bridge eval` and commands from other extensions can
   still do it. Confirm the target with `window current` first.
9. Line and column numbers in every result are 1-based.
10. `bridge eval` is off unless the user enabled `codereinsBridge.allowEval`. Prefer
    typed commands and `command run`.

## More Information

- README: `codereins/README.md`
- Design and command map: `docs/DESIGN.md`
- Test plan and results: `codereins/tests/TEST.md`

## Version

0.1.0
