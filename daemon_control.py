#!/usr/bin/env python3
"""Управление отдельным browserd: start, stop, status."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "playwright_profile"
STATE_FILE = PROFILE_DIR / ".browserd_state.json"
DAEMON_FILE = BASE_DIR / "browserd.py"
LOG_FILE = BASE_DIR / "daemon_control.log"

PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"
PYTHONW = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"

PROTOCOL_VERSION = 4
CONNECT_TIMEOUT = 3
START_TIMEOUT = 120
STOP_TIMEOUT = 30


def log(message: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as stream:
            stream.write(
                time.strftime("%Y-%m-%d %H:%M:%S ")
                + message.replace("\n", " ") + "\n"
            )
    except OSError:
        pass


def print_and_log(message: str) -> None:
    print(message)
    log(message)


def load_state():
    try:
        with open(STATE_FILE, "rb") as stream:
            state = json.loads(stream.read().decode("utf-8"))
        return state if isinstance(state, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def pid_is_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    if pid <= 0:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    try:
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p

        get_exit_code = kernel.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_exit_code.restype = ctypes.c_int

        close_handle = kernel.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        handle = open_process(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False

        exit_code = ctypes.c_ulong()
        try:
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            close_handle(handle)
    except Exception:
        # Не запускаем второй демон, если надёжно определить состояние процесса
        # не получилось. Это безопаснее, чем рисковать одним профилем в двух процессах.
        return False


def read_line(stream) -> dict:
    line = stream.readline()
    if not line:
        raise ConnectionError("Демон закрыл TCP-соединение")
    result = json.loads(line.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Ответ демона не является JSON-объектом")
    return result


def authenticated_request(state: dict, command: str, request_id: int) -> dict:
    host = state.get("tcp_host")
    port = int(state.get("tcp_port", 0))
    token = str(state.get("auth_token", ""))

    if host != "127.0.0.1" or not (1 <= port <= 65535) or not token:
        raise ValueError("В state-файле некорректные параметры подключения")

    with socket.create_connection(
        (host, port), timeout=CONNECT_TIMEOUT
    ) as sock:
        sock.settimeout(CONNECT_TIMEOUT)

        with sock.makefile("rwb") as stream:
            auth = {"cmd": "auth", "token": token, "id": request_id}
            stream.write(json.dumps(auth).encode("utf-8") + b"\n")
            stream.flush()

            auth_reply = read_line(stream)
            if not auth_reply.get("ok"):
                raise PermissionError(
                    auth_reply.get("error", "Ошибка авторизации демона")
                )
            if auth_reply.get("protocol") != PROTOCOL_VERSION:
                raise RuntimeError(
                    "Версия протокола демона не совпадает: "
                    f"{auth_reply.get('protocol')!r}"
                )

            if command == "ping":
                request = {"cmd": "ping", "id": request_id + 1}
            else:
                request = {"cmd": command, "id": request_id + 1}

            stream.write(json.dumps(request).encode("utf-8") + b"\n")
            stream.flush()
            return read_line(stream)


def daemon_is_reachable(state: dict) -> bool:
    try:
        reply = authenticated_request(state, "ping", 100)
        return bool(reply.get("ok") and reply.get("pong"))
    except Exception as exc:
        log(f"Проверка подключения не прошла: {type(exc).__name__}: {exc}")
        return False


def start_daemon() -> int:
    if not PYTHON.exists():
        print_and_log(f"Не найден Python виртуального окружения: {PYTHON}")
        print_and_log("Сначала создайте .venv и установите зависимости.")
        return 2

    if not DAEMON_FILE.exists():
        print_and_log(f"Не найден файл демона: {DAEMON_FILE}")
        return 2

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    if state and state.get("version") == PROTOCOL_VERSION:
        if daemon_is_reachable(state):
            print_and_log(
                f"Демон уже работает. PID={state.get('pid')}, "
                f"порт={state.get('tcp_port')}"
            )
            return 0

        # Если PID ещё жив, не пытаемся запускать второй демон с тем же профилем.
        if pid_is_alive(state.get("pid")):
            print_and_log(
                "В state-файле указан живой процесс, но подключиться к нему "
                "не удалось. Новый демон НЕ запущен, чтобы не повредить профиль. "
                "Проверьте browserd.log и состояние процесса."
            )
            return 3

        print_and_log(
            "Найден state-файл от завершившегося процесса. "
            "Lock-файл профиля не удаляется; ОС освободит блокировку процесса."
        )

    elif STATE_FILE.exists() and state is None:
        print_and_log(
            "State-файл существует, но не читается. Новый демон не запущен "
            "из соображений безопасности. Проверьте файл вручную."
        )
        return 3

    # pythonw.exe не создаёт консольное окно. Сам browserd пишет диагностику
    # в browserd.log. Не используем CREATE_BREAKAWAY_FROM_JOB: демон запускается
    # этим скриптом отдельно от MCP-хоста, например из Explorer/Планировщика.
    pythonw = PYTHONW if PYTHONW.exists() else PYTHON
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(BASE_DIR),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            [
                str(pythonw),
                str(DAEMON_FILE),
                "--profile",
                str(PROFILE_DIR),
            ],
            **kwargs,
        )
    except Exception as exc:
        print_and_log(f"Не удалось запустить демон: {type(exc).__name__}: {exc}")
        return 4

    print_and_log(
        f"Запущен процесс browserd, ожидаю готовности. PID={process.pid}"
    )

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = load_state()

        if state and state.get("version") == PROTOCOL_VERSION:
            if state.get("status") in ("running", "degraded"):
                if daemon_is_reachable(state):
                    print_and_log(
                        f"Демон готов. PID={state.get('pid')}, "
                        f"порт={state.get('tcp_port')}"
                    )
                    return 0

        if process.poll() is not None:
            # Процесс может завершиться, если профиль уже занят старым демоном.
            state = load_state()
            if state and state.get("version") == PROTOCOL_VERSION:
                if daemon_is_reachable(state):
                    print_and_log(
                        f"Подключение к уже работающему демону успешно. "
                        f"PID={state.get('pid')}"
                    )
                    return 0

            print_and_log(
                f"Процесс запуска завершился с кодом {process.returncode}. "
                "Смотрите browserd.log."
            )
            return 5

        time.sleep(0.5)

    # Важно: не убиваем процесс из-за медленного старта.
    print_and_log(
        f"Демон не подтвердил готовность за {START_TIMEOUT} секунд. "
        "Процесс не завершался принудительно. Проверьте browserd.log."
    )
    return 6


def stop_daemon() -> int:
    state = load_state()
    if not state:
        print_and_log("State-файл демона не найден или не читается.")
        return 2

    if state.get("version") != PROTOCOL_VERSION:
        print_and_log(
            "Версия протокола state-файла не совпадает. "
            "Остановка не отправлена."
        )
        return 3

    pid = state.get("pid")
    try:
        reply = authenticated_request(state, "stop", 200)
    except Exception as exc:
        print_and_log(
            f"Не удалось отправить штатную команду stop: "
            f"{type(exc).__name__}: {exc}"
        )
        print_and_log(
            "Процесс не убивался. Проверьте, открыт ли демон и актуален ли "
            "state-файл."
        )
        return 4

    if not reply.get("ok"):
        print_and_log("Демон отклонил stop: " + str(reply.get("error")))
        return 5

    print_and_log(f"Команда штатной остановки отправлена демону PID={pid}.")
    deadline = time.monotonic() + STOP_TIMEOUT

    while time.monotonic() < deadline:
        fresh_state = load_state()
        if not pid_is_alive(pid):
            print_and_log("Процесс демона завершился штатно.")
            return 0
        if fresh_state is None:
            # State удаляется самим демоном при штатной остановке.
            time.sleep(0.5)
            if not pid_is_alive(pid):
                print_and_log("Процесс демона завершился штатно.")
                return 0
        time.sleep(0.5)

    print_and_log(
        "Демон не завершился за отведённое время. "
        "Принудительное завершение НЕ выполнялось; браузер оставлен как есть."
    )
    return 6


def show_status() -> int:
    state = load_state()
    if not state:
        print_and_log("State-файл отсутствует или не читается.")
        return 1

    print_and_log(
        "State: status=%s pid=%s host=%s port=%s"
        % (
            state.get("status"),
            state.get("pid"),
            state.get("tcp_host"),
            state.get("tcp_port"),
        )
    )

    if daemon_is_reachable(state):
        print_and_log("TCP-проверка: демон отвечает.")
        return 0

    print_and_log("TCP-проверка: демон не ответил.")
    return 2


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].lower() not in (
        "start", "stop", "status"
    ):
        print("Использование: daemon_control.py start|stop|status")
        return 64

    command = sys.argv[1].lower()
    if command == "start":
        return start_daemon()
    if command == "stop":
        return stop_daemon()
    return show_status()


if __name__ == "__main__":
    sys.exit(main())