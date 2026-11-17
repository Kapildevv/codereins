"""Client for the bridge extension: newline-delimited JSON over a named pipe (Windows)
or a Unix socket, one connection per request.

Each VS Code window registers itself in ``<home>/windows/<pid>.json`` with its endpoint
and a token; ``home()`` is ``$CODEREINS_HOME`` or ``~/.codereins``.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

ERROR_PIPE_BUSY = 231
Transport = Callable[[str, bytes, float], bytes]


class BridgeError(Exception):
    """A request to a VS Code window failed. ``code`` is a stable machine-readable id."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def home() -> Path:
    """Folder holding the window registry, the CLI session and file backups."""
    return Path(os.environ.get("CODEREINS_HOME") or Path.home() / ".codereins")


def _pipe_request(endpoint: str, payload: bytes, timeout: float) -> bytes:
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            deadline = time.monotonic() + min(timeout, 5.0)
            while True:
                try:
                    f = open(endpoint, "r+b", buffering=0)
                    break
                except OSError as e:
                    if getattr(e, "winerror", None) == ERROR_PIPE_BUSY and time.monotonic() < deadline:
                        time.sleep(0.05)
                        continue
                    raise
            with f:
                view = memoryview(payload)
                while view:
                    view = view[f.write(view):]
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    buf += chunk
            box["data"] = bytes(buf)
        except BaseException as e:  # noqa: BLE001 - handed to the caller's thread
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise BridgeError("timeout", f"VS Code did not answer within {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box["data"]


def _socket_request(endpoint: str, payload: bytes, timeout: float) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(endpoint)
            s.sendall(payload)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout as e:
            raise BridgeError("timeout", f"VS Code did not answer within {timeout:g}s") from e
    return bytes(buf)


def default_transport(endpoint: str, payload: bytes, timeout: float) -> bytes:
    """Send one request line and return the reply line."""
    try:
        raw = _pipe_request(endpoint, payload, timeout) if os.name == "nt" else _socket_request(endpoint, payload, timeout)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise BridgeError("not_running", "That VS Code window is gone") from e
    except OSError as e:
        raise BridgeError("io_error", f"Could not talk to VS Code: {e}") from e
    return raw.split(b"\n", 1)[0]


class BridgeClient:
    """Calls methods in one VS Code window."""

    def __init__(self, window: dict, *, timeout: float = 30.0, dry_run: bool = False,
                 transport: Optional[Transport] = None):
        self.window = window
        self.timeout = timeout
        self.dry_run = dry_run
        self._transport = transport or default_transport
        self._next_id = 0

    def call(self, method: str, params: Optional[dict] = None, *, mutating: bool = False,
             timeout: Optional[float] = None) -> Any:
        """Call ``method``. With ``dry_run`` a mutating call is described, not sent."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.dry_run and mutating:
            return {"dry_run": True, "method": method, "params": params}
        self._next_id += 1
        req = {"id": self._next_id, "token": self.window["token"], "method": method, "params": params}
        raw = self._transport(self.window["endpoint"], (json.dumps(req) + "\n").encode("utf-8"),
                              timeout or self.timeout)
        if not raw.strip():
            raise BridgeError("no_reply", "VS Code closed the connection without answering")
        try:
            resp = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise BridgeError("bad_reply", f"Unreadable reply from VS Code: {e}") from e
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise BridgeError(err.get("code", "error"), err.get("message", "request failed"))
        return resp.get("result")


def read_registry() -> list[dict]:
    """Every registered window, live or not, newest focus first."""
    folder = home() / "windows"
    out = []
    if folder.is_dir():
        for f in folder.glob("*.json"):
            try:
                entry = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(entry, dict) and entry.get("endpoint") and entry.get("token"):
                entry["_file"] = str(f)
                out.append(entry)
    out.sort(key=lambda w: (bool(w.get("focused")), w.get("lastFocusedAt") or 0, w.get("startedAt") or 0), reverse=True)
    return out


def live_windows(*, prune: bool = True, transport: Optional[Transport] = None) -> list[dict]:
    """Registered windows that answer a ping. Registrations of dead windows are removed."""
    live = []
    for w in read_registry():
        try:
            BridgeClient(w, timeout=3.0, transport=transport).call("ping")
            live.append(w)
        except BridgeError as e:
            if prune and e.code == "not_running":
                try:
                    os.unlink(w["_file"])
                except OSError:
                    pass
    return live
