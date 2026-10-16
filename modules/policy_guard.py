"""Жёсткий policy guard (ТЗ §6.4, Этап 4).

Проверяет КАЖДЫЙ tool call до исполнения. Каждое hard-rule из SKILL.md
имеет здесь техническую проверку; отказ содержит `error_code`,
`rule_source` (ссылка на раздел SKILL.md), `attempted_tool/args` и
`suggested_safe_actions`.

Принцип контракта: расхождение SKILL.md ↔ guard = баг контракта
(см. docs/contract-audit.md, docs/policy-errors.md).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from modules import url_validator as uv

log = logging.getLogger("FirefoxMCP_PolicyGuard")

# ---------------------------------------------------------------
# error_code -> раздел SKILL.md (единый источник истины)
# ---------------------------------------------------------------
RULE_SOURCES = {
    "POLICY_SAME_TAB_REQUIRED": "SKILL.md §Жёсткая политика вкладок",
    "POLICY_NEW_TAB_NOT_ALLOWED": "SKILL.md §Жёсткая политика вкладок",
    "POLICY_SITE_SEARCH_NOT_REQUESTED": "SKILL.md §Поиск на сайте",
    "POLICY_SCROLL_NOT_REQUESTED": "SKILL.md §Обработка скрытых элементов",
    "POLICY_PLACEHOLDER_URL_FORBIDDEN": "SKILL.md §Запрет placeholder-URL",
    "POLICY_INVALID_YOUTUBE_VIDEO_ID": "SKILL.md §Запрет placeholder-URL",
    "POLICY_EVAL_VIDEO_FORBIDDEN": "SKILL.md §Видео",
    "POLICY_THEATER_NOT_FULLSCREEN": "SKILL.md §Театральный режим YouTube",
    "POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND": "SKILL.md §Обработка скрытых элементов",
    "POLICY_BLIND_RETRY_FORBIDDEN": "SKILL.md §Повторы",
    "VERIFY_REQUIRED": "SKILL.md §Жёсткое исполнение политики",
    "VERIFY_FAILED": "SKILL.md §Жёсткое исполнение политики",
    "VIDEO_CONTEXT_CHANGED": "SKILL.md §Потеря контекста из-за radio autoplay",
    "NO_EPISODE_CONTEXT": "SKILL.md §Нативная навигация по эпизодам",
    "NO_CONFIDENT_MATCH_ON_CURRENT_PAGE": "SKILL.md §Точность сопоставления элементов",
    "THEATER_STATE_UNKNOWN": "SKILL.md §Театральный режим YouTube",
    "THEATER_SET_FAILED": "SKILL.md §Театральный режим YouTube",
    "SETTINGS_INVALID_FALLBACK": "SKILL.md §Пользовательские настройки инструментов",
}

#: browser_eval: паттерны video-related аргументов (ТЗ §POLICY_EVAL_VIDEO_FORBIDDEN)
EVAL_VIDEO_RE = re.compile(
    r"video|player|fullscreen|requestFullscreen|volume|theater|quality|"
    r"subtitles?|movie_player|ytp-", re.IGNORECASE)

#: попытка fullscreen-API для «широкого экрана»
FULLSCREEN_RE = re.compile(r"requestFullscreen|webkitRequestFullScreen|fullscreen",
                           re.IGNORECASE)

#: тексты иконочных кнопок, поиск которых по тексту запрещён
ICON_TEXTS = {"theater", "expand", "tv", "play", "театр", "театральный",
              "широкоэкранный"}

#: действия browser_video, требующие expected_video_id (video-mutations)
VIDEO_MUTATION_ACTIONS = {
    "play", "pause", "volume", "set_volume", "volume_set", "seek",
    "toggle_theater_mode", "theater_set", "theater_toggle", "set_quality",
    "episode_next", "episode_prev", "episode_open", "chapter_seek",
    "next_video",
}
EPISODE_ACTIONS = {"episode_list", "episode_next", "episode_prev",
                   "episode_open", "chapter_list", "chapter_seek"}


@dataclass
class PolicyDecision:
    allowed: bool
    error_code: str = ""
    rule_source: str = ""
    attempted_tool: str = ""
    attempted_args: dict = field(default_factory=dict)
    suggested_safe_actions: list = field(default_factory=list)
    requires_user_confirmation: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"allowed": self.allowed}
        if not self.allowed:
            d.update({
                "error_code": self.error_code,
                "rule_source": self.rule_source or RULE_SOURCES.get(
                    self.error_code, ""),
                "attempted_tool": self.attempted_tool,
                "attempted_args": self.attempted_args,
                "suggested_safe_actions": self.suggested_safe_actions,
                "requires_user_confirmation": self.requires_user_confirmation,
                "message": self.message,
            })
        return d


def _deny(code: str, tool: str, args: dict, message: str = "",
          suggestions: Optional[list] = None,
          confirmation: bool = False) -> PolicyDecision:
    return PolicyDecision(
        allowed=False, error_code=code,
        rule_source=RULE_SOURCES.get(code, ""),
        attempted_tool=tool, attempted_args=args or {},
        suggested_safe_actions=suggestions or [],
        requires_user_confirmation=confirmation,
        message=message or code,
    )


ALLOW = PolicyDecision(allowed=True)


class PolicyGuard:
    """Middleware-проверка tool call против user settings + context."""

    def __init__(self, settings_loader, context_manager=None):
        self._loader = settings_loader
        self._cm = context_manager
        # Анти-цикл: (tool, url) -> время последнего deny по новой вкладке.
        # Повторный deny того же вызова в течение короткого окна заменяется
        # разрешением на browser_navigate в текущей вкладке — иначе агент,
        # механически повторяющий browser_new_tab, зацикливается и уходит в
        # переспрашивание пользователя (фидбек: «агент не может открыть сайт»).
        self._newtab_deny_at: dict = {}
        self._NEWTAB_DEDUPE_SECONDS = 20

    def _allow_inplace_navigation(self, args: dict) -> PolicyDecision:
        """Внутренне переписать запрещённый new_tab на навигацию в текущей вкладке."""
        import time as _time

        url = str(args.get("url") or "")
        key = ("browser_new_tab", url)
        now = _time.monotonic()
        last = self._newtab_deny_at.get(key, 0.0)
        if now - last < self._NEWTAB_DEDUPE_SECONDS:
            # Второй подряд отказ за короткое окно: молча разрешаем
            # навигацию в текущей вкладке (это и есть штатное поведение).
            log.info("PolicyGuard: повтор new_tab %s — автозамена на "
                     "navigate в текущей вкладке (анти-цикл)", url)
            return ALLOW
        self._newtab_deny_at[key] = now
        return None  # sentinel: обычная ветка deny

    # ------------------------------------------------------------------
    @property
    def settings(self):
        return self._loader.get()

    def context(self):
        if self._cm is None:
            return None
        return self._cm.get_active_context()

    # ------------------------------------------------------------------
    def check(self, tool: str, args: dict, context=None) -> PolicyDecision:
        try:
            return self._check_inner(tool, args, context)
        except Exception:  # noqa: BLE001 — guard не должен ронять tool call
            log.exception("PolicyGuard.check(%s) internal error — allow", tool)
            return ALLOW

    def _check_inner(self, tool: str, args: dict, context=None) -> PolicyDecision:
        args = args or {}
        ctx = context if context is not None else self.context()
        s = self.settings

        # предупреждение о fallback настроек не блокирует вызов,
        # но поднимается в warning логе (см. load()).

        if tool in ("browser_new_tab",) or (
                tool == "browser_navigate" and args.get("new_tab")):
            d = self._check_new_tab(tool, args, ctx, s)
            if not d.allowed:
                return d

        if tool in ("browser_navigate", "browser_new_tab"):
            d = self._check_url(tool, args, ctx, s)
            if not d.allowed:
                return d

        if tool == "browser_scroll":
            d = self._check_scroll(args, s)
            if not d.allowed:
                return d

        if tool == "browser_eval":
            d = self._check_eval(args)
            if not d.allowed:
                return d

        if tool in ("browser_click_text", "browser_click_selector"):
            d = self._check_hidden_retry(tool, args, ctx, s)
            if not d.allowed:
                return d

        # blind retry для всех mutate-инструментов (RC2/RC7): два идентичных
        # неудачных повтора без изменения состояния → блок. Для browser_video
        # это же правило исполняется внутри _check_video.
        if tool in ("browser_scroll", "browser_new_tab", "browser_navigate",
                    "browser_click_text", "browser_click_selector",
                    "browser_press_key"):
            d = self._check_blind_retry(tool, args, s)
            if not d.allowed:
                return d

        if tool == "browser_search_on_page":
            d = self._check_icon_text_search(args)
            if not d.allowed:
                return d

        if tool == "browser_video":
            d = self._check_video(args, ctx, s)
            if not d.allowed:
                return d

        # site-level forbidden tools (user JSON имеет приоритет над эвристиками).
        # browser_navigate разрешён в текущей вкладке: правила intent описывают
        # предпочтительный путь, но навигация = действие текущей вкладки.
        # Исключение — виртуальный инструмент browser_navigate_to_search_url:
        # отдельного MCP-инструмента нет, его семантика = navigate/new_tab на
        # URL выдачи, поэтому он блокирует именно эти вызовы (и только их).
        if ctx is not None:
            url = str(args.get("url") or "")
            site_forbidden = s.site_forbidden_tools(
                getattr(ctx, "site", "") or "")
            virtual_hit = ("browser_navigate_to_search_url" in site_forbidden
                           and tool in ("browser_navigate", "browser_new_tab")
                           and uv.is_search_results_url(url))
            if tool != "browser_navigate" or virtual_hit:
                if tool in site_forbidden or virtual_hit:
                    override_cat = {
                        "browser_new_tab": "allow_new_tab",
                        "browser_scroll": "allow_scroll",
                        "browser_navigate": "allow_site_search",
                    }.get(tool)
                    if virtual_hit and tool == "browser_navigate":
                        override_cat = "allow_site_search"
                    if not (override_cat and s.phrase_matches(
                            override_cat, args.get("override_phrase"))):
                        if virtual_hit:
                            code = "POLICY_SITE_SEARCH_NOT_REQUESTED"
                        elif tool == "browser_scroll":
                            code = "POLICY_SCROLL_NOT_REQUESTED"
                        else:
                            code = "POLICY_NEW_TAB_NOT_ALLOWED"
                        return _deny(
                            code, tool, args,
                            message="Инструмент '%s' запрещён правилами сайта "
                                    "user_tool_settings.json" % tool,
                            suggestions=[{"tool": "browser_get_context"}],
                        )

        return ALLOW

    # ------------------------------------------------------------------
    def _check_new_tab(self, tool, args, ctx, s) -> PolicyDecision:
        phrase = args.get("override_phrase")
        global_allow = bool(s.g("allow_new_tab"))
        if phrase and s.phrase_matches("allow_new_tab", phrase):
            return ALLOW
        if global_allow and not getattr(ctx, "same_tab_required", True):
            return ALLOW
        if global_allow and not phrase:
            # allow_new_tab без override-фразы: автоматический повтор в
            # ТЕКУЩЕЙ вкладке (автономный recovery), а не переспрашивание
            # пользователя. Требовать подтверждение здесь — значит заставлять
            # агента отвечать «скажите, перейти ли по адресу…», что запрещено
            # SKILL.md (§«Пользователь просит открыть сайт»).
            url_arg = str(args.get("url") or "")
            nav_suggestion = None
            if url_arg.startswith(("http://", "https://")):
                nav_suggestion = {
                    "tool": "browser_navigate",
                    "args": {"url": url_arg, "new_tab": False},
                    "auto_retry": True,
                }
            return _deny(
                "POLICY_NEW_TAB_NOT_ALLOWED", tool, args,
                message="browser_new_tab требует override-фразу пользователя "
                        "(например «открой в новой вкладке»). Если пользователь "
                        "сказал просто «открой <сайт>» — немедленно повтори "
                        "операцию через browser_navigate в текущей вкладке "
                        "(см. suggestion с auto_retry), не переспрашивая его.",
                suggestions=([] if nav_suggestion is None else [nav_suggestion])
                            + [{"tool": "browser_find_link",
                                "args": {"pattern": ""}}],
                confirmation=False)
        # Автономный recovery (см. SKILL.md §«Пользователь просит открыть
        # сайт»): «открой <сайт>» — это навигация в ТЕКУЩЕЙ вкладке, а не
        # запрос новой. Поэтому прямой browser_navigate(url) предлагается
        # первым и помечается как auto_retry: агент обязан выполнить его
        # немедленно в том же ходе, без переспрашивания пользователя.
        # Анти-цикл (фидбек пользователя): если агент уже получил deny по
        # этому URL и снова вызывает new_tab с тем же адресом, guard сам
        # переписывает вызов на навигацию в текущей вкладке (ALLOW), чтобы
        # задача выполнялась без третьего отказа и без эскалации к
        # пользователю.
        inplace = self._allow_inplace_navigation(args)
        if inplace is not None:
            # Повторный вызов new_tab с тем же URL в коротком окне:
            # молча исполняем навигацию в ТЕКУЩЕЙ вкладке. Отмечаем это
            # в args, чтобы middleware server.py вызвал navigate вместо
            # new_tab (иначе демон откроет дубликат вкладки).
            args["__inplace_navigate"] = True
            return ALLOW
        nav_suggestion = None
        url_arg = str(args.get("url") or "")
        if url_arg.startswith(("http://", "https://")):
            nav_suggestion = {
                "tool": "browser_navigate",
                "args": {"url": url_arg, "new_tab": False},
                "auto_retry": True,
            }
        suggestions = ([] if nav_suggestion is None else [nav_suggestion]) + [
            {"tool": "browser_find_link", "args": {"pattern": ""}},
            {"tool": "browser_search_on_page", "args": {"pattern": ""}},
        ]
        return _deny(
            "POLICY_NEW_TAB_NOT_ALLOWED", tool, args,
            message="Новая вкладка запрещена: работайте в текущей вкладке. "
                    "Разрешение — только по явной фразе пользователя. "
                    "Если пользователь сказал просто «открой <сайт>» — "
                    "немедленно повтори операцию через browser_navigate "
                    "в текущей вкладке (см. suggestion с auto_retry).",
            suggestions=suggestions,
            confirmation=False)

    # ------------------------------------------------------------------
    def _check_url(self, tool, args, ctx, s) -> PolicyDecision:
        url = str(args.get("url") or "")
        if not url:
            return ALLOW
        if uv.is_placeholder_url(url):
            return _deny(
                "POLICY_PLACEHOLDER_URL_FORBIDDEN", tool, args,
                message="Placeholder-URL запрещены всегда. Извлеките реальный "
                        "11-символьный video_id со страницы.",
                suggestions=[{"tool": "browser_find_link",
                              "args": {"pattern": ""}}])
        if uv.is_search_results_url(url):
            if not s.phrase_matches("allow_site_search",
                                    args.get("override_phrase")) \
                    and not args.get("allow_site_search"):
                return _deny(
                    "POLICY_SITE_SEARCH_NOT_REQUESTED", tool, args,
                    message="Переход на URL выдачи = site-search без запроса "
                            "пользователя. Ищите на текущей странице.",
                    suggestions=[{"tool": "browser_search_on_page",
                                  "args": {"pattern": _query_from(url)}},
                                 {"tool": "browser_find_link",
                                  "args": {"pattern": _query_from(url)}}])
        vid = uv.extract_youtube_video_id(url)
        if vid is not None and url.lower().find("youtu") >= 0:
            if not uv.is_valid_youtube_video_id(vid):
                return _deny(
                    "POLICY_INVALID_YOUTUBE_VIDEO_ID", tool, args,
                    message="video_id '%s' не соответствует формату "
                            "^[A-Za-z0-9_-]{11}$." % vid,
                    suggestions=[{"tool": "browser_find_link",
                                  "args": {"pattern": ""}}])
        # watch-подобный URL с невалидным/пустым v-параметром (ловит
        # url_validator: 'watch?v=short', 'youtu.be/abc')
        vr = uv.validate_navigation_url(
            url, allow_search_override=True)
        if (not vr.valid
                and vr.error_code == "POLICY_INVALID_YOUTUBE_VIDEO_ID"):
            return _deny(
                "POLICY_INVALID_YOUTUBE_VIDEO_ID", tool, args,
                message=vr.message or "Невалидный YouTube video_id",
                suggestions=[{"tool": "browser_find_link",
                              "args": {"pattern": ""}}])
        # same-tab: навигация с уходом с текущего сайта при locked контексте
        if tool == "browser_navigate" and not args.get("new_tab"):
            if ctx is not None and getattr(ctx, "same_tab_required", False) \
                    and getattr(ctx, "page_type", "") in ("watch", "radio") \
                    and not uv.is_search_results_url(url):
                target_host = (url.split("//")[-1].split("/")[0] or "").lower()
                cur_host = (getattr(ctx, "site", "") or "").lower()
                if cur_host and target_host and \
                        not target_host.endswith(cur_host.removeprefix("www.")):
                    if not s.phrase_matches("allow_new_tab",
                                            args.get("override_phrase")):
                        return _deny(
                            "POLICY_SAME_TAB_REQUIRED", tool, args,
                            message="Контекст зафиксирован на '%s'; переход на "
                                    "'%s' без разрешения пользователя." %
                                    (cur_host, target_host),
                            suggestions=[{"tool": "browser_get_context"},
                                         {"tool": "browser_video",
                                          "args": {"action": "state"}}],
                            confirmation=True)
        return ALLOW

    # ------------------------------------------------------------------
    def _check_scroll(self, args, s) -> PolicyDecision:
        if s.g("allow_scroll"):
            return ALLOW
        if s.phrase_matches("allow_scroll", args.get("override_phrase")):
            return ALLOW
        return _deny(
            "POLICY_SCROLL_NOT_REQUESTED", "browser_scroll", args,
            message="Скролл без запроса пользователя запрещён. Для скрытых "
                    "элементов используйте browser_expand_menu.",
            suggestions=[{"tool": "browser_expand_menu",
                          "args": {"trigger_text": ""}},
                         {"tool": "browser_find_link",
                          "args": {"pattern": ""}}])

    # ------------------------------------------------------------------
    def _check_eval(self, args) -> PolicyDecision:
        # совместимость аргументов: server/browserd используют "js",
        # агенты иногда передают "expression"
        js = str(args.get("js") or args.get("expression")
                 or args.get("script") or "")
        if EVAL_VIDEO_RE.search(js):
            code = ("POLICY_THEATER_NOT_FULLSCREEN"
                    if FULLSCREEN_RE.search(js)
                    else "POLICY_EVAL_VIDEO_FORBIDDEN")
            return _deny(
                code, "browser_eval", args,
                message="browser_eval запрещён для управления видео/"
                        "плеером/theater/fullscreen/volume. "
                        "Используйте browser_video.",
                suggestions=[{"tool": "browser_video",
                              "args": {"action": "layout_state"}},
                             {"tool": "browser_video",
                              "args": {"action": "state"}}])
        return ALLOW

    # ------------------------------------------------------------------
    def _check_hidden_retry(self, tool, args, ctx, s) -> PolicyDecision:
        if self._cm is None or ctx is None:
            return ALLOW
        text = str(args.get("text") or "")
        if text.strip().lower() in ICON_TEXTS:
            return _deny(
                "POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND", tool, args,
                message="Клик по тексту иконочной кнопки ('%s') запрещён: "
                        "theater-кнопка — иконка/SVG/aria-label. Используйте "
                        "browser_video(layout_state/theater_set)." % text,
                suggestions=[{"tool": "browser_video",
                              "args": {"action": "layout_state"}},
                             {"tool": "browser_video",
                              "args": {"action": "theater_set",
                                       "value": True}}])
        # повторный клик после «скрыт» без expand между попытками
        if getattr(ctx, "pending_hidden_element", "") and not getattr(
                ctx, "expanded_since_hidden", False) and \
                str(args.get("text") or args.get("selector") or "") == \
                getattr(ctx, "pending_hidden_element", ""):
            expanded = getattr(ctx, "expanded_since_hidden", False)
            if not expanded:
                return _deny(
                    "POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND", tool, args,
                    message="Элемент '%s' был скрыт; повторный клик без "
                            "browser_expand_menu запрещён." % text,
                    suggestions=[{"tool": "browser_expand_menu",
                                  "args": {"trigger_text": text}},
                                 {"tool": "browser_find_link",
                                  "args": {"pattern": text}}])
        return ALLOW

    # ------------------------------------------------------------------
    def _check_icon_text_search(self, args) -> PolicyDecision:
        pat = str(args.get("pattern") or "").strip().lower()
        if pat in ICON_TEXTS:
            return _deny(
                "POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND",
                "browser_search_on_page", args,
                message="Поиск на странице текстов иконок ('%s') запрещён — "
                        "они отсутствуют в DOM как текст. Используйте "
                        "browser_video(layout_state)." % pat,
                suggestions=[{"tool": "browser_video",
                              "args": {"action": "layout_state"}}])
        return ALLOW

    # ------------------------------------------------------------------
    def _check_video(self, args, ctx, s) -> PolicyDecision:
        action = str(args.get("action") or "").strip().lower()
        action = {"theater": "toggle_theater_mode",
                  "toggle_theater": "toggle_theater_mode",
                  "theater_mode": "toggle_theater_mode",
                  "set_theater": "theater_set",
                  "wide_mode": "theater_set",
                  "theater_on": "theater_set",
                  "theater_off": "theater_set"}.get(action, action)

        if action == "fullscreen_enter":
            return ALLOW  # ТЗ 5.4: штатное действие, алиасы requestFullscreen

        if action in ("theater_on", "theater_off", "wide_mode",
                      "default_size", "video_check", "browser_snapshot",
                      "quality_set", "cleanup", "download_source"):
            return _deny(
                "POLICY_EVAL_VIDEO_FORBIDDEN", "browser_video", args,
                message="Устаревшее действие '%s'. Для theater используйте "
                        "layout_state + theater_set." % action,
                suggestions=[{"tool": "browser_video",
                              "args": {"action": "layout_state"}},
                             {"tool": "browser_video",
                              "args": {"action": "theater_set",
                                       "value": True}}])

        if action == "fullscreen_enter":
            return _deny(
                "POLICY_THEATER_NOT_FULLSCREEN", "browser_video", args,
                message="Браузерный fullscreen не является theater-режимом. "
                        "Для «широкого экрана» — theater_set.",
                suggestions=[{"tool": "browser_video",
                              "args": {"action": "theater_set",
                                       "value": True}}])

        # blind theater_toggle при unknown layout
        if action == "toggle_theater_mode" and ctx is not None:
            lay = getattr(ctx, "layout", {}) or {}
            conf = lay.get("confidence")
            if lay.get("mode") in (None, "unknown") or (
                    isinstance(conf, (int, float)) and
                    conf < float(s.g("theater_confidence_threshold"))):
                return _deny(
                    "THEATER_STATE_UNKNOWN", "browser_video", args,
                    message="Состояние layout неизвестно/low-confidence — "
                            "blind toggle запрещён. Сначала layout_state, "
                            "затем идемпотентный theater_set.",
                    suggestions=[{"tool": "browser_video",
                                  "args": {"action": "layout_state"}},
                                 {"tool": "browser_video",
                                  "args": {"action": "theater_set",
                                           "value": True}}])

        # episode-действия без очереди
        if action in EPISODE_ACTIONS and action != "episode_list":
            if ctx is not None and not getattr(ctx, "has_episode_queue", False) \
                    and not getattr(ctx, "list_id", None):
                return _deny(
                    "NO_EPISODE_CONTEXT", "browser_video", args,
                    message="Нет контекста episodes/playlist. Сначала "
                            "episode_list или browser_get_context.",
                    suggestions=[{"tool": "browser_video",
                                  "args": {"action": "episode_list"}},
                                 {"tool": "browser_get_context"}])

        # video-mutation vs radio drift (ожидаемое видео)
        if action in VIDEO_MUTATION_ACTIONS and ctx is not None:
            expected = args.get("expected_video_id") or \
                getattr(ctx, "expected_video_id", None)
            actual = getattr(ctx, "video_id", None)
            if expected and actual and expected != actual:
                return _deny(
                    "VIDEO_CONTEXT_CHANGED", "browser_video", args,
                    message="Radio autoplay сменил видео (expected=%s, "
                            "actual=%s). Действия к чужому видео не "
                            "применяются." % (expected, actual),
                    suggestions=[{"tool": "browser_video",
                                  "args": {"action": "episode_open",
                                           "value": expected}},
                                 {"tool": "browser_get_context"}])

        # post-verification включена глобально — требуйте verify flag
        if action in VIDEO_MUTATION_ACTIONS and s.g("verify_after_mutation") \
                and args.get("verify") is False:
            return _deny(
                "VERIFY_REQUIRED", "browser_video", args,
                message="verify_after_mutation=true: отключать пост-проверку "
                        "нельзя.",
                suggestions=[{"tool": "browser_video",
                              "args": {"action": action,
                                       "verify": True}}])

        # blind retry mutate video-действия
        d = self._check_blind_retry("browser_video", args, s)
        if not d.allowed:
            return d
        return ALLOW

    # ------------------------------------------------------------------
    def _check_blind_retry(self, tool, args, s) -> PolicyDecision:
        if self._cm is None:
            return ALLOW
        n = self._cm.count_recent_identical(tool, args)
        max_retries = int(s.g("max_retries") or 2)
        if n >= max_retries:
            last_ok = self._cm.last_mutate_ok(tool, args)
            if last_ok is not True:
                return _deny(
                    "POLICY_BLIND_RETRY_FORBIDDEN", tool, args,
                    message="Повтор #%d идентичного mutate-действия без "
                            "изменения состояния (> max_retries=%d)." %
                            (n + 1, max_retries),
                    suggestions=[{"tool": "browser_get_context"},
                                 {"tool": "browser_screenshot"}])
        return ALLOW

    def check_blind_retry(self, tool, args) -> PolicyDecision:
        """Публичная точка для middleware server.py (для всех mutate tools)."""
        if tool == "browser_video":
            return ALLOW  # уже покрыто в check()
        return self._check_blind_retry(tool, args, self.settings)


def _query_from(url: str) -> str:
    from urllib.parse import unquote, urlparse, parse_qs
    try:
        qs = parse_qs(urlparse(url).query)
        for k in ("search_query", "q", "query"):
            if qs.get(k):
                return unquote(qs[k][0])[:80]
    except ValueError:
        pass
    return ""


__all__ = ["PolicyGuard", "PolicyDecision", "RULE_SOURCES",
           "VIDEO_MUTATION_ACTIONS", "EPISODE_ACTIONS"]
