# Changelog SKILL.md

Все изменения контракта агента (`SKILL.md`) фиксируются здесь. Формат — по дате, свежие сверху.

## 15.10.2026 — Синхронизация с PolicyGuard и новыми инструментами (Этап 12 ТЗ)

### Заголовки разделов (источник истины для `rule_source`)
Заголовки приведены в соответствие с ключами `RULE_SOURCES` в `modules/policy_guard.py`
(18 из 18 ссылок теперь разрешимы; проверка связи — `tests/test_policy_guard.py::test_rule_sources_have_skill_sections`).

| Было | Стало / добавлено |
|---|---|
| «Строгое правило одной вкладки и одного окна» | **«Жёсткая политика вкладок»** |
| — | **«Поиск на сайте»** |
| — | **«Обработка скрытых элементов»** |
| — | **«Запрет placeholder-URL»** |
| — | **«Театральный режим YouTube»** |
| — | **«Потеря контекста из-за radio autoplay»** |
| — | **«Нативная навигация по эпизодам»** |
| — | **«Жёсткое исполнение политики»** |
| — | **«§13 Пользовательские настройки инструментов»** |
| «Повторы» | **«Повторы и слепые ретраи»** |

### Таблица инструментов
- Добавлены описания реально зарегистрированных инструментов: `browser_get_context`,
  `browser_validate_url`, `browser_reload_settings`, `browser_expand_menu`,
  `browser_go_back`, `browser_history_back`, `browser_go_forward`, `browser_click_selector`.
- Расширена сигнатура `browser_video`: `value/target/expected_video_id/verify/override_phrase`.
- Уточнены параметры `browser_navigate/browser_new_tab/browser_scroll/browser_click_text`:
  добавлен `override_phrase` (белый список фраз из настроек).

### Снятые ложные запреты (§5 «Видео»)
- Удалён запрет на команды `theater_on/theater_off/wide_mode` и утверждение, что
  театральный режим «недоступен как общее API». Теперь документированы рабочие действия:
  `layout_state` (мультиканальная детекция с `confidence/evidence`),
  `theater_set:true/false` (идемпотентный set), `toggle_theater_mode` (переключение).
- Убрано указание использовать `requestFullscreen()` через `browser_eval` — вызов теперь
  блокируется guard'ом (`POLICY_FORBIDDEN_JS`) и локальным `VIDEO_JS_RE`.

### Новые разделы с инструкциями для агента
- **Эпизоды**: `episode_list/next/prev/open`, поведение при `NO_EPISODE_CONTEXT`,
  верификация `video_id` после перехода.
- **Главы**: `chapter_list/chapter_seek`, ошибка `NO_CHAPTER_CONTEXT`.
- **Радио-дрейф**: после навигации сверять `is_radio/video_id` (`browser_get_context`);
  при `VIDEO_CONTEXT_CHANGED` — не продолжать действие, перевести диалог.
- **Громкость**: допуск подтверждения — `volume_tolerance` (по умолчанию 0.005), а не ±0.02;
  расхождение считается неуспехом, требуется повтор или сообщение пользователю.
- **Настройки**: ссылка на `config/user_tool_settings.json` + `docs/settings-reference.md`;
  hot-reload через `browser_reload_settings`.

## 14.10.2026 — Исходная версия
Первоначальная загрузка `SKILL.md`. Известные расхождения с кодом зафиксированы
в `docs/contract-audit.md` и устранены правками выше.
