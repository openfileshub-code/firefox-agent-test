# Release notes

## v0.2.0 — Политика, настройки и YouTube-домен подключены к рантайму (15.10.2026)

Главное: модули из этапов 0–4 перестали быть мёртвым кодом. Middleware в `server.py`
прогоняет **каждый** tool call через SettingsLoader → PolicyGuard → ContextManager →
StateVerifier → AuditLogger.

### Новое поведение агента
- Отказ вместо «попробуй иначе»: любой заблокированный вызов возвращает
  `{ok:false, error_code, rule_source, suggested_safe_actions}` (18 кодов, справочник —
  `docs/policy-errors.md`).
- Настройки пользователя исполняются: `allowed_tools/forbidden_tools`, лимиты scroll,
  whitelist override-фраз (`override_phrase` у navigate/new_tab/scroll/click_text/video),
  `volume_tolerance`, per-site правила. Hot-reload по mtime + `browser_reload_settings()`.
- Контекст видео отслеживается постоянно: `video_id/list_id/index/is_radio` приходят
  в ответах navigate/new_tab/episode-действий и в `browser_get_context`; radio-autoplay
  дрейф ловится кодом `VIDEO_CONTEXT_CHANGED` до выполнения mutation-действия.

### Новые инструменты MCP
| Инструмент | Назначение |
|---|---|
| `browser_get_context` | Снимок контекста страницы + проверка video-контекста |
| `browser_validate_url` | Дешевая проверка URL (placeholder / битый video_id) до навигации |
| `browser_reload_settings` | Перечитать `user_tool_settings.json` без рестарта |

### Новые действия `browser_video`
- `layout_state` — мультиканальная детекция макета плеера (CSS-классы + aria-label +
  ratio + fullscreen-сигнал) с `confidence` и перечнем `evidence`.
- `theater_set` (`true/false`) — идемпотентная установка театрального режима
  (`changed:false`, если уже в целевом состоянии). Прежний `toggle_theater_mode` сохранён.
- `episode_list / episode_next / episode_prev / episode_open` — нативная навигация по
  очереди эпизодов YouTube с верификацией нового `video_id`.
- `chapter_list / chapter_seek` — главы текущего видео (playerDescriptors, fallback —
  таймкоды описания); `NO_CHAPTER_CONTEXT` при отсутствии глав.
- Параметры `target / expected_video_id / verify` — предварительная и пост-верификация
  результата (`VERIFY_REQUIRED`, рассогласование состояния).

### Безопасность eval
`browser_eval` блокирует `requestFullscreen()/webkitRequestFullScreen/exitFullscreen`
и прочие прямые обращения к плееру (`VIDEO_JS_RE` расширен; guard — `POLICY_FORBIDDEN_JS`).
«Широкий экран» достигается только через `theater_set`/`toggle_theater_mode`.

### Модули
Добавлены: `modules/relevance_ranker.py` (ранжирование кандидатов `browser_find_link`,
поле `candidate_id/score/video_id`, код `NO_CONFIDENT_MATCH_ON_CURRENT_PAGE`),
`modules/state_verifier.py`, `modules/audit_logger.py` (JSONL-аудит `logs/tool_audit.jsonl`,
параметры хэшируются — секреты не попадают в лог).

### Громкость
Подтверждение установки громкости использует `global.volume_tolerance` (0.005) вместо
хардкода ±0.02. Кейс RC9 (10% запрошено, 8.01% фактически) более не считается успехом.

### Документация и тесты
- `SKILL.md` синхронизирован с guard'ом (см. `docs/skill-changelog.md`).
- Тесты: 152 passed. Новые файлы: `test_policy_guard.py`, `test_url_validator.py`,
  `test_regression_chat_cases.py` (RC1–RC11 + middleware), `test_chapters.py`,
  `test_state_verifier_audit.py`.
- Регрессионные кейсы: закрыты RC1–RC2, RC4–RC11 полностью, RC3/RC6/RC8 — на уровне
  guard+адаптер+документация (полный перечень статусов — `docs/repro-cases.md`).

### Совместимость
- Старые имена действий (`set_theater`, `theater_toggle`, `next_video`) работают как раньше.
- Ответы инструментов содержат прежние поля; новые поля добавлены aditively.
- При отсутствии/битости `user_tool_settings.json` применяется безопасный набор
  настроек по умолчанию с предупреждением `SETTINGS_INVALID_FALLBACK` в логе.

## v0.1.0 — Базовый браузерный MCP-демон
FastMCP-сервер (22 инструмента), Playwright-демон `browserd.py`, адаптеры YouTube,
старые защитные механизмы: локальная `validate_url()`, `VIDEO_JS_RE`, `_verify_volume()`.
