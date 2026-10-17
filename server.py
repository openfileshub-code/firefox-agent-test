#!/usr/bin/env python3
"""MCP client for an independently managed browserd process."""

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

# Интеграция модулей политики (ТЗ этапы 2-5): guard/настройки/контекст
# подключаются к каждому tool call через middleware call().
import sys as _sys
if str(Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.context_manager import ContextManager          # noqa: E402
from modules.policy_guard import (                          # noqa: E402
    PolicyGuard, PolicyVerdict, RULE_SOURCES,
)
from modules.settings_loader import (                        # noqa: E402
    SettingsLoader, init_global_loader,
)
from modules import url_validator as uv                      # noqa: E402
from modules.relevance_ranker import RelevanceRanker          # noqa: E402
from modules.audit_logger import get_audit_logger             # noqa: E402
from modules import timing_config                             # noqa: E402


BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "playwright_profile"
STATE_FILE = PROFILE_DIR / ".browserd_state.json"
#: Совместимые версии протокола state-файла (демон v4 ещё работоспособен;
#: новые команды просто вернут понятную ошибку "unknown command").
SUPPORTED_PROTOCOL_VERSIONS = (4, 5)
MAX_FRAME_BYTES = 6 * 1024 * 1024

TIMEOUTS = {
    "ping": 3,
    "tabs": 15,
    "press_key": 8,
    "scroll": 8,
    "click_text": 15,
    "navigate": 60,
    "get_content": 30,
    "screenshot": 40,
    "eval": 25,
    "schedule_action": 30,
    "cancel_schedule": 15,
    "list_schedules": 15,
    "disable_radio": 90,
    "save_to_playlist": 45,
    "default": 30,
}


def _dumps(value: Any) -> bytes:
    try:
        import orjson
        return orjson.dumps(value)
    except ImportError:
        return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _loads(value: bytes) -> Any:
    try:
        import orjson
        return orjson.loads(value)
    except ImportError:
        return json.loads(value.decode("utf-8"))


def _make_logger():
    result = logging.getLogger("browserd-mcp")
    result.setLevel(logging.INFO)
    result.propagate = False
    if not result.handlers:
        try:
            (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                BASE_DIR / "logs" / "mcp_debug.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"
            ))
            result.addHandler(handler)
        except Exception:
            pass
    return result


log = _make_logger()


# ---------------------------------------------------------------------------
# Синглтоны политики (ТЗ этап 10): единый источник настроек/контекста/guard'а
# на процесс MCP-сервера.
# ---------------------------------------------------------------------------
SETTINGS_LOADER = SettingsLoader(explicit_path=os.environ.get("BROWSERD_SETTINGS"))
SETTINGS_LOADER.load()
init_global_loader(SETTINGS_LOADER)

CONTEXT_MANAGER = ContextManager(settings=SETTINGS_LOADER.get())
AUDIT = get_audit_logger()
POLICY_GUARD = PolicyGuard(SETTINGS_LOADER, CONTEXT_MANAGER)


def validate_url(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return "Укажите корректный URL без пробелов"
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return "Поддерживаются только полноценные http(s) URL"
    except Exception:
        return "Некорректный URL"

    lowered = value.lower()
    if any(marker in lowered for marker in (
        "<", ">", "{", "}", "[your", "placeholder", "site.tld", "..."
    )):
        return "URL выглядит как пример или плейсхолдер"
    # Модульный валидатор (ТЗ этап 5): placeholder-URL и невалидные video_id.
    # Site-search URL здесь НЕ блокируем: это ответственность PolicyGuard,
    # который учитывает override_phrase/allow_site_search из настроек.
    result = uv.validate_navigation_url(
        value,
        context=CONTEXT_MANAGER.get_active_context(),
        settings=SETTINGS_LOADER.get(),
        allow_search_override=True,
    )
    if not result.valid:
        return "%s: %s" % (result.error_code or "POLICY_URL_REJECTED",
                           result.message)
    return None


VIDEO_JS_RE = re.compile(
    r"\.(play|pause|currentTime\s*=|volume\s*=|muted\s*=|load\s*$)"
    r"|querySelector(?:All)?\s*\(\s*['\"]video"
    # RC6: fullscreen-API для «широкого экрана» — ровно тот вызов,
    # которым агент ломал theater-режим.
    r"|requestFullscreen|webkitRequestFullScreen|webkitRequestFullscreen"
    r"|exitFullscreen|fullscreenElement|fullscreen",
    re.IGNORECASE,
)


class DaemonConnector:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.reader_task = None
        self.write_lock = None
        self.futures: Dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.pid = None
        self.tabs_cache = []
        self.active_index = 0
        self._autostart_in_progress = False
        self._last_autostart_error = ""

    @staticmethod
    def read_state():
        try:
            state = _loads(STATE_FILE.read_bytes())
            return state if isinstance(state, dict) else None
        except Exception:
            return None

    async def ensure_connected(self):
        if self.writer and not self.writer.is_closing():
            return

        # 1. Активный демон с совместимым протоколом — подключаемся.
        state = self.read_state()
        if state and state.get("version") in SUPPORTED_PROTOCOL_VERSIONS:
            try:
                await self._connect_to(state)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Подключение к активному демону не удалось: %s", exc)

        # 2. State-файл демоном новой версии (агент обновился раньше демона).
        if state:
            try:
                v = int(state.get("version"))
            except (TypeError, ValueError):
                v = None
            if v is not None and v > max(SUPPORTED_PROTOCOL_VERSIONS):
                raise RuntimeError(
                    "Демон запущен более новой версией протокола (v%s), чем "
                    "этот MCP-сервер. Перезапустите демон и MCP-хост из одной "
                    "сборки (python daemon_control.py restart)." % v
                )

        # 3. Демон не запущен / state битый / процесс мёртв — автозапуск
        #    через daemon_control (ТЗ: «открой сайт» должно работать без
        #    ручного browser_start; агент не должен переспрашивать).
        if await self.autostart_daemon():
            state = self.read_state()
            if state and state.get("version") in SUPPORTED_PROTOCOL_VERSIONS:
                try:
                    await self._connect_to(state)
                    return
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        "Демон запущен автоматически, но подключиться не "
                        "удалось: %s. Подробности — в browserd.log." % exc
                    )
            raise RuntimeError(
                "Демон запущен автоматически, но его state-файл недоступен. "
                "Проверьте browserd.log."
            )

        # 4. Не удалось ни подключиться, ни запустить — понятная ошибка
        #    с указанием причины (без ложных обещаний автозапуска).
        detail = getattr(self, "_last_autostart_error", "") or ""
        if state:
            reason = ("state-файл есть (v%s, status=%s), но демон недоступен"
                      % (state.get("version"), state.get("status")))
        else:
            reason = "state-файл не найден"
        raise RuntimeError(
            "Браузер не запущен и автозапуск не удался (%s). %s"
            "Запустите вручную: python daemon_control.py start "
            "(подробности — в daemon_control.log и browserd.log)."
            % (reason, ("Последняя ошибка: " + detail + ". ") if detail else "")
        )

    async def _connect_to(self, state: dict):
        """Открыть TCP-соединение с демоном по state-файлу и авторизоваться."""
        if state.get("status") not in ("running", "degraded"):
            raise RuntimeError("Демон не готов: status=%s" % state.get("status"))

        host = state.get("tcp_host")
        port = int(state.get("tcp_port", 0))
        token = str(state.get("auth_token", ""))
        if host != "127.0.0.1" or not 1 <= port <= 65535 or not token:
            raise RuntimeError("Некорректный state-файл демона")

        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=MAX_FRAME_BYTES),
            timeout=10,
        )
        self.write_lock = asyncio.Lock()
        self.pid = state.get("pid")
        self.reader_task = asyncio.create_task(self._reader_loop())

        auth = await self._request(
            {"cmd": "auth", "token": token},
            timeout=10,
            require_ok=False,
        )
        if not auth.get("ok") or auth.get("protocol") not in SUPPORTED_PROTOCOL_VERSIONS:
            await self.disconnect()
            raise RuntimeError("Авторизация демона или версия протокола неверна")

        log.info("Connected to standalone daemon pid=%s", self.pid)

    #: Максимальное ожидание готовности демона после автозапуска, сек.
    AUTOSTART_WAIT_SECONDS = 90

    async def autostart_daemon(self) -> bool:
        """Запустить browserd через daemon_control.start_daemon() и дождаться state.

        Возвращает True, если state-файл появился и стал читаемым.
        Ошибка запуска НЕ бросается наружу — сохраняется в
        ``_last_autostart_error`` (вызывающий формирует итоговую ошибку).
        """
        if self._autostart_in_progress:
            # Параллельные tool call'ы: ждём, пока первый завершит запуск.
            deadline = time.monotonic() + self.AUTOSTART_WAIT_SECONDS
            while time.monotonic() < deadline:
                state = self.read_state()
                if state and state.get("version") in SUPPORTED_PROTOCOL_VERSIONS \
                        and state.get("status") in ("running", "degraded"):
                    return True
                await asyncio.sleep(0.5)
            return False

        self._autostart_in_progress = True
        try:
            log.info("Автозапуск браузера: демон не найден, запускаю browserd")
            rc_holder: Dict[str, Any] = {}

            def _run_launcher():
                import daemon_control

                rc_holder["rc"] = daemon_control.start_daemon()

            launcher = asyncio.create_task(asyncio.to_thread(_run_launcher))
            deadline = time.monotonic() + self.AUTOSTART_WAIT_SECONDS
            while True:
                state = self.read_state()
                if (state
                        and state.get("version") in SUPPORTED_PROTOCOL_VERSIONS
                        and state.get("status") in ("running", "degraded")):
                    await asyncio.sleep(0.2)
                    return True
                if launcher.done():
                    rc = rc_holder.get("rc")
                    if rc == 0:
                        # start_daemon нашёл уже работающий демон
                        state = self.read_state()
                        if state and state.get("version") in SUPPORTED_PROTOCOL_VERSIONS:
                            return True
                    self._last_autostart_error = (
                        "daemon_control завершился с кодом %r "
                        "(см. daemon_control.log)." % rc
                    )
                    return False
                if time.monotonic() > deadline:
                    self._last_autostart_error = (
                        "демон не подтвердил готовность за %d сек "
                        "(см. browserd.log)." % self.AUTOSTART_WAIT_SECONDS
                    )
                    return False
                await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            log.exception("Автозапуск демона завершился ошибкой")
            self._last_autostart_error = "%s: %s" % (type(exc).__name__, exc)
            return False
        finally:
            self._autostart_in_progress = False

    async def _reader_loop(self):
        try:
            while self.reader:
                line = await self.reader.readline()
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    raise ConnectionError("Ответ демона превышает лимит")
                message = _loads(line)
                if not isinstance(message, dict):
                    continue
                if "event" in message:
                    tabs = message.get("tabs")
                    if isinstance(tabs, list):
                        self.tabs_cache = tabs
                        self.active_index = int(
                            message.get("active", self.active_index)
                        )
                    continue
                request_id = message.get("id")
                future = self.futures.pop(request_id, None)
                if future and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Daemon connection reader failed")
        finally:
            self._connection_lost()

    def _connection_lost(self):
        for future in list(self.futures.values()):
            if not future.done():
                future.set_exception(
                    ConnectionError("Соединение с демоном разорвано")
                )
        self.futures.clear()
        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = None
        self.writer = None

    async def disconnect(self):
        task = self.reader_task
        self.reader_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        writer = self.writer
        self.reader = self.writer = None
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        for future in list(self.futures.values()):
            if not future.done():
                future.cancel()
        self.futures.clear()

    async def _request(
        self, payload: dict, timeout: float, require_ok: bool = True
    ) -> dict:
        if not self.writer or self.writer.is_closing():
            raise ConnectionError("Нет соединения с browserd")

        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.futures[request_id] = future
        message = dict(payload, id=request_id)

        try:
            async with self.write_lock:
                self.writer.write(_dumps(message) + b"\n")
                await self.writer.drain()
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.futures.pop(request_id, None)
            raise TimeoutError(
                "Истекло время ожидания ответа. Браузер не останавливался; "
                "результат команды может быть неизвестен."
            )
        finally:
            self.futures.pop(request_id, None)

        if require_ok and not response.get("ok"):
            raise RuntimeError(response.get("error", "Команда не выполнена"))
        return response

    async def send(self, command: str, params: Optional[dict] = None,
                   timeout: Optional[float] = None):
        if not self.writer or self.writer.is_closing():
            # Прозрачный самоподхват: если соединение потеряно (демон был
            # перезапущен/остановлен), переподключаемся по state-файлу —
            # при необходимости с автозапуском. Явные browser_start/browser_stop
            # не трогаем: у browser_stop своё семантическое поведение.
            if command != "stop":
                try:
                    await self.ensure_connected()
                except Exception as exc:  # noqa: BLE001
                    raise ConnectionError(
                        "Нет активного подключения к браузеру и "
                        "переподключение не удалось: %s" % exc
                    )
            else:
                raise ConnectionError(
                    "Нет активного подключения. Демон уже остановлен."
                )
        payload = {"cmd": command}
        if params:
            payload.update(params)
        response = await self._request(
            payload,
            timeout or TIMEOUTS.get(command, TIMEOUTS["default"]),
        )
        return {
            key: value for key, value in response.items()
            if key not in ("id", "protocol")
        }

    async def stop(self):
        # Explicit tool only. No process kill and no state-file deletion here.
        response = await self.send("stop", timeout=20)
        await self.disconnect()
        return response


connector = DaemonConnector()


@asynccontextmanager
async def lifespan(_server):
    try:
        yield {}
    finally:
        # MCP shutdown closes only this client's TCP socket.
        await connector.disconnect()


mcp = FastMCP("firefox-agent", lifespan=lifespan)


#: соответствие MCP tool -> команда демона (для enrich/маршрутизации)
_COMMAND_TO_TOOL = {
    "navigate": "browser_navigate",
    "new_tab": "browser_new_tab",
    "scroll": "browser_scroll",
    "eval": "browser_eval",
    "click_text": "browser_click_text",
    "click_selector": "browser_click_selector",
    "search_on_page": "browser_search_on_page",
    "video_action": "browser_video",
    "expand_menu": "browser_expand_menu",
    "context_get": "browser_get_context",
    "schedule_action": "browser_schedule_action",
    "cancel_schedule": "browser_cancel_schedule",
    "list_schedules": "browser_list_schedules",
    "disable_radio": "browser_video",
    "save_to_playlist": "browser_video",
}

#: команды, чьи ответы наполняют BrowserContext
_CONTEXT_COMMANDS = {
    "navigate", "new_tab", "switch_tab", "tabs", "video_action",
    "context_get", "episode_next", "episode_prev", "episode_open",
    "episode_list", "click_text", "click_selector", "expand_menu",
    "disable_radio", "save_to_playlist", "schedule_action",
}


async def call(command: str, params: Optional[dict] = None, *,
               tool: Optional[str] = None,
               guard_args: Optional[dict] = None) -> dict:
    """Единая точка исполнения команд демона (middleware, ТЗ этап 10).

    1. SettingsLoader hot-reload (mtime) перед каждой проверкой.
    2. PolicyGuard.check() — отказ возвращается как структурированный
       объект (error_code / rule_source / suggested_safe_actions), а не
       исключение, чтобы агент видел безопасные альтернативы.
    3. Исполнение команды демона.
    4. ContextManager.update_from_result() — наполнение контекста
       (video_id/list_id/layout/history) из ответа.
    """
    SETTINGS_LOADER.reload_if_changed()
    tool_name = tool or _COMMAND_TO_TOOL.get(command, command)
    check_args = dict(guard_args if guard_args is not None else (params or {}))

    # Policy Guard v2.0 (Sprint 3): Anti-Panic Lock. После INTENT_REDIRECT
    # деструктивные инструменты (web_search / browser_navigate на главную /
    # URL-выдача) временно заблокированы, чтобы агент не ушёл в паническую
    # петлю и не УНИЧТОЖИЛ контекст страницы (Context Sanctity).
    if CONTEXT_MANAGER.is_tool_blocked(tool_name):
        log.info("Anti-Panic lock: %s blocked (PANIC_LOOP_DETECTED)",
                 tool_name)
        panic = {
            "ok": False,
            "allowed": False,
            "verdict": "blocked",
            "error_code": "PANIC_LOOP_DETECTED",
            "rule_source": RULE_SOURCES.get("PANIC_LOOP_DETECTED", ""),
            "attempted_tool": tool_name,
            "attempted_args": check_args,
            "message": (
                "[SYSTEM POLICY OVERRIDE]: Ты пытаешься покинуть страницу "
                "из-за ошибки. Остановись. Выполни "
                "browser_find_interactive_element или "
                "browser_ui_search_box для поиска на ТЕКУЩЕЙ странице. "
                "ЗАПРЕЩЕНО использовать web_search, google-search или "
                "browser_navigate на главную."),
            "system_injection": (
                "[SYSTEM POLICY OVERRIDE]: Ты пытаешься покинуть страницу "
                "из-за ошибки. Остановись. Выполни "
                "browser_find_interactive_element или browser_ui_search_box "
                "для поиска на ТЕКУЩЕЙ странице. ЗАПРЕЩЕНО использовать "
                "web_search, google-search или browser_navigate на главную."),
            "__system_injection__": {
                "priority": "critical",
                "message": ("[SYSTEM POLICY OVERRIDE]: Ты пытаешься покинуть "
                            "страницу из-за ошибки. Остановись. Выполни "
                            "browser_find_interactive_element или "
                            "browser_ui_search_box для поиска на ТЕКУЩЕЙ "
                            "странице. ЗАПРЕЩЕНО использовать web_search, "
                            "google-search или browser_navigate на главную."),
                "required_action": "immediate_correction",
            },
            "suggested_safe_actions": [
                {"tool": "browser_find_interactive_element",
                 "args": {"pattern": str(check_args.get("query")
                                         or check_args.get("pattern") or "")},
                 "priority": "immediate", "auto_retry": True},
                {"tool": "browser_ui_search_box",
                 "args": {"query": str(check_args.get("query") or "")},
                 "priority": "secondary"},
            ],
        }
        AUDIT.record(tool_name, check_args, decision=panic, result=panic,
                     context=CONTEXT_MANAGER.get_active_context().to_public_dict())
        return panic

    # Policy Guard v2.0 (Sprint 2): Intercept & Translate. Перехватываем
    # деструктивные НАМЕРЕНИЯ до классических hard-rules и возвращаем
    # system_injection с точным алгоритмом коррекции + ставим Anti-Panic Lock.
    intent = POLICY_GUARD.evaluate_intent(
        tool_name, check_args,
        CONTEXT_MANAGER.get_active_context().to_public_dict())
    if intent.verdict == PolicyVerdict.INTENT_REDIRECT:
        log.info("PolicyGuard intent redirect: %s (%s)",
                 tool_name, intent.error_code)
        CONTEXT_MANAGER.set_panic_lock(duration_ms=10000)
        result = {"ok": False, **intent.to_dict()}
        AUDIT.record(tool_name, check_args, decision=intent.to_dict(),
                     result=result,
                     context=CONTEXT_MANAGER.get_active_context().to_public_dict())
        return result

    decision = POLICY_GUARD.check(tool_name, check_args)
    if not decision.allowed:
        log.info("PolicyGuard deny: %s %s (%s)",
                 tool_name, decision.error_code, decision.rule_source)
        result = {"ok": False, **decision.to_dict()}
        CONTEXT_MANAGER.update_from_result(tool_name, check_args, result)
        AUDIT.record(tool_name, check_args, decision=decision.to_dict(),
                     result=result, context=CONTEXT_MANAGER.get_active_context().to_public_dict())
        return result

    # слепой повтор mutate-действия для всех инструментов (не только video)
    retry = POLICY_GUARD.check_blind_retry(tool_name, check_args)
    if not retry.allowed:
        log.info("PolicyGuard deny (blind retry): %s", tool_name)
        result = {"ok": False, **retry.to_dict()}
        CONTEXT_MANAGER.update_from_result(tool_name, check_args, result)
        AUDIT.record(tool_name, check_args, decision=retry.to_dict(),
                     result=result, context=CONTEXT_MANAGER.get_active_context().to_public_dict())
        return result

    # Прозрачный самоподхват браузера (ТЗ §8.1 + фидбек пользователя):
    # любая команда, кроме явного stop, при отсутствии подключения сама
    # подключается к демону или запускает его. Агент не должен сначала
    # вызывать browser_start — «открой сайт» работает одним вызовом.
    if command != "stop":
        try:
            await connector.ensure_connected()
        except Exception as exc:  # noqa: BLE001
            log.warning("Автоподключение демона перед %s не удалось: %s",
                        command, exc)
            result = {
                "ok": False,
                "error": "DaemonUnavailable",
                "message": str(exc),
                "hint": "Демон не запущен и автозапуск не удался. "
                        "Проверьте daemon_control.log/browserd.log или "
                        "выполните вручную: python daemon_control.py start",
            }
            CONTEXT_MANAGER.update_from_result(tool_name, check_args, result)
            AUDIT.record(tool_name, check_args, decision=decision.to_dict(),
                         result=result, context=CONTEXT_MANAGER.get_active_context().to_public_dict())
            return result

    # Анти-цикл PolicyGuard: повторный browser_new_tab с тем же URL
    # автоматически переписывается в навигацию текущей вкладки.
    if command == "new_tab" and check_args.get("__inplace_navigate"):
        url = str((params or {}).get("url") or check_args.get("url") or "")
        if url.startswith(("http://", "https://")):
            log.info("PolicyGuard inplace-rewrite: new_tab(%s) -> navigate",
                     url)
            command = "navigate"
            params = {"url": url}
        else:
            command, params = "new_tab", params or {}

    # ТЗ 6.3: фиксация истории посещений вкладки ДО фактической навигации —
    # текущий URL попадает в url_history, и guard сможет отличить возврат
    # на уже посещённую страницу (POLICY_USE_HISTORY_BACK) от перехода
    # вперёд. Запись только для навигации текущей вкладки (не new_tab).
    if command == "navigate" and not check_args.get("new_tab"):
        CONTEXT_MANAGER.note_pending_navigation(
            str(check_args.get("url") or (params or {}).get("url") or ""))

    # ТЗ §15.4 (C6): site search через форму сайта. URL-фолбэк для YouTube
    # выполняется демоном как навигация на выдачу — политический контроль
    # виртуального инструмента browser_navigate_to_search_url применяется
    # здесь до исполнения (иначе демон обошёл бы middleware).
    if command == "site_search":
        # Блок 1 ТЗ: единая политическая проверка через PolicyGuard
        # (семантика виртуального browser_navigate_to_search_url).
        # Разрешено, если политика сайта НЕ запрещает поиск по URL либо
        # передана дословная фраза пользователя override_phrase.
        decision2 = POLICY_GUARD.check_site_search_policy(
            str(check_args.get("query") or ""),
            check_args.get("override_phrase"),
            site=str(check_args.get("site") or ""))
        if not decision2.allowed:
            log.info("PolicyGuard deny (site_search): %s",
                     decision2.error_code)
            result = {"ok": False, **decision2.to_dict()}
            CONTEXT_MANAGER.update_from_result(tool_name, check_args, result)
            AUDIT.record(tool_name, check_args, decision=decision2.to_dict(),
                         result=result,
                         context=CONTEXT_MANAGER.get_active_context().to_public_dict())
            return result

    # ТЗ §12/§14 (этап 7): жёсткий MCP-потолок на каждый tool call.
    # Потолок = max(mcp_tool_hard_timeout_ms из behavior.json, штатный
    # таймаут команды). Дольше потолка ждать нельзя: агент обязан получить
    # структурированный COMMAND_TIMEOUT с state_uncertain вместо зависания.
    ceiling_ms = timing_config.get("timing.mcp_tool_hard_timeout_ms", 5000)
    try:
        ceiling_ms = float(ceiling_ms)
    except (TypeError, ValueError):
        ceiling_ms = 5000.0
    base_timeout = TIMEOUTS.get(command, TIMEOUTS["default"])
    hard_timeout = max(ceiling_ms / 1000.0, float(base_timeout))
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            connector.send(command, params), timeout=hard_timeout)
    except asyncio.TimeoutError:
        log.warning("action=%s duration_ms=%d ok=false error_code=COMMAND_TIMEOUT",
                    command, int((time.perf_counter() - started) * 1000))
        result = {
            "ok": False,
            "error_code": "COMMAND_TIMEOUT",
            "error": "COMMAND_TIMEOUT",
            "message": (
                "Команда '%s' не получила ответ демона за %.1f с "
                "(жёсткий MCP-потолок). Состояние браузера после команды "
                "неизвестно — НЕ повторяй mutate-действие вслепую." %
                (command, hard_timeout)),
            "hint": ("Проверь состояние через browser_get_context / "
                     "browser_video(get_state) или browser_tabs; при "
                     "подтверждённом отсутствии эффекта повтори один раз."),
            "state_uncertain": True,
            "retry_allowed": False,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "budget_ms": int(hard_timeout * 1000),
        }

    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("duration_ms",
                          int((time.perf_counter() - started) * 1000))
        # Этап 7 ТЗ: state_verifier — пост-проверка mutate-действий с verify=true.
        # Ожидания берутся из параметров; данные — из ответа адаптера. Если
        # данных для проверки нет — passed=True (нет false-negative).
        if (tool_name == "browser_video" and check_args.get("verify")
                and result.get("ok", True)):
            from modules.state_verifier import verify_video_action_result
            vaction = str(check_args.get("action") or "")
            merged = dict(result)
            # ожидания из параметров tool call'а — адаптер о них не знает
            if vaction in ("theater_set", "set_theater"):
                val = check_args.get("value", check_args.get("target"))
                if isinstance(val, str):
                    val = val.strip().lower() in ("true", "1", "on",
                                                  "включить", "вкл")
                if isinstance(val, bool):
                    merged["target"] = val
            if check_args.get("expected_video_id"):
                merged["expected_video_id"] = check_args["expected_video_id"]
            vres = verify_video_action_result(vaction, merged,
                                              settings=SETTINGS_LOADER)
            if not vres.passed:
                result = {"ok": False, **result, **vres.to_dict()}
        # guard/verifier-отказы не должны выглядеть как успешный ответ
        # демона (иначе агент увидит ok:true вместе с error_code)
        elif result.get("error_code") and result.get("ok", True):
            result = {**result, "ok": False}
        if command in _CONTEXT_COMMANDS:
            CONTEXT_MANAGER.update_from_result(tool_name, check_args, result)
            # ТЗ 6.3: после успешной навигации зафиксировать целевой URL в
            # url_history вкладки (источник данных для POLICY_USE_HISTORY_BACK).
            if (command == "navigate" and isinstance(result, dict)
                    and result.get("ok", True)):
                CONTEXT_MANAGER.record_visited_url(
                    str(result.get("url")
                        or check_args.get("url")
                        or (params or {}).get("url") or ""))
    # ТЗ §12.2 (этап 7): per-call лог в формате ключ-значение.
    ok = bool(result.get("ok")) if isinstance(result, dict) else True
    err_code = (result.get("error_code") or result.get("error") or "") \
        if isinstance(result, dict) else ""
    log.info(
        "tool=%s action=%s duration_ms=%d ok=%s error_code=%s",
        tool_name, command,
        int((time.perf_counter() - started) * 1000), ok, err_code or "-")
    AUDIT.record(tool_name, check_args, decision=decision.to_dict(),
                 result=result if isinstance(result, dict) else {"ok": True},
                 context=CONTEXT_MANAGER.get_active_context().to_public_dict())
    return result


@mcp.tool()
async def browser_start() -> dict:
    """Подключиться к отдельно запущенному browserd."""
    await connector.ensure_connected()
    result = await call("tabs")
    connector.tabs_cache = result.get("tabs", [])
    connector.active_index = result.get("active", 0)
    return {
        "connected": True,
        "pid": connector.pid,
        "tabs": connector.tabs_cache[:20],
        "active": connector.active_index,
    }


@mcp.tool()
async def browser_stop() -> dict:
    """Явно остановить браузер и демон."""
    return await connector.stop()


@mcp.tool()
async def browser_navigate(
    url: str, new_tab: bool = False, override_phrase: str = ""
) -> dict:
    """Перейти по URL.

    Args:
        url: полноценный http(s) URL (placeholder-URL запрещены).
        new_tab: открыть в новой вкладке (требует override_phrase с
            явным запросом пользователя — см. user_tool_settings.json).
        override_phrase: дословная фраза пользователя-оператора,
            разрешающая исключение из политики («открой в новой
            вкладке», «ищи на сайте», «прокрути страницу»).
    """
    error = validate_url(url)
    if error:
        raise ValueError(error)
    if new_tab:
        return await call("new_tab", {"url": url}, guard_args={
            "url": url, "new_tab": True, "override_phrase": override_phrase})
    return await call("navigate", {"url": url}, guard_args={
        "url": url, "override_phrase": override_phrase})


@mcp.tool()
async def browser_go_back(timeout: int = 45) -> dict:
    """Шаг НАЗАД по истории вкладки (обёртка над page.go_back()).

    Использовать вместо перезагрузки URL, когда нужно «вернуться».
    Если предыдущей страницы в истории нет, вернёт ok=true и
    went_back=false (это штатная ситуация, а не ошибка).
    """
    return await call("go_back", {"timeout": timeout})


@mcp.tool()
async def browser_history_back(timeout: int = 45) -> dict:
    """Синоним browser_go_back (шаг назад по истории вкладки)."""
    return await call("go_back", {"timeout": timeout})


@mcp.tool()
async def browser_go_forward(timeout: int = 45) -> dict:
    """Шаг ВПЕРЁД по истории вкладки (симметрично browser_go_back)."""
    return await call("go_forward", {"timeout": timeout})


@mcp.tool()
async def browser_click_text(
    text: str = "",
    index: int = 0,
    selector: str = "",
    override_phrase: str = "",
) -> dict:
    """Клик по тексту элемента ИЛИ по CSS-селектору.

    Args:
        text: подстрока текста элемента (fuzzy-поиск get_by_text).
        index: индекс среди найденных совпадений (по умолчанию 0).
        selector: CSS-селектор — для иконочных кнопок без текста
            (например "button[aria-label='Гид']"). Если text пуст и
            selector задан — клик выполняется по селектору.
    """
    if not str(text).strip() and not str(selector).strip():
        raise ValueError("Укажите хотя бы одно: text или selector")
    return await call("click_text", {
        "text": text, "index": index, "selector": selector,
        "override_phrase": override_phrase,
    }, guard_args={
        "text": text, "index": index, "selector": selector,
        "override_phrase": override_phrase,
    })


@mcp.tool()
async def browser_click_selector(selector: str) -> dict:
    """Клик по CSS-селектору (для кнопок/иконок без текста)."""
    if not str(selector).strip():
        raise ValueError("CSS selector не задан")
    return await call("click_selector", {"selector": selector})


@mcp.tool()
async def browser_press_key(key: str) -> dict:
    return await call("press_key", {"key": key})


@mcp.tool()
async def browser_scroll(
    direction: str = "down", amount: int = 800, override_phrase: str = ""
) -> dict:
    """Прокрутить страницу. Без явного запроса пользователя блокируется
    политикой (POLICY_SCROLL_NOT_REQUESTED): для скрытых элементов
    используйте browser_expand_menu.

    Args:
        direction: up/down/left/right.
        amount: пикселей.
        override_phrase: дословная фраза пользователя, разрешающая скролл
            (whitelist allow_scroll из user_tool_settings.json).
    """
    return await call("scroll", {
        "direction": direction, "amount": amount,
    }, guard_args={
        "direction": direction, "amount": amount,
        "override_phrase": override_phrase,
    })


@mcp.tool()
async def browser_type_text(
    selector: str, text: str, clear: bool = True
) -> dict:
    return await call("type_text", {
        "selector": selector, "text": text, "clear": clear
    })


@mcp.tool()
async def browser_eval(js: str) -> dict:
    if VIDEO_JS_RE.search(js or ""):
        raise ValueError("Для управления видео используйте browser_video")
    return await call("eval", {"js": js})


@mcp.tool()
async def browser_screenshot(full_page: bool = False) -> dict:
    return await call("screenshot", {"full_page": full_page})


@mcp.tool()
async def browser_get_content(
    max_chars: int = 8000, mode: str = "text"
) -> dict:
    if mode not in ("text", "html"):
        raise ValueError("mode должен быть text или html")
    return await call("get_content", {
        "max_chars": max_chars, "mode": mode
    })


@mcp.tool()
async def browser_new_tab(url: str = "", override_phrase: str = "") -> dict:
    """Открыть новую вкладку. Запрещено политикой одной вкладки, если
    пользователь явно не разрежил это фразой из whitelist allow_new_tab.

    Args:
        url: необязательный URL для перехода в новой вкладке.
        override_phrase: дословная фраза пользователя («открой в новой
            вкладке» и т.п. из whitelist настроек).
    """
    if url:
        error = validate_url(url)
        if error:
            raise ValueError(error)
    return await call("new_tab", {"url": url}, guard_args={
        "url": url, "override_phrase": override_phrase})


@mcp.tool()
async def browser_close_tab(index: int = -1) -> dict:
    return await call("close_tab", {"index": index})


@mcp.tool()
async def browser_switch_tab(index: int) -> dict:
    return await call("switch_tab", {"index": index})


@mcp.tool()
async def browser_tabs() -> dict:
    result = await call("tabs")
    connector.tabs_cache = result.get("tabs", [])
    connector.active_index = result.get("active", 0)
    return result


@mcp.tool()
async def browser_video(
    action: str,
    value: Any = None,
    target: Any = None,
    expected_video_id: str = "",
    verify: Optional[bool] = None,
    override_phrase: str = "",
) -> dict:
    """Управление видеоплеером через систему адаптеров платформ.

    Адаптер выбирается автоматически по URL/DOM (YouTube, Twitch, VK Video,
    Rutube или generic HTML5 fallback). Поддерживаемые действия:
    play, pause, volume/set_volume, seek (seconds), state,
    set_quality ('480p', '1080p60', 'Auto'... — через UI-автоматизацию,
    без устаревшего IFrame API), toggle_theater_mode (алиасы:
    theater_toggle, toggle_theater, theater_mode, theater, set_theater),
    theater_set (value/target: true|false — идемпотентно, changed:false
    при уже нужном режиме), layout_state (мультиканальный снимок режима
    с confidence/evidence),
    disable_radio (ТЗ 5.3: выход из радио-микса — навигация на чистый
    watch?v=<video_id>, возврат radio_mode/video_id/url),
    save_to_playlist (ТЗ 5.5: value = имя плейлиста, по умолчанию
    «Смотреть позже»; кнопка сохранения -> ожидание ytd-popup-container
    -> клик по пункту -> подтверждение; видео не перезапускается),
    episode_list / episode_next / episode_prev / episode_open (нативная
    навигация по плейлисту через ytInitialData; value для episode_open —
    index или video_id), chapter_list / chapter_seek (главы описания;
    value для chapter_seek — индекс главы или время в секундах),
    next_video, toggle_chat_visibility, detect_login.
    Действие state возвращает расширенный контекст (ТЗ 5.2): platform,
    url, video_id, title, paused, currentTime, duration, volume, muted,
    fullscreen, theater_mode, radio_mode, playlist_id, autoplay_upcoming;
    при невозможности определить видео — video_id: null + warning.
    requestFullscreen через browser_eval заблокирован — для «широкого
    экрана» используйте theater_set/toggle_theater_mode.

    Поле value для volume принимает доли 0..1 либо проценты 0..100
    (авто-нормализация: 0.35 / 35 / "35%" / "0.35" -> 35%). Canonicalный
    параметр — value; target принимается только для обратной совместимости.
    На YouTube громкость устанавливается через GUI-перетаскивание
    ползунка с последующей верификацией и отводом курсора из плеера
    (метод gui_slider_drag); native video.volume — fallback.
    Действие 'state' возвращает расширенный статус:
    помимо fullscreen — theater_mode, full_window_mode, cinematics
    (для YouTube определяется по ytp-theater-mode / html5-theater-mode /
    классу theater; cinematics НЕ считается театральным режимом).

    Переключение театрального режима выполняется системой дублирующих
    каналов: aria/text-кнопка -> CSS .ytp-size-button -> горячая клавиша
    't' -> JS click(), с верификацией смены класса после каждого канала.

    Args:
        action: действие над плеером (см. список выше).
        value: числовой/строковый аргумент (громкость, quality label,
            True/False для theater_set, video_id/index для episode_open).
        target: синоним value для theater_set(true/false) и episode_open.
        expected_video_id: ожидаемое видео; при расхождении с фактическим
            (radio autoplay) действие блокируется VIDEO_CONTEXT_CHANGED.
        verify: запрос пост-верификации мутации; при глобальном
            verify_after_mutation=true отключить нельзя (VERIFY_REQUIRED).
        override_phrase: фраза пользователя, разрешающая исключение.

    Результат содержит поля 'adapter' и 'platform'.
    """
    params: Dict[str, Any] = {"action": action}
    if value is not None:
        params["value"] = value
    if target is not None:
        params["target"] = target
    if expected_video_id:
        params["expected_video_id"] = expected_video_id
    if verify is not None:
        params["verify"] = verify
    guard_args = {
        "action": action,
        "value": value if value is not None else target,
        "expected_video_id": expected_video_id or None,
        "verify": verify,
        "override_phrase": override_phrase,
    }
    return await call("video_action", params, guard_args=guard_args)


@mcp.tool()
async def browser_schedule_action(
    trigger: str,
    action: str = "navigate",
    payload: Optional[Dict[str, Any]] = None,
    expected_video_id: str = "",
    scope: str = "current_tab",
    options: Optional[Dict[str, Any]] = None,
) -> dict:
    """Запланировать действие на событие страницы — без цикла опроса ИИ.

    Расписание исполняется на стороне демона/страницы (ТЗ 4.4, раздел 6):
    триггер ждёт событие в DOM и мгновенно выполняет действие. Агент НЕ
    должен поллить browser_video(state) в ожидании окончания видео.

    Поддерживаемый триггер: video_ended (событие ended у <video>;
    пауза/перемотка пользователем учитываются автоматически — переход
    произойдёт только после фактического окончания).
    Поддерживаемое действие: navigate (только http/https; javascript:,
    file:, data: запрещены — NavigationBlocked).

    Args:
        trigger: 'video_ended'.
        action: 'navigate' (по умолчанию).
        payload: {'url': 'https://www.youtube.com/watch?v=TARGET_ID'}.
        expected_video_id: привязка к текущему ролику; если к моменту
            события видео сменилось (ручная смена / radio autoplay),
            переход НЕ выполняется (state='skipped_context'). Пусто —
            берётся video_id активного видео; если он неизвестен —
            ValueRequired.
        scope: 'current_tab' (иное значение пока не поддерживается).
        options: {'once': true, 'cancel_on_user_navigation': true,
            'verify_video_id': true, 'timeout_seconds': 0}.

    Возвращает {'ok': true, 'schedule_id', 'trigger',
    'expected_video_id', 'target_url', 'state': 'armed'}.
    Состояния: armed -> fired | canceled | skipped_context | inactive
    (закрытие вкладки). Список/статусы — browser_list_schedules.
    """
    # ТЗ 5.3: LLM нередко передаёт payload/options как JSON-строки —
    # безопасно приводим к dict ДО вызова демона (демон делает то же
    # через _coerce_dict, но структурированный отказ здесь понятнее).
    def _coerce_mapping(value, name):
        if value is None or isinstance(value, dict):
            return value or {}
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        return None

    payload = _coerce_mapping(payload, "payload")
    options = _coerce_mapping(options, "options")
    if payload is None or options is None:
        bad = "payload" if payload is None else "options"
        return {
            "ok": False,
            "error_code": "VALUE_REQUIRED",
            "message": ("Параметр '%s' должен быть словарём или корректной "
                        "JSON-строкой объекта (например '{\"url\": \"https://"
                        "...\"}'). ЗАПРЕЩЕНО повторять вызов с тем же "
                        "некорректным значением — исправь формат." % bad),
        }

    params: Dict[str, Any] = {
        "trigger": trigger,
        "action": action,
        "payload": payload or {},
        "scope": scope,
        "options": options or {},
    }
    if expected_video_id:
        params["expected_video_id"] = expected_video_id
    return await call("schedule_action", params,
                      tool="browser_schedule_action")


@mcp.tool()
async def browser_cancel_schedule(schedule_id: str) -> dict:
    """Отменить расписание browser_schedule_action (ТЗ 6.6).

    Снимает слушателя со страницы и помечает расписание 'canceled';
    переход не выполнится, даже если видео завершится после отмены.
    Неизвестный schedule_id -> ScheduleNotFound (список —
    browser_list_schedules).
    """
    return await call("cancel_schedule", {"schedule_id": schedule_id},
                      tool="browser_cancel_schedule")


@mcp.tool()
async def browser_list_schedules() -> dict:
    """Список всех расписаний с актуальными состояниями.

    Перед ответом выполняется sweep: закрытые вкладки помечают свои
    расписания 'inactive', зафиксированные срабатывания — 'fired'.
    """
    return await call("list_schedules", {},
                      tool="browser_list_schedules")


@mcp.tool()
async def browser_expand_menu(
    trigger_text: str,
    mode: str = "hover",
    wait_ms: int = 1500,
    zone_selector: str = "",
) -> dict:
    """Раскрыть меню/дропдаун и вернуть НОВЫЕ интерактивные элементы.

    Находит триггер по тексту (fuzzy + семантический поиск лемм/синонимов),
    выполняет hover или click, ждёт анимацию/AJAX, повторно сканирует DOM
    (включая Shadow DOM) и возвращает только элементы, появившиеся после
    раскрытия. При таймауте скана ставит флаг partial_scan=true.

    Args:
        trigger_text: текст пункта меню, напр. "Продукты" / "Категории".
        mode: "hover" или "click".
        wait_ms: ожидание анимации (500-5000 мс, по умолчанию 1500).
        zone_selector: необязательный CSS-селектор зоны сканирования.
    """
    return await call("expand_menu", {
        "trigger_text": trigger_text,
        "mode": mode,
        "wait_ms": wait_ms,
        "zone_selector": zone_selector,
    })


@mcp.tool()
async def browser_dom_text_search(
    pattern: str, regex: bool = False
) -> dict:
    """Аналог Ctrl+F: поиск текстового совпадения в DOM ТЕКУЩЕЙ страницы.

    ВНИМАНИЕ: НЕ ПЕРЕЗАГРУЖАЕТ страницу и НЕ УНИЧТОЖАЕТ контекст.
    Возвращает кликабельные метаданные найденных элементов (href, selector,
    index, actionable, score). Используй для поиска на уже загруженной
    странице вместо любой URL-навигации (Context Sanctity).

    Args:
        pattern: искомый текст (или regex при regex=True).
        regex: трактовать pattern как регулярное выражение.
    """
    return await call("search_on_page", {
        "pattern": pattern, "regex": regex
    })


#: Семантический алиас v2.0 (Sprint 1): прежнее имя сохранено для
#: обратной совместимости со старыми промптами/адаптерами.
browser_search_on_page = browser_dom_text_search


@mcp.tool()
async def browser_navigate_to_search_results(query: str, site: str = "",
                                             override_phrase: str = "") -> dict:
    """ВНИМАНИЕ: выполняет ПЕРЕХОД ПО URL на страницу выдачи сайта.

    ПЕРЕЗАГРУЖАЕТ текущую страницу и УНИЧТОЖАЕТ текущие результаты
    (для YouTube — переход на /results?search_query=).

    ЗАПРЕЩЕНО использовать, если пользователь просит найти что-то на
    уже загруженной странице. Для поиска НА ТЕКУЩЕЙ странице используй
    browser_dom_text_search или browser_find_interactive_element; для
    поиска через форму сайта — browser_ui_search_box.

    Политика: перехватывается PolicyGuard и требует allow_site_search или
    дословной фразы пользователя override_phrase
    (POLICY_SITE_SEARCH_NOT_REQUESTED / INTENT_REDIRECT_*).

    Args:
        query: поисковый запрос.
        site: ключ сайта из конфига (по умолчанию — определён по URL).
        override_phrase: дословная фраза пользователя, разрешающая поиск
            на сайте (например «ищи на сайте», «найди на ютубе …»).
    """
    params = {"query": query, "override_phrase": override_phrase}
    if site:
        params["site"] = site
    return await call("site_search", params)


#: Семантический алиас v2.0 (Sprint 1): прежнее имя сохранено для
#: обратной совместимости (тесты и старые промпты используют его).
browser_site_search = browser_navigate_to_search_results


@mcp.tool()
async def browser_ui_search_box(query: str, site: str = "") -> dict:
    """Поиск через UI-строку сайта: находит input[type='search'], кликает,
    вводит запрос и нажимает Enter.

    Рекомендуемый безопасный способ поиска на открытой странице
    (Recovery Injection после INTENT_REDIRECT). НЕ выполняет ручной
    переход на URL выдачи до отправки формы сайтом.

    Args:
        query: поисковый запрос.
        site: необязательный ключ сайта из config/player_selectors.json.
    """
    params: dict = {"query": query}
    if site:
        params["site"] = site
    return await call("ui_search_box", params)


@mcp.tool()
async def browser_find_interactive_element(pattern: str = "") -> dict:
    """Основной инструмент поиска кликабельных элементов ТЕКУЩЕЙ страницы.

    Находит кнопки, ссылки, видео-тайлы по нечёткому совпадению текста/href
    с ранжированием. Возвращает candidates (candidate_id/text/href/video_id/
    score). НЕ ПЕРЕЗАГРУЖАЕТ страницу.

    Если лучший score < relevance_threshold — ответ содержит error_code
    NO_CONFIDENT_MATCH_ON_CURRENT_PAGE: не кликай вслепую, раскрой меню
    (browser_expand_menu) или уточни запрос.
    """
    result = await call("find_link", {"pattern": pattern})
    items = result.get("links") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return result
    try:
        threshold = float(SETTINGS_LOADER.get().g("relevance_threshold")
                          or 0.55)
    except Exception:
        threshold = 0.55
    ranked = RelevanceRanker(threshold=threshold).rank_and_wrap(
        pattern, items, source=str(result.get("source") or "live_dom"))
    ranked["url"] = result.get("url", "")
    return ranked


#: Семантический алиас v2.0 (Sprint 1): прежнее имя сохранено для
#: обратной совместимости (suggested_safe_actions используют его).
browser_find_link = browser_find_interactive_element


@mcp.tool()
async def browser_get_context(tab_index: int = -1) -> dict:
    """Снимок активного контекста: url/title/tab_id + video_id/list_id/
    index/is_radio (URL + ytInitialData), layout плеера (theater/fullscreen
    с confidence/evidence), has_episode_queue, same_tab_required,
    expected_video_id.

    Использовать перед video-mutaction действиями и после любого
    подозрения на смену видео (radio autoplay, RC11).
    """
    result = await call("context_get", {"index": tab_index})
    ctx = CONTEXT_MANAGER.get_active_context()
    check = CONTEXT_MANAGER.check_video_context(ctx.video_id)
    out = dict(result) if isinstance(result, dict) else {"ok": False,
                                                         "error": str(result)}
    out["context"] = ctx.to_public_dict()
    out["video_context_check"] = check.to_dict()
    return out


@mcp.tool()
async def browser_validate_url(url: str) -> dict:
    """Проверить URL против политики без перехода: placeholder-маркеры,
    search-results URL, валидность youtube video_id, canonical watch URL.

    Полезен для RC4/RC5: извлеките реальный video_id со страницы
    (browser_find_link) и проверьте его здесь до навигации.
    """
    result = uv.validate_navigation_url(
        url,
        context=CONTEXT_MANAGER.get_active_context(),
        settings=SETTINGS_LOADER.get(),
    )
    out = result.to_dict()
    if not out.get("valid"):
        out["rule_source"] = result.rule_source
    return out


@mcp.tool()
async def browser_reload_settings(path: str = "") -> dict:
    """Перезагрузить user_tool_settings.json (hot-reload).

    Без path — проверка mtime выбранного файла (обычно достаточно:
    middleware делает это перед каждым вызовом). С path — явная
    загрузка конкретного файла с валидацией по схеме; невалидные
    настройки откатываются к safe defaults (SETTINGS_INVALID_FALLBACK).
    """
    if path:
        loader = SettingsLoader(explicit_path=path)
        loaded = loader.load()
        if loaded.source != "file":
            return {
                "ok": False,
                "error_code": "SETTINGS_INVALID_FALLBACK",
                "reason": "; ".join(loaded.warnings) or "невалидные настройки",
                "active_path": SETTINGS_LOADER.active_path,
            }
        # атомарная замена состояния существующего синглтона —
        # PolicyGuard/адаптеры продолжают видеть тот же объект
        SETTINGS_LOADER.__dict__.update(loader.__dict__)
        init_global_loader(SETTINGS_LOADER)
        CONTEXT_MANAGER.set_settings(SETTINGS_LOADER.get())
        s = SETTINGS_LOADER.get()
        return {
            "ok": True,
            "reloaded": True,
            "active_path": SETTINGS_LOADER.active_path,
            "settings_hash": s.hash,
            "warnings": s.warnings,
            "global": s.global_rules,
        }
    changed = SETTINGS_LOADER.reload_if_changed()
    s = SETTINGS_LOADER.get()
    CONTEXT_MANAGER.set_settings(s)
    return {
        "ok": True,
        "changed": changed,
        "active_path": SETTINGS_LOADER.active_path,
        "settings_hash": s.hash,
    }


# ---------------------------------------------------------------------
# Пользовательские алиасы (личные имена вроде «мой джем»).
# Хранятся ОТДЕЛЬНО от user_tool_settings.json — в config/user_aliases.json,
# чтобы не ломать схему настроек политик (SKILL.md §Пользовательские алиасы).
# ---------------------------------------------------------------------
ALIASES_PATH = BASE_DIR / "config" / "user_aliases.json"


def _load_aliases_file() -> dict:
    """Читает user_aliases.json; битый/отсутствующий файл = пустое хранилище."""
    try:
        data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": "1.0", "aliases": {}}
    except (json.JSONDecodeError, OSError):
        log.warning("user_aliases.json повреждён — используем пустое "
                    "хранилище алиасов")
        return {"version": "1.0", "aliases": {}}
    if not isinstance(data, dict) or not isinstance(
            data.get("aliases"), dict):
        return {"version": "1.0", "aliases": {}}
    return data


def _save_aliases_file(data: dict) -> None:
    """Атомарная запись: temp-файл + os.replace (не частичный dump)."""
    ALIASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ALIASES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, ALIASES_PATH)


@mcp.tool()
async def browser_save_alias(alias_name: str, target_url: str = "",
                             search_query: str = "") -> dict:
    """Сохранить пользовательский ярлык (алиас) в config/user_aliases.json.

    Алиас — личное имя пользователя для ссылки или поискового запроса
    («мой джем» → конкретное видео/плейлист). Дописывает отдельный файл,
    НЕ трогая user_tool_settings.json и его схему.

    Правила (SKILL.md §Пользовательские алиасы): сохраняй только с явного
    согласия пользователя; при следующем «открой <имя>» сначала смотри
    browser_list_aliases. Нужен хотя бы один из target_url / search_query.
    """
    name = (alias_name or "").strip().lower()
    url = (target_url or "").strip()
    query = (search_query or "").strip()
    if not name:
        return {"ok": False, "error_code": "ALIAS_NAME_REQUIRED",
                "message": "alias_name не может быть пустым."}
    if not url and not query:
        return {"ok": False, "error_code": "ALIAS_TARGET_REQUIRED",
                "message": "Укажите target_url или search_query для алиаса "
                           "'%s'." % name}
    if url:
        if uv.is_placeholder_url(url):
            return {"ok": False,
                    "error_code": "POLICY_PLACEHOLDER_URL_FORBIDDEN",
                    "message": "Placeholder-URL нельзя сохранить как алиас: "
                               "%s" % url}
        if uv.is_search_results_url(url):
            return {"ok": False,
                    "error_code": "POLICY_SITE_SEARCH_NOT_REQUESTED",
                    "message": "URL поисковой выдачи нельзя сохранять как "
                               "алиас; сохраните точный search_query вместо "
                               "него."}
    data = _load_aliases_file()
    existed = name in data["aliases"]
    data["aliases"][name] = {
        "target_url": url,
        "search_query": query,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        _save_aliases_file(data)
    except OSError as exc:
        return {"ok": False, "error_code": "ALIAS_WRITE_FAILED",
                "message": "Не удалось записать %s: %s" % (ALIASES_PATH, exc)}
    return {"ok": True, "alias_name": name, "existed": existed,
            "path": str(ALIASES_PATH),
            "target_url": url, "search_query": query}


@mcp.tool()
async def browser_list_aliases() -> dict:
    """Список сохранённых пользовательских алиасов (config/user_aliases.json).

    Возвращает aliases: [{alias_name, target_url, search_query, updated_at}].
    Используй перед попытками найти «личное имя» пользователя на странице.
    """
    data = _load_aliases_file()
    items = [{"alias_name": k,
              "target_url": v.get("target_url", ""),
              "search_query": v.get("search_query", ""),
              "updated_at": v.get("updated_at", "")}
             for k, v in sorted(data["aliases"].items())]
    return {"ok": True, "count": len(items), "aliases": items,
            "path": str(ALIASES_PATH)}


@mcp.tool()
async def browser_delete_alias(alias_name: str) -> dict:
    """Удалить пользовательский алиас из config/user_aliases.json."""
    name = (alias_name or "").strip().lower()
    if not name:
        return {"ok": False, "error_code": "ALIAS_NAME_REQUIRED",
                "message": "alias_name не может быть пустым."}
    data = _load_aliases_file()
    if name not in data["aliases"]:
        return {"ok": False, "error_code": "ALIAS_NOT_FOUND",
                "message": "Алиас '%s' не найден." % name}
    del data["aliases"][name]
    try:
        _save_aliases_file(data)
    except OSError as exc:
        return {"ok": False, "error_code": "ALIAS_WRITE_FAILED",
                "message": "Не удалось записать %s: %s" % (ALIASES_PATH, exc)}
    return {"ok": True, "deleted": name, "path": str(ALIASES_PATH)}


if __name__ == "__main__":
    mcp.run(transport="stdio")