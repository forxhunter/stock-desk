"""Windows 11 window effects through DWM / user32 (ctypes only; no-ops elsewhere)."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_IS_WIN = sys.platform == "win32"

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WinCompAttrData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(_AccentPolicy)),
                ("SizeOfData", ctypes.c_size_t)]


def _dwm_set(hwnd: int, attr: int, value: int) -> bool:
    if not _IS_WIN:
        return False
    try:
        v = ctypes.c_int(value)
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(attr), ctypes.byref(v), ctypes.sizeof(v)) == 0
    except Exception:
        return False


def dark_titlebar(hwnd: int) -> bool:
    return _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)


def round_corners(hwnd: int) -> bool:
    return _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)


def acrylic(hwnd: int, r: int, g: int, b: int, alpha: int, enable: bool = True) -> bool:
    """Blur what is behind the window and tint it (undocumented but stable since Win10 1803)."""
    if not _IS_WIN:
        return False
    try:
        policy = _AccentPolicy()
        policy.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND if enable else ACCENT_DISABLED
        policy.AccentFlags = 2
        policy.GradientColor = (alpha << 24) | (b << 16) | (g << 8) | r      # ABGR
        data = _WinCompAttrData(WCA_ACCENT_POLICY, ctypes.pointer(policy), ctypes.sizeof(policy))
        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(_WinCompAttrData)]
        fn.restype = ctypes.c_int
        return bool(fn(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def set_topmost(hwnd: int, on: bool) -> bool:
    """Put the window above all others (or back into the normal stack) without activating it."""
    if not _IS_WIN:
        return False
    try:
        fn = ctypes.windll.user32.SetWindowPos
        fn.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_uint]
        fn.restype = ctypes.c_int
        # HWND_TOPMOST = -1, HWND_NOTOPMOST = -2; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE = 0x13
        return bool(fn(wintypes.HWND(hwnd), wintypes.HWND(-1 if on else -2), 0, 0, 0, 0, 0x0013))
    except Exception:
        return False
