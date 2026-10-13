"""Адаптер Twitch (ТЗ 1.2Б).

Интерфейс Twitch меняется часто, поэтому основной упор на DOM-навигацию:
data-a-target атрибуты (из конфига), aria-label и поиск по тексту кнопок
как fallback. Качество выбирается из списка "Source" — совпадение по
тексту опции (например "1080p60").
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from adapters.base import BaseVideoAdapter, ElementNotFoundError

logger = logging.getLogger("FirefoxMCP_Adapters.Twitch")


class TwitchAdapter(BaseVideoAdapter):
    platform = "twitch"
    domain_hints = ("twitch.tv",)

    async def set_quality(self, label: str) -> dict[str, Any]:
        if not label:
            return {"ok": False, "error": "ValueRequired",
                    "message": "set_quality требует метку (напр. '1080p60', "
                               "'480p30' или 'lowest')"}

        # 1. Шестерёнка настроек потока.
        toggle_sel = self.selectors.get(
            "settings_toggle_selector",
            "[data-a-target='player-settings-menu-toggle']")
        gear = self.page.locator(toggle_sel).first
        try:
            await gear.wait_for(state="visible", timeout=2000)
        except Exception:  # noqa: BLE001
            gear = await self._find_by_aria(
                self.selectors.get("settings_button_aria", ["Settings", "Настройки"]),
                timeout_ms=1500)
            if gear is None:
                # Fallback по тексту (ТЗ).
                gear = await self._find_by_text(["Settings", "Настройки"],
                                                selector="button", timeout_ms=1500)
        if gear is None:
            raise ElementNotFoundError(
                "Twitch: кнопка настроек плеера не найдена",
                tried_selectors=[toggle_sel],
            )
        await gear.click(timeout=2000)

        # 2. Подменю Sources / Quality.
        source_texts = self.selectors.get(
            "sources_menu_text", ["Source", "Источник"])
        submenu = None
        for text in source_texts + ["Quality", "Качество"]:
            item = self.page.locator(
                '[role="menuitem"], [data-a-target*="submenu"]'
            ).filter(has_text=text).first
            try:
                await item.wait_for(state="visible", timeout=800)
                submenu = item
                break
            except Exception:  # noqa: BLE001
                continue
        if submenu is not None:
            await submenu.click(timeout=2000)
            await asyncio.sleep(0.4)

        # 3. Опция качества по совпадению текста ("1080p60" и т.п.).
        option_sel = self.selectors.get(
            "quality_option_selector",
            "[data-a-target*='quality-option'], [role='menuitemradio']")
        options = self.page.locator(option_sel)
        count = await options.count()
        chosen_text = None
        normalized_label = label.strip().lower().replace(" ", "")
        special = {"lowest": ("lowest", "min"), "highest": ("highest", "max")}
        for i in range(count):
            opt = options.nth(i)
            text = (await opt.text_content() or "").strip()
            low = text.lower().replace(" ", "")
            match = False
            if normalized_label in ("auto", "source"):
                match = "auto" in low or "source" in low
            elif normalized_label in special:
                match = any(k in low for k in special[normalized_label])
            else:
                match = normalized_label in low
            if match:
                await opt.click(timeout=2000)
                chosen_text = text
                break

        await self.page.keyboard.press("Escape")

        if chosen_text is None:
            available = []
            for i in range(min(count, 15)):
                available.append((await options.nth(i).text_content() or "").strip())
            raise ElementNotFoundError(
                f"Twitch: источник '{label}' не найден в списке",
                available=available,
            )

        return {
            "ok": True,
            "action": "set_quality",
            "requested": label,
            "selected_source": chosen_text,
            "method": "ui_automation",
        }

    async def toggle_chat_visibility(self) -> dict[str, Any]:
        # Кнопка чата: aria-label ("Collapse/Expand Chat") либо текст "Chat".
        btn = await self._find_by_aria(
            self.selectors.get(
                "chat_toggle_aria",
                ["Collapse Chat", "Expand Chat", "Показать чат", "Скрыть чат"]),
            timeout_ms=2000)
        if btn is None:
            btn = await self._find_by_text(
                self.selectors.get("chat_toggle_text", ["Chat", "Чат"]),
                selector="button", timeout_ms=1500)
        if btn is None:
            raise ElementNotFoundError(
                "Twitch: кнопка переключения чата не найдена")
        await btn.click(timeout=2000)
        return {"ok": True, "action": "toggle_chat_visibility"}
