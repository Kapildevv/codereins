"""The parent chain of a process, without third-party packages.

Used to find the editor window that started this CLI: each window's bridge registers its
extension host's pid, and an agent running inside that window (Claude Code, say) is a
descendant of that extension host.

A parent pid can outlive its process and be handed to a new one. A link is dropped when
the "parent" provably started after the child; a link whose start times cannot be read is
kept, because a guard that refuses too often is safer than one that misses its host.
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

Lookup = Callable[[int], Optional[float]]


def chain(pid: int, parent_of: Callable[[int], Optional[int]], created: Lookup) -> list[int]:
    """Ancestors of ``pid``, nearest first. Stops at the root, at a pid seen before, and at
    a parent that started after its child (a reused pid)."""
    out: list[int] = []
    seen = {pid}
    child = pid
    while True:
        p = parent_of(child)
        if not p or p in seen:
            return out
        t_child, t_parent = created(child), created(p)
        if t_child is not None and t_parent is not None and t_parent > t_child:
            return out
        out.append(p)
        seen.add(p)
        child = p


# ── per-platform lookups ─────────────────────────────────────────────────
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    def _parent_lookup() -> Callable[[int], Optional[int]]:
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap in (None, _INVALID_HANDLE):
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            out: dict[int, int] = {}
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                out[entry.th32ProcessID] = entry.th32ParentProcessID
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
            return out.get
        finally:
            _k32.CloseHandle(snap)

    def _created(pid: int) -> Optional[float]:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
                return None
            return float((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            _k32.CloseHandle(h)

elif os.path.isdir("/proc/self"):
    def _stat(pid: int) -> Optional[tuple[int, float]]:
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read()
        except OSError:
            return None
        fields = data[data.rindex(b")") + 2:].split()  # the command name may hold spaces
        return int(fields[1]), float(fields[19])        # ppid (field 4), starttime (field 22)

    def _parent_lookup() -> Callable[[int], Optional[int]]:
        def parent_of(pid: int) -> Optional[int]:
            st = _stat(pid)
            return st[0] if st else None
        return parent_of

    def _created(pid: int) -> Optional[float]:
        st = _stat(pid)
        return st[1] if st else None

else:  # macOS and other Unix: one `ps` call, no start times
    def _parent_lookup() -> Callable[[int], Optional[int]]:
        out: dict[int, int] = {}
        text = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True,
                              timeout=10, check=True).stdout
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                out[int(parts[0])] = int(parts[1])
        return out.get

    def _created(pid: int) -> Optional[float]:
        return None


def ancestors(pid: Optional[int] = None) -> list[int]:
    """Ancestors of ``pid`` (default: this process), nearest first."""
    return chain(os.getpid() if pid is None else pid, _parent_lookup(), _created)


def parent(pid: int) -> Optional[int]:
    """Parent pid of ``pid``, or None when it is gone or unknown."""
    return _parent_lookup()(pid) or None
