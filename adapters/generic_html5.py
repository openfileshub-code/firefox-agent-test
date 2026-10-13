"""Базовый fallback-адаптер: любой HTML5 <video> на произвольной странице.

Используется реестром, когда платформа не распознана (ТЗ 1.3), а также
как источник graceful fallback-действий для всех остальных адаптеров
(делегирование через ``self._html5`` в BaseVideoAdapter).

Особенность: умеет искать <video> и внутри доступных iframe
(cross-origin iframe недоступен — тогда эмуляция клика по области видео).
"""

from __future__ import annotations

import logging
from typing import Any

from adapters.base import BaseVideoAdapter, ElementNotFoundError

logger = logging.getLogger("FirefoxMCP_Adapters.GenericHTML5")


class GenericHTML5Adapter(BaseVideoAdapter):
    platform = "generic_html5"
    domain_hints = ()  # матчится только как последний fallback

    def __init__(self, page: Any, video_controller: Any = None):
        super().__init__(page, video_controller)
        cfg = self.selectors or {}
        self.video_selector: str = cfg.get("video_selector", "video")

    # ------------------------------------------------------------------
    # Поиск <video>: основной документ -> iframe
    # ------------------------------------------------------------------
    async def _has_video(self) -> bool:
        try:
            if await self.page.locator(self.video_selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                if await frame.locator(self.video_selector).count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _ensure_video(self) -> None:
        if not await self._has_video():
            raise ElementNotFoundError(
                "На странице не найден элемент <video> (ни в документе, "
                "ни в доступных iframe)",
                tried_selectors=[self.video_selector],
            )

    # ------------------------------------------------------------------
    # Действия: всё делегируем HTML5-контроллеру; без него — клик-эмуляция
    # ------------------------------------------------------------------
    async def play(self) -> dict[str, Any]:
        await self._ensure_video()
        if self.video_controller is None:
            await self._click_center_overlay()
            return {"ok": True, "action": "play", "method": "overlay_click",
                    "adapter": type(self).__name__}
        return await self._html5("play")

    async def pause(self) -> dict[str, Any]:
        await self._ensure_video()
        return await self._html5("pause")

    async def set_volume(self, percent: float) -> dict[str, Any]:
        await self._ensure_video()
        return await self._html5("volume_set", percent)

    async def seek(self, seconds: float) -> dict[str, Any]:
        await self._ensure_video()
        return await self._html5("seek_to", seconds)

    async def state(self) -> dict[str, Any]:
        await self._ensure_video()
        return await self._html5("state")

    async def fullscreen_enter(self) -> dict[str, Any]:
        await self._ensure_video()
        return await self._html5("fullscreen_enter")

    async def set_quality(self, label: str) -> dict[str, Any]:
        """Качество для неизвестного плеера: пробуем универсальное меню.

        Шестерёнка/три точки по aria-label, затем пункт с текстом метки.
        Если ничего нет — честная ошибка вместо тихого no-op.
        """
        gear = await self._find_by_aria(
            ["Settings", "Настройки", "More options", "Ещё"], timeout_ms=1500)
        if gear is None:
            return {
                "ok": False,
                "error": "NotSupportedByAdapter",
                "message": (
                    "GenericHTML5: кнопка настроек качества не найдена; "
                    f"для '{label}' используйте адаптер конкретной платформы"
                ),
                "adapter": type(self).__name__,
            }
        await gear.click(timeout=2000)
        item = await self._find_by_text([str(label)], timeout_ms=1500)
        if item is None:
            await self.page.keyboard.press("Escape")
            raise ElementNotFoundError(
                f"GenericHTML5: пункт качества '{label}' не найден в меню")
        await item.click(timeout=2000)
        await self.page.keyboard.press("Escape")
        return {"ok": True, "action": "set_quality", "requested": label,
                "method": "generic_menu_heuristic"}
