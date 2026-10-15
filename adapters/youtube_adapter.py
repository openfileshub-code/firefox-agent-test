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
    # Volume (системная правка №7): обход «сброса громкости» на YouTube
    # ------------------------------------------------------------------
    async def set_volume(self, percent: float) -> dict[str, Any]:
        """Установка громкости с защитой от перехвата атрибута volume.

        Причины «сброса громкости», которые здесь закрываются:
        1. Расширения youtubewebembed / yt-cinematics переопределяют
           сеттер ``volume`` на HTMLMediaElement и при любом
           ``v.volume = x`` вызывают ``player.updateVolumeSettings()``
           (или применяют собственную сохранённую громкость), из-за чего
           значение сразу возвращается к прежнему. Обход: запись через
           оригинальный нативный сеттер из window.wrappedJSObject
           (обходит JS-перехват расширения) + повторная верификация.
        2. Собственная логика плеера: после клавиш v / +/- / стрелок
           YouTube возвращает громкость, сохранённую в настройках
           аккаунта. Это штатное поведение продукта; оно не является
           ошибкой автоматизации, поэтому результат явно помечается
           полем ``verified`` — если значение не удержалось, агент
           получает предупреждение вместо молчаливого сбоя.
        3. muted=true маскирует громкость: перед установкой звук
           включается.
        """
        try:
            target = self._normalize_volume_arg(percent)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "ValueRequired",
                "message": ("set_volume требует число: 0..1 или проценты "
                            "0..100 (например 50 или 0.5)"),
            }

        # Шаг 1: запись через нативный сеттер, минуя JS-перехваты
        # расширений (youtubewebembed/yt-cinematics).
        direct_ok = False
        try:
            direct_ok = bool(await self.page.evaluate(r"""
            (target) => {
              const v = document.querySelector('video');
              if (!v) return false;
              v.muted = false;
              let proto = Object.getPrototypeOf(v);
              let desc = null;
              while (proto && !desc) {
                desc = Object.getOwnPropertyDescriptor(proto, 'volume');
                proto = Object.getPrototypeOf(proto);
              }
              // wrappedJSObject даёт non-adopted копию элемента Firefox:
              // её свойства не видят expando-сеттеры из контентных скриптов.
              const w = window.wrappedJSObject;
              const vw = (w && w.document) ? w.document.querySelector('video') : null;
              if (vw) {
                try { vw.muted = false; } catch (e) {}
                try { vw.volume = target; } catch (e) {}
              }
              if (desc && desc.set) {
                try { desc.set.call(v, target); } catch (e) {}
              } else {
                try { v.volume = target; } catch (e) {}
              }
              return true;
            }
            """, target))
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube volume: прямой канал недоступен: %s", exc)

        if not direct_ok:
            # Fallback: обычный HTML5-путь (generic-страницы без плеера YT).
            # Передаём уже нормализованную долю target, а не сырой percent.
            result = await super().set_volume(target)
            if isinstance(result, dict):
                result.setdefault("method", "html5_fallback")
            return result

        # Шаг 2: верификация с переустановкой (наследует защиту base.py).
        verified = await self._verify_volume(target, attempts=3, delay=0.25)
        if not verified.get("applied"):
            # Шаг 3 (дубль по спецификации): горячие клавиши плеера.
            # Up/Down меняют громкость шагами ~4.5% — точного значения
            # они не гарантируют, но фиксируют выбор в настройках плеера
            # и обходят программные перехваты. Пробуем только если
            # прямая запись так и не закрепилась.
            await self._volume_via_hotkeys(target)
            verified = await self._verify_volume(target, attempts=2, delay=0.3)

        state = await self._html5("state")
        volume_now, muted_now = await self._read_video_volume()
        out: dict[str, Any] = {
            "ok": True,
            "action": "set_volume",
            "requested_percent": round(target * 100, 1),
            "volume": volume_now,
            "muted": muted_now,
            "verified": bool(verified.get("applied")),
            "retries": verified.get("attempts", 0),
            "method": "native_setter_wrappedJSObject" if direct_ok else "html5",
            "platform": self.platform,
        }
        if isinstance(state, dict) and isinstance(state.get("state"), dict):
            out["state"] = state["state"]
        if not out["verified"]:
            out["warning"] = (
                "Громкость не зафиксирована: значение переопределяется "
                "плеером/расширением (типично для youtubewebembed, "
                "yt-cinematics или функции «восстанавливать сохранённую "
                "громкость» YouTube). Отключите расширение или измените "
                "громкость вручную в настройках плеера."
            )
        return out

    async def _volume_via_hotkeys(self, target: float) -> None:
        """Грубая подстройка громкости клавишами ↑/↓ (±~4.5% за нажатие)."""
        try:
            current, _muted = await self._read_video_volume()
            if current is None:
                return
            step = 0.045
            diff = target - current
            presses = int(round(abs(diff) / step))
            if presses == 0 or presses > 25:
                return
            key = "ArrowUp" if diff > 0 else "ArrowDown"
            for _ in range(presses):
                await self.page.keyboard.press(key)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            logger.info("YouTube volume: клавиши-дублёры не сработали: %s", exc)

    # ------------------------------------------------------------------
    # Theater mode (системные правки №2, №3)
    # ------------------------------------------------------------------
    async def get_player_modes(self) -> dict[str, Any]:
        """YouTube-специфичные режимы: theater/fullscreen/small-player.

        Театральный режим определяется по классам контейнера и плеера:
        * ``html5-theater-mode`` / ``ytp-theater-mode`` — современный
          wide/theater-режим watch-страницы;
        * ``theater`` — legacy-класс (и стендовые реализации);
        * ``html5-full-window-mode`` / ``ytp-full-window`` — «весь экран»;
        * ``html5-small-player-mode`` — компактный плеер (по умолчанию).
        Отдельно помечается кинематографический режим yt-cinematics
        (``cinematics-enabled``), который НЕ является theater-режимом.

        Дополнительно возвращается ``layout_state`` — мультиканальная
        детекция (ТЗ этап 8, RC10): CSS-классы (вес 0.45), aria-label
        кнопки размера (0.35), соотношение сторон плеера (0.15),
        признак отсутствия fullscreen (0.05). Поля ``confidence`` и
        ``evidence`` позволяют guard'у отличать достоверный theater от
        ложного ``theater_mode:false``.
        """
        modes = await super().get_player_modes()
        try:
            info = await self.page.evaluate(r"""
            () => {
              const out = { theater: false, full_window: false,
                            small_player: false, cinematics: false,
                            container_class: '',
                            aria_theater_on: null, aria_theater_off: null,
                            ratio: null };
              const sels = ['#movie_player', '#ytd-player',
                            'ytd-watch-flexy', '.html5-video-container'];
              for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                  const cls = (el.className || '') + '';
                  if (/html5-theater-mode|ytp-theater-mode|\btheater\b/i.test(cls))
                    out.theater = true;
                  if (/full-window/i.test(cls)) out.full_window = true;
                  if (/small-player/i.test(cls)) out.small_player = true;
                  if (/cinematics-enabled/i.test(cls)) out.cinematics = true;
                }
              }
              const mp = document.querySelector('#movie_player');
              if (mp) out.container_class = String(mp.className || '');
              // aria-сигнал: состояние кнопки переключения режима
              const labelsOn = ['Exit theater mode', 'Выйти из широкоэкранного',
                                'Выйти в стандартный режим',
                                'Switch to default view'];
              const labelsOff = ['Enter theater mode', 'Theater mode',
                                 'Широкоэкранный режим',
                                 'Переключить широкоэкранный режим'];
              const btn = document.querySelector('.ytp-size-button')
                       || document.querySelector('button[aria-label]');
              const norm = (s) => String(s || '').toLowerCase();
              for (const b of document.querySelectorAll(
                     '.ytp-size-button, #movie_player button[aria-label]')) {
                const a = norm(b.getAttribute('aria-label'));
                if (!a) continue;
                if (labelsOn.some(l => a.includes(norm(l)))) out.aria_theater_on = true;
                if (labelsOff.some(l => a.includes(norm(l)))) out.aria_theater_off = true;
              }
              // геометрический сигнал: ширина плеера относительно окна
              try {
                if (mp && window.innerWidth > 0) {
                  out.ratio = mp.getBoundingClientRect().width / window.innerWidth;
                }
              } catch (e) {}
              return out;
            }
            """) or {}
        except Exception:  # noqa: BLE001
            info = {}
        modes["theater_mode"] = bool(info.get("theater"))
        modes["full_window_mode"] = bool(info.get("full_window"))
        modes["small_player_mode"] = bool(info.get("small_player"))
        modes["cinematics"] = bool(info.get("cinematics"))
        modes["container_class"] = str(info.get("container_class", ""))[:300]

        # --- layout_state: мультиканальная детекция (RC10) ---
        evidence: list[str] = []
        score = 0.0
        if info.get("theater"):
            score += 0.45
            evidence.append("css_theater_class")
        aria_on = info.get("aria_theater_on")
        aria_off = info.get("aria_theater_off")
        if aria_on is True and aria_off is not True:
            score += 0.35
            evidence.append("aria_exit_theater")
        elif aria_off is True and aria_on is not True:
            evidence.append("aria_enter_theater")  # сигнал «не theater»
        ratio = info.get("ratio")
        if isinstance(ratio, (int, float)):
            if ratio >= 0.92:
                score += 0.15
                evidence.append("ratio_wide:%.2f" % ratio)
            else:
                evidence.append("ratio_default:%.2f" % ratio)
        if not modes.get("fullscreen"):
            score += 0.05
            evidence.append("not_fullscreen")
        confidence = round(min(1.0, max(0.0, score)), 2)
        # Решение: theater, если основной CSS-сигнал ИЛИ два независимых
        # дополнительных сигнала (aria+ratio) подтверждают широкий режим.
        theater_detected = bool(info.get("theater")) or (
            aria_on is True and isinstance(ratio, (int, float)) and ratio >= 0.92)
        if theater_detected and confidence < 0.45:
            confidence = 0.45
        modes["theater_mode"] = theater_detected
        modes["layout_state"] = {
            "mode": ("theater" if theater_detected else
                     ("default" if info.get("small_player") or
                      aria_off is True or confidence >= 0.45 else "unknown")),
            "theater_mode": theater_detected,
            "fullscreen": modes.get("fullscreen"),
            "confidence": confidence,
            "evidence": evidence,
        }
        return modes

    async def layout_state(self) -> dict[str, Any]:
        """Мультиканальный снимок layout с confidence/evidence (RC10)."""
        modes = await self.get_player_modes()
        lay = modes.get("layout_state") or {}
        if not lay:
            return {
                "ok": False,
                "error": "THEATER_STATE_UNKNOWN",
                "action": "layout_state",
                "message": "Layout не определяется на этой странице.",
            }
        return {
            "ok": True,
            "action": "layout_state",
            "layout": lay,
            "theater_mode": lay.get("theater_mode"),
            "fullscreen": lay.get("fullscreen"),
            "container_class": modes.get("container_class", ""),
        }

    async def set_theater_mode(self, enabled: Any) -> dict[str, Any]:
        """Идемпотентная установка theater-режима (RC7).

        Повторный запрос того же режима — no-op (``changed:false``);
        переключение выполняется только при реальном расхождении
        целевого и текущего состояний.
        """
        if isinstance(enabled, str):
            low = enabled.strip().lower()
            enabled = low in ("true", "1", "on", "yes", "включить", "да")
        elif enabled is None:
            enabled = True
        else:
            enabled = bool(enabled)

        before = await self.get_player_modes()
        current = bool(before.get("theater_mode"))
        lay_before = before.get("layout_state") or {}
        if current == enabled:
            return {
                "ok": True,
                "action": "theater_set",
                "changed": False,
                "theater_mode": current,
                "layout": lay_before,
                "message": "Режим уже '%s' — действие не выполнено (no-op)." % (
                    "theater" if enabled else "default"),
            }

        toggled = await self.toggle_theater_mode()
        after = await self.get_player_modes()
        now = bool(after.get("theater_mode"))
        result = {
            "ok": True,
            "action": "theater_set",
            "changed": now != current,
            "theater_mode": now,
            "layout": after.get("layout_state") or {},
            "method": toggled.get("method") if isinstance(toggled, dict) else None,
            "tried_channels": (toggled.get("tried_channels")
                               if isinstance(toggled, dict) else []),
        }
        if now != enabled:
            result["ok"] = False
            result["error"] = "THEATER_SET_FAILED"
            result["message"] = (
                "После переключения theater_mode=%s, ожидалось %s; "
                "проверьте layout_state (confidence=%s)" % (
                    now, enabled, (after.get("layout_state") or {}).get("confidence")))
        return result

    async def toggle_theater_mode(self) -> dict[str, Any]:
        """Переключение театрального режима с дублирующими каналами (правка №3).

        Надёжная последовательность (spec YouTube 2024-2026):
        1. aria/text-поиск кнопки (локале-независимые метки);
        2. CSS .ytp-size-button (кнопка в контроллере плеера);
        3. горячая клавиша «t» (официальная клавиша theater-режима);
        4. программный вызов .ytp-size-button.click();
        после каждого канала — проверка смены состояния; ошибка только
        если НИ ОДИН канал не изменил режим.
        """
        before = await self.get_player_modes()
        tried: list[str] = []

        channels = (
            ("aria_button", self._theater_channel_aria),
            ("css_size_button", self._theater_channel_css),
            ("hotkey_t", self._theater_channel_hotkey),
            ("js_click", self._theater_channel_js),
        )

        for name, channel in channels:
            applied = False
            try:
                applied = await channel()
            except Exception as exc:  # noqa: BLE001
                logger.warning("YouTube theater: канал %s исключение: %s",
                               name, exc)
            if not applied:
                tried.append(f"{name}: элемент/действие недоступны")
                continue
            tried.append(name)

            after = await self.get_player_modes()
            if after.get("theater_mode") != before.get("theater_mode"):
                return {
                    "ok": True,
                    "action": "toggle_theater_mode",
                    "method": name,
                    "theater_mode": after.get("theater_mode"),
                    "before_class": before.get("container_class", ""),
                    "after_class": after.get("container_class", ""),
                    "container_changed": (
                        before.get("container_class", "")
                        != after.get("container_class", "")
                    ),
                    "tried_channels": tried,
                }
            logger.info(
                "YouTube theater: канал %s выполнен, состояние не изменилось",
                name)

        raise ElementNotFoundError(
            "YouTube: переключение театрального режима не подтверждено ни "
            "одним из каналов (aria-кнопка, .ytp-size-button, клавиша «t», "
            "JS click). Возможно, страница ещё не загрузила плеер или "
            "режим недоступен на этом типе страницы.",
            tried_channels=tried,
            before_theater=before.get("theater_mode"),
        )

    async def _theater_channel_aria(self) -> bool:
        aria_labels = self.selectors.get(
            "theater_aria",
            ["Theater mode", "Широкоэкранный режим",
             "Toggle theater mode", "Переключить широкоэкранный режим",
             "Widescreen mode", "Wide screen mode"])
        btn = await self._find_by_aria(aria_labels, timeout_ms=2000)
        if btn is None:
            btn = await self._find_by_text(aria_labels, timeout_ms=800)
        if btn is None:
            return False
        try:
            await btn.click(timeout=2000)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube theater: клик по aria-кнопке: %s", exc)
            return False

    async def _theater_channel_css(self) -> bool:
        selector = self.selectors.get(
            "theater_button_selector", ".ytp-size-button")
        btn = self.page.locator(selector).first
        try:
            await btn.wait_for(state="visible", timeout=1500)
            await btn.click(timeout=2000)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _theater_channel_hotkey(self) -> bool:
        """Горячая клавиша «t» (официальная клавиша theater-режима YT).

        Перед отправкой фокус уводится с текстовых полей/чата, иначе
        «t» будет набрана как текст, а не как команда плеера.
        """
        try:
            await self.page.evaluate(r"""
            () => {
              const ae = document.activeElement;
              if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA'
                         || ae.isContentEditable)) ae.blur();
              const player = document.querySelector('#movie_player')
                          || document.querySelector('video');
              if (player) {
                try { player.focus({ preventScroll: true }); } catch (e) {}
              }
            }
            """)
            await self.page.keyboard.press("t")
            await asyncio.sleep(0.4)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube theater: клавиша «t»: %s", exc)
            return False

    async def _theater_channel_js(self) -> bool:
        try:
            clicked = await self.page.evaluate(r"""
            () => {
              const btn = document.querySelector('.ytp-size-button');
              if (!btn) return false;
              btn.click();
              return true;
            }
            """)
            await asyncio.sleep(0.3)
            return bool(clicked)
        except Exception:  # noqa: BLE001
            return False

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
