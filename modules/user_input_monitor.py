"""Слушатели пользовательского ввода и конфликт-страховка (ТЗ v2.0 §8).

Перед переключающим/геометрическим действием агент обязан убедиться, что
пользователь не взаимодействует со страницей прямо сейчас: параллельный
человеческий input + автоматическое действие = гонка состояний (R-кейс
«USER_INPUT_CONFLICT должен возвращаться за <= 600 мс»).

Реализация:
* ``INPUT_INIT_JS`` — add_init_script-ловушка, пишущая timestamp
  последнего отслеживаемого события в ``window.__agentLastUserInput``;
  переживает навигацию;
* :class:`UserInputMonitor` — Python-сторона: установка ловушки,
  ожидание тишины (quiescence) с бюджетом, структурированный отказ
  ``USER_INPUT_CONFLICT`` при активности пользователя.

Конфигурация: config/behavior.json секция ``user_input``
(quiescence_ms=250, max_wait_for_quiescence_ms=300, abort_on_activity=true,
tracked_events=[mousemove,mousedown,keydown,wheel,touchstart]).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from adapters.base import structured_error
from modules import timing_config

logger = logging.getLogger("FirefoxMCP_Adapters.UserInput")

#: Инициализационный скрипт: помечает время последнего пользовательского
#: ввода. Слушатели пассивные ({passive:true}) — не тормозят страницу и
#: не вызывают preventDefault. События агента фильтруются по флагу
#: isTrusted только со стороны детектора «свежести» времени — сам JS
#: тривиален и не ломается на разных движках.
INPUT_INIT_JS = r"""
(() => {
  if (window.__agentInputMonitorInstalled) return;
  window.__agentInputMonitorInstalled = true;
  window.__agentLastUserInput = 0;
  const events = ['mousemove', 'mousedown', 'mouseup', 'click',
                  'keydown', 'keyup', 'wheel', 'touchstart', 'scroll'];
  for (const ev of events) {
    window.addEventListener(ev, () => {
      try { window.__agentLastUserInput = Date.now(); } catch (e) {}
    }, { passive: true, capture: true });
  }
})();
"""


class UserInputMonitor:
    """Монитор активности пользователя над данной Playwright-страницей."""

    def __init__(self, page: Any):
        self.page = page
        self._installed = False

    async def install(self) -> bool:
        """Ставит init-скрипт + немедленную инъекцию в текущий документ."""
        ok = True
        try:
            await self.page.add_init_script(INPUT_INIT_JS)
        except Exception as exc:  # noqa: BLE001
            logger.info("user_input: add_init_script недоступна: %s", exc)
            ok = False
        try:
            await self.page.evaluate(INPUT_INIT_JS)
        except Exception as exc:  # noqa: BLE001
            logger.info("user_input: инъекция в текущий документ: %s", exc)
            ok = False
        self._installed = ok
        return ok

    async def last_input_age_ms(self) -> float | None:
        """Возраст последнего пользовательского события, мс (None — нет данных)."""
        try:
            ts = await self.page.evaluate(
                "() => window.__agentLastUserInput || 0")
        except Exception:  # noqa: BLE001
            return None
        if not ts:
            return float("inf")  # событий не было вообще — тишина
        return max(0.0, time.time() * 1000.0 - float(ts))

    async def ensure_quiescence(self, action: str = "") -> dict[str, Any] | None:
        """Проверяет паузу пользовательского ввода перед действием.

        Возвращает ``None``, если ввод спокойнее ``quiescence_ms`` дольше
        этого времени; иначе ждёт до ``max_wait_for_quiescence_ms`` и при
        сохранении активности возвращает структурированную ошибку
        ``USER_INPUT_CONFLICT`` (общий бюджет проверки <= 600 мс по ТЗ §8).
        """
        if not timing_config.get("user_input.abort_on_activity", True):
            return None
        quiescence_ms = float(timing_config.get("user_input.quiescence_ms", 250))
        max_wait_ms = float(timing_config.get(
            "user_input.max_wait_for_quiescence_ms", 300))
        started = time.monotonic()
        deadline = started + max_wait_ms / 1000.0
        while True:
            age = await self.last_input_age_ms()
            if age is None or age >= quiescence_ms:
                return None  # тихо — можно действовать
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if time.monotonic() >= deadline:
                logger.warning(
                    "user_input_conflict action=%s duration_ms=%d "
                    "last_input_age_ms=%d", action or "?", int(elapsed_ms),
                    int(age))
                return structured_error(
                    "USER_INPUT_CONFLICT",
                    "Пользователь активно взаимодействует со страницей "
                    "(последнее событие %d мс назад); автоматическое "
                    "действие '%s' отменено, чтобы не конфликтовать с "
                    "вводом." % (int(age), action),
                    action=action,
                    diagnostics={
                        "last_input_age_ms": int(age),
                        "quiescence_ms": int(quiescence_ms),
                        "waited_ms": int(elapsed_ms),
                    })
            await asyncio.sleep(min(0.05, max(0.01, deadline - time.monotonic())))
