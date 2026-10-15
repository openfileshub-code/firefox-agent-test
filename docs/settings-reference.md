# Справочник пользовательских настроек (config/user_tool_settings.json) — Этап 1

## Источники по приоритету (settings_loader)

1. Явный CLI-путь (`--settings <path>` / параметр инициализации).
2. Переменная окружения `FIREFOX_AGENT_SETTINGS`.
3. `%APPDATA%\firefox-agent\user_tool_settings.json` (Windows профиль пользователя).
4. Проектный файл `config/user_tool_settings.json`.
5. Built-in safe defaults (см. ниже).

Первый существующий и валидированный источник выигрывает; невалидный JSON → warning + следующий источник/defaults, демон не падает (warning с `error_code: SETTINGS_INVALID_FALLBACK`).

## Safe defaults (built-in)

```json
{
  "same_tab_by_default": true, "allow_new_tab": false, "allow_site_search": false,
  "allow_scroll": false, "forbid_placeholder_urls": true,
  "prefer_native_video_controls": true, "verify_after_mutation": true,
  "max_retries": 2, "relevance_threshold": 0.55, "volume_tolerance": 0.005,
  "theater_confidence_threshold": 0.75
}
```

## Разделы файла

| Ключ | Назначение |
|---|---|
| `version` | версия схемы ("1.0") |
| `global` | глобальные флаги и пороги (см. safe defaults) |
| `sites.<domain>` | site-specific правила: `aliases` (доп. домены), `intents.<name>` |
| `sites.<d>.intents.<i>` | `trigger_examples`, `allowed_tools`, `forbidden_tools`, `tool`, `actions`, `action_map`, `selection_strategy`, `fallback`, `verify_fields`, `forbid_site_search` |
| `override_phrases` | whitelist фраз явного разрешения: `allow_new_tab`, `allow_site_search`, `allow_scroll` (+произвольные ключи) |
| `policies` | включение конкретных guard-правил: `same_tab_context`, `no_unrequested_scroll`, `no_placeholder_urls`, `video_context_drift` |
| `debug` | `enabled`, `dump_dom_on_error`, `keep_error_screenshots` |

## Поведение

- Нет файла → safe defaults. Файл невалиден → safe defaults + warning (`SETTINGS_INVALID_FALLBACK`).
- Сайт не описан → применяются `global` правила. Intent не описан → generic safe policy.
- Настройки имеют приоритет над эвристиками агента.
- Агент получает эффективные `allowed_tools`/`forbidden_tools` через `browser_get_context()`.
- Hot reload: по изменению mtime активного файла; явно — `browser_reload_settings()`.
- `settings_hash` — sha256 от канонического JSON (sorted keys); попадает в контекст и audit log.

## Контракты данных (модели)

- `UserSettings` — типизированный view над JSON (см. `modules/settings_loader.py`).
- `ActionContext` — tab_id,url,site,page_type,video_id,list_id,index,is_radio,same_tab_required,allowed_tools,forbidden_tools,expected_video_id,last_verified_state,layout.
- `PolicyDecision` — allowed,error_code,rule_source,message,attempted_tool,attempted_args,suggested_safe_actions,requires_user_confirmation (см. docs/policy-errors.md).
- `LayoutState` — mode(default|theater|fullscreen|full_window|minimized|unknown),theater_mode,fullscreen,full_window_mode,minimized,confidence,evidence[].
- `Episode` — video_id,title,index,duration,url,is_current,visible,source(playlist_panel|yt_initial_data|player_response|url_params|series_shelf|related).
- `LinkCandidate` — candidate_id,text,href,video_id,element_type,visible(+hidden_but_expandable),source,position,score.
- `VerifyResult` — ok,kind(volume|theater|episode_open|navigation),expected,actual,retries_used,error_code.
