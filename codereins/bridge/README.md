# CodeReins Bridge

Companion extension for the `codereins` command. It gives that command a way
into a running window: the extension API is the only supported way to read editor state
or run a command inside VS Code.

- One server per window, on a named pipe (Windows) or a Unix socket. No network port.
- Each window writes `~/.codereins/windows/<pid>.json` with its endpoint and a
  random token. Every request must carry that token.
- `codereinsBridge.enabled` (default on) switches the channel off.
- `codereinsBridge.allowEval` (default off) allows `bridge eval`. Both settings are
  user-scope only, so a workspace cannot change them.

Install and remove it with `codereins bridge install` / `bridge uninstall`.
