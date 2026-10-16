# Коды ошибок policy guard и структурированные решения (Этап 1)

Каждое решение guard — JSON:

```json
{
  "allowed": false,
  "error_code": "POLICY_SITE_SEARCH_NOT_REQUESTED",
  "rule_source": "SKILL.md §Жёсткая политика вкладок / §Hard Tab Policy",
  "message": "Переход на страницу поиска сайта запрещён без явного запроса пользователя.",
  "attempted_tool": "browser_new_tab",
  "attempted_args": {"url": "https://www.youtube.com/results?search_query=..."},
  "suggested_safe_actions": [
    {"tool": "browser_find_link", "args": {"pattern": "<запрос>"},
     "reason": "Найти релевантную ссылку на текущей странице"},
    {"tool": "browser_expand_menu", "reason": "Раскрыть скрытые элементы"}
  ],
  "requires_user_confirmation": false
}
```

## Реестр error codes

| Код | Триггер | rule_source (SKILL.md) | Обычное suggested_safe_action |
|---|---|---|---|
| `POLICY_SAME_TAB_REQUIRED` | выход из зафиксированной текущей вкладки без override | §Жёсткая политика вкладок | `browser_navigate` в текущей вкладке |
| `POLICY_NEW_TAB_NOT_ALLOWED` | `browser_new_tab`/`new_tab=true` без override-фразы | §Жёсткая политика вкладок | `browser_find_link`, `browser_navigate(new_tab=false)` |
| `POLICY_SITE_SEARCH_NOT_REQUESTED` | URL `/results?search_query=`, `/search?q=`, `/query?` без override | §Поиск на сайте, §Hard Tab Policy | `browser_find_link` (ПЕРВЫМ), `browser_search_on_page` — агент обязан немедленно выполнить первое suggestion (§Обработка отказов PolicyGuard) |
| `POLICY_SCROLL_NOT_REQUESTED` | `browser_scroll` без override-фразы | §Обработка скрытых элементов | `browser_expand_menu` |
| `POLICY_PLACEHOLDER_URL_FORBIDDEN` | `... {} VIDEO_ID example test placeholder` в URL | §Запрет placeholder-URL | извлечь реальный ID (`browser_find_link`) |
| `POLICY_INVALID_YOUTUBE_VIDEO_ID` | video_id не соответствует `^[A-Za-z0-9_-]{11}$` | §Запрет placeholder-URL | canonical URL с валидным ID |
| `POLICY_EVAL_VIDEO_FORBIDDEN` | `browser_eval` с video/player/fullscreen/volume/theater/quality/subtitles-аргументами | §Видео, §Театральный режим | `browser_video` с нужным action |
| `POLICY_THEATER_NOT_FULLSCREEN` | попытка fullscreen-API/eval для «широкого экрана» | §Театральный режим YouTube | `browser_video(theater_set)` |
| `POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND` | повторный клик по скрытому тексту без `browser_expand_menu` между попытками | §Обработка скрытых элементов | `browser_expand_menu` → `browser_find_link` |
| `POLICY_BLIND_RETRY_FORBIDDEN` | повтор mutate-действия без изменения состояния (лимит `max_retries`, default 2) | §Повторы | `browser_get_context` / проверка состояния |
| `VERIFY_REQUIRED` | mutate-действие без включённой верификации (когда `verify_after_mutation:true`) | §Жёсткое исполнение политики | `browser_video(state/layout_state)` |
| `VERIFY_FAILED` | пост-верификация не прошла после retries | §Жёсткое исполнение политики | structured error + screenshot |
| `VIDEO_CONTEXT_CHANGED` | `expected_video_id != actual_video_id` (radio drift) | §Потеря контекста из-за radio autoplay | `episode_open(video_id=expected)` |
| `NO_EPISODE_CONTEXT` | episode-действие при отсутствии очереди/плейлиста | §Нативная навигация по эпизодам | `episode_list`, `browser_get_context` |
| `NO_CONFIDENT_MATCH_ON_CURRENT_PAGE` | лучший кандидат ranker < `relevance_threshold` (0.55) | §Точность сопоставления элементов | уточнить запрос; `browser_expand_menu` |
| `NO_CHAPTER_CONTEXT` | `chapter_seek`/`chapter_list` при отсутствии глав у видео | §Нативная навигация по эпизодам | работать с эпизодами (`episode_list`) или таймкодами вручную |
| `THEATER_STATE_UNKNOWN` | layout confidence < 0.45 (unknown band) | §Театральный режим YouTube | fallback-определение, не blind toggle |
| `THEATER_SET_FAILED` | theater не установлен после fallback chain + retries | §Театральный режим YouTube | `browser_screenshot` для диагностики |
| `SETTINGS_INVALID_FALLBACK` | невалидный `user_tool_settings.json` → применены safe defaults | §Пользовательские настройки инструментов | исправить JSON, `browser_reload_settings` |

Override-фразы проверяются строго по whitelist из настроек (`override_phrases.*`); агент не имеет права выдумывать фразы.
