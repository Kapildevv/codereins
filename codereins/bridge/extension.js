'use strict';
/*
 * CodeReins Bridge.
 *
 * One JSON-RPC server per window, on a named pipe (Windows) or a Unix socket.
 * Requests are newline-delimited JSON: {id, token, method, params}.
 * Replies:                              {id, ok, result} | {id, ok:false, error:{code,message}}.
 *
 * The window advertises itself in <home>/windows/<pid>.json (endpoint + token), where
 * <home> is $CODEREINS_HOME or ~/.codereins. Nothing listens on a
 * network port, so a web page cannot reach it; a caller needs the token from that file.
 *
 * Positions on the wire are 1-based {line, column}, like the editor UI and `code --goto`.
 */
const vscode = require('vscode');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const VERSION = require('./package.json').version;
const MAX_LINE = 16 * 1024 * 1024;
const IS_WIN = process.platform === 'win32';

let server = null;
let registryFile = null;
let socketFile = null;
let state = null;

class RpcError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const fail = (code, message) => { throw new RpcError(code, message); };
const need = (p, ...keys) => {
  for (const k of keys) if (p[k] === undefined || p[k] === null) fail('bad_params', `Missing parameter: ${k}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function homeDir() {
  return process.env.CODEREINS_HOME || path.join(os.homedir(), '.codereins');
}

// ── converters ───────────────────────────────────────────────────────────
const P = (line, col) => new vscode.Position(Math.max(0, (line | 0) - 1), Math.max(0, ((col || 1) | 0) - 1));
const R = (a) => new vscode.Range(P(a[0], a[1]), P(a[2] === undefined ? a[0] : a[2], a[3] === undefined ? a[1] : a[3]));

function toUri(p) {
  if (typeof p !== 'string' || !p) fail('bad_params', 'Expected a path or URI');
  if (/^[a-zA-Z][a-zA-Z0-9+.-]+:\/\//.test(p) || /^untitled:/.test(p)) return vscode.Uri.parse(p);
  return vscode.Uri.file(path.resolve(p));
}

function ser(v, seen, budget) {
  seen = seen || new WeakSet();
  budget = budget || { n: 100000 };
  if (v === null || v === undefined) return null;
  const t = typeof v;
  if (t === 'string' || t === 'number' || t === 'boolean') return v;
  if (t === 'bigint') return v.toString();
  if (t === 'function' || t === 'symbol') return undefined;
  if (--budget.n < 0) return '[truncated]';
  if (v instanceof vscode.Uri) return v.scheme === 'file' ? v.fsPath : v.toString();
  if (v instanceof vscode.Position) return { line: v.line + 1, column: v.character + 1 };
  if (v instanceof vscode.Range) return { start: ser(v.start), end: ser(v.end) };
  if (v instanceof vscode.Location) return { path: ser(v.uri), range: ser(v.range) };
  if (v instanceof vscode.MarkdownString) return v.value;
  if (v instanceof Date) return v.toISOString();
  if (v instanceof Uint8Array) return { $bytes: v.length };
  if (v instanceof Error) return { error: v.message };
  if (seen.has(v)) return '[circular]';
  seen.add(v);
  if (Array.isArray(v)) return v.map((x) => { const y = ser(x, seen, budget); return y === undefined ? null : y; });
  if (v instanceof Map) return Object.fromEntries([...v].map(([k, x]) => [String(k), ser(x, seen, budget)]));
  if (v instanceof Set) return [...v].map((x) => ser(x, seen, budget));
  const out = {};
  for (const k of Object.keys(v)) {
    if (k.startsWith('_')) continue;
    try { const x = ser(v[k], seen, budget); if (x !== undefined) out[k] = x; } catch (_) { /* unreadable getter */ }
  }
  return out;
}

// JSON arguments for `commands.execute` may carry {$uri}, {$pos:[l,c]}, {$range:[l,c,l,c]}, {$sel:[...]}.
function revive(v) {
  if (Array.isArray(v)) return v.map(revive);
  if (v && typeof v === 'object') {
    if (typeof v.$uri === 'string') return toUri(v.$uri);
    if (Array.isArray(v.$pos)) return P(v.$pos[0], v.$pos[1]);
    if (Array.isArray(v.$range)) return R(v.$range);
    if (Array.isArray(v.$sel)) { const r = R(v.$sel); return new vscode.Selection(r.start, r.end); }
    const o = {};
    for (const k of Object.keys(v)) o[k] = revive(v[k]);
    return o;
  }
  return v;
}

const enumName = (e, v) => (e && e[v] !== undefined ? String(e[v]) : v);

// ── documents and editors ────────────────────────────────────────────────
async function getDoc(p) {
  const target = p.path || p.uri;
  if (target) return vscode.workspace.openTextDocument(toUri(target));
  const ed = vscode.window.activeTextEditor;
  if (!ed) fail('no_active_editor', 'No active text editor; pass a path');
  return ed.document;
}

function activeEditor() {
  const ed = vscode.window.activeTextEditor;
  if (!ed) fail('no_active_editor', 'No active text editor');
  return ed;
}

const docInfo = (doc) => ({
  path: ser(doc.uri),
  uri: doc.uri.toString(),
  languageId: doc.languageId,
  lineCount: doc.lineCount,
  isDirty: doc.isDirty,
  isUntitled: doc.isUntitled,
  version: doc.version,
  eol: doc.eol === vscode.EndOfLine.CRLF ? 'CRLF' : 'LF',
});

const editorInfo = (ed) => ({
  ...docInfo(ed.document),
  viewColumn: ed.viewColumn === undefined ? null : ed.viewColumn,
  selections: ed.selections.map((s) => ({
    start: ser(s.start), end: ser(s.end), active: ser(s.active), isEmpty: s.isEmpty,
    text: s.isEmpty ? '' : ed.document.getText(s).slice(0, 20000),
  })),
  visibleRanges: ser(ed.visibleRanges),
  options: { tabSize: ed.options.tabSize, insertSpaces: ed.options.insertSpaces },
});

const fullRange = (doc) => new vscode.Range(new vscode.Position(0, 0), doc.lineAt(doc.lineCount - 1).range.end);

function tabKind(input) {
  if (input instanceof vscode.TabInputText) return 'text';
  if (input instanceof vscode.TabInputTextDiff) return 'diff';
  if (input instanceof vscode.TabInputCustom) return 'custom';
  if (input instanceof vscode.TabInputWebview) return 'webview';
  if (input instanceof vscode.TabInputNotebook) return 'notebook';
  if (input instanceof vscode.TabInputNotebookDiff) return 'notebook-diff';
  if (input instanceof vscode.TabInputTerminal) return 'terminal';
  return 'other';
}
const tabUri = (t) => (t.input && (t.input.uri || t.input.modified)) || null;
const tabInfo = (t) => ({
  label: t.label, kind: tabKind(t.input), path: tabUri(t) ? ser(tabUri(t)) : null,
  isActive: t.isActive, isDirty: t.isDirty, isPinned: t.isPinned, isPreview: t.isPreview,
});

async function applyTextEdit(doc, build, save) {
  const we = new vscode.WorkspaceEdit();
  build(we);
  if (!(await vscode.workspace.applyEdit(we))) fail('edit_failed', 'VS Code rejected the edit');
  if (save) await doc.save();
  return docInfo(doc);
}

// ── language features ────────────────────────────────────────────────────
async function at(p) {
  need(p, 'line');
  const doc = await getDoc(p);
  return [doc.uri, P(p.line, p.column)];
}
const locations = (command) => async (p) => {
  const [uri, pos] = await at(p);
  const res = (await vscode.commands.executeCommand(command, uri, pos)) || [];
  return res.map((l) => (l.targetUri
    ? { path: ser(l.targetUri), range: ser(l.targetSelectionRange || l.targetRange) }
    : ser(l)));
};

function symbolTree(s) {
  const kind = enumName(vscode.SymbolKind, s.kind);
  if (s.location) return { name: s.name, kind, container: s.containerName || null, path: ser(s.location.uri), range: ser(s.location.range) };
  return {
    name: s.name, kind, detail: s.detail || '', range: ser(s.range), selectionRange: ser(s.selectionRange),
    children: (s.children || []).map(symbolTree),
  };
}

const SEVERITY = ['error', 'warning', 'info', 'hint'];
const diagInfo = (uri, d) => ({
  path: ser(uri), range: ser(d.range), severity: SEVERITY[d.severity] || String(d.severity),
  message: d.message, source: d.source || null,
  code: d.code && typeof d.code === 'object' ? d.code.value : (d.code === undefined ? null : d.code),
});

function editSummary(we) {
  return we.entries().map(([uri, edits]) => ({ path: ser(uri), edits: edits.length }));
}

// ── terminals, tasks ─────────────────────────────────────────────────────
const ANSI = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]/g;
const clean = (s) => s.replace(ANSI, '').replace(/\r\n/g, '\n').replace(/\r/g, '');

// A terminal created with a name keeps that name as its identity. Terminal.name is the live title,
// which can read the shell's name ("powershell") for a moment after createTerminal({name}) (seen on
// VS Code 1.146), so a lookup by name straight after creating the terminal could miss it.
const termName = (t) => (t.creationOptions && t.creationOptions.name) || t.name;

async function termInfo(t) {
  const all = vscode.window.terminals;
  let pid = null;
  try { pid = (await Promise.race([t.processId, sleep(1500)])) || null; } catch (_) { /* closed */ }
  return {
    index: all.indexOf(t), name: termName(t), title: t.name, processId: pid,
    isActive: t === vscode.window.activeTerminal,
    exitStatus: t.exitStatus ? ser(t.exitStatus) : null,
    shellIntegration: !!t.shellIntegration,
    cwd: t.shellIntegration && t.shellIntegration.cwd ? ser(t.shellIntegration.cwd) : null,
  };
}

function findTerminal(p) {
  const all = vscode.window.terminals;
  let t;
  if (p.index !== undefined && p.index !== null) t = all[p.index];
  else if (p.name) t = all.find((x) => termName(x) === p.name) || all.find((x) => x.name === p.name);
  else t = vscode.window.activeTerminal || all[all.length - 1];
  if (!t) fail('no_terminal', 'No matching terminal');
  return t;
}

function waitShellIntegration(t, ms) {
  if (t.shellIntegration) return Promise.resolve(t.shellIntegration);
  return new Promise((resolve) => {
    const d = vscode.window.onDidChangeTerminalShellIntegration((e) => {
      if (e.terminal === t) { clearTimeout(timer); d.dispose(); resolve(e.shellIntegration); }
    });
    const timer = setTimeout(() => { d.dispose(); resolve(t.shellIntegration || null); }, ms);
  });
}

const taskInfo = (t) => ({
  name: t.name, source: t.source, type: t.definition && t.definition.type,
  group: t.group ? t.group.id : null, isDefault: !!(t.group && t.group.isDefault), detail: t.detail || null,
  scope: t.scope && typeof t.scope === 'object' ? t.scope.name : (t.scope === vscode.TaskScope.Global ? 'global' : 'workspace'),
});

async function findTask(p) {
  need(p, 'name');
  const tasks = await vscode.tasks.fetchTasks();
  const hit = tasks.filter((t) => t.name === p.name && (!p.source || t.source === p.source));
  if (!hit.length) fail('no_task', `No task named ${p.name}`);
  if (hit.length > 1 && !p.source) fail('ambiguous_task', `Several tasks are named ${p.name}; pass a source: ${hit.map((t) => t.source).join(', ')}`);
  return hit[0];
}

// ── misc ─────────────────────────────────────────────────────────────────
const bpInfo = (b) => ({
  id: b.id, enabled: b.enabled, condition: b.condition || null, hitCondition: b.hitCondition || null,
  logMessage: b.logMessage || null,
  path: b.location ? ser(b.location.uri) : null,
  line: b.location ? b.location.range.start.line + 1 : null,
  function: b.functionName || null,
});

// VS Code echoes breakpoint changes back from its main process. Until that echo lands, a
// breakpoint removed right after it was added can reappear in vscode.debug.breakpoints.
// Wait until `stable()` holds across two round trips to the main process.
async function settleBreakpoints(stable) {
  for (let i = 0; i < 40; i++) {
    await vscode.commands.executeCommand('setContext', 'codereinsBridge.tick', i);
    if (stable()) {
      await sleep(30);
      await vscode.commands.executeCommand('setContext', 'codereinsBridge.tick', -i);
      if (stable()) return;
    }
    await sleep(30);
  }
}

const extInfo = (e) => ({
  id: e.id, version: e.packageJSON.version, displayName: e.packageJSON.displayName || e.packageJSON.name,
  isActive: e.isActive, isBuiltin: !!e.packageJSON.isBuiltin,
  kind: e.extensionKind === vscode.ExtensionKind.UI ? 'ui' : 'workspace', path: e.extensionPath,
});

const GIT_STATUS = ['index-modified', 'index-added', 'index-deleted', 'index-renamed', 'index-copied',
  'modified', 'deleted', 'untracked', 'ignored'];
const change = (c) => ({ path: c.uri.fsPath, status: GIT_STATUS[c.status] || String(c.status) });

const folders = () => (vscode.workspace.workspaceFolders || []).map((f) => ({ index: f.index, name: f.name, path: ser(f.uri) }));

function info() {
  return {
    bridgeVersion: VERSION,
    id: state ? state.id : null,
    vscodeVersion: vscode.version,
    appName: vscode.env.appName,
    appRoot: vscode.env.appRoot,
    uiKind: enumName(vscode.UIKind, vscode.env.uiKind),
    language: vscode.env.language,
    shell: vscode.env.shell,
    remoteName: vscode.env.remoteName || null,
    pid: process.pid,
    focused: vscode.window.state.focused,
    workspace: {
      name: vscode.workspace.name || null,
      file: vscode.workspace.workspaceFile ? ser(vscode.workspace.workspaceFile) : null,
      folders: folders(),
      trusted: vscode.workspace.isTrusted,
    },
    theme: {
      kind: enumName(vscode.ColorThemeKind, vscode.window.activeColorTheme.kind),
      name: vscode.workspace.getConfiguration('workbench').get('colorTheme') || null,
    },
    allowEval: !!vscode.workspace.getConfiguration('codereinsBridge').get('allowEval'),
  };
}

const TARGETS = { user: vscode.ConfigurationTarget.Global, workspace: vscode.ConfigurationTarget.Workspace, folder: vscode.ConfigurationTarget.WorkspaceFolder };

function configGet(p) {
  need(p, 'key');
  const c = vscode.workspace.getConfiguration(undefined, p.resource ? toUri(p.resource) : undefined);
  const i = c.inspect(p.key) || {};
  return {
    key: p.key, value: ser(c.get(p.key)), default: ser(i.defaultValue), user: ser(i.globalValue),
    workspace: ser(i.workspaceValue), folder: ser(i.workspaceFolderValue),
  };
}

// Reply first, then act: the action tears down the extension host that would send the reply.
const deferred = (command) => () => {
  setTimeout(() => vscode.commands.executeCommand(command), 150);
  return { scheduled: command };
};

// ── method table ─────────────────────────────────────────────────────────
const METHODS = {
  ping: () => ({ pong: true, id: state.id, bridgeVersion: VERSION }),
  info,

  'commands.list': async (p) => {
    let ids = await vscode.commands.getCommands(!p.internal);
    if (p.filter) { const f = String(p.filter).toLowerCase(); ids = ids.filter((c) => c.toLowerCase().includes(f)); }
    return ids.sort();
  },
  'commands.execute': async (p) => {
    need(p, 'command');
    return ser(await vscode.commands.executeCommand(p.command, ...revive(p.args || [])));
  },
  eval: async (p) => {
    need(p, 'code');
    if (!vscode.workspace.getConfiguration('codereinsBridge').get('allowEval')) {
      fail('eval_disabled', 'eval is off. Set "codereinsBridge.allowEval": true in VS Code user settings to allow it.');
    }
    const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
    return ser(await new AsyncFunction('vscode', 'require', p.code)(vscode, require));
  },

  'window.focus': () => vscode.commands.executeCommand('workbench.action.focusWindow').then(() => ({ focused: true })),
  'window.reload': deferred('workbench.action.reloadWindow'),
  'window.close': deferred('workbench.action.closeWindow'),

  // editors, tabs, documents
  'editor.state': () => (vscode.window.activeTextEditor ? editorInfo(vscode.window.activeTextEditor) : null),
  'editor.visible': () => vscode.window.visibleTextEditors.map(editorInfo),
  'editor.open': async (p) => {
    const doc = await getDoc(p);
    const opts = { preview: !!p.preview, preserveFocus: !!p.preserveFocus };
    if (p.viewColumn) opts.viewColumn = p.viewColumn;
    if (p.line) opts.selection = R([p.line, p.column || 1, p.endLine || p.line, p.endColumn || p.column || 1]);
    return editorInfo(await vscode.window.showTextDocument(doc, opts));
  },
  'editor.new': async (p) => {
    const doc = await vscode.workspace.openTextDocument({ content: p.content || '', language: p.language });
    return editorInfo(await vscode.window.showTextDocument(doc, { preview: false }));
  },
  'editor.select': (p) => {
    need(p, 'ranges');
    const ed = activeEditor();
    ed.selections = p.ranges.map((a) => { const r = R(a); return new vscode.Selection(r.start, r.end); });
    ed.revealRange(ed.selection, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
    return editorInfo(ed);
  },
  'editor.setLanguage': async (p) => {
    need(p, 'languageId');
    return docInfo(await vscode.languages.setTextDocumentLanguage(await getDoc(p), p.languageId));
  },
  'tabs.list': () => vscode.window.tabGroups.all.map((g) => ({ viewColumn: g.viewColumn, isActive: g.isActive, tabs: g.tabs.map(tabInfo) })),
  'tabs.close': async (p) => {
    const all = vscode.window.tabGroups.all.flatMap((g) => g.tabs);
    let tabs;
    if (p.all) tabs = all;
    else if (p.path) { const want = toUri(p.path).toString(); tabs = all.filter((t) => tabUri(t) && tabUri(t).toString() === want); }
    else if (p.label) tabs = all.filter((t) => t.label === p.label);
    else tabs = all.filter((t) => t.isActive && t.group.isActive);
    if (!tabs.length) fail('no_tab', 'No matching tab');
    const closed = tabs.map(tabInfo);
    const ok = await vscode.window.tabGroups.close(tabs, true);
    return { closed: ok ? closed : [], ok };
  },
  'doc.list': () => vscode.workspace.textDocuments.filter((d) => d.uri.scheme === 'file' || d.uri.scheme === 'untitled').map(docInfo),
  'doc.read': async (p) => {
    const doc = await getDoc(p);
    let range;
    if (p.startLine || p.endLine) {
      const s = Math.max(1, p.startLine || 1);
      const e = Math.min(doc.lineCount, p.endLine || doc.lineCount);
      range = new vscode.Range(new vscode.Position(s - 1, 0), doc.lineAt(Math.max(s, e) - 1).range.end);
    }
    return { ...docInfo(doc), text: doc.getText(range) };
  },
  'doc.edit': async (p) => {
    need(p, 'edits');
    const doc = await getDoc(p);
    return applyTextEdit(doc, (we) => {
      for (const e of p.edits) {
        const text = e.text === undefined ? '' : String(e.text);
        if (e.range) we.replace(doc.uri, R(e.range), text);
        else if (e.insert) we.insert(doc.uri, P(e.insert[0], e.insert[1]), text);
        else if (e.append) we.insert(doc.uri, doc.lineAt(doc.lineCount - 1).range.end, text);
        else if (e.all) we.replace(doc.uri, fullRange(doc), text);
        else fail('bad_params', 'Each edit needs range, insert, append or all');
      }
    }, p.save);
  },
  'doc.replace': async (p) => {
    need(p, 'find');
    const doc = await getDoc(p);
    const text = doc.getText();
    let re;
    try {
      const src = p.regex ? p.find : String(p.find).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      re = new RegExp(src, 'g' + (p.ignoreCase ? 'i' : '') + (p.regex ? 'm' : ''));
    } catch (e) { fail('bad_regex', e.message); }
    const single = new RegExp(re.source, re.flags.replace('g', ''));
    const repl = p.replace === undefined ? '' : String(p.replace);
    const we = new vscode.WorkspaceEdit();
    let n = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m[0] === '') { re.lastIndex++; continue; }
      const out = p.regex ? m[0].replace(single, repl) : repl;
      we.replace(doc.uri, new vscode.Range(doc.positionAt(m.index), doc.positionAt(m.index + m[0].length)), out);
      n++;
      if (!p.all) break;
    }
    if (n && !(await vscode.workspace.applyEdit(we))) fail('edit_failed', 'VS Code rejected the edit');
    if (n && p.save) await doc.save();
    return { replacements: n, ...docInfo(doc) };
  },
  'doc.save': async (p) => {
    if (p.all) return { saved: await vscode.workspace.saveAll(!!p.includeUntitled) };
    const doc = await getDoc(p);
    const saved = await doc.save();
    return { saved, ...docInfo(doc) };
  },
  'files.apply': async (p) => {
    need(p, 'ops');
    const we = new vscode.WorkspaceEdit();
    for (const op of p.ops) {
      if (op.create) {
        const o = { overwrite: !!op.overwrite, ignoreIfExists: !!op.ignoreIfExists };
        if (op.content !== undefined) o.contents = Buffer.from(String(op.content), 'utf8');
        we.createFile(toUri(op.create), o);
      } else if (op.rename) {
        need(op, 'to');
        we.renameFile(toUri(op.rename), toUri(op.to), { overwrite: !!op.overwrite });
      } else if (op.delete) {
        we.deleteFile(toUri(op.delete), { recursive: !!op.recursive, ignoreIfNotExists: !!op.ignoreIfNotExists });
      } else fail('bad_params', 'Each op needs create, rename or delete');
    }
    if (!(await vscode.workspace.applyEdit(we))) fail('edit_failed', 'VS Code rejected the file operation');
    return { applied: p.ops.length };
  },

  // workspace
  'workspace.folders': folders,
  'workspace.addFolder': (p) => {
    need(p, 'path');
    const n = (vscode.workspace.workspaceFolders || []).length;
    const ok = vscode.workspace.updateWorkspaceFolders(n, 0, { uri: toUri(p.path), name: p.name });
    if (!ok) fail('workspace_update_failed', 'VS Code rejected the folder change');
    return { added: p.path };
  },
  'workspace.removeFolder': (p) => {
    need(p, 'path');
    const want = toUri(p.path).toString().toLowerCase();
    const f = (vscode.workspace.workspaceFolders || []).find((x) => x.uri.toString().toLowerCase() === want);
    if (!f) fail('no_folder', `Not a workspace folder: ${p.path}`);
    if (!vscode.workspace.updateWorkspaceFolders(f.index, 1)) fail('workspace_update_failed', 'VS Code rejected the folder change');
    return { removed: p.path };
  },
  'workspace.findFiles': async (p) => {
    need(p, 'include');
    const res = await vscode.workspace.findFiles(p.include, p.exclude === undefined ? undefined : p.exclude, p.maxResults || 1000);
    return res.map((u) => ser(u)).sort();
  },
  'config.get': configGet,
  'config.update': async (p) => {
    need(p, 'key');
    const target = TARGETS[p.target || 'user'];
    if (target === undefined) fail('bad_params', 'target must be user, workspace or folder');
    const c = vscode.workspace.getConfiguration(undefined, p.resource ? toUri(p.resource) : undefined);
    await c.update(p.key, p.remove ? undefined : p.value, target);
    return configGet(p);
  },

  // language features
  'lang.languages': () => vscode.languages.getLanguages().then((l) => l.sort()),
  'lang.diagnostics': async (p) => {
    let out = [];
    if (p.path) { const uri = toUri(p.path); out = vscode.languages.getDiagnostics(uri).map((d) => diagInfo(uri, d)); }
    else for (const [uri, list] of vscode.languages.getDiagnostics()) for (const d of list) out.push(diagInfo(uri, d));
    if (p.severity) out = out.filter((d) => d.severity === p.severity);
    return out;
  },
  'lang.definition': locations('vscode.executeDefinitionProvider'),
  'lang.typeDefinition': locations('vscode.executeTypeDefinitionProvider'),
  'lang.implementation': locations('vscode.executeImplementationProvider'),
  'lang.declaration': locations('vscode.executeDeclarationProvider'),
  'lang.references': locations('vscode.executeReferenceProvider'),
  'lang.hover': async (p) => {
    const [uri, pos] = await at(p);
    const hovers = (await vscode.commands.executeCommand('vscode.executeHoverProvider', uri, pos)) || [];
    return hovers.map((h) => ({
      range: h.range ? ser(h.range) : null,
      contents: h.contents.map((c) => (typeof c === 'string' ? c : c.value)),
    }));
  },
  'lang.documentSymbols': async (p) => {
    const doc = await getDoc(p);
    return ((await vscode.commands.executeCommand('vscode.executeDocumentSymbolProvider', doc.uri)) || []).map(symbolTree);
  },
  'lang.workspaceSymbols': async (p) => ((await vscode.commands.executeCommand('vscode.executeWorkspaceSymbolProvider', p.query || '')) || []).map(symbolTree),
  'lang.rename': async (p) => {
    need(p, 'newName');
    const [uri, pos] = await at(p);
    const we = await vscode.commands.executeCommand('vscode.executeDocumentRenameProvider', uri, pos, p.newName);
    if (!we) fail('no_rename', 'No rename provider answered for this position');
    const files = editSummary(we);
    const applied = p.apply === false ? false : await vscode.workspace.applyEdit(we);
    if (applied && p.save) await vscode.workspace.saveAll(false);
    return { applied, files };
  },
  'lang.format': async (p) => {
    const doc = await getDoc(p);
    const ed = vscode.window.visibleTextEditors.find((e) => e.document === doc);
    const options = { tabSize: ed ? ed.options.tabSize : 4, insertSpaces: ed ? ed.options.insertSpaces : true };
    const edits = p.range
      ? await vscode.commands.executeCommand('vscode.executeFormatRangeProvider', doc.uri, R(p.range), options)
      : await vscode.commands.executeCommand('vscode.executeFormatDocumentProvider', doc.uri, options);
    if (!edits) fail('no_formatter', `No formatter answered for ${doc.languageId}`);
    const out = await applyTextEdit(doc, (we) => we.set(doc.uri, edits), p.save);
    return { edits: edits.length, ...out };
  },
  'lang.codeActions': async (p) => {
    const doc = await getDoc(p);
    const range = p.range ? R(p.range) : (p.line ? R([p.line, p.column || 1]) : fullRange(doc));
    const actions = (await vscode.commands.executeCommand('vscode.executeCodeActionProvider', doc.uri, range, p.kind)) || [];
    const list = actions.map((a, i) => ({
      index: i, title: a.title, kind: a.kind ? a.kind.value : null, isPreferred: !!a.isPreferred,
      hasEdit: !!a.edit, command: a.command ? (a.command.command || a.command) : null,
    }));
    if (p.apply === undefined || p.apply === null) return list;
    const a = typeof p.apply === 'number' ? actions[p.apply] : actions.find((x) => x.title === p.apply);
    if (!a) fail('no_action', 'No such code action');
    if (a.edit && !(await vscode.workspace.applyEdit(a.edit))) fail('edit_failed', 'VS Code rejected the edit');
    const cmd = a.command && typeof a.command === 'object' ? a.command : (typeof a.command === 'string' ? a : null);
    if (cmd) await vscode.commands.executeCommand(cmd.command, ...(cmd.arguments || []));
    return { applied: a.title };
  },
  'lang.completions': async (p) => {
    const [uri, pos] = await at(p);
    const res = await vscode.commands.executeCommand('vscode.executeCompletionItemProvider', uri, pos, p.trigger);
    const items = (res && res.items) || [];
    return items.slice(0, p.limit || 50).map((i) => ({
      label: typeof i.label === 'string' ? i.label : i.label.label,
      kind: enumName(vscode.CompletionItemKind, i.kind), detail: i.detail || null,
      insertText: i.insertText === undefined ? null : (typeof i.insertText === 'string' ? i.insertText : i.insertText.value),
    }));
  },

  // terminals
  'terminal.list': () => Promise.all(vscode.window.terminals.map(termInfo)),
  'terminal.create': async (p) => {
    const t = vscode.window.createTerminal({ name: p.name, cwd: p.cwd, shellPath: p.shellPath, shellArgs: p.shellArgs, env: p.env });
    if (p.show !== false) t.show(true);
    return termInfo(t);
  },
  'terminal.send': async (p) => {
    need(p, 'text');
    const t = findTerminal(p);
    if (p.show) t.show(true);
    t.sendText(String(p.text), p.enter !== false);
    return termInfo(t);
  },
  'terminal.show': async (p) => { const t = findTerminal(p); t.show(!!p.preserveFocus); return termInfo(t); },
  'terminal.kill': async (p) => { const t = findTerminal(p); const i = await termInfo(t); t.dispose(); return { killed: i }; },
  'terminal.exec': async (p) => {
    need(p, 'command');
    const reuse = !p.new && (p.index !== undefined || p.name || vscode.window.terminals.length > 0);
    const t = reuse ? findTerminal(p) : vscode.window.createTerminal({ name: p.name || 'codereins', cwd: p.cwd });
    if (p.show !== false) t.show(true);
    const si = await waitShellIntegration(t, p.integrationTimeoutMs || 20000);
    if (!si) fail('no_shell_integration', 'Shell integration is not active in this terminal, so output cannot be captured; use `terminal send`.');
    let exec = null;
    let output = '';
    let sub;
    const ended = new Promise((resolve) => {
      sub = vscode.window.onDidEndTerminalShellExecution((e) => { if (e.execution === exec) resolve(e.exitCode === undefined ? null : e.exitCode); });
    });
    exec = si.executeCommand(String(p.command));
    const reading = (async () => { for await (const data of exec.read()) output += data; })().catch(() => {});
    let timedOut = false;
    let timer;
    const exitCode = await Promise.race([
      ended.then(async (code) => { await Promise.race([reading, sleep(2000)]); return code; }),
      new Promise((resolve) => { timer = setTimeout(() => { timedOut = true; resolve(null); }, p.timeoutMs || 60000); }),
    ]);
    clearTimeout(timer);
    sub.dispose();
    return { command: p.command, exitCode, timedOut, output: p.raw ? output : clean(output), terminal: await termInfo(t) };
  },

  // tasks
  'tasks.list': async (p) => (await vscode.tasks.fetchTasks(p.type ? { type: p.type } : undefined)).map(taskInfo),
  'tasks.running': () => vscode.tasks.taskExecutions.map((e) => taskInfo(e.task)),
  'tasks.run': async (p) => {
    const task = await findTask(p);
    let exec = null;
    let sub;
    const ended = new Promise((resolve) => {
      sub = vscode.tasks.onDidEndTaskProcess((e) => { if (e.execution === exec || (exec && e.execution.task.name === task.name)) resolve(e.exitCode === undefined ? null : e.exitCode); });
    });
    exec = await vscode.tasks.executeTask(task);
    if (!p.wait) { sub.dispose(); return { started: taskInfo(task) }; }
    let timedOut = false;
    let timer;
    const exitCode = await Promise.race([
      ended,
      new Promise((resolve) => { timer = setTimeout(() => { timedOut = true; resolve(null); }, p.timeoutMs || 120000); }),
    ]);
    clearTimeout(timer);
    sub.dispose();
    return { task: taskInfo(task), exitCode, timedOut };
  },
  'tasks.terminate': (p) => {
    need(p, 'name');
    const hit = vscode.tasks.taskExecutions.filter((e) => e.task.name === p.name);
    if (!hit.length) fail('no_task', `No running task named ${p.name}`);
    hit.forEach((e) => e.terminate());
    return { terminated: hit.length };
  },

  // debugging
  'debug.state': () => {
    const s = vscode.debug.activeDebugSession;
    return { active: s ? { id: s.id, name: s.name, type: s.type } : null, breakpoints: vscode.debug.breakpoints.length };
  },
  'debug.start': async (p) => {
    if (!p.name && !p.config) fail('bad_params', 'Pass a launch configuration name or a config object');
    const wf = vscode.workspace.workspaceFolders || [];
    const folder = p.folder ? vscode.workspace.getWorkspaceFolder(toUri(p.folder)) : wf[0];
    const started = await vscode.debug.startDebugging(folder, p.config || p.name, { noDebug: !!p.noDebug });
    return { started };
  },
  'debug.stop': async () => { await vscode.debug.stopDebugging(); return { stopped: true }; },
  'debug.request': async (p) => {
    need(p, 'command');
    const s = vscode.debug.activeDebugSession;
    if (!s) fail('no_debug_session', 'No active debug session');
    return ser(await s.customRequest(p.command, p.args));
  },
  'debug.breakpoints.list': () => vscode.debug.breakpoints.map(bpInfo),
  'debug.breakpoints.add': async (p) => {
    need(p, 'path', 'line');
    const bp = new vscode.SourceBreakpoint(new vscode.Location(toUri(p.path), P(p.line, 1)), true, p.condition, p.hitCondition, p.logMessage);
    vscode.debug.addBreakpoints([bp]);
    await settleBreakpoints(() => vscode.debug.breakpoints.some((b) => b.id === bp.id));
    return bpInfo(bp);
  },
  'debug.breakpoints.remove': async (p) => {
    let list = vscode.debug.breakpoints;
    if (!p.all) {
      need(p, 'path');
      const want = toUri(p.path).toString().toLowerCase();
      list = list.filter((b) => b.location && b.location.uri.toString().toLowerCase() === want
        && (!p.line || b.location.range.start.line + 1 === p.line));
    }
    const ids = new Set(list.map((b) => b.id));
    vscode.debug.removeBreakpoints(list);
    await settleBreakpoints(() => !vscode.debug.breakpoints.some((b) => ids.has(b.id)));
    return { removed: list.length };
  },

  // extensions, git
  'ext.list': (p) => vscode.extensions.all.filter((e) => p.builtin || !e.packageJSON.isBuiltin).map(extInfo),
  'ext.info': (p) => {
    need(p, 'id');
    const e = vscode.extensions.getExtension(p.id);
    if (!e) fail('no_extension', `Not installed or not enabled in this window: ${p.id}`);
    const pj = e.packageJSON;
    return {
      ...extInfo(e), description: pj.description || null, activationEvents: pj.activationEvents || [],
      commands: ((pj.contributes && pj.contributes.commands) || []).map((c) => ({ command: c.command, title: c.title })),
    };
  },
  'ext.activate': async (p) => {
    need(p, 'id');
    const e = vscode.extensions.getExtension(p.id);
    if (!e) fail('no_extension', `Not installed or not enabled in this window: ${p.id}`);
    await e.activate();
    return extInfo(e);
  },
  'git.status': async () => {
    const e = vscode.extensions.getExtension('vscode.git');
    if (!e) fail('no_git', 'The built-in Git extension is disabled');
    const api = (e.isActive ? e.exports : await e.activate()).getAPI(1);
    return api.repositories.map((r) => {
      const h = r.state.HEAD || {};
      return {
        root: r.rootUri.fsPath, branch: h.name || null, commit: h.commit || null,
        upstream: h.upstream ? `${h.upstream.remote}/${h.upstream.name}` : null,
        ahead: h.ahead === undefined ? null : h.ahead, behind: h.behind === undefined ? null : h.behind,
        staged: r.state.indexChanges.map(change), unstaged: r.state.workingTreeChanges.map(change),
        merge: r.state.mergeChanges.map(change),
      };
    });
  },

  // UI
  'ui.message': async (p) => {
    need(p, 'message');
    const show = { info: vscode.window.showInformationMessage, warning: vscode.window.showWarningMessage, error: vscode.window.showErrorMessage }[p.level || 'info'];
    if (!show) fail('bad_params', 'level must be info, warning or error');
    const items = p.items || [];
    if (!p.wait && !items.length) { show(String(p.message)); return { shown: true }; }
    const picked = await show(String(p.message), { modal: !!p.modal }, ...items);
    return { picked: picked === undefined ? null : picked };
  },
  'ui.statusBar': (p) => { need(p, 'text'); vscode.window.setStatusBarMessage(String(p.text), p.timeoutMs || 5000); return { shown: true }; },
  'ui.quickPick': async (p) => {
    need(p, 'items');
    const picked = await vscode.window.showQuickPick(p.items, { title: p.title, placeHolder: p.placeHolder, canPickMany: !!p.many, ignoreFocusOut: true });
    return { picked: picked === undefined ? null : picked };
  },
  'ui.inputBox': async (p) => {
    const value = await vscode.window.showInputBox({ title: p.title, prompt: p.prompt, value: p.value, placeHolder: p.placeHolder, password: !!p.password, ignoreFocusOut: true });
    return { value: value === undefined ? null : value };
  },
  'ui.openExternal': async (p) => { need(p, 'url'); return { opened: await vscode.env.openExternal(vscode.Uri.parse(p.url, true)) }; },
  'clipboard.read': async () => ({ text: await vscode.env.clipboard.readText() }),
  'clipboard.write': async (p) => { need(p, 'text'); await vscode.env.clipboard.writeText(String(p.text)); return { written: String(p.text).length }; },
};

// ── transport ────────────────────────────────────────────────────────────
function authorized(token) {
  if (typeof token !== 'string') return false;
  const a = Buffer.from(token);
  const b = Buffer.from(state.token);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function send(sock, obj) {
  try { sock.write(JSON.stringify(obj) + '\n'); } catch (_) { /* peer gone */ }
}

async function handleLine(sock, line) {
  let id = null;
  let out;
  try {
    const req = JSON.parse(line);
    id = req.id === undefined ? null : req.id;
    if (!authorized(req.token)) {
      send(sock, { id, ok: false, error: { code: 'unauthorized', message: 'Bad or missing token' } });
      sock.end();
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(METHODS, req.method)) fail('unknown_method', `Unknown method: ${req.method}`);
    const result = await METHODS[req.method](req.params || {});
    out = { id, ok: true, result: result === undefined ? null : result };
  } catch (e) {
    out = { id, ok: false, error: { code: e && typeof e.code === 'string' ? e.code : 'error', message: String((e && e.message) || e) } };
  }
  send(sock, out);
}

function onConnection(sock) {
  let buf = '';
  let chain = Promise.resolve();
  sock.setEncoding('utf8');
  sock.on('error', () => {});
  sock.on('data', (chunk) => {
    buf += chunk;
    if (buf.length > MAX_LINE) { sock.destroy(); return; }
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (line.trim()) chain = chain.then(() => handleLine(sock, line));
    }
  });
}

function writeRegistry() {
  if (!state || !registryFile) return;
  const data = {
    id: state.id, pid: process.pid, endpoint: state.endpoint, token: state.token,
    bridgeVersion: VERSION, vscodeVersion: vscode.version, appName: vscode.env.appName,
    name: vscode.workspace.name || null,
    workspaceFile: vscode.workspace.workspaceFile ? ser(vscode.workspace.workspaceFile) : null,
    folders: folders().map((f) => f.path),
    focused: vscode.window.state.focused, lastFocusedAt: state.lastFocusedAt, startedAt: state.startedAt,
  };
  const tmp = `${registryFile}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(tmp, JSON.stringify(data), { mode: 0o600 });
    fs.renameSync(tmp, registryFile);
  } catch (_) {
    try { fs.writeFileSync(registryFile, JSON.stringify(data), { mode: 0o600 }); } catch (e) { console.error('[codereins-bridge]', e); }
    try { fs.unlinkSync(tmp); } catch (__) { /* nothing to clean */ }
  }
}

function start(context) {
  const home = homeDir();
  const winDir = path.join(home, 'windows');
  fs.mkdirSync(winDir, { recursive: true, mode: 0o700 });
  const id = String(process.pid);
  let endpoint;
  if (IS_WIN) {
    endpoint = `\\\\.\\pipe\\codereins-${id}-${crypto.randomBytes(8).toString('hex')}`;
  } else {
    const sockDir = path.join(home, 'sock');
    fs.mkdirSync(sockDir, { recursive: true, mode: 0o700 });
    endpoint = path.join(sockDir, `${id}.sock`);
    try { fs.unlinkSync(endpoint); } catch (_) { /* no stale socket */ }
    socketFile = endpoint;
  }
  state = {
    id, endpoint, token: crypto.randomBytes(32).toString('hex'),
    startedAt: Date.now(), lastFocusedAt: vscode.window.state.focused ? Date.now() : 0,
  };
  registryFile = path.join(winDir, `${id}.json`);
  server = net.createServer(onConnection);
  server.on('error', (e) => console.error('[codereins-bridge]', e));
  server.listen(endpoint, () => {
    if (!IS_WIN) { try { fs.chmodSync(endpoint, 0o600); } catch (_) { /* best effort */ } }
    writeRegistry();
  });
  context.subscriptions.push(
    vscode.window.onDidChangeWindowState((s) => { if (s.focused) state.lastFocusedAt = Date.now(); writeRegistry(); }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => writeRegistry()),
    { dispose: stop },
  );
}

function stop() {
  if (server) { try { server.close(); } catch (_) { /* already closed */ } server = null; }
  for (const f of [registryFile, socketFile]) {
    if (f) { try { fs.unlinkSync(f); } catch (_) { /* already gone */ } }
  }
  registryFile = null;
  socketFile = null;
  state = null;
}

function showStatus() {
  if (!state) { vscode.window.showInformationMessage('CodeReins Bridge is off (codereinsBridge.enabled is false).'); return; }
  vscode.window.showInformationMessage(`CodeReins Bridge ${VERSION}: window ${state.id} is listening on ${state.endpoint}`);
}

function activate(context) {
  context.subscriptions.push(vscode.commands.registerCommand('codereinsBridge.showStatus', showStatus));
  if (!vscode.workspace.getConfiguration('codereinsBridge').get('enabled', true)) return;
  try { start(context); } catch (e) { vscode.window.showErrorMessage(`CodeReins Bridge failed to start: ${e.message}`); }
}

function deactivate() { stop(); }

module.exports = { activate, deactivate };
