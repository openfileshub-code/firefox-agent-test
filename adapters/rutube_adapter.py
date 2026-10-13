"""Адаптер Rutube (ТЗ 1.2Г).

Контекст: у Rutube есть JSON API для embed-плеера, но на самой странице
работает обычный плеер с HTML5 <video>. Приоритет — управление через
<video>-элемент; качество — через меню настроек (шестерёнка или «три
точки») с поиском пункта по тексту метки.

Все селекторы — из config/player_selectors.json (секция "rutube").
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from adapters.base import BaseVideoAdapter, ElementNotFoundError

logger = logging.getLogger("FirefoxMCP_Adapters.Rutube")


class RutubeAdapter(BaseVideoAdapter):
    platform = "rutube"
    domain_hints = ("rutube.ru",)

    # ------------------------------------------------------------------
    # Play/Pause: приоритет HTML5 <video>, fallback клик по оверлею
    # ------------------------------------------------------------------
    async def play(self) -> dict[str, Any]:
        result = await self._html5("play")
        if not result.get("ok"):
            logger.info("Rutube: HTML5 play не удался — клик по оверлею")
            await self._click_center_overlay()
            result = {"ok": True, "action": "play", "method": "overlay_click",
                      "adapter": type(self).__name__}
        return result

    async def pause(self) -> dict[str, Any]:
        return await self._html5("pause")

    # ------------------------------------------------------------------
    # Качество: шестерёнка / три точки -> список качеств
    # ------------------------------------------------------------------
    async def set_quality(self, label: str) -> dict[str, Any]:
        if not label:
            return {"ok": False, "error": "ValueRequired",
                    "message": "set_quality требует метку качества, напр. '480p'"}

        gear = await self._find_by_aria(
            self.selectors.get(
                "settings_button_aria", ["Settings", "Настройки", "Ещё"]),
            timeout_ms=2000)
        if gear is None:
            sel = self.selectors.get(
                "settings_button_selector",
                "[class*='settings' i][role='button']")
            candidate = self.page.locator(sel).first
            try:
                await candidate.wait_for(state="visible", timeout=1500)
                gear = candidate
            except Exception:  # noqa: BLE001
                gear = None
        if gear is None:
            raise ElementNotFoundError(
                "Rutube: кнопка настроек (шестерёнка/три точки) не найдена")

        await gear.click(timeout=2000)

        # Пункт «Качество», если меню двухуровневое.
        q_entry = await self._find_by_text(
            self.selectors.get("quality_menu_text", ["Качество", "Quality"]),
            selector="[role='menuitem'], [role='option'], li, button",
            timeout_ms=1200)
        if q_entry is not None:
            await q_entry.click(timeout=1500)
            await asyncio.sleep(0.3)

        item = await self._find_by_text(
            [str(label)],
            selector="[role='menuitemradio'], [role='option'], li, button",
            timeout_ms=1500)
        if item is None:
            await self.page.keyboard.press("Escape")
            raise ElementNotFoundError(
                f"Rutube: пункт качества '{label}' не найден в меню")
        await item.click(timeout=2000)
        await self.page.keyboard.press("Escape")
        return {"ok": True, "action": "set_quality", "requested": label,
                "method": "ui_automation", "adapter": type(self).__name__}

    # ------------------------------------------------------------------
    # Fullscreen
    # ------------------------------------------------------------------
    async def fullscreen_enter(self) -> dict[str, Any]:
        labels = self.selectors.get(
            "fullscreen_aria", ["Fullscreen", "Полный экран", "Развернуть"])
        btn = await self._find_by_aria(labels, timeout_ms=2000)
        if btn is not None:
            await btn.click(timeout=2000)
            await asyncio.sleep(0.4)
            return {"ok": True, "action": "fullscreen_enter",
                    "method": "ui_button", "adapter": type(self).__name__}
        return await self._html5("fullscreen_enter")
