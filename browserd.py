#!/usr/bin/env python3
"""
Standalone Camoufox daemon for the Firefox/Camoufox MCP project.

Протокол совместим с приложенным server.py, если в нём
PROTOCOL_VERSION = 4.

Отключение MCP-клиента, потеря TCP-соединения и таймаут команды
не останавливают браузер. Штатная остановка выполняется только
аутентифицированной командой stop.

Этот код не может предотвратить принудительное завершение процесса
Windows, завершение Job Object, выход пользователя из системы,
перезагрузку или аварийное завершение Firefox.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE = BASE_DIR / "playwright_profile"
LOG_FILE = BASE_DIR / "browserd.log"

# Должно совпадать с server.py.
PROTOCOL_VERSION = 5

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_CLIENTS = 8
MAX_PENDING_REQUESTS_PER_CLIENT = 32
MAX_CONTENT_CHARS = 50_000
MAX_SCREENSHOT_B64_CHARS = 4_000_000

WATCHDOG_INTERVAL_SECONDS = 30
WATCHDOG_TIMEOUT_SECONDS = 10


def json_bytes(value: Any) -> bytes:
    """Сериализовать объект в UTF-8 JSON."""
    try:
        import orjson

        return orjson.dumps(value, default=str)
    except ImportError:
        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")


def json_loads(value: bytes) -> Any:
    """Разобрать UTF-8 JSON."""
    try:
        import orjson

        return orjson.loads(value)
    except ImportError:
        return json.loads(value.decode("utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_logger() -> logging.Logger:
    logger = logging.getLogger("browserd")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        try:
            handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"
            ))
            logger.addHandler(handler)
        except Exception:
            # Если файл лога недоступен, демон всё равно должен вывести
            # ошибку в stderr при запуске через python.exe.
            logging.basicConfig(level=logging.INFO)

    return logger


log = make_logger()


class StateStore:
    """Атомарная запись состояния демона для MCP-клиента."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}

    def write(self, **fields: Any) -> None:
        self.data.update(fields)
        self.data["version"] = PROTOCOL_VERSION

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.tmp"
            )

            with open(temporary, "wb") as stream:
                stream.write(json_bytes(self.data))
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temporary, self.path)
        except Exception:
            log.exception("Не удалось записать state-файл")

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def acquire_profile_lock(lock_path: Path):
    """
    Захватить блокировку профиля на всё время жизни демона.

    Не удаляйте lock-файл вручную, чтобы запустить второй процесс:
    блокируется открытый файл, а не его наличие на диске.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        return handle

    except Exception:
        with contextlib.suppress(Exception):
            handle.close()
        return None


def release_profile_lock(handle) -> None:
    if not handle:
        return

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        log.exception("Не удалось освободить блокировку профиля")
    finally:
        with contextlib.suppress(Exception):
            handle.close()


class BrowserController:
    """Команды браузера и управление общим persistent context."""

    def __init__(self, profile_dir: Path, headless: bool):
        self.profile_dir = profile_dir
        self.headless = headless
        self.state = StateStore(profile_dir / ".browserd_state.json")

        self.cm = None
        self.context = None
        self._real_aexit = None

        self._page_ids: dict[int, str] = {}
        self.active_index = 0
        self.active_tab_id: str | None = None

        # Большинство действий со страницей сериализуются: команды не должны
        # одновременно менять active_index или одно и то же состояние страницы.
        self._command_lock = asyncio.Lock()

        self._stop_event = asyncio.Event()
        self.stopping = False

        # writer -> per-connection write lock. Один TCP-поток ответа на клиента
        # предотвращает перемешивание строк JSON с событиями и ответами.
        self._clients: dict[Any, asyncio.Lock] = {}

        self._watchdog_failures = 0
        self._network_events: list[dict[str, Any]] = []

        self._pending_dialog = None
        self._pending_dialog_future: asyncio.Future | None = None
        self._dialog_task: asyncio.Task | None = None

        self.link_cache = None
        self.video_controller = None
        self.page_agent = None
        self.forms = None
        self.websocket_monitor = None

    @property
    def pages(self) -> list[Any]:
        try:
            return list(self.context.pages) if self.context else []
        except Exception:
            return []

    def _page_id(self, page: Any) -> str:
        key = id(page)
        if key not in self._page_ids:
            self._page_ids[key] = uuid.uuid4().hex[:12]
        return self._page_ids[key]

    def _resolve_page(self, params: dict[str, Any] | None = None):
        params = params or {}
        pages = self.pages

        requested_tab_id = params.get("tab_id")
        if requested_tab_id:
            requested_tab_id = str(requested_tab_id)
            for page in pages:
                if page is None:
                    continue
                with contextlib.suppress(Exception):
                    if page.is_closed():
                        continue
                if self._page_id(page) == requested_tab_id:
                    self.active_tab_id = requested_tab_id
                    self.active_index = pages.index(page)
                    return page
            raise LookupError(
                f"Вкладка с tab_id={requested_tab_id!r} не найдена"
            )

        if not pages:
            return None

        if self.active_tab_id:
            for index, page in enumerate(pages):
                if self._page_id(page) == self.active_tab_id:
                    # ТЗ 4.1: активная вкладка могла закрыться вручную —
                    # не возвращаем закрытую страницу как активную.
                    with contextlib.suppress(Exception):
                        if page.is_closed():
                            break
                    self.active_index = index
                    return page

        self.active_index = max(0, min(self.active_index, len(pages) - 1))
        page = pages[self.active_index]
        self.active_tab_id = self._page_id(page)
        return page

    async def require_page(self, params: dict[str, Any] | None = None):
        page = self._resolve_page(params)
        if page is None:
            raise RuntimeError("Нет открытых вкладок")
        try:
            if page.is_closed():
                raise RuntimeError("Текущая вкладка закрыта")
        except AttributeError:
            pass
        return page

    async def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            from camoufox.async_api import AsyncCamoufox
        except Exception as exc:
            self.state.write(
                pid=os.getpid(),
                status="error",
                error=f"Не удалось импортировать Camoufox: {exc}",
                updated_at=utc_now(),
            )
            raise RuntimeError(
                f"Импорт camoufox.async_api завершился ошибкой: {exc}"
            ) from exc

        self.cm = AsyncCamoufox(
            headless=self.headless,
            persistent_context=True,
            user_data_dir=str(self.profile_dir),
        )

        try:
            # В режиме persistent_context ожидается BrowserContext.
            self.context = await self.cm.__aenter__()
            self._real_aexit = self.cm.__aexit__
        except Exception as exc:
            self.state.write(
                pid=os.getpid(),
                status="error",
                error=f"Запуск Camoufox завершился ошибкой: {exc}",
                updated_at=utc_now(),
            )
            raise

        if self.context is None or not hasattr(self.context, "pages"):
            raise RuntimeError(
                "AsyncCamoufox не вернул BrowserContext с атрибутом pages. "
                "Проверьте API установленной версии Camoufox."
            )

        try:
            from modules.forms import Forms
            from modules.link_cache import LinkCache
            from modules.page_agent import PageAgent
            from modules.video_controller import VideoController
            from modules.websocket_monitor import WebSocketMonitor

            self.forms = Forms()
            self.link_cache = LinkCache()
            self.page_agent = PageAgent()
            self.video_controller = VideoController()
            self.websocket_monitor = WebSocketMonitor()
        except Exception:
            # Ошибка одного расширенного модуля не должна скрывать её
            # и приводить к ложному сообщению об успешной загрузке.
            log.exception("Не удалось загрузить один или несколько модулей")

        if not self.pages:
            await self.context.new_page()

        for page in self.pages:
            self._wire_page(page)

        if self.pages:
            self.active_index = 0
            self.active_tab_id = self._page_id(self.pages[0])

        log.info(
            "Camoufox started; pid=%d profile=%s tabs=%d",
            os.getpid(),
            self.profile_dir,
            len(self.pages),
        )

    def _wire_page(self, page: Any) -> None:
        self._page_id(page)

        if self.websocket_monitor:
            with contextlib.suppress(Exception):
                self.websocket_monitor.attach(page)

        with contextlib.suppress(Exception):
            page.on("close", lambda _page: self._schedule(self._on_page_closed()))

        with contextlib.suppress(Exception):
            page.on("framenavigated", self._on_frame_navigated)

        with contextlib.suppress(Exception):
            page.on("requestfailed", self._on_request_failed)

        with contextlib.suppress(Exception):
            page.on("response", self._on_response)

        # Обработчик возвращается сразу; само решение выполняет отдельная задача,
        # чтобы другие MCP-команды могли запросить состояние/ответить на диалог.
        with contextlib.suppress(Exception):
            page.on("dialog", self._on_dialog_event)

        self._install_dialog_shield(page)

    #: JS-щит модальных окон (системная правка №4). Инжектится в каждую
    #: вкладку до выполнения скриптов страницы: window.alert/confirm/prompt
    #: перестают блокировать браузер — alert игнорируется, confirm/prompt
    #: возвращают безопасные значения. Нативный dialog-хук остаётся как
    #: второй слой защиты (на случай, если страница восстановила оригиналы).
    _DIALOG_SHIELD_JS = r"""
(() => {
  if (window.__browserdDialogShield) return;
  window.__browserdDialogShield = true;
  try {
    const nativeAlert = window.alert ? window.alert.bind(window) : null;
    const nativeConfirm = window.confirm ? window.confirm.bind(window) : null;
    const nativePrompt = window.prompt ? window.prompt.bind(window) : null;
    window.__browserdNativeDialogs =
      { alert: nativeAlert, confirm: nativeConfirm, prompt: nativePrompt };
  } catch (e) {}
  const log = [];
  window.__browserdDialogLog = log;
  const record = (type, message, result) => {
    try {
      log.push({ type, message: String(message ?? '').slice(0, 500),
                 result, time: Date.now() });
      if (log.length > 50) log.splice(0, log.length - 50);
    } catch (e) {}
  };
  window.alert = (message) => { record('alert', message, 'ignored'); };
  window.confirm = (message) => {
    record('confirm', message, false);
    return false;              // штатный ответ «Отмена»
  };
  window.prompt = (message, defaultValue) => {
    record('prompt', message, defaultValue ?? null);
    return (defaultValue === undefined || defaultValue === null)
      ? null                   // штатный ответ «Отмена»
      : String(defaultValue);  // значение по умолчанию вместо блокировки
  };
})();
"""

    def _install_dialog_shield(self, page: Any) -> None:
        """Ставит JS-щит и на текущий документ, и на все будущие навигации."""
        # API BrowserContext в разных версиях/сборках называется по-разному
        # (add_init_script / addInitScript) — пробуем оба.
        for method_name in ("add_init_script", "addInitScript"):
            try:
                if self.context is not None and hasattr(
                        self.context, method_name):
                    getattr(self.context, method_name)(
                        script=self._DIALOG_SHIELD_JS)
                    break
            except Exception:  # noqa: BLE001
                continue

        applied = False
        for method_name in ("add_init_script", "addInitScript"):
            try:
                if hasattr(page, method_name):
                    getattr(page, method_name)(script=self._DIALOG_SHIELD_JS)
                    applied = True
            except Exception:  # noqa: BLE001
                continue
        if not applied:
            log.warning("Page.add_init_script недоступен; щит ставится "
                        "только через evaluate в текущий документ")

        # Текущий документ уже мог выполняться без щита — ставим немедленно.
        with contextlib.suppress(Exception):
            page.evaluate(self._DIALOG_SHIELD_JS)

    @staticmethod
    def _schedule(coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            with contextlib.suppress(Exception):
                coro.close()

    async def _on_page_closed(self) -> None:
        await self.push_tabs()

    def _on_frame_navigated(self, frame: Any) -> None:
        try:
            if getattr(frame, "parent_frame", None) is not None:
                return
        except Exception:
            pass

        if self.link_cache:
            with contextlib.suppress(Exception):
                self.link_cache.mark_dirty()

        self._schedule(self._push_navigation(frame))

    async def _push_navigation(self, frame: Any) -> None:
        with contextlib.suppress(Exception):
            await self.broadcast({
                "event": "url_changed",
                "url": str(getattr(frame, "url", ""))[:2000],
                "tabs": await self.tab_list(),
                "active": self.active_index,
            })

    def _on_request_failed(self, request: Any) -> None:
        failure = getattr(request, "failure", None)
        self._record_network_event({
            "kind": "request_failed",
            "url": str(getattr(request, "url", ""))[:1500],
            "error": str(failure or "")[:500],
        })

    def _on_response(self, response: Any) -> None:
        try:
            status = int(getattr(response, "status", 0))
        except (TypeError, ValueError):
            return

        if status >= 400:
            self._record_network_event({
                "kind": "http_error",
                "status": status,
                "url": str(getattr(response, "url", ""))[:1500],
            })

    def _record_network_event(self, event: dict[str, Any]) -> None:
        self._network_events.append({**event, "time": utc_now()})
        self._network_events = self._network_events[-100:]

    def _on_dialog_event(self, dialog: Any) -> None:
        # Системная правка №4: JS-щит (см. _DIALOG_SHIELD_JS) перехватывает
        # alert/confirm/prompt до появления нативного диалога. Если диалог
        # всё-таки возник (страница восстановила оригиналы window.*, либо
        # это beforeunload/beforeprint), он не должен блокировать браузер:
        # отвечаем штатным значением сразу и не ждём решения агента.
        try:
            dtype = str(getattr(dialog, "type", "") or "").lower()
        except Exception:  # noqa: BLE001
            dtype = ""

        if dtype in ("alert", "beforeunload", "beforeprint"):
            self._schedule(self._dismiss_dialog(dialog))
            return
        if dtype == "confirm":
            self._schedule(self._dismiss_dialog(dialog))  # == «Отмена»
            return
        if dtype == "prompt":
            async def _answer_prompt() -> None:
                with contextlib.suppress(Exception):
                    await dialog.accept(
                        str(getattr(dialog, "default_value", "")
                            or getattr(dialog, "defaultValue", "") or ""))
            self._schedule(_answer_prompt())
            return

        if self._dialog_task and not self._dialog_task.done():
            # Не оставляем второй неизвестный диалог блокировать вкладку.
            self._schedule(self._dismiss_dialog(dialog))
            return

        self._pending_dialog = dialog
        self._pending_dialog_future = asyncio.get_running_loop().create_future()
        self._schedule(self._broadcast_dialog(dialog))
        self._dialog_task = asyncio.create_task(
            self._wait_and_resolve_dialog(dialog, self._pending_dialog_future)
        )

    async def _broadcast_dialog(self, dialog: Any) -> None:
        await self.broadcast({
            "event": "dialog",
            "type": str(getattr(dialog, "type", "unknown")),
            "message": str(getattr(dialog, "message", ""))[:1000],
        })

    async def _wait_and_resolve_dialog(
        self,
        dialog: Any,
        future: asyncio.Future,
    ) -> None:
        try:
            action, prompt_text = await asyncio.wait_for(future, timeout=120)
            if action == "accept":
                await dialog.accept(prompt_text or "")
            else:
                await dialog.dismiss()
        except asyncio.TimeoutError:
            log.warning("Диалог не получил ответа за 120 секунд; отклоняю")
            with contextlib.suppress(Exception):
                await dialog.dismiss()
        except Exception:
            log.exception("Не удалось разрешить браузерный диалог")
            with contextlib.suppress(Exception):
                await dialog.dismiss()
        finally:
            if self._pending_dialog is dialog:
                self._pending_dialog = None
                self._pending_dialog_future = None
            self._dialog_task = None

    async def _dismiss_dialog(self, dialog: Any) -> None:
        with contextlib.suppress(Exception):
            await dialog.dismiss()

    #: Служебные URL, не являющиеся «настоящими» вкладками (ТЗ 4.1).
    _BLANK_URLS = ("about:blank", "about:home")

    async def tab_list(self) -> list[dict[str, Any]]:
        """Список живых вкладок (ТЗ 4.1: фильтрация фантомов).

        Исключаются:
        * закрытые страницы (``page.is_closed() == True``);
        * служебные ``about:blank``, если в контексте есть другие
          непустые вкладки.

        Если активная вкладка исчезла из живого набора, демон
        автоматически переключается на первую живую вкладку; при
        полном отсутствии живых страниц создается новая служебная,
        но закрытая вкладка никогда не возвращается как активная.
        """
        raw_pages = self.pages

        # Pass 1: собираем данные и признак живости.
        infos: list[dict[str, Any]] = []
        for page in raw_pages:
            try:
                closed = bool(page.is_closed())
            except Exception:
                closed = False
            if closed:
                continue
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            title = ""
            with contextlib.suppress(Exception):
                title = await page.title()
            infos.append({
                "page": page,
                "tab_id": self._page_id(page),
                "url": url,
                "title": title,
            })

        # Pass 2: фильтрация about:blank, если есть содержательные вкладки.
        has_real = any(
            info["url"] and info["url"] not in self._BLANK_URLS
            for info in infos
        )
        live = [
            info for info in infos
            if not (has_real and (
                not info["url"] or info["url"] in self._BLANK_URLS))
        ]

        if len(live) != len(infos):
            log.info(
                "tab_list: отфильтровано %d служебных about:blank вкладок",
                len(infos) - len(live),
            )

        # Pass 3: синхронизация активной вкладки с живым набором.
        live_ids = {info["tab_id"] for info in live}
        if self.active_tab_id not in live_ids:
            if live:
                self.active_tab_id = live[0]["tab_id"]
                log.info(
                    "Активная вкладка исчезла; переключение на живую: %s",
                    self.active_tab_id,
                )
            elif self.context is not None and not self.stopping:
                with contextlib.suppress(Exception):
                    new_page = await self.context.new_page()
                    self._wire_page(new_page)
                    self.active_tab_id = self._page_id(new_page)
                    log.warning(
                        "Живых вкладок не осталось; создана служебная: %s",
                        self.active_tab_id,
                    )
                    return await self.tab_list()

        result = []
        for index, info in enumerate(live):
            result.append({
                "index": index,
                "tab_id": info["tab_id"],
                "url": info["url"],
                "title": info["title"],
                "active": info["tab_id"] == self.active_tab_id,
                "closed": False,
            })

        return result

    async def broadcast(self, event: dict[str, Any]) -> None:
        payload = json_bytes(event) + b"\n"
        dead_writers = []

        for writer, write_lock in tuple(self._clients.items()):
            try:
                async with write_lock:
                    writer.write(payload)
                    await writer.drain()
            except Exception:
                dead_writers.append(writer)

        for writer in dead_writers:
            self._clients.pop(writer, None)

    async def push_tabs(self) -> None:
        await self.broadcast({
            "event": "tabs_sync",
            "tabs": await self.tab_list(),
            "active": self.active_index,
        })

    async def explicit_stop(self) -> None:
        """Остановить браузер только после явной аутентифицированной команды."""
        if self.stopping:
            return

        self.stopping = True
        log.warning("Получена явная команда stop")

        try:
            if self._pending_dialog_future and not self._pending_dialog_future.done():
                self._pending_dialog_future.set_result(("dismiss", ""))

            if self._real_aexit:
                await self._real_aexit(None, None, None)
            elif self.context:
                await self.context.close()
        except Exception:
            log.exception("Ошибка при штатной остановке браузера")
        finally:
            self.state.clear()
            self._stop_event.set()

    async def watchdog(self) -> None:
        """
        Диагностическая проверка. Ошибки меняют state на degraded,
        но никогда не закрывают страницу, контекст или процесс.
        """
        while not self.stopping:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)

            page = self._resolve_page()
            if page is None:
                continue

            try:
                result = await asyncio.wait_for(
                    page.evaluate("1"),
                    timeout=WATCHDOG_TIMEOUT_SECONDS,
                )
                if result == 1:
                    self._watchdog_failures = 0
                else:
                    self._watchdog_failures += 1
            except Exception as exc:
                self._watchdog_failures += 1
                log.warning(
                    "Watchdog probe failed (%d): %s",
                    self._watchdog_failures,
                    exc,
                )

            status = (
                "degraded"
                if self._watchdog_failures >= 3
                else "running"
            )
            self.state.write(
                status=status,
                watchdog_failures=self._watchdog_failures,
                last_watchdog=utc_now(),
            )

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("cmd")

        params = request.get("params")
        if not isinstance(params, dict):
            params = {
                key: value
                for key, value in request.items()
                if key not in ("id", "cmd", "params")
            }

        if command == "ping":
            return {
                "ok": True,
                "pong": True,
                "pid": os.getpid(),
            }

        if command == "stop":
            # TCP-обработчик отправит ответ до остановки контекста.
            asyncio.create_task(self._stop_after_reply())
            return {"ok": True, "stopping": True}

        # Эти две команды должны проходить, даже когда обычное действие
        # удерживает lock и ожидает ответа на browser dialog.
        if command == "dialog_state":
            return await self.cmd_dialog_state(params)

        if command == "dialog_resolve":
            return await self.cmd_dialog_resolve(params)

        async with self._command_lock:
            try:
                method = getattr(self, "cmd_" + str(command), None)
                if method is None:
                    return {
                        "ok": False,
                        "error": f"Неизвестная команда: {command}",
                    }

                result = await method(params)
                if not isinstance(result, dict):
                    return {
                        "ok": False,
                        "error": "Внутренняя ошибка: команда вернула не объект",
                    }
                return result

            except Exception as exc:
                log.exception("Ошибка команды %s", command)
                return {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    async def _stop_after_reply(self) -> None:
        await asyncio.sleep(0.35)
        await self.explicit_stop()

    @staticmethod
    def _normalize_url(value: Any) -> str:
        url = str(value or "").strip()
        if not url:
            raise ValueError("URL не задан")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        return url

    async def cmd_tabs(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "tabs": await self.tab_list(),
            "active": self.active_index,
        }

    #: JS-сниппет: видео-идентификаторы страницы YouTube из ytInitialData
    #: (нативный источник list_id/index/is_radio — без URL-парсинга).
    _YT_INITIAL_DATA_JS = r"""
    () => {
      const out = {};
      try {
        const d = window.ytInitialData;
        if (!d) return out;
        const contents = (((d.contents || {}).twoColumnWatchNextResults || {})
          .playlist || {}).playlist || {};
        const listId = contents.listId || null;
        if (listId) { out.list_id = listId; }
        const items = contents.results || [];
        let index = null;
        const vid = ((document.querySelector('#movie_player') || {})
                     .getVideoData) ? null : null;
        let current = null;
        try {
          const mp = document.querySelector('#movie_player');
          if (mp && typeof mp.getVideoData === 'function') {
            current = (mp.getVideoData() || {}).video_id || null;
          }
        } catch (e) { /* getVideoData недоступен */ }
        if (!current) {
          const m = location.href.match(/[?&]v=([A-Za-z0-9_-]{11})/);
          current = m ? m[1] : null;
        }
        if (current) out.video_id = current;
        items.forEach((it, i) => {
          const vid = ((((it.playlistVideoRenderer || {}).videoId) ||
                        ((it.compactVideoRenderer || {}).videoId)));
          if (vid && vid === current) index = i;
        });
        if (index !== null) out.index = index;
        if (listId && /^RD/.test(listId)) out.is_radio = true;
        if (items.length) out.episode_count = items.length;
      } catch (e) { /* не YouTube / иная структура */ }
      return out;
    }
    """

    async def cmd_context_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Снимок активного контекста вкладки для ContextManager/PolicyGuard.

        Возвращает url/title/tab_id + video_id/list_id/index/is_radio
        (URL + ytInitialData) и layout-режимы плеера. Используется
        инструментом browser_get_context (server.py) и middleware для
        наполнения BrowserContext (RC5/RC10/RC11).
        """
        page = await self.require_page(params)
        result: dict[str, Any] = {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "tab_id": self._page_id(page),
            "video_id": None,
            "list_id": None,
            "index": None,
            "is_radio": False,
            "has_episode_queue": False,
        }

        # 1) Идентификаторы из URL (дёшево и надёжно).
        with contextlib.suppress(Exception):
            from urllib.parse import urlparse, parse_qs
            parts = urlparse(page.url or "")
            qs = parse_qs(parts.query)
            vid = (qs.get("v") or [""])[0]
            if vid:
                result["video_id"] = vid
            lid = (qs.get("list") or [""])[0]
            if lid:
                result["list_id"] = lid
            idx = (qs.get("index") or [""])[0]
            if idx.isdigit():
                result["index"] = int(idx)
            if (qs.get("start_radio") or [""])[0] == "1" or lid.startswith("RD"):
                result["is_radio"] = True

        # 2) Нативные данные страницы (ytInitialData / getVideoData).
        with contextlib.suppress(Exception):
            native = await page.evaluate(self._YT_INITIAL_DATA_JS) or {}
            for key in ("video_id", "list_id", "index", "is_radio"):
                if native.get(key) is not None and result.get(key) in (None, False):
                    result[key] = native[key]
            if native.get("episode_count"):
                result["has_episode_queue"] = True
                result["episode_count"] = native["episode_count"]

        # 3) Layout плеера через адаптер (multi-signal theater/fullscreen).
        try:
            from adapters.registry import get_adapter

            adapter = get_adapter(
                page,
                page_url=getattr(page, "url", "") or None,
                page_dom_sample=await self._collect_dom_sample(page),
                video_controller=self.video_controller,
            )
            modes = await adapter.get_player_modes()
            if isinstance(modes, dict):
                result["layout"] = modes.get("layout_state") or {}
                result["state"] = modes
        except Exception:  # noqa: BLE001
            log.debug("context.get: адаптер недоступен", exc_info=True)

        return result

    async def cmd_navigate(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        url = self._normalize_url(params.get("url"))
        timeout_ms = max(
            1000,
            min(int(params.get("timeout", 45)), 180) * 1000,
        )

        # Намеренно без автоматического повтора: навигация могла состояться,
        # даже если клиент получил таймаут.
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )

        if self.link_cache:
            with contextlib.suppress(Exception):
                self.link_cache.mark_dirty()

        status = getattr(response, "status", None)
        result = {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "http_status": status,
            "http_error": bool(status and status >= 400),
            "tab_id": self._page_id(page),
        }
        # Идентификаторы для ContextManager (RC5/RC11): видео/плейлист
        # видны уже из URL без дорогого evaluate.
        with contextlib.suppress(Exception):
            from urllib.parse import urlparse as _up, parse_qs as _pq
            _qs = _pq(_up(page.url or "").query)
            vid = (_qs.get("v") or [""])[0]
            if vid:
                result["video_id"] = vid
            lid = (_qs.get("list") or [""])[0]
            if lid:
                result["list_id"] = lid
                if lid.startswith("RD"):
                    result["is_radio"] = True
            idx = (_qs.get("index") or [""])[0]
            if idx.isdigit():
                result["index"] = int(idx)
        return result

    async def cmd_go_back(self, params: dict[str, Any]) -> dict[str, Any]:
        """Шаг назад по истории вкладки (системная правка №5).

        Обёртка над page.go_back(): агенту больше не нужно «возвращаться»
        перезагрузкой URL. Если предыдущей страницы в истории нет
        (например, вкладка только что открыта), go_back вернёт None —
        это штатная ситуация, а не ошибка.
        """
        page = await self.require_page(params)
        timeout_ms = max(
            1000,
            min(int(params.get("timeout", 45)), 180) * 1000,
        )

        before_url = page.url
        with contextlib.suppress(Exception):
            await page.bring_to_front()

        response = await page.go_back(wait_until="domcontentloaded",
                                      timeout=timeout_ms)

        if self.link_cache:
            with contextlib.suppress(Exception):
                self.link_cache.mark_dirty()

        return {
            "ok": True,
            "went_back": response is not None,
            "from_url": before_url,
            "url": page.url,
            "title": await page.title(),
            "http_status": getattr(response, "status", None),
        }

    async def cmd_go_forward(self, params: dict[str, Any]) -> dict[str, Any]:
        """Шаг вперёд по истории вкладки (симметрично cmd_go_back)."""
        page = await self.require_page(params)
        timeout_ms = max(
            1000,
            min(int(params.get("timeout", 45)), 180) * 1000,
        )

        before_url = page.url
        response = await page.go_forward(wait_until="domcontentloaded",
                                         timeout=timeout_ms)

        if self.link_cache:
            with contextlib.suppress(Exception):
                self.link_cache.mark_dirty()

        return {
            "ok": True,
            "went_forward": response is not None,
            "from_url": before_url,
            "url": page.url,
            "title": await page.title(),
            "http_status": getattr(response, "status", None),
        }

    async def cmd_new_tab(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.context:
            raise RuntimeError("BrowserContext не запущен")

        page = await self.context.new_page()
        self._wire_page(page)
        self.active_index = max(0, len(self.pages) - 1)
        self.active_tab_id = self._page_id(page)

        url_value = str(params.get("url") or "").strip()
        status = None
        if url_value:
            response = await page.goto(
                self._normalize_url(url_value),
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            status = getattr(response, "status", None)

        result = {
            "ok": True,
            "index": self.active_index,
            "tab_id": self.active_tab_id,
            "url": page.url,
            "http_status": status,
            "tabs": await self.tab_list(),
        }
        if url_value:
            with contextlib.suppress(Exception):
                from urllib.parse import urlparse as _up, parse_qs as _pq
                _qs = _pq(_up(page.url or "").query)
                vid = (_qs.get("v") or [""])[0]
                if vid:
                    result["video_id"] = vid
                lid = (_qs.get("list") or [""])[0]
                if lid:
                    result["list_id"] = lid
                    if lid.startswith("RD"):
                        result["is_radio"] = True
                idx = (_qs.get("index") or [""])[0]
                if idx.isdigit():
                    result["index"] = int(idx)
        return result

    async def cmd_close_tab(self, params: dict[str, Any]) -> dict[str, Any]:
        pages = self.pages
        if not pages:
            return {"ok": False, "error": "Нет открытых вкладок"}

        if params.get("tab_id"):
            page = self._resolve_page(params)
            index = pages.index(page)
        else:
            index = int(params.get("index", -1))
            if index < 0:
                index += len(pages)
            if index < 0 or index >= len(pages):
                return {"ok": False, "error": "Индекс вкладки вне диапазона"}
            page = pages[index]

        tab_id = self._page_id(page)
        await page.close()

        remaining = self.pages
        if not remaining and not self.stopping:
            # Не останавливаем persistent context при закрытии последней
            # вкладки: создаём пустую вкладку для дальнейшей работы.
            new_page = await self.context.new_page()
            self._wire_page(new_page)
            remaining = self.pages
            self.active_index = 0
            self.active_tab_id = self._page_id(new_page)
        elif remaining:
            self.active_index = max(0, min(index, len(remaining) - 1))
            self.active_tab_id = self._page_id(remaining[self.active_index])

        return {
            "ok": True,
            "closed": index,
            "closed_tab_id": tab_id,
            "remaining": len(remaining),
            "tabs": await self.tab_list(),
        }

    async def cmd_switch_tab(self, params: dict[str, Any]) -> dict[str, Any]:
        pages = self.pages
        if not pages:
            return {"ok": False, "error": "Нет открытых вкладок"}

        tab_id = str(params.get("tab_id") or "")
        if tab_id:
            page = next(
                (
                    candidate for candidate in pages
                    if self._page_id(candidate) == tab_id
                ),
                None,
            )
            if page is None:
                return {"ok": False, "error": "Вкладка не найдена"}
            index = pages.index(page)
        else:
            index = int(params.get("index", 0))
            if index < 0:
                index += len(pages)
            if index < 0 or index >= len(pages):
                return {"ok": False, "error": "Индекс вкладки вне диапазона"}
            page = pages[index]

        self.active_index = index
        self.active_tab_id = self._page_id(page)

        with contextlib.suppress(Exception):
            await page.bring_to_front()

        return {
            "ok": True,
            "index": index,
            "tab_id": self.active_tab_id,
            "url": page.url,
            "tabs": await self.tab_list(),
        }

    # ------------------------------------------------------------------
    # Единый безопасный клик (ТЗ 4.3): scrollIntoViewIfNeeded ->
    # wait visible/enabled -> click; структурированные коды ошибок.
    # ------------------------------------------------------------------
    CLICK_TIMEOUT_DEFAULT_S = 8
    CLICK_TIMEOUT_MAX_S = 60

    def _click_timeout_ms(self, params: dict[str, Any]) -> int:
        try:
            seconds = float(params.get("timeout", self.CLICK_TIMEOUT_DEFAULT_S))
        except (TypeError, ValueError):
            seconds = self.CLICK_TIMEOUT_DEFAULT_S
        seconds = max(1.0, min(seconds, self.CLICK_TIMEOUT_MAX_S))
        return int(seconds * 1000)

    async def _safe_click(self, locator: Any, timeout_ms: int,
                          label: str) -> dict[str, Any]:
        """Клик с прокруткой и диагностическими кодами ошибок (ТЗ 4.3)."""
        try:
            if await locator.count() == 0:
                return {
                    "ok": False,
                    "error": "ElementNotFound",
                    "message": f"Элемент не найден: {label}",
                    "hint": "browser_find_link / browser_screenshot для диагностики",
                }
        except Exception:  # noqa: BLE001
            pass

        try:
            # scrollIntoViewIfNeeded до проверки видимости — элемент может
            # существовать, но лежать за пределами текущего вьюпорта.
            await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": "ElementHidden",
                "message": f"Элемент скрыт или недоступен для прокрутки: {label}",
                "hint": f"scrollIntoViewIfNeeded не удалась: {exc}. "
                        "Попробуйте browser_expand_menu или browser_scroll.",
            }

        try:
            await locator.wait_for(state="visible", timeout=min(timeout_ms, 5000))
        except Exception:  # noqa: BLE001
            return {
                "ok": False,
                "error": "ElementHidden",
                "message": f"Элемент не стал видимым после прокрутки: {label}",
                "hint": "Возможно, требуется раскрыть меню (browser_expand_menu).",
            }

        try:
            enabled = await locator.is_enabled()
        except Exception:  # noqa: BLE001
            enabled = True
        if not enabled:
            return {
                "ok": False,
                "error": "ElementDisabled",
                "message": f"Элемент отключён (disabled): {label}",
                "hint": "Дождитесь активации элемента или проверьте состояние страницы.",
            }

        try:
            await locator.click(timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "outside of the viewport" in message.lower():
                return {
                    "ok": False,
                    "error": "ElementHidden",
                    "message": f"Элемент остался вне области видимости: {label}",
                    "hint": "Повторите browser_scroll_to_text/browser_expand_menu.",
                }
            raise
        return {"ok": True}

    async def cmd_click_text(self, params: dict[str, Any]) -> dict[str, Any]:
        # Совмещённый интерфейс (системная правка №1): если передан
        # selector и не передан text — кликаем по селектору. Это даёт
        # агенту возможность нажимать иконочные кнопки без текста,
        # найденные через browser_eval.
        selector = str(params.get("selector") or "").strip()
        text = str(params.get("text") or "").strip()
        if selector and not text:
            return await self.cmd_click_selector_impl({**params,
                                                       "selector": selector})

        page = await self.require_page(params)
        if not text:
            return {"ok": False,
                    "error": "Не задан ни текст, ни CSS selector"}

        index = int(params.get("index", 0))
        timeout_ms = self._click_timeout_ms(params)

        locator = page.get_by_text(
            text,
            exact=bool(params.get("exact", False)),
        )
        count = await locator.count()
        if count == 0:
            return {
                "ok": False,
                "error": "ElementNotFound",
                "message": "Элемент с таким текстом не найден",
                "hint": "browser_search_text / browser_find_link для поиска",
            }

        if index < 0 or index >= count:
            return {
                "ok": False,
                "error": f"Индекс {index} вне диапазона; найдено: {count}",
            }

        target = locator.nth(index)
        click_result = await self._safe_click(target, timeout_ms, f"text={text!r}")
        if not click_result.get("ok"):
            return click_result

        if self.link_cache:
            with contextlib.suppress(Exception):
                self.link_cache.mark_dirty()

        return {
            "ok": True,
            "clicked": text,
            "index": index,
            "url": page.url,
        }

    async def cmd_click_selector(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        selector = str(params.get("selector") or "").strip()
        if not selector:
            return {"ok": False, "error": "CSS selector не задан"}

        locator = page.locator(selector).first
        click_result = await self._safe_click(
            locator, self._click_timeout_ms(params), f"selector={selector!r}")
        if not click_result.get("ok"):
            return click_result

        if self.link_cache:
            with contextlib.suppress(Exception):
                self.link_cache.mark_dirty()

        return {"ok": True, "selector": selector, "url": page.url}

    async def cmd_press_key(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        key = str(params.get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "Клавиша не задана"}

        await page.keyboard.press(key)
        return {"ok": True, "key": key}

    async def cmd_scroll(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        direction = str(params.get("direction", "down")).lower()

        if direction not in ("up", "down", "left", "right"):
            return {
                "ok": False,
                "error": "Направление должно быть up/down/left/right",
            }

        amount = min(max(abs(int(params.get("amount", 800))), 1), 100_000)
        sign = -1 if direction in ("up", "left") else 1

        dx = sign * amount if direction in ("left", "right") else 0
        dy = sign * amount if direction in ("up", "down") else 0

        await page.evaluate(
            "([x,y]) => window.scrollBy({left:x,top:y,behavior:'smooth'})",
            [dx, dy],
        )
        return {"ok": True, "direction": direction, "amount": amount}

    async def cmd_type_text(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        selector = str(params.get("selector") or "").strip()
        if not selector:
            return {"ok": False, "error": "CSS selector не задан"}

        locator = page.locator(selector).first
        if await locator.count() == 0:
            return {"ok": False, "error": "Поле не найдено"}
        if not await locator.is_visible():
            return {"ok": False, "error": "Поле скрыто"}
        if not await locator.is_enabled():
            return {"ok": False, "error": "Поле отключено"}

        text = str(params.get("text") or "")
        if params.get("clear", True):
            await locator.fill("")
        if text:
            delay = max(0, min(int(params.get("delay", 12)), 500))
            await locator.type(text, delay=delay)

        return {
            "ok": True,
            "selector": selector,
            "chars": len(text),
        }

    async def cmd_get_content(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        mode = str(params.get("mode", "text")).lower()
        limit = max(
            1,
            min(int(params.get("max_chars", 8000)), MAX_CONTENT_CHARS),
        )

        if mode == "html":
            value = await page.content()
            key = "html"
        elif mode == "text":
            value = await page.locator("body").inner_text()
            key = "text"
        else:
            return {"ok": False, "error": "mode должен быть text или html"}

        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            key: value[:limit],
            "chars_total": len(value),
            "truncated": len(value) > limit,
        }

    async def cmd_eval(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        script = str(params.get("js") or params.get("script") or "")
        if not script.strip():
            return {"ok": False, "error": "JavaScript не задан"}

        timeout = max(1.0, min(float(params.get("timeout", 15)), 120.0))
        result = await asyncio.wait_for(
            page.evaluate(script),
            timeout=timeout,
        )

        rendered = json.dumps(result, ensure_ascii=False, default=str)
        return {
            "ok": True,
            "result": rendered[:MAX_CONTENT_CHARS],
            "truncated": len(rendered) > MAX_CONTENT_CHARS,
        }

    async def cmd_screenshot(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        directory = self.profile_dir / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)

        path = directory / f"shot_{int(time.time() * 1000)}.png"
        data = await page.screenshot(
            path=str(path),
            full_page=bool(params.get("full_page", False)),
        )
        encoded = base64.b64encode(data).decode("ascii")

        if len(encoded) > MAX_SCREENSHOT_B64_CHARS:
            return {
                "ok": True,
                "path": str(path),
                "image_included": False,
                "reason": "Изображение сохранено на диск, но слишком велико для TCP-ответа",
            }

        return {
            "ok": True,
            "path": str(path),
            "b64": encoded,
        }

    async def cmd_search_on_page(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        page = await self.require_page(params)
        pattern = str(params.get("pattern") or "")
        if not pattern:
            return {"ok": False, "error": "Шаблон поиска не задан"}

        if params.get("regex"):
            result = await page.evaluate(
                r"""
                pattern => {
                  let re;
                  try {
                    re = new RegExp(pattern, 'gi');
                  } catch (e) {
                    return {error: String(e)};
                  }

                  const text = document.body?.innerText || '';
                  const matches = [];
                  let match;
                  let loops = 0;

                  while (
                    (match = re.exec(text)) &&
                    matches.length < 50 &&
                    loops++ < 10000
                  ) {
                    matches.push(match[0]);
                    if (!match[0]) re.lastIndex++;
                  }
                  return matches;
                }
                """,
                pattern,
            )

            if isinstance(result, dict) and result.get("error"):
                return {"ok": False, "error": result["error"]}

            return {
                "ok": True,
                "pattern": pattern,
                "matches": result,
            }

        # Для литерального поиска используем Playwright-локатор.
        locator = page.get_by_text(pattern, exact=False)
        count = await locator.count()
        matches = []

        for index in range(min(count, 30)):
            item = locator.nth(index)
            try:
                if not await item.is_visible():
                    continue
                text = await item.inner_text(timeout=2000)
                matches.append(text[:300])
            except Exception:
                continue

        return {
            "ok": True,
            "pattern": pattern,
            "matches": matches,
        }

    async def cmd_find_link(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        pattern = str(params.get("pattern") or "").strip()

        if self.link_cache:
            try:
                await self.link_cache.ensure_fresh(page)
                results = (
                    self.link_cache.search(pattern)
                    if pattern
                    else list(self.link_cache.links)
                )
                return {
                    "ok": True,
                    "source": "inventory",
                    "count": len(results),
                    "links": results[:100],
                }
            except Exception:
                log.exception("LinkCache failed; using live DOM fallback")

        try:
            links = await page.evaluate(
                r"""
                () => [...document.querySelectorAll(
                  'a[href],button,[role="button"],[role="tab"],[role="menuitem"]'
                )]
                  .filter(el => {
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display !== 'none' &&
                      s.visibility !== 'hidden' &&
                      Number(s.opacity) !== 0 &&
                      r.width > 0 && r.height > 0;
                  })
                  .slice(0, 500)
                  .map(el => ({
                    text: (
                      el.getAttribute('aria-label') ||
                      el.getAttribute('title') ||
                      el.innerText ||
                      el.textContent ||
                      ''
                    ).replace(/\s+/g, ' ').trim().slice(0, 240),
                    href: el.href || '',
                    type: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    disabled: !!el.disabled ||
                      el.getAttribute('aria-disabled') === 'true'
                  }))
                """
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Не удалось получить элементы страницы: {exc}",
            }

        if pattern:
            needle = pattern.casefold().replace("ё", "е")
            links = [
                item for item in links
                if needle in (
                    str(item.get("text", "")) + " " +
                    str(item.get("href", ""))
                ).casefold().replace("ё", "е")
            ]

        return {
            "ok": True,
            "source": "live_dom",
            "count": len(links),
            "links": links[:100],
        }

    async def cmd_inspect_page(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        result: dict[str, Any] = {
            "url": page.url,
            "title": await page.title(),
            "tabs": await self.tab_list(),
        }

        if self.page_agent:
            try:
                result.update(await self.page_agent.inspect(
                    page,
                    include_frames=bool(params.get("include_frames", True)),
                ))
            except Exception:
                log.exception("PageAgent.inspect failed")

        if self.link_cache:
            try:
                await self.link_cache.ensure_fresh(page)
                result["interactive_inventory"] = self.link_cache.links[:600]
            except Exception:
                log.exception("LinkCache inventory failed")

        return {"ok": True, **result}

    async def cmd_search_text(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        query = str(params.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "Поисковый текст не задан"}

        if self.page_agent:
            matches = await self.page_agent.search_text(
                page,
                query,
                include_frames=bool(params.get("include_frames", True)),
                limit=max(1, min(int(params.get("limit", 30)), 100)),
            )
            return {"ok": True, "matches": matches}

        result = await self.cmd_search_on_page({"pattern": query})
        return result

    async def cmd_scroll_to_text(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        page = await self.require_page(params)
        text = str(params.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Текст не задан"}

        if self.page_agent:
            result = await self.page_agent.scroll_to_text(page, text)
            return {"ok": True, **result}

        locator = page.get_by_text(text, exact=False).first
        if await locator.count() == 0:
            return {"ok": False, "error": "Текст не найден"}

        await locator.scroll_into_view_if_needed(timeout=8000)
        return {
            "ok": True,
            "text": text,
            "visible": await locator.is_visible(),
        }

    async def cmd_click_selector(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.cmd_click_selector_impl(params)

    async def cmd_click_selector_impl(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        page = await self.require_page(params)
        selector = str(params.get("selector") or "").strip()
        if not selector:
            return {"ok": False, "error": "CSS selector не задан"}

        locator = page.locator(selector).first
        click_result = await self._safe_click(
            locator, self._click_timeout_ms(params), f"selector={selector!r}")
        if not click_result.get("ok"):
            return click_result

        if self.link_cache:
            with contextlib.suppress(Exception):
                self.link_cache.mark_dirty()

        return {
            "ok": True,
            "selector": selector,
            "url": page.url,
        }

    async def cmd_form_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        if not self.forms:
            return {"ok": False, "error": "Модуль Forms не загружен"}

        result = await self.forms.inventory(page)
        return {"ok": True, **result}

    async def cmd_form_fill(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        if not self.forms:
            return {"ok": False, "error": "Модуль Forms не загружен"}

        values = params.get("values")
        if not isinstance(values, dict):
            return {"ok": False, "error": "values должен быть JSON-объектом"}

        result = await self.forms.fill(page, values)
        return {"ok": True, **result}

    # ------------------------------------------------------------------
    # Раскрытие меню (ТЗ 3.3): hover/click по триггеру -> новые элементы
    # ------------------------------------------------------------------
    async def _find_trigger_by_text(self, page: Any, trigger_text: str) -> Any:
        """Fuzzy-поиск элемента-триггера меню по тексту.

        Порядок: точное совпадение -> подстрока -> семантический поиск по
        инвентарю LinkCache (леммы/синонимы). Возвращает Playwright Locator
        или None.
        """
        if not trigger_text:
            return None
        # 1) Точное совпадение текста.
        locator = page.get_by_text(trigger_text, exact=True)
        try:
            if await locator.count() > 0 and await locator.first.is_visible():
                return locator.first
        except Exception:  # noqa: BLE001
            pass
        # 2) Нечёткое вхождение подстроки.
        locator = page.get_by_text(trigger_text, exact=False)
        try:
            count = await locator.count()
            for i in range(min(count, 10)):
                cand = locator.nth(i)
                if await cand.is_visible():
                    return cand
        except Exception:  # noqa: BLE001
            pass
        # 3) Семантика: ищем в инвентаре, затем матчим DOM по xpath-подсказке.
        if self.link_cache is not None:
            try:
                await self.link_cache.ensure_fresh(page)
                hits = self.link_cache.search(trigger_text)
                for hit in hits[:5]:
                    text = (hit.get("text") or "").strip()
                    if not text:
                        continue
                    loc = page.get_by_text(text, exact=False)
                    if await loc.count() > 0:
                        return loc.first
            except Exception:  # noqa: BLE001
                log.debug("expand_menu: семантический поиск недоступен", exc_info=True)
        return None

    async def cmd_expand_menu(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        trigger_text = str(params.get("trigger_text") or "").strip()
        mode = str(params.get("mode") or "hover").strip().lower()
        if mode not in ("hover", "click"):
            return {"ok": False, "error": "mode должен быть 'hover' или 'click'"}
        if not trigger_text:
            return {"ok": False, "error": "trigger_text не задан"}

        wait_ms = max(500, min(int(params.get("wait_ms", 1500)), 5000))
        zone_selector = str(params.get("zone_selector") or "")

        if self.link_cache is None:
            return {"ok": False, "error": "LinkCache/сканер не загружены"}

        trigger = await self._find_trigger_by_text(page, trigger_text)
        if trigger is None:
            return {
                "ok": False,
                "error": "ElementNotFoundError",
                "message": f"Триггер меню '{trigger_text}' не найден",
                "hint": "browser_screenshot для диагностики; проверьте регистр/язык",
            }

        # Снимок ДО раскрытия.
        before = await self.link_cache.scan_interactive_elements(page, zone_selector)
        before_keys = {
            (e.get("text"), e.get("href"), e.get("tag"), e.get("selector"))
            for e in before["elements"]
        }

        # Действие раскрытия.
        await trigger.scroll_into_view_if_needed(timeout=5000)
        if mode == "click":
            await trigger.click(timeout=5000)
        else:
            await trigger.hover(timeout=5000)
            # Держим курсор над элементом: доп. микро-движение против anti-hover.
            with contextlib.suppress(Exception):
                box = await trigger.bounding_box()
                if box:
                    await page.mouse.move(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2 + 1, steps=2)

        # Ожидание анимации/AJAX (0.5–2 сек по ТЗ).
        await asyncio.sleep(wait_ms / 1000.0)

        # Снимок ПОСЛЕ и дифф.
        after = await self.link_cache.scan_interactive_elements(page, zone_selector)
        new_elements = [
            e for e in after["elements"]
            if (e.get("text"), e.get("href"), e.get("tag"), e.get("selector"))
            not in before_keys
        ]

        partial = bool(before.get("partial_scan") or after.get("partial_scan"))
        result: dict[str, Any] = {
            "ok": True,
            "action": "expand_menu",
            "trigger_text": trigger_text,
            "mode": mode,
            "new_elements_count": len(new_elements),
            "new_elements": new_elements[:200],
            "total_after": len(after["elements"]),
            "partial_scan": partial,
        }
        if partial:
            result["warning"] = (
                "Скан выполнен частично (таймаут JS); вернуто текущее состояние DOM")
        if not new_elements and not partial:
            result["note"] = (
                "Новых элементов не обнаружено: возможно, меню уже было "
                "раскрыто, триггер не является меню или требуется другое "
                "действие (попробуйте mode='click')")
        return result

    async def _collect_dom_sample(self, page: Any) -> dict[str, Any]:
        """Собирает DOM-образец для реестра адаптеров (ТЗ 1.3 п.2).

        Проверяет присутствие характерных маркеров платформ через
        document.querySelector и возвращает короткий срез HTML.
        Ошибки страницы (закрыта/навигация) не должны ломать действие —
        в этом случае возвращаем пустой образец (маршрутизация по URL).
        """
        try:
            from adapters.registry import REGISTRY, _dom_markers_for

            markers: list[str] = []
            for cls in REGISTRY:
                for m in _dom_markers_for(cls):
                    if m not in markers:
                        markers.append(m)
            if not markers:
                return {}
            present = await page.evaluate(
                """(ms) => {
                    const out = [];
                    for (const m of ms) {
                        try { if (document.querySelector(m)) out.push(m); }
                        catch (e) { /* невалидный селектор пропускаем */ }
                    }
                    return out;
                }""",
                markers,
            )
            html_head = ""
            try:
                html_head = await page.evaluate(
                    "() => document.body ? document.body.innerHTML.slice(0, 20000) : ''"
                )
            except Exception:  # noqa: BLE001
                pass
            return {"selectors_present": present, "html": html_head}
        except Exception:  # noqa: BLE001
            log.debug("Не удалось собрать DOM-образец для реестра", exc_info=True)
            return {}

    #: Псевдонимы действий browser_video (системные правки №2, №3).
    #: Нормализуются ДО маршрутизации адаптера.
    #: ВАЖНО: set_theater/wide_mode/theater_on/theater_off — это НЕ toggle,
    #: а идемпотентный theater_set (RC7): повторный запрос того же режима
    #: не должен менять состояние.
    _VIDEO_ACTION_ALIASES = {
        "theater_toggle": "toggle_theater_mode",
        "toggle_theater": "toggle_theater_mode",
        "theater_mode": "toggle_theater_mode",
        "theater": "toggle_theater_mode",
        "set_theater": "theater_set",
        "wide_mode": "theater_set",
        "theater_on": "theater_set",
        "theater_off": "theater_set",
        "back": "go_back",  # на случай, если агент вызовет через video_action
    }

    #: Действия, требующие контекст эпизодов/плейлиста (ТЗ этап 9).
    _EPISODE_ACTIONS = {"episode_list", "episode_next", "episode_prev",
                        "episode_open", "chapter_list", "chapter_seek"}

    async def cmd_video_action(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        action = str(params.get("action") or "").strip().lower()
        value = params.get("value")

        if not action:
            return {"ok": False, "error": "Действие с видео не задано"}

        # «включить/выключить» из action_map настроек может прийти строкой.
        if action == "theater_set" and isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "on", "включить", "да"):
                value = True
            elif low in ("false", "0", "off", "выключить", "нет"):
                value = False

        action = self._VIDEO_ACTION_ALIASES.get(action, action)

        # Действия episodes/playlist обрабатываются на уровне демона
        # (ТЗ этап 9): они работают с ytInitialData и навигацией, а не
        # через платформенный адаптер плеера.
        if action in self._EPISODE_ACTIONS:
            ep_params = dict(params)
            ep_params["value"] = value
            if action == "episode_list":
                return await self.cmd_episode_list(ep_params)
            if action == "episode_next":
                return await self.cmd_episode_next(ep_params)
            if action == "episode_prev":
                return await self.cmd_episode_prev(ep_params)
            if action == "episode_open":
                return await self.cmd_episode_open(ep_params)
            if action == "chapter_list":
                return await self.cmd_chapter_list(ep_params)
            if action == "chapter_seek":
                return await self.cmd_chapter_seek(ep_params)

        # Системная правка №7: «сброс громкости» — это YouTube-фича
        # (после v/volume_up/volume_down плеер возвращает сохранённую
        # пользователем громкость), а не HTML5-поведение. Прямое
        # v.volume=... при этом конфликтует с youtubewebembed и
        # yt-cinematics, которые перехватывают атрибут volume и
        # переписывают его обратно. Поэтому для платформенных
        # адаптеров volume_set идёт отдельным защищённым путём.
        if action in ("volume_set", "volume", "set_volume"):
            try:
                from adapters.registry import get_adapter as _get_adapter

                _dom_sample = await self._collect_dom_sample(page)
                _adapter = _get_adapter(
                    page,
                    page_url=getattr(page, "url", "") or None,
                    page_dom_sample=_dom_sample,
                    video_controller=self.video_controller,
                )
                if type(_adapter).__name__ != "GenericHTML5Adapter" and \
                        hasattr(_adapter, "set_volume"):
                    result = await _adapter.set_volume(value)
                    if isinstance(result, dict):
                        result.setdefault("platform", _adapter.platform)
                        return result
            except ImportError:
                pass

        # --- Маршрутизация через систему адаптеров (ТЗ раздел 2) ---
        try:
            from adapters.registry import get_adapter

            # URL страницы всегда участвует в маршрутизации (ТЗ 1.3 п.1);
            # дополнительно подставляем DOM-маркеры, если они есть на странице
            # (ТЗ 1.3 п.2) — это спасает на embed-плеерах и редиректах.
            dom_sample = await self._collect_dom_sample(page)
            adapter = get_adapter(
                page,
                page_url=getattr(page, "url", "") or None,
                page_dom_sample=dom_sample,
                video_controller=self.video_controller,
            )
            result = await adapter.execute(action, value)
            if isinstance(result, dict):
                result.setdefault("platform", adapter.platform)
                return result
            return {"ok": True, "action": action, "result": result,
                    "platform": adapter.platform}
        except ImportError:
            log.warning("Адаптеры недоступны; работа через VideoController напрямую")
        except Exception as exc:  # noqa: BLE001
            log.exception("Адаптер '%s' завершился ошибкой", action)
            return {
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
                "hint": getattr(exc, "details", {}).get(
                    "hint", "browser_screenshot для диагностики"),
            }

        # --- Legacy-путь: прямой VideoController без адаптеров ---
        if self.video_controller:
            result = await self.video_controller.action(page, action, value)

            # Совместимость со старым VideoController (строковый результат)
            # и с новой редакцией (словарь).
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                is_error = result.lstrip().startswith(("❌", "⚠️"))
                return {
                    "ok": not is_error,
                    "action": action,
                    "result": result,
                }
            return {
                "ok": True,
                "action": action,
                "result": result,
            }

        return {
            "ok": False,
            "error": "VideoController не загружен",
        }

    # ------------------------------------------------------------------
    # Нативная навигация по эпизодам YouTube (ТЗ этап 9)
    # ------------------------------------------------------------------
    _EPISODE_LIST_JS = r"""
    () => {
      const out = {list_id: null, video_id: null, index: null, episodes: []};
      try {
        const m = location.href.match(/[?&]v=([A-Za-z0-9_-]{11})/);
        out.video_id = m ? m[1] : null;
        const d = window.ytInitialData;
        if (!d) return out;
        const contents = (((d.contents || {}).twoColumnWatchNextResults || {})
          .playlist || {}).playlist || {};
        out.list_id = contents.listId || null;
        const items = contents.results || [];
        items.forEach((it, i) => {
          const r = it.playlistVideoRenderer || it.compactVideoRenderer || {};
          if (!r.videoId) return;
          let title = '';
          try {
            title = ((r.title || {}).runs || []).map(x => x.text).join('');
          } catch (e) { title = ''; }
          if (r.videoId === out.video_id) out.index = i;
          out.episodes.push({index: i, video_id: r.videoId,
                             title: String(title || '').slice(0, 200)});
        });
      } catch (e) { /* не YouTube */ }
      return out;
    }
    """

    def _watch_url(self, video_id: str, list_id=None, index=None) -> str:
        url = "https://www.youtube.com/watch?v=" + video_id
        if list_id:
            url += "&list=" + list_id
        if index is not None:
            url += "&index=" + str(int(index))
        return url

    async def cmd_episode_list(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        data = await page.evaluate(self._EPISODE_LIST_JS) or {}
        episodes = data.get("episodes") or []
        if not episodes:
            return {
                "ok": False,
                "error": "NO_EPISODE_CONTEXT",
                "message": ("На текущей странице не найдена очередь эпизодов/"
                            "плейлист (ytInitialData.playlist пуст). Откройте "
                            "видео из плейлиста или включите radio."),
                "video_id": data.get("video_id"),
                "list_id": data.get("list_id"),
            }
        return {
            "ok": True,
            "action": "episode_list",
            "url": page.url,
            "video_id": data.get("video_id"),
            "list_id": data.get("list_id"),
            "index": data.get("index"),
            "count": len(episodes),
            "episodes": episodes[:100],
        }

    async def _episode_step(self, params, delta: int) -> dict[str, Any]:
        page = await self.require_page(params)
        data = await page.evaluate(self._EPISODE_LIST_JS) or {}
        episodes = data.get("episodes") or []
        if not episodes:
            return {
                "ok": False,
                "error": "NO_EPISODE_CONTEXT",
                "message": ("Нет контекста episodes/playlist. Сначала "
                            "episode_list или browser_get_context."),
                "suggested_safe_actions": [
                    {"tool": "browser_video", "args": {"action": "episode_list"}},
                    {"tool": "browser_get_context"},
                ],
            }
        cur = data.get("index")
        if cur is None:
            cur = 0
        nxt = cur + delta
        if nxt < 0 or nxt >= len(episodes):
            return {
                "ok": False,
                "error": "EPISODE_OUT_OF_RANGE",
                "message": "Эпизод %d вне очереди (0..%d)." % (nxt, len(episodes) - 1),
                "index": cur,
                "count": len(episodes),
            }
        target = episodes[nxt]
        url = self._watch_url(target["video_id"], data.get("list_id"), nxt)
        result = await self.cmd_navigate({"url": url})
        verified = result.get("url", "") and target["video_id"] in result.get("url", "")
        return {
            **result,
            "action": "episode_next" if delta > 0 else "episode_prev",
            "from_index": cur,
            "index": nxt,
            "video_id": target["video_id"],
            "list_id": data.get("list_id"),
            "title": target.get("title", ""),
            "verified": bool(verified),
        }

    async def cmd_episode_next(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._episode_step(params, +1)

    async def cmd_episode_prev(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._episode_step(params, -1)

    async def cmd_episode_open(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        data = await page.evaluate(self._EPISODE_LIST_JS) or {}
        episodes = data.get("episodes") or []
        if not episodes:
            return {
                "ok": False,
                "error": "NO_EPISODE_CONTEXT",
                "message": ("Нет контекста episodes/playlist. Сначала "
                            "episode_list или browser_get_context."),
            }
        value = params.get("value")
        target = None
        try:
            idx = int(value)
            if 0 <= idx < len(episodes):
                target = episodes[idx]
        except (TypeError, ValueError):
            pass
        if target is None and isinstance(value, str):
            for ep in episodes:
                if ep.get("video_id") == value:
                    target = ep
                    break
        if target is None:
            return {
                "ok": False,
                "error": "EPISODE_NOT_FOUND",
                "message": "Эпизод %r не найден в очереди (0..%d)." % (
                    value, len(episodes) - 1),
            }
        url = self._watch_url(target["video_id"], data.get("list_id"),
                              target["index"])
        result = await self.cmd_navigate({"url": url})
        return {
            **result,
            "action": "episode_open",
            "index": target["index"],
            "video_id": target["video_id"],
            "list_id": data.get("list_id"),
            "title": target.get("title", ""),
            "verified": target["video_id"] in (result.get("url") or ""),
        }

    # ------------------------------------------------------------------
    # Главы (chapters) — ТЗ этап 9: chapter_list / chapter_seek
    # Источник: ytInitialPlayerResponse.playerDescriptors.chapteredActions
    # (основной) и ссылки вида «MM:SS Название» в описании (fallback).
    # ------------------------------------------------------------------
    _CHAPTER_LIST_JS = r"""
    () => {
      const out = {chapters: [], video_id: null, source: null};
      try {
        const pr = window.ytInitialPlayerResponse;
        if (pr && pr.videoDetails) out.video_id = pr.videoDetails.videoId || null;
        const ds = (((pr || {}).playerDescriptors || [])[0] || {})
                     .chapteredActions;
        const watch = (ds || {}).watchNextChapterDescriptor;
        if (watch && Array.isArray(watch.chapters)) {
          out.source = 'ytInitialPlayerResponse';
          watch.chapters.forEach((c, i) => {
            let title = '';
            try {
              title = ((c.title || {}).simpleText) ||
                      (((c.title || {}).runs || [])[0] || {}).text || '';
            } catch (e) {}
            out.chapters.push({index: i, title: title,
                               start_seconds: c.time || 0});
          });
        }
      } catch (e) {}
      if (!out.chapters.length) {
        // fallback: таймкоды в описании (#chapter или «0:00 Title»)
        try {
          const spans = document.querySelectorAll(
            '#description a.yt-simple-endpoint, #structured-description a');
          const seen = {};
          spans.forEach(a => {
            const t = (a.textContent || '').trim();
            const m = t.match(/^(\d+):([0-5]\d)(?::([0-5]\d))?\s+(.+)$/);
            if (!m) return;
            const secs = m[3] !== undefined
              ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3])
              : (+m[1]) * 60 + (+m[2]);
            const title = m[4].trim();
            const key = secs + '|' + title;
            if (seen[key]) return;
            seen[key] = true;
            out.chapters.push({index: out.chapters.length,
                               title: title, start_seconds: secs});
          });
          if (out.chapters.length) out.source = 'description-timestamps';
        } catch (e) {}
      }
      return out;
    }
    """

    async def cmd_chapter_list(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        data = await page.evaluate(self._CHAPTER_LIST_JS) or {}
        chapters = data.get("chapters") or []
        if not chapters:
            return {
                "ok": False,
                "error": "NO_CHAPTER_CONTEXT",
                "message": ("На текущем видео главы не найдены "
                            "(ни playerDescriptors, ни таймкоды в описании)."),
                "video_id": data.get("video_id"),
                "suggested_safe_actions": [
                    {"tool": "browser_get_content"},
                    {"tool": "browser_video", "args": {"action": "seek"}},
                ],
            }
        return {
            "ok": True,
            "action": "chapter_list",
            "url": page.url,
            "video_id": data.get("video_id"),
            "source": data.get("source"),
            "count": len(chapters),
            "chapters": chapters[:200],
        }

    async def cmd_chapter_seek(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        data = await page.evaluate(self._CHAPTER_LIST_JS) or {}
        chapters = data.get("chapters") or []
        if not chapters:
            return {
                "ok": False,
                "error": "NO_CHAPTER_CONTEXT",
                "message": ("Главы не найдены — используйте seek с "
                            "явным временем в секундах."),
                "suggested_safe_actions": [
                    {"tool": "browser_video",
                     "args": {"action": "seek", "value": 60}},
                    {"tool": "browser_video",
                     "args": {"action": "chapter_list"}},
                ],
            }
        value = params.get("value")
        if value is None:
            value = params.get("target")
        target = None
        # 1) индекс главы (int или int-строка)
        try:
            idx = int(value)
            if 0 <= idx < len(chapters):
                target = chapters[idx]
        except (TypeError, ValueError):
            target = None
        # 2) точное совпадение заголовка
        if target is None and isinstance(value, str):
            low = value.strip().lower()
            for ch in chapters:
                if ch.get("title", "").strip().lower() == low:
                    target = ch
                    break
        # 3) fuzzy по заголовку
        if target is None and isinstance(value, str) and value.strip():
            low = value.strip().lower()
            best = None
            for ch in chapters:
                t = ch.get("title", "").strip().lower()
                if low in t or t in low:
                    best = ch
                    break
            target = best
        # 4) время в секундах
        if target is None:
            try:
                secs = float(value)
                target = {"index": None, "title": "",
                          "start_seconds": secs, "synthetic_time": True}
            except (TypeError, ValueError):
                target = None
        if target is None:
            return {
                "ok": False,
                "error": "CHAPTER_NOT_FOUND",
                "message": "Глава %r не найдена (0..%d)." % (
                    value, len(chapters) - 1),
                "chapters": [c.get("title") for c in chapters[:50]],
            }
        seek_result = await self.cmd_video_action(
            {"action": "seek", "value": target["start_seconds"]})
        return {
            **(seek_result if isinstance(seek_result, dict) else
               {"ok": bool(seek_result), "result": seek_result}),
            "action": "chapter_seek",
            "chapter_index": target.get("index"),
            "chapter_title": target.get("title", ""),
            "start_seconds": target["start_seconds"],
            "video_id": data.get("video_id"),
            "chapter_source": data.get("source"),
        }

    async def cmd_websocket_events(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.websocket_monitor:
            return {"ok": False, "error": "WebSocketMonitor не загружен"}

        limit = max(1, min(int(params.get("limit", 50)), 100))
        return {
            "ok": True,
            "events": self.websocket_monitor.snapshot(limit),
        }

    async def cmd_network_errors(
        self,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        return {"ok": True, "events": self._network_events[-100:]}

    async def cmd_dialog_state(
        self,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        dialog = self._pending_dialog
        if dialog is None:
            return {"ok": True, "pending": False}

        return {
            "ok": True,
            "pending": True,
            "type": str(getattr(dialog, "type", "unknown")),
            "message": str(getattr(dialog, "message", ""))[:1000],
        }

    async def cmd_dialog_resolve(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        future = self._pending_dialog_future
        if self._pending_dialog is None or future is None:
            return {"ok": False, "error": "Нет ожидающего диалога"}

        if future.done():
            return {"ok": False, "error": "Диалог уже обрабатывается"}

        action = str(params.get("action") or "").lower()
        if action not in ("accept", "dismiss"):
            return {
                "ok": False,
                "error": "action должен быть accept или dismiss",
            }

        prompt_text = str(params.get("prompt_text") or "")
        future.set_result((action, prompt_text))
        return {"ok": True, "action": action}


class TCPServer:
    """TCP JSON-lines сервер на localhost с токеном аутентификации."""

    def __init__(self, controller: BrowserController):
        self.controller = controller
        self.server = None
        self.port = 0
        self.token = secrets.token_urlsafe(32)
        self.client_slots = asyncio.Semaphore(MAX_CLIENTS)

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self.handle_client,
            host="127.0.0.1",
            port=0,
            limit=MAX_FRAME_BYTES,
        )

        sockets = self.server.sockets or []
        if not sockets:
            raise RuntimeError("TCP server не получил слушающий сокет")

        self.port = int(sockets[0].getsockname()[1])
        self.controller.state.write(
            pid=os.getpid(),
            status="running",
            tcp_host="127.0.0.1",
            tcp_port=self.port,
            auth_token=self.token,
            profile_dir=str(self.controller.profile_dir.resolve()),
            started_at=time.time(),
            updated_at=utc_now(),
        )
        log.info("Listening on 127.0.0.1:%d", self.port)

    async def send(
        self,
        writer: asyncio.StreamWriter,
        message: dict[str, Any],
        write_lock: asyncio.Lock,
    ) -> None:
        data = json_bytes(message) + b"\n"

        if len(data) > MAX_RESPONSE_BYTES:
            data = json_bytes({
                "id": message.get("id"),
                "ok": False,
                "error": "Ответ превышает лимит размера",
            }) + b"\n"

        try:
            async with write_lock:
                writer.write(data)
                await writer.drain()
        except Exception:
            # Разрыв клиента не останавливает команду/браузер.
            pass

    async def _run_request(
        self,
        request: dict[str, Any],
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
    ) -> None:
        request_id = request.get("id")

        try:
            result = await self.controller.dispatch(request)
        except Exception as exc:
            log.exception("Необработанная ошибка dispatch")
            result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        response = dict(result)
        response["id"] = request_id
        await self.send(writer, response, write_lock)

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        async with self.client_slots:
            authenticated = False
            write_lock = asyncio.Lock()
            request_tasks: set[asyncio.Task] = set()

            try:
                try:
                    raw = await asyncio.wait_for(
                        reader.readline(),
                        timeout=10,
                    )
                except asyncio.TimeoutError:
                    return
                except (ValueError, asyncio.LimitOverrunError):
                    return

                if not raw or len(raw) > MAX_FRAME_BYTES:
                    return

                try:
                    auth_request = json_loads(raw)
                except Exception:
                    return

                if (
                    not isinstance(auth_request, dict)
                    or auth_request.get("cmd") != "auth"
                ):
                    return

                supplied_token = str(auth_request.get("token") or "")
                if not secrets.compare_digest(supplied_token, self.token):
                    await self.send(
                        writer,
                        {
                            "id": auth_request.get("id"),
                            "ok": False,
                            "error": "Ошибка авторизации",
                        },
                        write_lock,
                    )
                    return

                authenticated = True
                self.controller._clients[writer] = write_lock

                await self.send(
                    writer,
                    {
                        "id": auth_request.get("id"),
                        "ok": True,
                        "protocol": PROTOCOL_VERSION,
                        "pid": os.getpid(),
                    },
                    write_lock,
                )

                while True:
                    try:
                        raw = await reader.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        await self.send(
                            writer,
                            {
                                "id": None,
                                "ok": False,
                                "error": "Запрос превышает лимит размера",
                            },
                            write_lock,
                        )
                        break

                    if not raw:
                        break
                    if len(raw) > MAX_FRAME_BYTES:
                        await self.send(
                            writer,
                            {
                                "id": None,
                                "ok": False,
                                "error": "Запрос превышает лимит размера",
                            },
                            write_lock,
                        )
                        break

                    try:
                        request = json_loads(raw)
                    except Exception:
                        await self.send(
                            writer,
                            {
                                "id": None,
                                "ok": False,
                                "error": "Некорректный JSON",
                            },
                            write_lock,
                        )
                        continue

                    if not isinstance(request, dict):
                        await self.send(
                            writer,
                            {
                                "id": None,
                                "ok": False,
                                "error": "Запрос должен быть JSON-объектом",
                            },
                            write_lock,
                        )
                        continue

                    if len(request_tasks) >= MAX_PENDING_REQUESTS_PER_CLIENT:
                        done, pending = await asyncio.wait(
                            request_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        request_tasks.difference_update(done)
                        request_tasks.update(pending)

                    task = asyncio.create_task(
                        self._run_request(request, writer, write_lock)
                    )
                    request_tasks.add(task)
                    task.add_done_callback(request_tasks.discard)

                    if request.get("cmd") == "stop":
                        # Дать задаче отправить подтверждение; затем больше
                        # запросов от этого клиента не принимаем.
                        with contextlib.suppress(Exception):
                            await task
                        break

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка TCP-клиента")
            finally:
                if authenticated:
                    self.controller._clients.pop(writer, None)

                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()

                # Уже принятые действия браузера не отменяем из-за разрыва
                # клиента. Их ответы просто не дойдут до закрытого соединения.

    async def serve(self) -> None:
        if self.server is None:
            raise RuntimeError("TCPServer не запущен")

        async with self.server:
            serve_task = asyncio.create_task(self.server.serve_forever())
            stop_task = asyncio.create_task(self.controller._stop_event.wait())

            done, pending = await asyncio.wait(
                (serve_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            for task in done:
                with contextlib.suppress(
                    asyncio.CancelledError,
                    Exception,
                ):
                    task.result()


async def run_daemon(profile: Path, headless: bool) -> None:
    controller = BrowserController(profile, headless)
    controller.state.write(
        pid=os.getpid(),
        status="starting",
        profile_dir=str(profile.resolve()),
        started_at=time.time(),
    )

    watchdog_task = None

    try:
        await controller.start()

        tcp_server = TCPServer(controller)
        await tcp_server.start()

        watchdog_task = asyncio.create_task(controller.watchdog())
        await tcp_server.serve()

    except Exception as exc:
        controller.state.write(
            pid=os.getpid(),
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            updated_at=utc_now(),
        )
        log.exception("Демон завершился с ошибкой")
        raise

    finally:
        if watchdog_task:
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task

        # При штатном browser_stop контекст уже закрыт в explicit_stop.
        # Здесь не закрываем браузер по таймауту или потере клиента.
        if controller.stopping:
            controller.state.clear()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone Camoufox browser daemon"
    )
    parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
        help="Путь к persistent-профилю браузера",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Запустить браузер без видимого окна",
    )
    args = parser.parse_args()

    profile = Path(args.profile).expanduser().resolve()
    lock_path = profile / ".browserd.lock"
    lock_handle = acquire_profile_lock(lock_path)

    if lock_handle is None:
        log.error(
            "Профиль уже занят другим процессом. Второй демон не запущен."
        )
        return 2

    try:
        asyncio.run(run_daemon(profile, bool(args.headless)))
        return 0
    except KeyboardInterrupt:
        log.warning(
            "Получен KeyboardInterrupt. Это ручное/системное завершение, "
            "а не команда MCP browser_stop."
        )
        return 130
    except Exception:
        # Полный traceback должен попасть в browserd.log.
        log.exception("Не удалось запустить или обслуживать демон")
        return 1
    finally:
        release_profile_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())