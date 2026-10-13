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
PROTOCOL_VERSION = 4

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

    async def tab_list(self) -> list[dict[str, Any]]:
        result = []
        pages = self.pages

        for index, page in enumerate(pages):
            title = ""
            with contextlib.suppress(Exception):
                title = await page.title()

            tab_id = self._page_id(page)
            try:
                url = str(page.url or "")
            except Exception:
                url = ""

            try:
                closed = bool(page.is_closed())
            except Exception:
                closed = False

            result.append({
                "index": index,
                "tab_id": tab_id,
                "url": url,
                "title": title,
                "active": tab_id == self.active_tab_id,
                "closed": closed,
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
        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "http_status": status,
            "http_error": bool(status and status >= 400),
            "tab_id": self._page_id(page),
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

        return {
            "ok": True,
            "index": self.active_index,
            "tab_id": self.active_tab_id,
            "url": page.url,
            "http_status": status,
            "tabs": await self.tab_list(),
        }

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

    async def cmd_click_text(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        text = str(params.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Текст не задан"}

        index = int(params.get("index", 0))
        timeout_ms = max(1000, min(int(params.get("timeout", 10)), 60) * 1000)

        locator = page.get_by_text(
            text,
            exact=bool(params.get("exact", False)),
        )
        count = await locator.count()
        if count == 0:
            return {"ok": False, "error": "Элемент с таким текстом не найден"}

        if index < 0 or index >= count:
            return {
                "ok": False,
                "error": f"Индекс {index} вне диапазона; найдено: {count}",
            }

        target = locator.nth(index)
        if not await target.is_visible():
            return {"ok": False, "error": "Найденный элемент скрыт"}
        if not await target.is_enabled():
            return {"ok": False, "error": "Найденный элемент отключён"}

        await target.scroll_into_view_if_needed(timeout=timeout_ms)
        await target.click(timeout=timeout_ms)

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
        if await locator.count() == 0:
            return {"ok": False, "error": "Элемент не найден"}
        if not await locator.is_visible():
            return {"ok": False, "error": "Элемент скрыт"}
        if not await locator.is_enabled():
            return {"ok": False, "error": "Элемент отключён"}

        await locator.scroll_into_view_if_needed(timeout=8000)
        await locator.click(timeout=8000)

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
        if await locator.count() == 0:
            return {"ok": False, "error": "Элемент не найден"}
        if not await locator.is_visible():
            return {"ok": False, "error": "Элемент скрыт"}
        if not await locator.is_enabled():
            return {"ok": False, "error": "Элемент отключён"}

        await locator.scroll_into_view_if_needed(timeout=8000)
        await locator.click(timeout=8000)

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

    async def cmd_video_action(self, params: dict[str, Any]) -> dict[str, Any]:
        page = await self.require_page(params)
        action = str(params.get("action") or "").strip().lower()
        value = params.get("value")

        if not action:
            return {"ok": False, "error": "Действие с видео не задано"}

        # --- Маршрутизация через систему адаптеров (ТЗ раздел 2) ---
        try:
            from adapters.registry import get_adapter

            adapter = get_adapter(
                page, video_controller=self.video_controller)
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