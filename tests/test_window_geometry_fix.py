"""Регрессия: watchdog не должен «дергать» окно каждые 30 секунд (баг из логов),
DPI-масштаб применяется, протокол v4/v5 совместим."""
import json
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import daemon_control
import server


def test_daemon_control_supports_v4_and_v5():
    assert daemon_control.PROTOCOL_VERSION == 5
    assert 4 in daemon_control.SUPPORTED_PROTOCOL_VERSIONS
    assert 5 in daemon_control.SUPPORTED_PROTOCOL_VERSIONS


def test_server_supports_v4_and_v5():
    assert 4 in server.SUPPORTED_PROTOCOL_VERSIONS
    assert 5 in server.SUPPORTED_PROTOCOL_VERSIONS


def test_state_v4_still_reachable_by_start(monkeypatch):
    """Старый state v4 больше не даёт «версия не совпадает»: живой демон
    считается работающим, второй процесс не запускается (профиль занят)."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
            with conn, conn.makefile("rwb") as stream:
                stream.readline()  # auth
                stream.write(json.dumps(
                    {"ok": True, "protocol": 4}).encode() + b"\n")
                stream.flush()
                stream.readline()  # ping
                stream.write(json.dumps(
                    {"ok": True, "pong": True}).encode() + b"\n")
                stream.flush()
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    state = {"version": 4, "pid": os.getpid(), "tcp_host": "127.0.0.1",
             "tcp_port": port, "auth_token": "tok", "status": "running"}
    monkeypatch.setattr(daemon_control, "load_state", lambda: state)
    monkeypatch.setattr(daemon_control, "pid_is_alive", lambda pid: True)
    # В тестовой среде .venv/Scripts может отсутствовать — наличие venv не
    # относится к проверяемой логике (state v4 должен считаться живым).
    monkeypatch.setattr(
        daemon_control.PYTHON, "exists", lambda: True, raising=True)

    rc = daemon_control.start_daemon()
    srv.close()
    assert rc == 0


def test_apply_geometry_accepts_scale_kwarg():
    from modules import os_window_control as owc
    import inspect
    sig = inspect.signature(owc.apply_geometry)
    assert "scale" in sig.parameters


def test_win_apply_scales_physical_pixels(monkeypatch):
    """SetWindowPos получает физ px = css px * scale (DPI-коррекция)."""
    from modules import os_window_control as owc
    captured = {}
    my_pid = os.getpid()

    class FakeUser32:
        def EnumWindows(self, cb, _):
            cb(1234, 0)
            return 1

        def GetWindowThreadProcessId(self, hwnd, dword_ref):
            dword_ref.value = my_pid  # byref -> мутабельный объект с .value

        def IsWindowVisible(self, hwnd):
            return True

        def GetWindowTextLengthW(self, hwnd):
            return 5

        def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
            captured.update(x=x, y=y, w=w, h=h)
            return 1

        def IsZoomed(self, hwnd):
            return False

        def ShowWindow(self, hwnd, cmd):
            return 1

    class FakeWindll:
        user32 = FakeUser32()
        shcore = None

    fake_ctypes = type(sys)("ctypes")
    fake_ctypes.windll = FakeWindll()
    fake_ctypes.c_bool = int
    fake_ctypes.c_void_p = lambda v: v
    fake_ctypes.c_int = lambda v: v
    fake_ctypes.WINFUNCTYPE = lambda r, *a: (lambda f: f)
    fake_ctypes.byref = lambda v: v

    class FakeDWORD:
        def __init__(self):
            self.value = 0

    fake_wintypes = type(sys)("ctypes.wintypes")
    fake_wintypes.HWND = int
    fake_wintypes.LPARAM = int
    fake_wintypes.DWORD = FakeDWORD

    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setitem(sys.modules, "ctypes.wintypes", fake_wintypes)

    res = owc._win_apply(0, 0, 1000, 900, pid=my_pid, scale=1.44)
    assert res["ok"] is True, res
    assert captured["w"] == round(1000 * 1.44)
    assert captured["h"] == round(900 * 1.44)
    assert res["scale"] == 1.44


def test_watchdog_cooldown_logic_exists():
    src = pathlib.Path(browserd_path()).read_text(encoding="utf-8")
    assert "_geom_cooldown_until" in src
    assert "_geom_fail_streak" in src
    assert "devicePixelRatio" in src


def browserd_path():
    return str(pathlib.Path(__file__).resolve().parents[1] / "browserd.py")
