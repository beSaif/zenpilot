"""Windows-only process helpers, built on ctypes so zenpilot needs no extra dependencies."""

import ctypes
from ctypes import wintypes
from pathlib import Path

_ERROR_MORE_DATA = 234


class _RmUniqueProcess(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _RmProcessInfo(ctypes.Structure):
    _fields_ = [
        ("Process", _RmUniqueProcess),
        ("strAppName", wintypes.WCHAR * 256),
        ("strServiceShortName", wintypes.WCHAR * 64),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


def file_holders(path: Path) -> list[int]:
    """Pids of the processes that have `path` open, asked of the Windows Restart Manager."""
    rm = ctypes.WinDLL("rstrtmgr")
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key):
        return []
    try:
        files = (wintypes.LPCWSTR * 1)(str(path))
        if rm.RmRegisterResources(session, 1, files, 0, None, 0, None):
            return []
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(0), wintypes.DWORD()
        infos = None
        for _ in range(5):  # the list can grow between calls
            err = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
            if err == 0:
                return [infos[i].Process.dwProcessId for i in range(count.value)] if infos else []
            if err != _ERROR_MORE_DATA:
                return []
            infos = (_RmProcessInfo * needed.value)()
            count.value = needed.value
        return []
    finally:
        rm.RmEndSession(session)
