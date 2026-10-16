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
    # Атрибут метода у PosixPath только для чтения, поэтому подменяем сам
    # модульный путь PYTHON на объект-заглушку.
    class _FakePy:
        def exists(self):
            return True

        def __str__(self):
            return "/usr/bin/python3"

    monkeypatch.setattr(daemon_control, "PYTHON", _FakePy())
    monkeypatch.setattr(daemon_control, "PYTHONW", _FakePy())

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


# ---------- Новый режим менеджера окон: track (наблюдение+запись) ----------

def test_default_config_is_track_mode():
    """В shipped-конфиге принудительное применение геометрии выключено."""
    import json, pathlib
    cfg = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "config" / "window.json")
        .read_text(encoding="utf-8"))
    assert cfg.get("geometry_mode", "track") == "track"
    # В track-режиме enforce не должен быть включён — иначе watchdog будет
    # двигать окно (баг из логов пользователя).
    if cfg.get("geometry_mode") == "track":
        assert not cfg.get("enforce_os_window")


def test_should_enforce_false_in_track_mode(tmp_path):
    from modules.window_manager import WindowManager
    p = tmp_path / "window.json"
    p.write_text(json.dumps({"width": 1000, "height": 900, "x": 0, "y": 0,
                             "enforce_os_window": True}), encoding="utf-8")
    wm = WindowManager(str(p))
    # enforce_os_window=True, но geometry_mode по умолчанию track -> не применяем
    assert wm.should_enforce_os_window() is False
    assert wm.should_track_geometry() is True


def test_should_enforce_true_only_in_apply_mode(tmp_path):
    from modules.window_manager import WindowManager
    p = tmp_path / "window.json"
    p.write_text(json.dumps({"width": 1000, "height": 900, "x": 0, "y": 0,
                             "enforce_os_window": True,
                             "geometry_mode": "apply"}), encoding="utf-8")
    wm = WindowManager(str(p))
    assert wm.should_enforce_os_window() is True
    assert wm.should_track_geometry() is False


def test_update_geometry_persists_to_disk(tmp_path):
    from modules.window_manager import WindowManager
    p = tmp_path / "window.json"
    p.write_text(json.dumps({"width": 1000, "height": 900, "x": 0, "y": 0}),
                 encoding="utf-8")
    wm = WindowManager(str(p))
    geo = {"outerWidth": 1447, "outerHeight": 993, "screenX": 120, "screenY": 40}
    assert wm.update_geometry(geo) is True
    # Перечитали с диска — значения отслеживания сохранены
    wm2 = WindowManager(str(p))
    assert wm2.config["width"] == 1447
    assert wm2.config["height"] == 993
    assert wm2.config["x"] == 120
    assert wm2.config["y"] == 40
    assert wm2.config.get("last_saved")
    # Повторный вызов с теми же значениями ничего не меняет и не пишет
    assert wm2.update_geometry(geo) is False


def test_update_geometry_ignores_invalid(tmp_path):
    from modules.window_manager import WindowManager
    p = tmp_path / "window.json"
    p.write_text(json.dumps({"width": 1000, "height": 900}), encoding="utf-8")
    wm = WindowManager(str(p))
    assert wm.update_geometry({"outerWidth": 0, "outerHeight": 0}) is False
    assert wm.update_geometry({}) is False
    assert wm.config["width"] == 1000


def test_start_block_skips_enforce_in_track_mode(tmp_path):
    """Блок старта демона вызывает apply_geometry только при should_enforce."""
    from modules.window_manager import WindowManager

    # track (дефолт): should_enforce False -> блок старта не применяется
    p1 = tmp_path / "w1.json"
    p1.write_text(json.dumps({"width": 1000, "height": 900,
                              "enforce_os_window": True}), encoding="utf-8")
    wm_track = WindowManager(str(p1))
    start_condition = (wm_track is not None
                        and wm_track.should_enforce_os_window())
    assert start_condition is False

    # apply + enforce: условие старта истинно
    p2 = tmp_path / "w2.json"
    p2.write_text(json.dumps({"width": 1000, "height": 900,
                              "enforce_os_window": True,
                              "geometry_mode": "apply"}), encoding="utf-8")
    wm_apply = WindowManager(str(p2))
    start_condition = (wm_apply is not None
                        and wm_apply.should_enforce_os_window())
    assert start_condition is True


def test_track_mode_watchdog_writes_config_not_moves_window(tmp_path, monkeypatch):
    """Watchdog в track-режиме вызывает update_geometry, а не apply_geometry."""
    import asyncio
    import browserd
    from modules.window_manager import WindowManager

    p = tmp_path / "window.json"
    p.write_text(json.dumps({"width": 1000, "height": 900, "x": 0, "y": 0,
                             "watchdog_interval_seconds": 5}), encoding="utf-8")
    wm = WindowManager(str(p))

    applied = []
    from modules import os_window_control as owc
    monkeypatch.setattr(owc, "apply_geometry",
                        lambda **kw: applied.append(kw) or {"ok": True})

    class FakePage:
        async def evaluate(self, js):
            return {"outerWidth": 1447, "outerHeight": 993,
                    "screenX": 33, "screenY": 44,
                    "innerWidth": 1431, "innerHeight": 900,
                    "devicePixelRatio": 1}

    bc = browserd.BrowserController.__new__(browserd.BrowserController)
    bc.window_manager = wm
    bc.headless = False
    bc._resolve_page = lambda: FakePage()
    bc._last_geometry_check = 0.0
    bc._geom_cooldown_until = 0.0

    asyncio.new_event_loop().run_until_complete(bc._check_window_geometry())

    assert applied == []  # окно НЕ двигалось
    wm2 = WindowManager(str(p))  # конфиг обновлён фактическими размерами
    assert wm2.config["width"] == 1447
    assert wm2.config["height"] == 993
    assert wm2.config["x"] == 33
    assert wm2.config["y"] == 44
