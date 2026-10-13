"""Базовый класс адаптера видеохостинга и загрузчик конфигурации селекторов.

Принципы (ТЗ раздел 1):
* UI Automation в приоритете над прямыми JS-вызовами, когда последние
  не гарантируют стабильности;
* поиск элементов по семантике (aria-label / role / текст), CSS-классы —
  только как дополнительный сигнал и только из config/player_selectors.json;
* graceful fallback: неподдерживаемое действие -> понятная ошибка или
  делегирование базовому HTML5-контроллеру.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("FirefoxMCP_Adapters")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "player_selectors.json"

_selector_cache: dict[str, Any] | None = None


def load_selector_config() -> dict[str, Any]:
    """Читает config/player_selectors.json (с кэшированием).

    При отсутствии/битости файла возвращает пустой словарь: адаптеры
    тогда полагаются только на семантические поиски (aria/role/текст),
    что допустимо архитектурно.
    """
    global _selector_cache
    if _selector_cache is not None:
        return _selector_cache
    try:
        _selector_cache = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось загрузить %s (%s); использую только семантику", CONFIG_PATH, exc)
        _selector_cache = {}
    return _selector_cache


class AdapterError(RuntimeError):
    """Базовая ошибка адаптера с человекочитаемым сообщением."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": type(self).__name__,
            "message": str(self),
            **self.details,
        }


class ElementNotFoundError(AdapterError):
    """Элемент не найден; предлагается скриншот для диагностики (ТЗ 5.3)."""

    def __init__(self, message: str, **details: Any):
        details.setdefault("hint", "Рекомендуется browser_screenshot для диагностики")
        super().__init__(message, **details)


class BaseVideoAdapter:
    """Абстрактный интерфейс видеоплеера.

    Наследники обязаны задать ``platform`` и могут переопределить любые
    методы. Не переопределённые методы падают в HTML5-реализацию базового
    класса или возвращают структурированную ошибку «не поддерживается».
    """

    platform: str = "generic_html5"
    #: Платформы, которым адаптер соответствует по URL/DOM. Реестр читает это поле.
    domain_hints: tuple[str, ...] = ()

    def __init__(self, page: Any, video_controller: Any = None):
        self.page = page
        self.video_controller = video_controller
        cfg = load_selector_config()
        self.selectors: dict[str, Any] = cfg.get(self.platform, {})

    # ------------------------------------------------------------------
    # Хелперы поиска (семантика вместо селекторов)
    # ------------------------------------------------------------------
    async def _find_by_aria(self, labels: list[str], timeout_ms: int = 3000) -> Any:
        """Находит первый доступный элемент по aria-label из списка.

        Реализация (исправление бага "неподдерживаемый селектор"):
        * используется Playwright-овый ``get_by_label`` — он корректно
          работает даже если браузер/движок не принимает CSS-флаг
          регистронезависимости ``[attr="v" i]`` (на некоторых сборках
          Firefox/Camoufox такой селектор бросает SyntaxError);
        * сначала точное совпадение, затем регистронезависимое;
        * общий бюджет ожидания распределяется между метками, чтобы
          метод возвращал ``None`` примерно за ``timeout_ms``, а не
          ``timeout_ms * len(labels)`` (важно для требования ТЗ
          «не нашёл кнопку настроек за 3 секунды -> ошибка»).
        """
        labels = [l for l in labels if l]
        if not labels:
            return None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        per_attempt = max(0.25, (timeout_ms / 1000.0) / (2 * len(labels)))
        for exact in (True, False):
            for label in labels:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    locator = self.page.get_by_label(label, exact=exact).first
                    await locator.wait_for(
                        state="visible", timeout=min(per_attempt, remaining) * 1000
                    )
                    return locator
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def _find_by_text(self, texts: list[str], selector: str = "", timeout_ms: int = 2000) -> Any:
        """Поиск элемента, чей текст содержит одну из подстрок (регистронезависимо)."""
        base = selector or "button, [role='button'], [role='menuitem'], li, span, div"
        for text in texts:
            try:
                locator = self.page.locator(base).filter(has_text=text).first
                await locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _click_center_overlay(self) -> None:
        """Эмуляция клика по центру видео-оверлея (play/pause жестом мыши)."""
        video = self.page.locator("video").first
        try:
            await video.click(timeout=3000)
        except Exception:
            box = await self.page.evaluate(
                "() => { const v=document.querySelector('video');"
                "if(!v) return null; const r=v.getBoundingClientRect();"
                "return {x:r.x+r.width/2, y:r.y+r.height/2}; }"
            )
            if not box:
                raise ElementNotFoundError("Видеоэлемент не найден для клика")
            await self.page.mouse.click(box["x"], box["y"])

    # ------------------------------------------------------------------
    # Базовые HTML5-действия (fallback из коробки)
    # ------------------------------------------------------------------
    async def play(self) -> dict[str, Any]:
        return await self._html5("play")

    async def pause(self) -> dict[str, Any]:
        return await self._html5("pause")

    async def set_volume(self, percent: float) -> dict[str, Any]:
        return await self._html5("volume_set", percent)

    async def seek(self, seconds: float) -> dict[str, Any]:
        return await self._html5("seek_to", seconds)

    async def state(self) -> dict[str, Any]:
        return await self._html5("state")

    async def _html5(self, action: str, value: Any = None) -> dict[str, Any]:
        """Делегирование базовому VideoController (graceful fallback)."""
        if self.video_controller is None:
            return {
                "ok": False,
                "error": "NoFallbackController",
                "message": (
                    f"Действие '{action}' не реализовано в адаптере "
                    f"{type(self).__name__} и нет HTML5-контроллера для fallback"
                ),
            }
        result = await self.video_controller.action(self.page, action, value)
        if isinstance(result, dict):
            result.setdefault("adapter", type(self).__name__)
            result.setdefault("delegated_action", action)
        return result

    # ------------------------------------------------------------------
    # Платформенно-специфичные действия: по умолчанию не поддерживаются
    # ------------------------------------------------------------------
    async def set_quality(self, label: str) -> dict[str, Any]:
        return self._unsupported("set_quality")

    async def toggle_theater_mode(self) -> dict[str, Any]:
        return self._unsupported("toggle_theater_mode")

    async def next_video(self) -> dict[str, Any]:
        return self._unsupported("next_video")

    async def toggle_chat_visibility(self) -> dict[str, Any]:
        return self._unsupported("toggle_chat_visibility")

    async def fullscreen_enter(self) -> dict[str, Any]:
        return await self._html5("fullscreen_enter")

    async def detect_login_required(self) -> bool:
        """True, если страница требует авторизации (детектор экранов входа)."""
        return False

    def _unsupported(self, name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "NotSupportedByAdapter",
            "message": (
                f"Действие '{name}' не поддерживается адаптером "
                f"{type(self).__name__} ({self.platform})"
            ),
            "adapter": type(self).__name__,
        }

    # ------------------------------------------------------------------
    # Диспетчер действий (маршрутизация VideoAction -> методы адаптера)
    # ------------------------------------------------------------------
    async def execute(self, action: str, value: Any = None) -> dict[str, Any]:
        action = (action or "").strip().lower()
        handler = {
            "play": lambda: self.play(),
            "pause": lambda: self.pause(),
            "volume": lambda: self.set_volume(value),
            "set_volume": lambda: self.set_volume(value),
            "seek": lambda: self.seek(value),
            "state": lambda: self.state(),
            "set_quality": lambda: self.set_quality(str(value or "")),
            "toggle_theater_mode": lambda: self.toggle_theater_mode(),
            "theater": lambda: self.toggle_theater_mode(),
            "next_video": lambda: self.next_video(),
            "toggle_chat_visibility": lambda: self.toggle_chat_visibility(),
            "fullscreen_enter": lambda: self.fullscreen_enter(),
            "detect_login": lambda: self.detect_login_required(),
        }.get(action)

        if handler is None:
            # Fallback на HTML5-контроллер для стандартных действий.
            result = await self._html5(action, value)
            if isinstance(result, dict) and result.get("error"):
                result["message"] = (
                    f"{result.get('message', '')} Действие '{action}' неизвестно "
                    f"адаптеру {type(self).__name__}."
                ).strip()
            return result

        result = await handler()
        if action == "detect_login":
            result = {"ok": True, "login_required": bool(result), "adapter": type(self).__name__}
        if isinstance(result, dict):
            result.setdefault("adapter", type(self).__name__)
        return result
