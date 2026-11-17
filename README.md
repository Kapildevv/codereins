# CodeReins

Drive a running Visual Studio Code from the command line, or from an AI agent.

`codereins` reads and changes what is actually open in your editor: windows, unsaved text,
language-server answers (problems, definition, references, rename), the integrated terminal
(with captured output and exit code), tasks, debugging, extensions, settings, and any VS Code
command id. Every answer comes from the real editor; nothing is reimplemented.

It also drives editors built on VS Code: Insiders, VSCodium and Antigravity IDE (`--app`).

> **Status: 0.1.0, early.** Tested on Windows 11 with VS Code 1.146 and Antigravity IDE 1.107.
> The macOS and Linux code paths exist but have not been run yet. Reports welcome in Issues.

## Install

You need Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/), git, and
VS Code 1.93+ with `code` on PATH.

```bash
uv tool install git+https://github.com/Kapildevv/codereins
codereins bridge install          # or: codereins bridge install --all  (every editor found)
codereins window list             # the windows that answer
```

`bridge install` adds a small companion extension to VS Code. A window opened afterwards starts
it by itself; a window that was already open needs **Developer: Reload Window** once.

Update with `uv tool upgrade codereins`. Remove with `codereins bridge uninstall`, then
`uv tool uninstall codereins`.

## A taste

```bash
codereins --json editor state                       # what the active editor holds
codereins --json lang diagnostics --severity error  # the Problems panel
codereins --json terminal exec "pnpm build" --wait 300
codereins editor replace 42 "    return total" --file src/app.py --save
codereins command run workbench.action.toggleSidebarVisibility
codereins                                           # interactive shell
```

Every command takes `--json` for machine-readable output. The full command list, options and
examples are in [codereins/README.md](codereins/README.md); the design is in
[docs/DESIGN.md](docs/DESIGN.md).

## For AI agents

An agent skill describing every command is at [skills/codereins/SKILL.md](skills/codereins/SKILL.md).
Copy that folder into your agent's skills folder (for Claude Code: `~/.claude/skills/codereins/`).

## Safety

- The bridge talks over a named pipe (Windows) or a Unix socket owned by your user, with a random
  token per window. It never opens a network port.
- `bridge eval` (arbitrary JavaScript) is off unless you set `"codereinsBridge.allowEval": true`
  in your user settings. A workspace cannot turn it on.
- An agent running inside a VS Code window dies if that window reloads or closes. CodeReins checks
  its own parent processes and refuses (`hosting_window`) to reload, close or re-folder the window
  it is running in.

## Credits and licence

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Built following the
[CLI-Anything](https://github.com/HKUDS/CLI-Anything) harness method, and includes one file from
that project. CodeReins is an independent project, not made or endorsed by the CLI-Anything
authors or by Microsoft. "Visual Studio Code" is a trademark of Microsoft Corporation.
