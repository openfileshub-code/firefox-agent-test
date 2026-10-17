# AUDIT_NOTES.md — Этап 0. Code audit (ТЗ v2.0, раздел 19, пункт C1–C10)

Дата аудита: 2026-10-17. Аудит выполнен по исходному коду репозитория `firefox-agent-test`
(состояние до внесения правок по объединённому ТЗ v2.0).

## Статусы утверждений C1–C10

| № | Утверждение | Статус | Комментарий |
|---|---|---|---|
| C1 | `set_theater_mode(true)` безусловно вызывает `toggle_theater_mode()` | **confirmed** | `adapters/youtube_adapter.py`, метод `set_theater_mode` (~стр. 731): пред-проверка состояния присутствует (`current == enabled → no-op`), НО при расхождении вызывается `toggle_theater_mode()`, который сам является «слепым» мультиканал-переключателем: после `dispatched=true` по каналу он пробует СЛЕДУЮЩИЙ переключающий канал, что допускает двойное переключение (нарушение ТЗ §10.6). Порог confidence не проверяется (нарушение ТЗ §10.3 / §11.3). |
| C2 | Детектор layout не использует авторитетный атрибут `ytd-watch-flexy[theater]` | **confirmed** | `get_player_modes()` сканирует только `className` элементов `#movie_player/#ytd-player/ytd-watch-flexy/.html5-video-container`; DOM-атрибут `theater`/`wide` у `ytd-watch-flexy` не читается вообще. Класс `.theater-mode` у `.html5-video-player` тоже не входит в список селекторов. Отсюда противоречивый вывод `ratio_wide + theater_mode:false + confidence:0.2` вместо `uncertain` (ТЗ R1, §11.4). |
| C3 | Плеер имеет классы `ytp-hide-controls`, `ytp-delhi-modern`, compact controls | **needs-repro** | Требует живого DOM-снапшота YouTube; код адаптера нигде не учитывает `ytp-hide-controls` и не применяет CSS принуждения видимости — исправление (§9) вводится независимо от этого. |
| C4 | Watchdog делает синхронные Win32-вызовы в asyncio-цикле | **confirmed** | `browserd.py::_apply_os_geometry()` (~стр. 931) синхронно вызывает `modules.os_window_control.apply_geometry(...)` (ctypes/Win32) прямо из `watchdog()` → `_check_window_geometry()` внутри asyncio-цикла, без `run_in_executor`. Также watchdog разделяет логическую последовательность проверок с общим состоянием контроллера. |
| C5 | `browser_search_on_page` возвращает текст без `href`/`index` | **confirmed** | `browserd.py::cmd_search_on_page()` (~стр. 1801): regex-ветка возвращает `list[str]` совпадений, литеральная ветка — `list[str]` (`inner_text`) без `href`, `selector`, `index`, `clickable`. Нарушает ТЗ §15.3. |
| C6 | `browser_find_link` не вызывался агентом, хотя был доступен | **confirmed (частично)** | Инструмент зарегистрирован в `server.py` (~стр. 1021) и делегирует `cmd_find_link`; live-DOM ветка возвращает `href/type/role/disabled`, но НЕ возвращает `selector`, `index`, `actionable`, `score` (ТЗ §15.2). Причина неиспользования агентом — отсутствие явных инструкций в SKILL.md для иконочных/текстовых сценариев; устраняется обновлением SKILL.md. |
| C7 | `browser_new_tab` с `__inplace_navigate:true` всё равно получал policy deny | **confirmed (структурно)** | `modules/policy_guard.py`: `_allow_inplace_navigation(args)` применяется для new_tab (~стр. 299), однако проверка поискового URL (`POLICY_SITE_SEARCH_NOT_REQUESTED`, ~стр. 351) выполняется на более раннем/независимом шаге `check()`, поэтому inplace-флаг не спасает от search-policy. Порядок проверок требует исправления (ТЗ §15.7). |
| C8 | `PROTOCOL_VERSION` в README = 4, в коде v5 | **confirmed** | `README.md` стр. 292/394: «PROTOCOL_VERSION = 4»; `browserd.py` стр. 42: `PROTOCOL_VERSION = 5`; `daemon_control.py`: 5; `server.py`: `SUPPORTED_PROTOCOL_VERSIONS = (4, 5)`. Требуется выравнивание документации (ТЗ §18.8 п.2). |
| C9 | Логи закоммичены, `.gitignore` очищен | **rejected** | Фактическое состояние репозитория: `.gitignore` восстановлен (`*.log`, `logs/*` c исключением `!logs/README.md`), в `logs/` лежит только README.md, логи в git не входят. Пункт уже выполнен ранее; закрепляется тестом. |
| C10 | `browser_tabs`, `browser_get_context` могут быть недоступны через discovery | **rejected** | Оба инструмента зарегистрированы декоратором `@mcp.tool()` в `server.py` (стр. 816, 1045) с подробными docstring. Проблема была в SKILL.md (агент не знал о них) — устраняется пунктом 18.7. |

## Подтверждённые корневые причины (раздел 3 ТЗ)

- **R1** — подтверждено (см. C2): детектор не читает авторитетные атрибуты, нет уровня `uncertain`.
- **R2** — подтверждено с уточнением (см. C1): идемпотентность верхнего уровня есть, но канал-цикл
  внутри `toggle_theater_mode` нарушает запрет повторного переключения после `dispatched=true`,
  а `set_theater_mode` выполняет переключающее действие даже при низкой уверенности детектора.
- **R3** — подтверждено косвенно: таймауты каналов aria/css-кликов 1500–2000 мс, `wait_for(visible)`,
  суммарно четыре канала подряд → десятки секунд; серверный `TIMEOUTS["default"]=30` с
  бесструктурным `TimeoutError`.
- **R4** — подтверждено: видимость кнопки зависит от autohide/hover (`ytp-hide-controls`), CSS-принуждение отсутствует.
- **R5** — подтверждено (см. C4).
- **R6** — подтверждено (см. C5, C6, C7): отсутствуют `browser_site_search`, кликабельные метаданные, `POLICY_LOOP_DETECTED`.
- **R7** — частично: C8 подтверждён, C9/C10 опровергнуты (уже исправлены ранее).

## Вывод

Все критические утверждения, кроме уже устранённых C9/C10, подтверждены. Правки вводятся
в порядке дорожной карты ТЗ §25: структурированные ошибки → детектор → идемпотентный
theater_set → fast-CSS → учёт ввода → таймауты/watchdog → PolicyGuard/поиск → SKILL/тесты/гигиена.

---

## Финальный статус (после завершения дорожной карты ТЗ §25, 2026-10-17)

| № | Утверждение | Статус исправления | Где |
|---|---|---|---|
| C1 | Toggle без confidence-порога, повторный канал после dispatched | **исправлено** (этапы 2–3) | `adapters/youtube_adapter.py`: идемпотентный `set_theater_mode` — пред-проверка S0, `ALREADY_IN_STATE`, `STATE_UNCERTAIN` при confidence < 0.8, запрет второго переключающего канала после `dispatched=true` (`STATE_VERIFICATION_UNCERTAIN`) |
| C2 | Детектор не читает `ytd-watch-flexy[theater]` | **исправлено** (этап 2) | Многоканальный JS-детектор: атрибут `theater`, класс `.theater-mode`, `[wide]`, aria кнопки, геометрия-fallback; противоречивые снапшоты → уровень `uncertain`; регресс-тест на снапшот `ratio_wide + theater_mode:false` |
| C3 | ytp-hide-controls / видимость кнопки | **исправлено** (этап 4) | Инъекция fast-CSS `agent-fast-mode` (принуждение видимости контролов), отказ от hover-зависимости |
| C4 | Синхронные Win32 в asyncio watchdog | **исправлено** (этап 6) | `browserd.py`: `apply_geometry` через `run_in_executor`, heartbeat с причинами деградации |
| C5 | search_on_page без href/selector/index | **исправлено** (этап 8) | `cmd_search_on_page` возвращает метаданные `href/selector/index/actionable/score` |
| C6 | find_link без selector/index/actionable/score | **исправлено** (этап 8) | `cmd_find_link` дополнен метаданными и ранжировщиком `score` |
| C7 | inplace-навигация блокировалась search-policy | **исправлено** (этап 8) | `modules/policy_guard.py`: `_allow_inplace_navigation` проверяется раньше поисковой политики; добавлен `POLICY_LOOP_DETECTED` с бюджетом deny |
| C8 | README v4 vs код v5 | **исправлено** (этап 10) | README RU/EN: `PROTOCOL_VERSION = 5`, сервер поддерживает (4, 5); добавлены разделы про structured errors/timeouts и `browser_site_search` |
| C9 | Логи/.gitignore | **не требовало действий** | Подтверждено на этапе 0 |
| C10 | Discovery инструментов | **исправлено ранее** | Закреплено правилами SKILL.md (этап 9) |

Корневые причины R1–R7: все подтверждённые устранены (R1→C2, R2→C1, R3→таймауты behavior.json+hard budgets, R4→fast-CSS, R5→C4, R6→C5/C6/C7+browser_site_search, R7→C8/SKILL).

Итоговая верификация: pytest — все тесты зелёные (включая новые test_youtube_theater*, test_server_timeout, test_policy_guard*), git-история содержит коммиты этапов 0–10.
