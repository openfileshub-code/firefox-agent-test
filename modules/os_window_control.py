"""Принудительное управление геометрией окна ОС (ТЗ 7.4).

Три платформы:
- Windows: ctypes.windll.user32 — EnumWindows/GetWindowThreadProcessId/
  GetWindowRect/SetWindowPos (SWP_NOZORDER | SWP_NOACTIVATE);
- Linux: wmctrl или xdotool (subprocess, без жёстких зависимостей);
- macOS: osascript (System Events).

Публичный API:
    apply_geometry(x, y, width, height, pid=None) -> dict
    get_geometry(pid=None) -> dict | None

Модуль не падает при отсутствии платформы/инструментов: возвращает
``{"ok": False, "error": "WindowGeometryError", ...}`` и пишет warning —
согласно ТЗ 7.7 демон обязан логировать, но не ронять браузер.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Optional

log = logging.getLogger("FirefoxMCP_OSWindow")

#: Классы/заголовки окон браузера для поиска на Linux/macOS.
_BROWSER_HINTS = (
    "Mozilla Firefox",
    "Camoufox",
    "camoufox",
    "firefox",
)


def _err(message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "error": "WindowGeometryError",
        "message": message,
        "hint": "Проверьте config/window.json и наличие инструментов WM.",
    }
    if details:
        result["details"] = details
    return result


# ----------------------------------------------------------------------
# Windows (ctypes user32) — ТЗ 7.4 «Windows»
# ----------------------------------------------------------------------

def _win_apply(x: int, y: int, width: int, height: int,
               pid: Optional[int]) -> dict[str, Any]:
    try:
        from ctypes import wintypes
        import ctypes
    except Exception as exc:  # pragma: no cover
        return _err(f"ctypes недоступен: {exc}")

    user32 = getattr(ctypes.windll, "user32", None)
    if user32 is None:  # pragma: no cover
        return _err("windll.user32 недоступен")

    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    target_pid = int(pid or os.getpid())

    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    IsWindowVisible = user32.IsWindowVisible
    GetWindowTextLength = user32.GetWindowTextLengthW
    SetWindowPos = user32.SetWindowPos

    found: list[int] = []

    def _cb(hwnd, _lparam):
        try:
            proc_id = wintypes.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value != target_pid:
                return True
            if not IsWindowVisible(hwnd):
                return True
            if GetWindowTextLength(hwnd) == 0:
                return True  # служебные невидимые окна браузера
            found.append(int(hwnd))
        except Exception:  # noqa: BLE001
            return True
        return True

    EnumWindows(EnumWindowsProc(_cb), 0)
    if not found:
        # Фолбэк: окно любого firefox/camoufox-процесса по заголовку.
        def _cb_any(hwnd, _lparam):
            try:
                if not IsWindowVisible(hwnd):
                    return True
                length = GetWindowTextLength(hwnd)
                if length == 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value or ""
                if any(h.lower() in title.lower() for h in _BROWSER_HINTS):
                    found.append(int(hwnd))
            except Exception:  # noqa: BLE001
                return True
            return True

        EnumWindows(EnumWindowsProc(_cb_any), 0)

    if not found:
        return _err("Окно браузера не найдено (EnumWindows пуст)",
                    pid=target_pid)

    hwnd = found[0]
    ok = SetWindowPos(hwnd, 0, int(x), int(y), int(width), int(height),
                      SWP_NOZORDER | SWP_NOACTIVATE)
    if not ok:  # pragma: no cover
        return _err("SetWindowPos вернул ошибку", hwnd=hwnd)
    log.info("Windows: применена геометрия %sx%s @(%s,%s) hwnd=%s",
             width, height, x, y, hwnd)
    return {"ok": True, "platform": "windows", "hwnd": hwnd,
            "x": x, "y": y, "width": width, "height": height}


def _win_get_geometry(pid: Optional[int]) -> Optional[dict[str, Any]]:
    try:
        from ctypes import wintypes
        import ctypes
    except Exception:  # noqa: BLE001
        return None
    user32 = ctypes.windll.user32
    target_pid = int(pid or os.getpid())
    found: list[int] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _l):
        proc_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if (proc_id.value == target_pid and user32.IsWindowVisible(hwnd)
                and user32.GetWindowTextLengthW(hwnd) > 0):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    if not found:
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(found[0], ctypes.byref(rect))
    return {"x": rect.left, "y": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top}


# ----------------------------------------------------------------------
# Linux (wmctrl / xdotool) — ТЗ 7.4 «Linux»
# ----------------------------------------------------------------------

def _linux_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def _linux_find_window_id(xdotool: bool) -> Optional[str]:
    patterns = ["--class", "firefox", "--name", ".*"]
    if shutil.which("wmctrl"):
        proc = _linux_run(["wmctrl", "-l"])
        for line in proc.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            title = parts[3].lower()
            if any(h.lower() in title for h in ("firefox", "camoufox")):
                return parts[0]
    if xdotool and shutil.which("xdotool"):
        for hint in _BROWSER_HINTS:
            proc = _linux_run(["xdotool", "search", "--name", hint])
            ids = proc.stdout.split()
            if ids:
                return ids[0]
    _ = patterns
    return None


def _linux_apply(x: int, y: int, width: int, height: int,
                _pid: Optional[int]) -> dict[str, Any]:
    wid = _linux_find_window_id(xdotool=bool(shutil.which("xdotool")))
    if wid is None:
        return _err("Окно браузера не найдено (wmctrl/xdotool)")
    if shutil.which("wmctrl"):
        proc = _linux_run(["wmctrl", "-i", "-r", wid,
                           "-e", f"0,{int(x)},{int(y)},{int(width)},{int(height)}"])
        if proc.returncode == 0:
            log.info("Linux(wmctrl): геометрия %sx%s @(%s,%s) win=%s",
                     width, height, x, y, wid)
            return {"ok": True, "platform": "linux", "tool": "wmctrl",
                    "window_id": wid, "x": x, "y": y,
                    "width": width, "height": height}
    if shutil.which("xdotool"):
        numeric = re.sub(r"[^0-9a-fA-F]", "", wid) or wid
        _linux_run(["xdotool", "windowmove", numeric, str(x), str(y)])
        proc = _linux_run(["xdotool", "windowsize", numeric,
                           str(width), str(height)])
        if proc.returncode == 0:
            log.info("Linux(xdotool): геометрия %sx%s @(%s,%s) win=%s",
                     width, height, x, y, numeric)
            return {"ok": True, "platform": "linux", "tool": "xdotool",
                    "window_id": numeric, "x": x, "y": y,
                    "width": width, "height": height}
    return _err("Ни wmctrl, ни xdotool не выполнили команду")


# ----------------------------------------------------------------------
# macOS (osascript / System Events) — ТЗ 7.4 «macOS»
# ----------------------------------------------------------------------

def _mac_apply(x: int, y: int, width: int, height: int,
               _pid: Optional[int]) -> dict[str, Any]:
    script = (
        'tell application "System Events"\n'
        '  repeat with p in (every process whose name contains "Firefox" '
        'or name contains "Camoufox")\n'
        '    tell window 1 of p\n'
        f'      set position to {{{int(x)}, {int(y)}}}\n'
        f'      set size to {{{int(width)}, {int(height)}}}\n'
        '    end tell\n'
        '    return "ok"\n'
        '  end repeat\n'
        '  return "notfound"\n'
        'end tell'
    )
    try:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return _err(f"osascript недоступен: {exc}")
    if proc.stdout.strip() == "ok":
        log.info("macOS(osascript): геометрия %sx%s @(%s,%s)",
                 width, height, x, y)
        return {"ok": True, "platform": "darwin", "tool": "osascript",
                "x": x, "y": y, "width": width, "height": height}
    return _err("osascript: окно браузера не найдено",
                stderr=proc.stderr.strip()[:200])


# ----------------------------------------------------------------------
# Публичный API
# ----------------------------------------------------------------------

def apply_geometry(x: int, y: int, width: int, height: int,
                   pid: Optional[int] = None) -> dict[str, Any]:
    """Принудительно задать геометрию верхнеуровневого окна браузера.

    Возвращает структурированный результат; никогда не бросает исключение
    наружу (ТЗ 7.7).
    """
    system = platform.system().lower()
    try:
        if system == "windows" or sys.platform == "win32":
            return _win_apply(x, y, width, height, pid)
        if system == "darwin":
            return _mac_apply(x, y, width, height, pid)
        return _linux_apply(x, y, width, height, pid)
    except Exception as exc:  # noqa: BLE001
        log.warning("apply_geometry failed: %s", exc)
        return _err(f"Ошибка применения геометрии: {exc}")


def get_geometry(pid: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Текущая геометрия окна ОС (для диагностики/сравнения с конфигом)."""
    system = platform.system().lower()
    try:
        if system == "windows" or sys.platform == "win32":
            return _win_get_geometry(pid)
        return None  # Linux/macOS: источник истины — page.evaluate в watchdog
    except Exception:  # noqa: BLE001
        return None


def is_available() -> bool:
    """Есть ли на текущей платформе рабочий метод управления окном."""
    system = platform.system().lower()
    if system == "windows":
        return True
    if system == "darwin":
        return shutil.which("osascript") is not None
    return shutil.which("wmctrl") is not None or \
        shutil.which("xdotool") is not None
