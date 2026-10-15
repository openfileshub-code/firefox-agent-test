# Repro-кейсы из логов чатов (Этап 0)

Каждый кейс: вход → фактическое (неверное) поведение → нарушенное правило SKILL.md → ожидаемое поведение после реализации → error_code.

## RC1. Новая вкладка с site-search вместо on-page find
- Вход: YouTube home, «открой Джем soprano турецкого».
- Факт: `browser_new_tab("https://www.youtube.com/results?search_query=...")`.
- Нарушение: §1 «Строгое правило одной вкладки» п.2; §3 «Не конструируй URL поисковой выдачи вручную».
- Ожидание: `browser_get_context` → `browser_find_link`/`browser_search_on_page` → ranker → клик видимого кандидата.
- Guard: `POLICY_NEW_TAB_NOT_ALLOWED`, `POLICY_SITE_SEARCH_NOT_REQUESTED`.

## RC2. Повтор запрещённого действия после замечания пользователя
- Вход: пользователь явно сказал «не лезть в поиск по сайту»; агент снова пытается search URL.
- Ожидание: guard блокирует повторно, тем же error_code.

## RC3. Скрытый элемент → незапрошенный scroll → слепой повтор клика
- Вход: `browser_click_text("Сопрано")` → «элемент скрыт» → `browser_scroll(down)` → повтор клика.
- Нарушение: §9 «Повторы», §1 «не делать лишних действий».
- Ожидание: `browser_expand_menu` → повторный `browser_find_link` → клик видимого.
- Guard: `POLICY_SCROLL_NOT_REQUESTED`, `POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND`, `POLICY_BLIND_RETRY_FORBIDDEN`.

## RC4. Placeholder URL
- Вход: `browser_navigate("https://www.youtube.com/watch?v=...")`.
- Ожидание: блок `POLICY_PLACEHOLDER_URL_FORBIDDEN`; реальный ID извлекается из DOM/ytInitialData.

## RC5. Реальный video_id не извлечён
- Вход: на странице есть `a[href="/watch?v=U8A9YEsj3zY"]`, агент строит placeholder.
- Ожидание: canonical URL `https://www.youtube.com/watch?v=U8A9YEsj3zY` (валидный 11-символьный ID).

## RC6. «широкий экран» = requestFullscreen + scroll + уход в поиск
- Вход: watch-страница, «широкий экран».
- Факт: `browser_eval("requestFullscreen()")` (denied), scroll, переход на `results?search_query=мой+джем`.
- Ожидание: `browser_video(layout_state)` → `theater_set true` → verify `layout.mode==theater`.
- Guard: `POLICY_EVAL_VIDEO_FORBIDDEN`, `POLICY_SCROLL_NOT_REQUESTED`, `POLICY_SAME_TAB_REQUIRED`, `POLICY_SITE_SEARCH_NOT_REQUESTED`.

## RC7. Theater уже включён — повтор должен быть no-op
- Вход: layout.mode==theater, повтор запроса «широкий экран».
- Ожидание: `theater_set true` → `{ok:true, changed:false}` (идемпотентность).

## RC8. theater_toggle → ElementNotFoundError → текстовый поиск иконок
- Факт: поиск текстов `theater/Theater/Expand/TV` (кнопка — иконка/SVG/aria-label, текста нет).
- Ожидание: fallback chain aria-label(RU/EN) → `.ytp-size-button` → клавиша `t`; запрет text-search иконок.

## RC9. volume 10% → state 0.080157 без верификации
- Ожидание: `volume_set 0.1` → verify ±0.005 → при выходе retry ≤ max_retries → иначе `VERIFY_FAILED`.

## RC10. theater_mode:false при визуальном theater
- Причина: детекция по одному CSS-классу.
- Ожидание: multi-signal `layout_state` (классы 0.45 + aria-label кнопки 0.35 + ratio 0.15 + not-fullscreen 0.05) c confidence/evidence.

## RC11. Radio autoplay drift
- Факт: `r3TzbOP7-YA` → `J3eqAxYPkmA`, команды применены к чужому видео.
- Ожидание: до mutation зафиксировать `expected_video_id`; после — сравнить; расхождение → `VIDEO_CONTEXT_CHANGED`, команда не применяется, предлагается `episode_open(video_id=expected)`.

Все кейсы закреплены в `tests/test_regression_chat_cases.py` (Этап 13).
