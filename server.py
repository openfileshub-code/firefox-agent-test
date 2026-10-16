#!/usr/bin/env python3
"""MCP client for an independently managed browserd process."""

import asyncio
import json
import logging
import os
import re
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
from modules.policy_guard import PolicyGuard                # noqa: E402
from modules.settings_loader import (                        # noqa: E402
    SettingsLoader, init_global_loader,
)
from modules import url_validator as uv                      # noqa: E402
from modules.relevance_ranker import RelevanceRanker          # noqa: E402
from modules.audit_logger import get_audit_logger             # noqa: E402


BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "playwright_profile"
STATE_FILE = PROFILE_DIR / ".browserd_state.json"
PROTOCOL_VERSION = 5
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
            handler = RotatingFileHandler(
                BASE_DIR / "mcp_debug.log",
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

        state = self.read_state()
        if not state:
            raise RuntimeError(
                "Демон не найден. Сначала запустите browserd.py отдельно."
            )
        if state.get("version") != PROTOCOL_VERSION:
            raise RuntimeError(
                "Версия протокола демона не совпадает. "
                "Остановите старый демон и запустите новую версию."
            )
        if state.get("status") not in ("running", "degraded"):
            raise RuntimeError(
                "Демон не готов: status=%s" % state.get("status")
            )

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
        if not auth.get("ok") or auth.get("protocol") != PROTOCOL_VERSION:
            await self.disconnect()
            raise RuntimeError("Авторизация демона или версия протокола неверна")

        log.info("Connected to standalone daemon pid=%s", self.pid)

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
            raise ConnectionError(
                "Нет активного подключения. Выполните browser_start. "
                "Демон не запускался и не останавливался автоматически."
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


mcp = FastMCP("camoufox-browserd", lifespan=lifespan)


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
}

#: команды, чьи ответы наполняют BrowserContext
_CONTEXT_COMMANDS = {
    "navigate", "new_tab", "switch_tab", "tabs", "video_action",
    "context_get", "episode_next", "episode_prev", "episode_open",
    "episode_list", "click_text", "click_selector", "expand_menu",
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

    result = await connector.send(command, params)

    if isinstance(result, dict):
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
    episode_list / episode_next / episode_prev / episode_open (нативная
    навигация по плейлисту через ytInitialData; value для episode_open —
    index или video_id), chapter_list / chapter_seek (главы описания;
    value для chapter_seek — индекс главы или время в секундах),
    next_video, toggle_chat_visibility, detect_login.
    Действие state возвращает video_id/list_id/is_radio (RC11).
    requestFullscreen через browser_eval заблокирован — для «широкого
    экрана» используйте theater_set/toggle_theater_mode.

    Поле value для volume принимает доли 0..1 либо проценты 0..100
    (авто-нормализация). Действие 'state' возвращает расширенный статус:
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
async def browser_search_on_page(
    pattern: str, regex: bool = False
) -> dict:
    return await call("search_on_page", {
        "pattern": pattern, "regex": regex
    })


@mcp.tool()
async def browser_find_link(pattern: str = "") -> dict:
    """Найти кликабельные элементы страницы по тексту/href с ранжированием.

    Возвращает candidates (candidate_id/text/href/video_id/score). Если
    лучший score < relevance_threshold — ответ содержит error_code
    NO_CONFIDENT_MATCH_ON_CURRENT_PAGE: не кликай вслепую, раскрой меню
    или уточни запрос.
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


if __name__ == "__main__":
    mcp.run(transport="stdio")