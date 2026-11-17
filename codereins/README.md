# codereins

Drive Visual Studio Code from the command line: windows, files, editors, language
features, terminals, tasks, debugging, extensions and settings. Built with the
[CLI-Anything](https://github.com/HKUDS/CLI-Anything) harness method (see `docs/DESIGN.md` for the design).

Every answer comes from the real VS Code. Nothing is reimplemented.

## How it reaches VS Code

| Backend | Used for | Needs a window open? |
|---|---|---|
| `code` launcher | open, diff, install/uninstall/list extensions, version | no |
| VS Code's JSONC files | user and workspace settings, keybindings, `.code-workspace` files | no |
| Bridge extension | everything live: editors, unsaved text, language servers, terminals, tasks, debugging, any command id | yes |

The `code` launcher cannot read or change a running window, so the live half needs a
small companion extension (shipped inside this package). It opens one named pipe per
window (a Unix socket on macOS and Linux), with a random token per window. No network
port is opened.

## Requirements

- VS Code 1.93 or newer, with `code` on PATH (or `CODEREINS_BIN` set to the launcher)
- Python 3.10 or newer

## Install

```bash
# from a clone of the repo; or: uv tool install git+https://github.com/Kapildevv/codereins
uv tool install --editable . --python 3.12      # puts codereins on PATH
codereins bridge install              # packages and installs the bridge into VS Code
codereins window list                 # open windows that answer
```

A window opened after `bridge install` starts the bridge by itself. A window that was
already open may need "Developer: Reload Window" once.

Remove it again with `codereins bridge uninstall` and `uv tool uninstall codereins`.

## Usage

```bash
codereins                       # interactive REPL
codereins --json <group> <command> ...   # one-shot, JSON for agents
```

Global options: `--json`, `--window <id|focused|path|name>`, `--app <editor>`,
`--project <file.code-workspace>`, `--dry-run`, `--timeout <seconds>`, `--user-data-dir`, `--extensions-dir`.

Positions are 1-based `LINE:COLUMN` everywhere, like the editor and `code --goto`.
Ranges are `L` (whole line), `L1-L2` (whole lines), `L:C` (a point) or `L1:C1-L2:C2`.

### Which window

With several windows open, the target is, in order: `--window`; the window chosen with
`window use`; the window whose workspace holds the current directory; the most recently
focused window.

### Other editors built on VS Code (`--app`)

`--app NAME` (or `CODEREINS_APP=NAME`) drives another editor that ships VS Code's
launcher: `vscode` (default when installed), `insiders`, `vscodium`, `antigravity-ide`.
Its own `product.json` gives its folders (`%APPDATA%\<nameShort>`, `~/<dataFolderName>/extensions`),
and window commands only see that editor's windows. With no `--app`, window commands see
every editor's windows and `window list` shows which editor each belongs to.

```bash
codereins app list                  # installed editors, profiles, live windows
codereins bridge install --all      # the bridge in every installed editor
codereins --app antigravity-ide window list
```

Profiles: `bridge install` covers the Default profile and every profile with its own
extensions; a profile that borrows Default's extensions (like VS Code's built-in "Agents"
profile) is covered by Default. `project profiles` shows which files each profile really uses.
Not handled: portable installs (`data` folder), and `config` for a profile with its own settings.

## Command groups

| Group | What it does |
|---|---|
| `app` | VS Code version, file locations, process status |
| `window` | list, use, current, info, open, focus, reload, close, recent |
| `project` | `.code-workspace` files: new, info, add-folder, remove-folder, set, unset, recommend, open, profiles |
| `file` | open, new, list, read, write, save, close, create, rename, delete, diff |
| `editor` | state, tabs, select, insert, replace, sub (find/replace), format, language, undo, redo |
| `command` | list every command id, run any of them with JSON arguments |
| `workspace` | folders, add-folder, remove-folder, find (glob), symbols |
| `lang` | diagnostics, definition, references, hover, symbols, rename, actions, completions, languages |
| `terminal` | list, new, send, exec (captures output and exit code), show, kill |
| `task` | list, run (optionally wait for the exit code), running, stop |
| `debug` | state, start, stop, request (any DAP request), breakpoints, break, unbreak |
| `ext` | list, install, uninstall, info, activate |
| `config` | path, list, get, set, unset, keys, bind, unbind |
| `ui` | notify, status, pick, input, clipboard-get, clipboard-set, open-url |
| `git` | status as VS Code's Git extension sees it |
| `export` | presets, render (problems as JSON or Markdown, window state, command list, setup bundle) |
| `bridge` | install, uninstall, status, build, ping, call, eval |
| `session` | status, history, undo, redo, clear-window |

Run `codereins <group> --help` for the options of each command.

## Examples

```bash
# What is open, and what does the active editor hold?
codereins --json window list
codereins --json editor state

# Edit through VS Code (undo history and save hooks apply), then check the result
codereins file open src/app.py:42
codereins editor replace 42 "    return total" --file src/app.py --save
codereins editor sub "foo\((\w+)\)" "bar(\$1)" --regex --all --file src/app.py --save

# Ask the language servers
codereins --json lang diagnostics --severity error
codereins --json lang references src/app.py:10:5
codereins --json lang rename src/app.py:10:5 new_name --save

# Run something in the integrated terminal and read its output
codereins --json terminal exec "pnpm build" --wait 300

# Any VS Code command, with typed arguments
codereins command list --filter terminal
codereins command run workbench.action.toggleSidebarVisibility
codereins command run vscode.open '{"$uri": "README.md"}'

# Settings, with comments in settings.json preserved; undo the last write
codereins config set editor.fontSize 15
codereins --json config get editor.fontSize --effective
codereins session undo

# Build a multi-root workspace file
codereins project new -o team.code-workspace --folder api --folder web
codereins --project team.code-workspace project set editor.tabSize 2
codereins --project team.code-workspace project open

# Export the Problems panel
codereins export render problems.md -p diagnostics-md --overwrite
```

`command run` arguments are JSON. Use `{"$uri": "path"}`, `{"$pos": [line, col]}`,
`{"$range": [l1, c1, l2, c2]}` where a command expects a Uri, Position or Range.

## Output and errors

- `--json` prints one JSON document on stdout. Without it, output is plain text.
- Failure: exit code 1, and with `--json` a document `{"error": "...", "type": "..."}`.
  Usage mistakes exit with code 2.
- `--dry-run` reports what would change. File edits are not written and state-changing
  requests are not sent to the window.

## State

`~/.codereins/` (override with `CODEREINS_HOME`) holds:

- `windows/<pid>.json`: one registration per live window (endpoint and token)
- `session.json`: the window chosen with `window use`, command-name history, backup stack
- `backups/`: a copy of every settings, keybindings or workspace file before this CLI
  rewrote it (`session undo` restores the latest)

History stores command names only, never arguments.

## Security

- The bridge accepts requests only with the per-window token, over a pipe or socket
  owned by your user. It never listens on a network port.
- Anything that can read your home folder and run as you could already run commands as
  you; the bridge adds no privilege beyond that. It does mean such a process can drive
  your editor, including typing into its terminals.
- `bridge eval` (arbitrary JavaScript against the extension API) is off until
  `"codereinsBridge.allowEval": true` is set in user settings. A workspace cannot set it.
- `"codereinsBridge.enabled": false` turns the channel off without uninstalling.
- An agent running inside a window (Claude Code in its panel, say) is a descendant of that
  window's extension host, so reloading or closing the window ends the agent's session. The
  CLI walks its own parent processes and refuses, with `hosting_window`, to end that window:
  `window reload/close`, `command run` of a reload, close or quit command (matched by
  pattern, so extensions' own reload commands count), the `workspace add-folder/remove-folder`
  changes that restart the extension host, and `window open --reuse/--add` of a folder into
  that editor. `bridge eval` is not checked.

## Limits

- Only what the extension API exposes is reachable. The contents of other extensions'
  webviews and arbitrary UI pixels are not.
- `terminal exec` needs shell integration (PowerShell, bash, zsh, fish). `terminal send`
  works in any shell but cannot return output.
- `file close` on an unsaved tab makes VS Code ask the user; the call waits until they answer.
- Not built: event streaming (watching for changes), snippets, notebooks, per-profile
  settings edits (`project profiles` lists them; pass `--user-data-dir` for another data folder).

## Tests

```bash
uv pip install --python .venv/Scripts/python.exe -e ".[test]"
CODEREINS_FORCE_INSTALLED=1 python -m pytest codereins/tests/ -v -s
```

The end-to-end tests start their own VS Code with a throwaway profile; your profile and
open windows are not touched. Plan and results: `tests/TEST.md`.
