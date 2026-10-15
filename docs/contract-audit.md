# Контракт-аудит: SKILL.md ↔ server.py ↔ browserd.py ↔ README (Этап 0)

Дата аудита: 2026-10-15. Основа: актуальное состояние репозитория + полная редакция ТЗ (https://ps-w.ru/ttt.md).

## Матрица рассинхронов

| Сущность | README | SKILL.md | Код | Логи чатов | Статус |
|---|---|---|---|---|---|
| `browser_expand_menu` | есть | **нет в основном списке** | есть (`server.py`, `browserd.cmd_expand_menu`) | не использовался агентом | рассинхрон |
| `browser_get_context` | нет | нет | нет | нужен по ТЗ | дыра (реализуется, Э3/Э10) |
| `browser_reload_settings` | нет | нет | нет | нужен по ТЗ | дыра (реализуется, Э2/Э10) |
| `theater_toggle` и алиасы | косвенно | **запрещён как «несуществующий»** (`theater_on/off/wide_mode`) | есть: `_VIDEO_ACTION_ALIASES` → `toggle_theater_mode` в `YoutubeAdapter` | ElementNotFoundError | противоречие контракта |
| `layout_state` / `theater_set` | нет | нет | частично: `get_player_modes()` (только CSS-классы, один сигнал) | theater_mode:false при визуальном theater | дыра (реализуется, Э8) |
| `episode_list/next/prev/open` | нет | нет | только `next_video` (кнопка/ссылка, без верификации) | нужен | дыра (реализуется, Э9) |
| `chapter_list/chapter_seek` | нет | нет | нет | нужен | дыра (реализуется, Э9) |
| video_id / list_id / is_radio в state | нет | нет | нет | placeholder URL, radio drift | дыра (реализуется, Э3/Э11) |
| Policy enforcement (new tab/site-search/scroll/placeholder/eval) | нет | мягкий промпт (§1 «Строгое правило одной вкладки», §3) | только `validate_url` (placeholder) и `VIDEO_JS_RE` в `browser_eval` | все нарушения из логов | рассинхрон: правила есть, кода нет (Э4) |
| `user_tool_settings.json` | нет | нет | нет | — | дыра (реализуется, Э1/Э2) |
| `browser_click_text(selector=...)` | есть | таблица говорит «не гарантирует aria-label» | есть поддержка selector | скрытые элементы | частичный рассинхрон |
| `browser_find_link` candidates со score | нет | описан qualitatively (§4) | возвращает список без `candidate_id/score/video_id` | слепые клики | дыра (реализуется, Э6) |

## Где регистрируются MCP-инструменты

- `server.py`: декораторы `@mcp.tool()` (FastMCP). Реальный список: browser_start/stop/navigate/go_back/go_forward/history_back/click_text/click_selector/press_key/scroll/type_text/eval/screenshot/get_content/new_tab/close_tab/switch_tab/tabs/video/expand_menu/search_on_page/find_link.
- `browserd.py`: диспетчер `dispatch` → методы `cmd_<command>`; видео-команды маршрутизируются через `adapters/registry.get_adapter`.

## Где формируется `browser_video state`

`browserd.cmd_video_action` → адаптер (`YoutubeAdapter.get_player_modes` → `BaseVideoAdapter.get_player_modes` → VideoController). Текущий theater-детект: только регулярки по CSS-классам (`html5-theater-mode|ytp-theater-mode|\btheater\b`) — один сигнал, без confidence/evidence, без aria-label кнопки, без ratio. Это причина дефекта «theater_mode:false при визуальном theater».

## Текущая theater-логика

`YoutubeAdapter.toggle_theater_mode`: каналы aria→css→hotkey t→js click, каждый с проверкой смены класса. Алиасы `theater_toggle/toggle_theater/theater_mode/theater/set_theater` нормализуются в `browserd._VIDEO_ACTION_ALIASES`. То есть действие СУЩЕСТВУЕТ в коде, но SKILL.md §5 запрещает его имена — прямое противоречие, из-за которого агент уходил в `requestFullscreen()` и текстовый поиск.

## Вывод

Неизвестных расхождений не осталось: все пункты диагностики ТЗ подтверждены кодом. Каждый рассинхрон закрывается этапами 1–13 плана реализации.
