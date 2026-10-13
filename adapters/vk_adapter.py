"""Адаптер VK Video (ТЗ 1.2В).

Контекст: VK использует проприетарный плеер, часто скрытый за слоями div;
видео может рендериться через <video>, а может — Canvas/WebGL. Поэтому:

* управление воспроизведением — приоритет на <video> внутри контейнера
  плеера; если его нет — эмуляция пробела (Space = Play/Pause в VK);
* громкость — ползунок (drag) либо mute/unmute кнопка;
* полноэкранный режим — кнопка внизу справа (поиск по aria/тексту);
* часть функций требует авторизации: ``detect_login_required`` детектирует
  экран входа и сообщает агенту о необходимости логина.

Все селекторы — из config/player_selectors.json (секция "vk").
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from adapters.base import BaseVideoAdapter, ElementNotFoundError

logger = logging.getLogger("FirefoxMCP_Adapters.VK")


class VkAdapter(BaseVideoAdapter):
    platform = "vk"
    domain_hints = ("vk.com", "vkvideo.ru")

    def __init__(self, page: Any, video_controller: Any = None):
        super().__init__(page, video_controller)
        self.dom_markers: list[str] = self.selectors.get(
            "dom_markers", [".VideoPlayer"])

    # ------------------------------------------------------------------
    # Наличие видеоэлемента внутри контейнера плеера
    # ------------------------------------------------------------------
    async def _has_video(self) -> bool:
        try:
            return await self.page.locator("video").count() > 0
        except Exception:  # noqa: BLE001
            return False

    async def _focus_player(self) -> None:
        """Клик по контейнеру плеера, чтобы перевести фокус с body на плеер."""
        for marker in self.dom_markers:
            el = self.page.locator(marker).first
            try:
                await el.click(timeout=1500, position={"x": 10, "y": 10})
                return
            except Exception:  # noqa: BLE001
                continue
        # Если маркеров нет — кликнем по видео/центру окна.
        try:
            await self._click_center_overlay()
        except Exception:  # noqa: BLE001
            logger.debug("VK: не удалось сфокусировать плеер кликом")

    # ------------------------------------------------------------------
    # Play / Pause: <video> -> fallback Space
    # ------------------------------------------------------------------
    async def play(self) -> dict[str, Any]:
        if not await self._has_video():
            logger.info("VK: <video> не найден (Canvas/WebGL?) — эмуляция Space")
            await self._focus_player()
            await self.page.keyboard.press("Space")
            return {"ok": True, "action": "play", "method": "space_key",
                    "adapter": type(self).__name__}
        result = await self._html5("play")
        if not result.get("ok"):
            await self._focus_player()
            await self.page.keyboard.press("Space")
            result = {"ok": True, "action": "play", "method": "space_key",
                      "adapter": type(self).__name__, "fallback_from": result}
        return result

    async def pause(self) -> dict[str, Any]:
        if not await self._has_video():
            await self._focus_player()
            await self.page.keyboard.press("Space")
            return {"ok": True, "action": "pause", "method": "space_key",
                    "adapter": type(self).__name__}
        return await self._html5("pause")

    # ------------------------------------------------------------------
    # Громкость: drag ползунка -> fallback mute/unmute кнопка
    # ------------------------------------------------------------------
    async def set_volume(self, percent: float) -> dict[str, Any]:
        slider_sel = self.selectors.get(
            "volume_slider_selector",
            "input[type='range'][class*='volume' i]")
        slider = self.page.locator(slider_sel).first
        try:
            await slider.wait_for(state="visible", timeout=2000)
            box = await slider.bounding_box()
            if box:
                x = box["x"] + box["width"] * max(0.0, min(1.0, percent / 100.0))
                y = box["y"] + box["height"] / 2
                await slider.hover(timeout=1000)
                await self.page.mouse.down()
                await self.page.mouse.move(x, y, steps=3)
                await self.page.mouse.up()
                return {"ok": True, "action": "set_volume", "percent": percent,
                        "method": "slider_drag", "adapter": type(self).__name__}
        except Exception as exc:  # noqa: BLE001
            logger.info("VK: ползунок громкости недоступен (%s), жму mute-кнопку", exc)

        # Fallback: если нужен звук — убедиться, что не muted; иначе toggle mute.
        mute_btn = await self._find_by_aria(
            self.selectors.get("mute_button_aria", ["Mute", "Без звука"]),
            timeout_ms=1500)
        if mute_btn is not None:
            currently_muted = False
            if self.video_controller is not None:
                st = await self._html5("state")
                currently_muted = bool(st.get("muted"))
            want_sound = percent > 0
            if currently_muted == want_sound:  # состояние не совпадает с целью
                await mute_btn.click(timeout=2000)
            return {"ok": True, "action": "set_volume", "percent": percent,
                    "method": "mute_toggle", "adapter": type(self).__name__,
                    "note": "Точная установка % невозможна без ползунка; "
                            "выполнен mute/unmute"}
        if self.video_controller is not None:
            return await self._html5("volume_set", percent)
        raise ElementNotFoundError(
            "VK: не найдены ни ползунок громкости, ни кнопка mute",
            tried_selectors=[slider_sel])

    # ------------------------------------------------------------------
    # Fullscreen
    # ------------------------------------------------------------------
    async def fullscreen_enter(self) -> dict[str, Any]:
        labels = self.selectors.get(
            "fullscreen_aria", ["Fullscreen", "Полноэкранный режим", "Развернуть"])
        btn = await self._find_by_aria(labels, timeout_ms=2000)
        if btn is None:
            btn = await self._find_by_text(labels, selector="button, [role='button']",
                                           timeout_ms=1500)
        if btn is not None:
            await btn.click(timeout=2000)
            await asyncio.sleep(0.4)
            return {"ok": True, "action": "fullscreen_enter",
                    "method": "ui_button", "adapter": type(self).__name__}
        # Fallback на нативный HTML5 fullscreen.
        return await self._html5("fullscreen_enter")

    # ------------------------------------------------------------------
    # Качество (шестерёнка/три точки -> список качеств)
    # ------------------------------------------------------------------
    async def set_quality(self, label: str) -> dict[str, Any]:
        if not label:
            return {"ok": False, "error": "ValueRequired",
                    "message": "set_quality требует метку качества"}
        gear = await self._find_by_aria(
            self.selectors.get("settings_button_aria", ["Настройки", "Ещё"]),
            timeout_ms=2000)
        if gear is None:
            raise ElementNotFoundError(
                "VK: кнопка настроек плеера не найдена",
                hint="Проверьте, не требуется ли авторизация "
                     "(browser_video action=detect_login)")
        await gear.click(timeout=2000)
        item = await self._find_by_text([str(label)], timeout_ms=1500)
        if item is None:
            await self.page.keyboard.press("Escape")
            raise ElementNotFoundError(f"VK: пункт качества '{label}' не найден")
        await item.click(timeout=2000)
        await self.page.keyboard.press("Escape")
        return {"ok": True, "action": "set_quality", "requested": label,
                "method": "ui_automation", "adapter": type(self).__name__}

    # ------------------------------------------------------------------
    # Детектор стены входа (ТЗ: VK может требовать авторизацию)
    # ------------------------------------------------------------------
    async def detect_login_required(self) -> bool:
        selectors = self.selectors.get(
            "login_wall_selectors",
            ["form[action*='login']", ".Login"])
        for sel in selectors:
            try:
                if await self.page.locator(sel).count() > 0:
                    logger.info("VK: обнаружен экран входа по селектору %s", sel)
                    return True
            except Exception:  # noqa: BLE001
                continue
        # Семантическая проверка: текст страницы про вход.
        try:
            body_text = (await self.page.locator("body").text_content() or "")
            low = body_text.lower()
            if ("войдите" in low or "вход для продолжения" in low
                    or "please log in" in low):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False
