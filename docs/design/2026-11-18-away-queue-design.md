# Away queue: design

Status: approved by the owner, 2026-11-18; not yet built. Part 1 of 3 (queue and approvals); part 2 is setup and a repo catalogue; part 3 is the prompter and other skills.

Flow diagram: `2026-11-18-away-queue-flow.html`, beside this file.

## Intent and why

The owner states the purpose: this tool exists mainly so a person can stay away for one week with no laptop and no mobile. They set a task, leave the laptop switched on, and with its tools it keeps working.

Nobody can be reached that week, so nothing may wait for an answer. Every decision a person must make is made before they leave; everything else is inside what they approved or set aside for their return. A ping reaches no one, and the default checklist (everything ticked) would stall the queue on day one, so a pre-flight shows the stalls first.

## Goals

1. **Queue on disk, run unattended.** A job has a brief (file), working folder, model, effort, rules file, done-checks, limits (budget, time) and dependencies.
2. **Agent cap.** One agent at a time on basic and Max plans, two on Team, Enterprise and API billing; user-changeable. Account-wide, busy sessions only; workers start no sub-agents by default. A busy session the tool did not start counts against the cap and is never touched.
3. **One approval checklist.** The user ticks what needs "ask me first": shell commands that change things; edits outside the job's folder; git commit; push and other GitHub actions; deploy or publish; installing anything; spend above a cap; delete or overwrite; sending anything outward; leaving the agreed plan; closing finished tabs and windows (goal 13). Presets: none ticked ("no approval"), all ticked ("ask for each action and commit"), custom; new users start all ticked. Permission rules and hooks in the job's generated rules file enforce it, never a model's judgment. "Leaving the agreed plan" cannot be a rule: the approved queue and briefs are the boundary, and a job needing more is set aside.
4. **While away, nothing asks.** A job that reaches anything outside its envelope (goal 15) is set aside with the exact question it waits on; its dependents wait and the rest of the queue carries on. On return the person answers by typed command naming that one request: yes (that request only), no, or free text; the session resumes with the answer. A yes is a one-time permit the runner writes and logs, never an edit of the job's rules. At the first refused action the runner ends the session and sets the job aside, so the model cannot work around the refusal. A ping is optional and off by default: nobody is there to receive it.
5. **Floor.** No setting removes these: a job cannot change its own rules, profile or the runner's state, or touch any session, tab, terminal or window the tool did not open; secrets never reach logs or reports; every action is logged. Only a person typing a command changes the approval level.
6. **Fence.** Every away job, whatever is ticked, works in its own git worktree and branch. Pushing to the default branch and deploying or publishing are never free inside the fence: set aside with the question when their item is ticked (unless pre-answered), refused when not. Reading `.env` files is always refused. Removing the fence is a separate switch, typed by a person at start and recorded in the report. It starts from a commit, so a folder with uncommitted changes is set aside, with the reason. A dependent job starts from its predecessor's branch once that job's checks pass. The tool never merges into the user's branch; the report lists the branches.
7. **Installs.** Only from a user-approved list. An approval covers the exact version and scope shown; a changed repo needs a fresh approval; a need found mid-run is queued, never installed.
8. **Context meter and handover.** Session size is the last main-chain assistant `usage` in the session transcript (input plus cache-creation plus cache-read tokens). Past a threshold, at the next stop the session writes a self-contained handover brief and the runner continues in a fresh session. It can also report on hand-run sessions.
9. **Load gate.** No job starts while free memory is low, the GPU is busy or a render is running; measure memory and running jobs each time, not tabs.
10. **Done-check.** When a session stops the runner runs the checks itself; "stopped" is not "succeeded". On failure: one retry with the failure output, then set aside.
11. **Brakes.** A job that keeps failing or makes no progress is set aside; only it and its dependants stop. The whole queue stops only when the same machine-level failure (sign-in, disk, network, a broken `claude`) hits several jobs in a row, because the next job would fail the same way. A per-job time cap, which always holds; a budget, best-effort from the stream's reported cost. Two week-long brakes: logs are rotated and free disk is re-checked during the run; if the `claude` sign-in stops mid-week the queue stops cleanly and records why (only a person can sign in again). The usage brake has its own section.
12. **Restarts.** All state is in files. After a crash, restart or power cut the runner starts itself with no person present, from an entry registered at pre-flight to start when the machine starts or the user logs on, and continues the approved queue; an interrupted job resumes by session id. A manual command also exists.
13. **Close/reload lock.** By default the tool refuses every operation that closes, kills or reloads a tab, terminal or window in a real editor, in every window (a reload ends that window's sessions). Closing finished things is a separate switch, off in every preset including "no approval"; when on, only things the tool opened (a ledger), only after their checks pass, never a window holding anything the tool did not open. The lock binds every caller, person or agent; a person closes or reloads in the editor itself.
14. **Return report.** The main interface: one page per away run. Each job's result; each set-aside job with the exact question it waits on; every decision made without the person; a timeline of the machine and queue (restarts, usage holds, limit waits, sign-in or version changes); spend; the branches to review; how to open each session as an editor tab.
15. **Pre-flight (before leaving; first in time).** One command, run by the person; the last act that needs a person. It does four things.
    - *Machine readiness.* Reports each check with its exact fix: mains power; sleep and hibernate off while plugged in; what closing the lid does; a pending operating-system update or restart; free disk space; network; the `claude` sign-in works; the `claude` version meets the minimum; the runner is registered to start by itself after a restart (pre-flight registers its own start entry on a yes). The tool changes no operating-system setting; the person fixes. The screen need not stay on and the session may be locked: the runner is headless, so only power and no sleep matter.
    - *Stall forecast.* Reads the queue and each job's rules and says which jobs will stop and at which ticked item (example: "job 3 will stop at its first commit, because 'git commit' is ticked"). The person unticks it, answers it now, or accepts that the job waits for their return.
    - *Answers given now.* A pre-answer for a named job and item ("job 3 may commit on its own branch") joins the approved queue; it is not a runtime yes.
    - *Approval.* The person approves the queue; briefs, job entries and pre-answers are frozen by hash. That frozen set is the envelope.

**The brief.** Part 3 fills it; required: goal and why; what is known and files to read first; limits and what must not be touched; the checks that prove it is done; what to send back; a fixed guardrail block.

## Non-goals

Pressing Approve inside an editor tab or setting a tab's model. Answering from a phone: the answer channel stays a seam, and any such channel must authenticate the sender and tie each answer to one request. Setup and the catalogue (part 2). The prompter and other skills (part 3). A GUI, cloud runs, several machines, a council of models.

## Anti-goals

Each is a failure even if all else works: anything closed or reloaded that the tool did not open; a component that auto-approves; a job able to loosen its own rules; spend past the cap; a secret in a log; an install nobody approved; a control channel that accepts answers from an unauthenticated sender.

## Constraints

- Windows 11 first, portable code. Works with a subscription sign-in, no API key, so the runner never uses `--bare`.
- Only switches the installed `claude` has. Checked on 2.1.316 with `--help`: `-p`, `--model`, `--effort`, `--permission-mode`, `--permission-prompts <host|none>`, `--settings`, `--setting-sources`, `--session-id`, `--resume`, `--max-budget-usd` (print mode only), `--output-format stream-json`, `--input-format`, `--allowedTools`, `--disallowedTools`, `--mcp-config`, `--strict-mcp-config`, `--agents`, `-n`, `-w`, `--append-system-prompt`. No `--max-turns`; time and budget caps replace it. The editor extension bundles a newer build than the one on PATH, so the runner records the binary path and version per job, checks the version at start, and refuses to run below a minimum the builder sets after step zero.
- Apache-2.0. Ideas only from AGPL projects, no code. No secrets in the repo. State and logs live in a gitignored workspace folder.
- Repo style: Python 3.10+, click, pytest, no third-party packages beyond click and prompt-toolkit (probes use ctypes).

## Approaches

**A. Editor chat tabs driven by peer messages.** Rejected: a sealed webview; nothing presses Approve or sets its model, and a new tab stalls at its first prompt.

**B. A headless queue runner (picked).** Starts `claude -p` for one job at a time with its own model, effort and rules file, so rules enforce approvals and no editor is needed; sessions open as editor tabs on return. Costs: nothing to watch live, tab-by-session-id untested, we own the queue logic.

**C. An Agent SDK service.** Rejected: likely billed to an API key, not the plan.

## How it works

**What a job starts with.** A fresh worktree has no installed dependencies, so the entry may list prepare commands, approved with it; restoring what the project pins is not an "install". Environment values go only to the runner's prepare and check commands, from files the entry names. Jobs inherit no plugins, MCP servers or connectors, which would bypass the shell rules; the entry names what each needs.

**A job's life.** Before the week: pre-flight and approval (goal 15). The runner starts from a plain terminal or its registered entry, never inside an editor's extension host (a reload would end it). It picks a ready job, takes a slot, passes the load gate and the usage brake, and writes the rules file from the floor, the checklist, the pre-answers and the fence. It records the rules hash, a new session id and the binary version, then starts `claude -p` (stream-json, the job's model, effort and budget, `--settings` at the rules file, sub-agents barred).

The session ends one of four ways. Finished: the runner runs the checks; a pass frees the slot; a fail resumes the session once with the failure output; a second fail sets the job aside. Outside the envelope: record the question, set the job aside, free the slot, move on. Usage limit: hold, then continue after the reset. Time or budget cap: end the process tree, set the job aside.

**Self-start and drift.** After a crash, restart or power cut the registered entry starts the runner with no one present; it re-checks the `claude` version and sign-in, resumes an interrupted job by session id and continues. A version that differs from the one recorded at pre-flight (auto-update) is re-checked against the minimum before the next launch and shown in the report. Holding auto-update off is to verify (item 8).

**The meter** reads the transcript during a run. A headless run's next stop is its end, so a session past the soft threshold finishes, is resumed once only to write the handover brief, and continues fresh; past a hard ceiling the runner ends the run first. Proposed defaults, user-settable: soft at half the model's context window, hard at four-fifths. Before each launch the runner compares hashes of the rules file, approvals and its own state; a mismatch stops the queue and shows in the report. This detects tampering, not prevents it: permission rules are a guard, not a sandbox; the fence limits the damage. With the shell item ticked, the rules allow-list known read-only commands and any other needs an answer (goal 4).

## Usage brake

Owner's requirement: slow the runs by the plan's remaining usage and time to reset, and wait for the reset. The rest is the designer's.

**Three steps by allowance left.** Normal: jobs run as briefed. Economy: a cheaper model or lower effort, only for jobs whose brief allows it; jobs that need the strong model wait. Hold: nothing starts until the reset, then the queue continues. Both the five-hour and the seven-day window count, and the tighter one rules. The user can set a reserve: a share of the window the queue never spends.

**Over a week.** The seven-day window usually rules. Jobs run in the order given; work is not artificially spread over the days. The reserve is what the person keeps for use on return.

**Sources, in order of preference.**

- (a) The runner's own stream. The documented `rate_limit_event` message carries `rate_limit_info` with `status` (allowed, allowed_warning, rejected) and optionally `utilization` and `resetsAt`. At the limit comes an assistant message with `error: "rate_limit"`, then an error result (API status 429).
- (b) A poll between jobs with `claude -p "/usage"`: percent and reset time per window, without a model turn.
- (c) Optional, through a seam: a usage file (percent used and reset time per window) written by an add-on the user already runs. Never required.

The tool never reads sign-in files and never calls any service with the sign-in token.

**Waiting.** Claude Code has its own wait-out-the-limit setting, `CLAUDE_CODE_RETRY_WATCHDOG` (2.1.301 and later). The default waits in the runner; the builder may prefer the built-in wait.

## Where it lives in this repo

State is plain files, written atomically. The slots file and the usage brake are shared by every queue on the account, so they live in the existing home folder (`~/.codereins`, or `CODEREINS_HOME`), using the locked save in `core/session.py`; other state is in the workspace.

**The lock.** Today's guard, `windows.refuse_if_hosting`, has no override, on purpose. The new lock's scope is anything that can end a session or its host: tabs, terminals, windows, reloads, extension uninstall or disable, and install steps that need a reload. Commands that close, kill or reload: `window reload`, `window close`, `file close` (`--all` closes every tab, Claude tabs included), `terminal kill`, `task stop`, `command run`, `workspace add-folder`/`remove-folder`, `window open --reuse/--add`, `bridge call`, `bridge eval`. Unmeasured, so locked until measured: `debug stop`, `debug request`, `ext install/uninstall`, `bridge install/uninstall`.

Gaps found:

- `bridge call` takes any bridge method and has no guard, not even the hosting check; `bridge call window.reload` passes today.
- `command_reach` matches only reload, restart, close-window, reopen and quit ids; `workbench.action.closeActiveEditor`, `closeAllEditors` and the terminal-kill commands pass.
- The existing guard covers only the hosting window; other windows' sessions are unprotected.

The CLI funnel is `BridgeClient.call` (`utils/bridge_client.py`) plus `backend.run_code`; the last chokepoint is `handleLine` in `bridge/extension.js`, since any same-user process can read a window's token file and call its pipe. The lock goes in both, CLI first (a bridge change needs `bridge install` and a reload). The ledger lives in the bridge; no API returns a Claude tab's identity, so it diffs the tab list around the open.

## Decided by the owner

- The purpose: a week away, no contact, the laptop left on and working; unattended jobs on the user's plan, no API key.
- Approach B; tabs only for the lock, meter and opening sessions.
- Goal 2: the cap by plan (one on basic and Max, two otherwise), changeable.
- Goal 3: the approval level per user, by checklist and three presets.
- Goal 7: installs only from an approved list, by one yes or a tick per item.
- Goals 8 and 9: a context meter and a move to a fresh session; no new work under heavy render or GPU load.
- Usage brake: slow runs by remaining usage; wait for the reset.
- Goal 13: nothing closed or reloaded by default; closing finished things selectable.

## Calls made by the designer

The owner may strike any.

- Goal 15: pre-flight with its four parts; the screen may be locked.
- Goal 4: nothing asks while away; yes-once, no or free text; a yes is a one-time permit; the session ends at the first refusal; ping optional, off by default.
- Goals 10, 11, 12 and 14: done-check with one retry; failure and time brakes; clean stop on sign-in loss; log rotation and disk re-check; self-start after a restart; version re-check after an auto-update; report.
- Usage brake: three steps, both windows, the reserve, the order of sources, no sign-in reads, waiting in the runner, no artificial spreading of work.
- Goal 2: account-wide, busy sessions only, sub-agents barred, setup asks the plan.
- Goal 3: new users start with all ticked (the stall forecast makes its cost visible before leaving); the plan as a boundary.
- Goal 5: the floor; tamper detection by hash. Goal 9: measured memory and running jobs, not tabs.
- Goal 6: the fence on every away job (the owner was told "no-approval runs"; widened), from a commit, one branch per job, no automatic merge.
- No inherited plugins or connectors; prepare commands; environment values only to the runner's own commands.
- One queue per workspace; hand-run busy sessions count against the cap; the meter's two thresholds.
- Goal 7: exact version and scope, fresh approval, mid-run needs queued.
- Goal 13: closing as a separate switch (off in every preset, ledger, after checks only, no foreign windows); the wide scope.
- The runner starts outside editors; gitignored state folder; Windows first; a neutral test job.

## To verify first

Step zero. Unknowns, not assumptions.

1. Does a session started outside the editor open as an editor tab by its id? Untested.
2. Does `rate_limit_event` appear in print-mode stream output on the installed version, and in what unit is `resetsAt`? Real transcripts show epoch seconds, with `rateLimitType` five_hour or seven_day.
3. Does `/usage` work in print mode? Documented, not run.
4. What exit code and final message come when the limit is hit? Does the built-in wait work in print mode?
5. How does an "ask" rule reach the permission handler in print mode? Setting a job aside needs the process to end: `--permission-prompts none` (the runner reads the denial from the stream) or `host` with a prompt tool. If neither surfaces an ask or a refusal to the runner, stop and return to the owner: setting aside depends on it.
6. The tool name to deny so workers cannot start sub-agents, plus any peer-messaging tools.
7. Can the runner start after a restart with nobody logged in on Windows, and is the `claude` sign-in usable there? If not, the fallback is start-at-log-on, and pre-flight must say an automatic restart without log-on would stop the week.
8. Can auto-update be held off for the runner's own child processes?

Added by the designer: whether `--max-budget-usd` means anything on a sign-in; whether `--input-format stream-json` can send a message mid-run, and how auto-compaction moves the meter.

## Acceptance scenario

A compressed week in a scratch repo: three small neutral jobs, nobody typing after approval. Pre-flight correctly forecasts which job will stop. One job finishes and passes its checks. One reaches the ticked item and is set aside with its question while the rest continue. One fails its checks twice and is set aside. The machine restarts mid-run and the queue continues by itself. A usage hold is waited out. The tool closes nothing in any editor. The report shows all of it truthfully. On "return", one typed answer resumes the set-aside job.

## Left to the builder

- Command names, file formats, folder names (proposed: `.codereins/away/`, ignoring itself).
- The optional ping, off by default: a user-supplied webhook (its URL is a secret, kept out of the repo and logs).
- How pre-flight reads each machine setting and registers the start entry (proposed on Windows: a scheduled task).
- How a busy session the tool did not start is detected (proposed: recent writes to its transcript).
- Where the bridge reads the lock switch and the ledger (proposed: a file in the home folder no job can write).
- The load probes, including what counts as "a render is running".
- Tests: unit tests use a stub in place of `claude`; tests that start real `claude` sessions sit behind an explicit switch, because they spend the tester's plan allowance.
- A fence for a non-git folder (recommended: refuse and set aside). The lock on unknown command ids and `bridge eval` (recommended: deny known patterns, refuse `eval` while locked).

## Settled at approval

The owner approved the design with these two defaults.

1. **Telling a person from an agent.** Four acts are reserved for a person: approving a queue, answering a request, changing the approval level, and removing the fence. An agent can run the same commands through a shell. Default: these commands refuse to run without an interactive terminal, jobs are denied the tool's own commands altogether, and changing the level or removing the fence also asks for a PIN the user set.
2. **Test windows.** The tool's own end-to-end tests reload and close throwaway editor windows that the test run itself starts. Default: allowed for those windows only, never for a window that existed before the run.
