"""Best-effort one-shot foreground activation for user-triggered windows."""
from __future__ import annotations

import sys
import time
from functools import lru_cache
from typing import Iterable, NamedTuple


EXPLORER_WINDOW_CLASSES = ("CabinetWClass", "ExploreWClass")
CHROMIUM_WINDOW_CLASSES = ("Chrome_WidgetWin_1",)


class WindowSnapshot(NamedTuple):
    foreground: int
    handles: frozenset[int]


@lru_cache(maxsize=1)
def _win32_api():
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                         wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return ctypes, user32, kernel32, enum_proc


def _handle_value(hwnd) -> int:
    if hwnd is None:
        return 0
    return hwnd if isinstance(hwnd, int) else int(hwnd.value or 0)


def _visible_windows(class_names: Iterable[str]) -> list[tuple[int, str]]:
    api = _win32_api()
    if not api:
        return []
    ctypes, user32, _, enum_proc = api
    allowed = set(class_names)
    windows: list[tuple[int, str]] = []

    @enum_proc
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value not in allowed:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        windows.append((_handle_value(hwnd), title.value))
        return True

    user32.EnumWindows(visit, 0)
    return windows


def _foreground_window() -> int:
    api = _win32_api()
    if not api:
        return 0
    return _handle_value(api[1].GetForegroundWindow())


def capture_window_snapshot(class_names: Iterable[str]) -> WindowSnapshot:
    try:
        handles = frozenset(hwnd for hwnd, _ in _visible_windows(class_names))
        return WindowSnapshot(_foreground_window(), handles)
    except Exception:
        return WindowSnapshot(0, frozenset())


def _activate_window(hwnd: int, foreground: int) -> bool:
    api = _win32_api()
    if not api:
        return False
    _, user32, kernel32, _ = api
    current_thread = kernel32.GetCurrentThreadId()
    thread_ids = {
        user32.GetWindowThreadProcessId(handle, None)
        for handle in (foreground, hwnd) if handle
    }
    attached = []
    try:
        for thread_id in thread_ids:
            if thread_id and thread_id != current_thread and \
                    user32.AttachThreadInput(current_thread, thread_id, True):
                attached.append(thread_id)
        user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
        flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        positioned = bool(user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags))  # HWND_TOP
        focused = bool(user32.SetForegroundWindow(hwnd))
        return positioned or focused
    finally:
        for thread_id in reversed(attached):
            user32.AttachThreadInput(current_thread, thread_id, False)


def bring_window_to_front(snapshot: WindowSnapshot, class_names: Iterable[str],
                          title_hint: str = "", timeout: float = 2.0) -> bool:
    """Raise a new or matching window once, unless the user has switched away."""
    if sys.platform != "win32":
        return False
    wanted = title_hint.strip().casefold()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            windows = _visible_windows(class_names)
            titles = dict(windows)
            handles = set(titles)
            current = _foreground_window()
            title_matches = [
                hwnd for hwnd, title in windows
                if wanted and wanted in title.casefold()
            ]
            target = 0
            if current in handles and (current not in snapshot.handles or
                                       current in title_matches):
                target = current
            if not target:
                target = next((hwnd for hwnd, _ in windows
                               if hwnd not in snapshot.handles), 0)
            if not target and title_matches:
                target = title_matches[0]
            if target:
                latest = _foreground_window()
                if latest not in (0, snapshot.foreground, target):
                    return False
                return _activate_window(target, latest or snapshot.foreground)
            if current not in (0, snapshot.foreground) and current not in handles:
                return False
            time.sleep(0.08)
    except Exception:
        pass
    return False
