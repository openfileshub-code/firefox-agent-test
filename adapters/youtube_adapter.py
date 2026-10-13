"""Адаптер YouTube (ТЗ 1.2А).

Контекст: официальный IFrame API больше не позволяет форсировать качество
(``setPlaybackQuality`` — no-op), поэтому управление качеством и режимами
реализовано через UI-автоматизацию плеера (клики по кнопкам настроек,
поиск пунктов меню по тексту/роли). play/pause/volume/seek делегируются
HTML5 <video> внутри страницы; при недоступности — эмуляция клика по
оверлею.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from adapters.base import BaseVideoAdapter, ElementNotFoundError

logger = logging.getLogger("FirefoxMCP_Adapters.YouTube")


class YoutubeAdapter(BaseVideoAdapter):
    platform = "youtube"
    domain_hints = ("youtube.com", "youtu.be", "music.youtube.com")

    # ------------------------------------------------------------------
    # Play/Pause: HTML5 video -> fallback клик по оверлею
    # ------------------------------------------------------------------
    async def play(self) -> dict[str, Any]:
        result = await self._html5("play")
        if not result.get("ok"):
            logger.info("YouTube: HTML5 play не удался, кликаю по оверлею")
            await self._click_center_overlay()
            result = await self._html5("state")
            result["method"] = "overlay_click"
        return result

    async def pause(self) -> dict[str, Any]:
        return await self._html5("pause")

    # ------------------------------------------------------------------
    # set_quality: шестерёнка -> меню -> пункт качества (UI automation)
    # ------------------------------------------------------------------
    async def set_quality(self, label: str) -> dict[str, Any]:
        if not label:
            return {"ok": False, "error": "ValueRequired",
                    "message": "set_quality требует метку качества, напр. '480p' или 'Auto'"}

        settings_aria = self.selectors.get(
            "settings_button_aria", ["Settings", "Настройки"])
        quality_texts = self.selectors.get(
            "quality_menu_text", ["Quality", "Качество"])

        gear = await self._find_by_aria(settings_aria, timeout_ms=3000)
        menu = None
        if gear is not None:
            try:
                await gear.click(timeout=2000)
                # Ожидаем появления меню с ролью menu (до 2 сек по ТЗ).
                deadline = asyncio.get_event_loop().time() + 2.0
                while asyncio.get_event_loop().time() < deadline:
                    candidate = self.page.locator('[role="menu"], [role="listbox"]').last
                    try:
                        await candidate.wait_for(state="visible", timeout=300)
                        menu = candidate
                        break
                    except Exception:  # noqa: BLE001
                        await asyncio.sleep(0.1)
            except Exception as exc:  # noqa: BLE001
                logger.warning("YouTube: клик по шестерёнке не удался: %s", exc)

        if menu is None:
            # Fallback (ТЗ): альтернативный поиск пункта по тексту "Качество".
            logger.info("YouTube: меню настроек не открылось; fallback — поиск по тексту")
            alt = await self._find_by_text(quality_texts, timeout_ms=1500)
            if alt is None:
                raise ElementNotFoundError(
                    "YouTube: не найдена кнопка настроек (шестерёнка) за 3 сек "
                    "и не удалось открыть меню качества",
                    tried_aria=settings_aria,
                    tried_texts=quality_texts,
                )
            try:
                await alt.click(timeout=2000)
                menu = self.page.locator('[role="menu"], [role="listbox"]').last
                await menu.wait_for(state="visible", timeout=2000)
            except Exception as exc:  # noqa: BLE001
                raise ElementNotFoundError(
                    f"YouTube: fallback-меню качества не открылся: {exc}") from exc

        # В выпадающем меню ищем подпункт "Качество"/"Quality", если он есть.
        submenu = await self._find_in(menu, quality_texts)
        target_menu = menu
        if submenu is not None:
            try:
                await submenu.click(timeout=2000)
                await asyncio.sleep(0.4)
                target_menu = self.page.locator(
                    '[role="menu"], [role="listbox"]').last
            except Exception:  # noqa: BLE001
                pass

        # Пункт с нужным ярлыком; проверяем наличие radio/checkbox как ТЗ.
        option = await self._find_in(target_menu, [label])
        if option is None:
            await self._close_menu()
            available = await self._menu_options(target_menu)
            raise ElementNotFoundError(
                f"YouTube: пункт качества '{label}' не найден в меню",
                available=available,
            )

        checked = await option.evaluate(
            "el => el.getAttribute('aria-checked') === 'true' ||"
            " !!el.querySelector('[role=\"radio\"][aria-checked=\"true\"],"
            "[role=\"checkbox\"][aria-checked=\"true\"]')"
        )
        if not checked:
            try:
                await option.click(timeout=2000)
            except Exception as exc:  # noqa: BLE001
                await self._close_menu()
                raise ElementNotFoundError(
                    f"YouTube: не удалось выбрать качество '{label}': {exc}") from exc
        await self._close_menu()

        current = await self.get_current_quality()
        ok = bool(current) and label.lower() in str(current).lower()
        return {
            "ok": True,
            "action": "set_quality",
            "requested": label,
            "current_quality": current,
            "verified": ok,
            "method": "ui_automation",
        }

    @staticmethod
    async def _find_in(menu: Any, texts: list[str]) -> Any:
        for text in texts:
            item = menu.locator(
                '[role="menuitemradio"], [role="menuitemcheckbox"], '
                '[role="menuitem"], [role="option"], li, button'
            ).filter(has_text=text).first
            try:
                await item.wait_for(state="visible", timeout=700)
                return item
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _menu_options(self, menu: Any) -> list[str]:
        try:
            return await menu.evaluate(
                "m => [...m.querySelectorAll('[role=\"menuitemradio\"],"
                "[role=\"menuitem\"],li')].map(x => x.textContent.trim())"
                ".filter(Boolean).slice(0, 20)"
            )
        except Exception:  # noqa: BLE001
            return []

    async def _close_menu(self) -> None:
        """Esc + клик вне области (по ТЗ)."""
        try:
            await self.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass

    async def get_current_quality(self) -> str:
        """Читает текущее качество из DOM: активная опция меню / data-атрибут."""
        try:
            return await self.page.evaluate(r"""
            () => {
              const active = document.querySelector(
                '[role="menuitemradio"][aria-checked="true"],' +
                '[role="option"][aria-selected="true"]');
              if (active) return active.textContent.trim();
              const dv = document.querySelector('[data-value]');
              if (dv && /\d{3,4}p|auto/i.test(dv.getAttribute('data-value')))
                return dv.getAttribute('data-value');
              const sd = window.yt && window.yt.playerConfig &&
                window.yt.playerConfig.streamData;
              if (sd && sd.length) {
                const fmt = sd.find(f => f.itag) || sd[0];
                return String(fmt.qualityLabel || fmt.quality || '');
              }
              return '';
            }
            """) or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # Theater mode
    # ------------------------------------------------------------------
    async def toggle_theater_mode(self) -> dict[str, Any]:
        aria_labels = self.selectors.get(
            "theater_aria", ["Theater mode", "Широкоэкранный режим"])
        before_class = await self._player_container_class()
        btn = await self._find_by_aria(aria_labels, timeout_ms=3000)
        if btn is None:
            btn = await self._find_by_text(aria_labels, timeout_ms=1500)
        if btn is None:
            raise ElementNotFoundError(
                "YouTube: кнопка театрального режима не найдена",
                tried_aria=aria_labels,
            )
        try:
            await btn.click(timeout=2000)
        except Exception as exc:  # noqa: BLE001
            raise ElementNotFoundError(
                f"YouTube: клик по кнопке theater mode не удался: {exc}") from exc
        await asyncio.sleep(0.6)
        after_class = await self._player_container_class()
        return {
            "ok": True,
            "action": "toggle_theater_mode",
            "container_changed": before_class != after_class,
            "before_class": before_class,
            "after_class": after_class,
        }

    async def _player_container_class(self) -> str:
        container = self.selectors.get("player_container", "#movie_player")
        try:
            return await self.page.evaluate(
                "(sel) => { const el = document.querySelector(sel);"
                "return el ? el.className : ''; }", container) or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # next_video
    # ------------------------------------------------------------------
    async def next_video(self) -> dict[str, Any]:
        aria_labels = self.selectors.get(
            "next_video_aria", ["Next video", "Следующее видео"])
        btn = await self._find_by_aria(aria_labels, timeout_ms=2000)
        if btn is not None:
            try:
                await btn.click(timeout=2000)
                return {"ok": True, "action": "next_video",
                        "method": "next_button"}
            except Exception as exc:  # noqa: BLE001
                logger.warning("YouTube: клик Next video не удался: %s", exc)

        # Ссылка на следующее видео в плейлисте / related.
        link = self.page.locator(
            '#related a#video-title, ytd-watch-next-secondary-results-renderer '
            'a[href*="watch"], a#thumbnail[href*="watch"]').first
        try:
            href = await link.get_attribute("href")
            if href:
                url = href if href.startswith("http") else "https://www.youtube.com" + href
                await self.page.goto(url)
                return {"ok": True, "action": "next_video",
                        "method": "playlist_link", "url": url}
        except Exception:  # noqa: BLE001
            pass

        raise ElementNotFoundError(
            "YouTube: не найдено ни кнопки 'Следующее видео', ни ссылки "
            "на следующее видео; проверьте, включён ли автовоспроизведение")
