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


BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "playwright_profile"
STATE_FILE = PROFILE_DIR / ".browserd_state.json"
PROTOCOL_VERSION = 4
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
    return None


VIDEO_JS_RE = re.compile(
    r"\.(play|pause|currentTime\s*=|volume\s*=|muted\s*=|load\s*$)"
    r"|querySelector(?:All)?\s*$\s*['\"]video",
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


async def call(command: str, params: Optional[dict] = None):
    return await connector.send(command, params)


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
async def browser_navigate(url: str, new_tab: bool = False) -> dict:
    error = validate_url(url)
    if error:
        raise ValueError(error)
    if new_tab:
        return await call("new_tab", {"url": url})
    return await call("navigate", {"url": url})


@mcp.tool()
async def browser_click_text(text: str, index: int = 0) -> dict:
    return await call("click_text", {"text": text, "index": index})


@mcp.tool()
async def browser_press_key(key: str) -> dict:
    return await call("press_key", {"key": key})


@mcp.tool()
async def browser_scroll(direction: str = "down", amount: int = 800) -> dict:
    return await call("scroll", {"direction": direction, "amount": amount})


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
async def browser_new_tab(url: str = "") -> dict:
    if url:
        error = validate_url(url)
        if error:
            raise ValueError(error)
    return await call("new_tab", {"url": url})


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
    action: str, value: Optional[float] = None
) -> dict:
    return await call("video_action", {"action": action, "value": value})


@mcp.tool()
async def browser_search_on_page(
    pattern: str, regex: bool = False
) -> dict:
    return await call("search_on_page", {
        "pattern": pattern, "regex": regex
    })


@mcp.tool()
async def browser_find_link(pattern: str = "") -> dict:
    return await call("find_link", {"pattern": pattern})


if __name__ == "__main__":
    mcp.run(transport="stdio")