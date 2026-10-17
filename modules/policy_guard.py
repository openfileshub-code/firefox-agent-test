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
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse

from modules import url_validator as uv

log = logging.getLogger("FirefoxMCP_PolicyGuard")

# ---------------------------------------------------------------
# error_code -> раздел SKILL.md (единый источник истины)
# ---------------------------------------------------------------
RULE_SOURCES = {
    "POLICY_SAME_TAB_REQUIRED": "SKILL.md §Жёсткая политика вкладок",
    "POLICY_NEW_TAB_NOT_ALLOWED": "SKILL.md §Жёсткая политика вкладок",
    "POLICY_USE_HISTORY_BACK": "SKILL.md §Навигация по истории («назад»)",
    "POLICY_DESTRUCTIVE_RELOAD_FORBIDDEN": "SKILL.md §Запрет деструктивных перезагрузок",
    "POLICY_SITE_SEARCH_NOT_REQUESTED": "SKILL.md §Поиск на сайте",
    "POLICY_SCROLL_NOT_REQUESTED": "SKILL.md §Обработка скрытых элементов",
    "POLICY_PLACEHOLDER_URL_FORBIDDEN": "SKILL.md §Запрет placeholder-URL",
    "POLICY_INVALID_YOUTUBE_VIDEO_ID": "SKILL.md §Запрет placeholder-URL",
    "POLICY_EVAL_VIDEO_FORBIDDEN": "SKILL.md §Видео",
    "POLICY_LOOP_DETECTED": "SKILL.md §Повторы",
    # Policy Guard v2.0: перехват намерения + анти-паника (Sprint 2/3)
    "INTENT_REDIRECT_SITE_SEARCH_DESTRUCTIVE":
        "SKILL.md §Протокол восстановления после Intent Redirect",
    "INTENT_REDIRECT_NAVIGATE_TO_SEARCH_URL":
        "SKILL.md §Протокол восстановления после Intent Redirect",
    "INTENT_REDIRECT_ROOT_NAVIGATION":
        "SKILL.md §Неприкосновенность контекста (Context Sanctity)",
    "PANIC_LOOP_DETECTED":
        "SKILL.md §Anti-Panic Lock",
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

#: page_type, при которых перезагрузка/переход на корень сайта уничтожает
#: рабочий контекст (DOM, кэш, результаты поиска) — ТЗ блок 7.
DESTRUCTIVE_PAGE_TYPES = {"watch", "search", "search_results"}


def _norm_host(host: str) -> str:
    """Канонический hostname без scheme/port и префикса 'www.'."""
    h = str(host or "").strip().lower()
    h = h.removeprefix("http://").removeprefix("https://")
    h = h.split("/")[0].split(":")[0]
    return h.removeprefix("www.")


def _history_back_suggestion(url: str = "") -> dict:
    """Готовое немедленное действие «назад по истории» (ТЗ 6.3)."""
    sug = {"tool": "browser_history_back", "args": {},
           "auto_retry": True, "priority": "immediate"}
    if url:
        sug["reason"] = "Возврат по истории вместо browser_navigate на %s" % url
    return sug


def _search_page_suggestion(pattern: str, reason: str = "") -> dict:
    """Готовый немедленный browser_search_on_page с извлечённым pattern
    (ТЗ 2.3/2.4) и флагом приоритета немедленного выполнения (ТЗ 2.4)."""
    sug = {"tool": "browser_search_on_page",
           "args": {"pattern": pattern},
           "priority": "immediate", "auto_retry": True}
    if reason:
        sug["reason"] = reason
    return sug


class PolicyVerdict(Enum):
    """Policy Guard v2.0: три уровня вердикта.

    ALLOWED                — действие разрешено;
    BLOCKED                — классический deny (обратная совместимость);
    INTENT_REDIRECT        — перехват намерения: вместо «стены» агент
                             получает system_injection с точным алгоритмом
                             коррекции (Recovery Injection).
    """

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    INTENT_REDIRECT = "intent_redirect"


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
    #: Recovery Injection (v2.0): директива для LLM с максимальным
    #: приоритетом; передаётся MCP-хосту как __system_injection__.
    system_injection: str = ""
    #: вердикт v2.0 (INTENT_REDIRECT вместо plain BLOCKED)
    verdict: PolicyVerdict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.verdict is None:
            self.verdict = (PolicyVerdict.ALLOWED if self.allowed
                            else PolicyVerdict.BLOCKED)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            # MCP-хосты (Unsloth/Claude Desktop) читают именно `ok` как
            # признак успеха вызова; `allowed` оставлен для обратной
            # совместимости со старыми потребителями middleware.
            "ok": self.allowed,
            "allowed": self.allowed,
            "verdict": self.verdict.value,
        }
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
            if self.system_injection:
                d["system_injection"] = self.system_injection
                # Формат для MCP-хостов (Unsloth/Claude Desktop): поле
                # интерпретируется как System Message следующего хода.
                d["__system_injection__"] = {
                    "priority": "critical",
                    "message": self.system_injection,
                    "required_action": "immediate_correction",
                }
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


def _redirect(code: str, tool: str, args: dict, system_injection: str,
              suggestions: Optional[list] = None,
              message: str = "") -> PolicyDecision:
    """Policy Guard v2.0: перехват намерения (Intercept & Translate).

    Вместо «стены» агент получает system_injection — директиву коррекции
    с максимальным приоритетом и готовые suggested_safe_actions."""
    return PolicyDecision(
        allowed=False,
        verdict=PolicyVerdict.INTENT_REDIRECT,
        error_code=code,
        rule_source=RULE_SOURCES.get(code, ""),
        attempted_tool=tool, attempted_args=args or {},
        suggested_safe_actions=suggestions or [],
        message=message or system_injection,
        system_injection=system_injection,
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
        # ТЗ §15 (C7): анти-цикл по denial'ам. Счётчик одинаковых отказанных
        # вызовов (tool, error_code, аргументы). После max_same_policy_denials
        # подряд срабатывает POLICY_LOOP_DETECTED — жёсткая остановка,
        # запрещающая агенту дальше повторять тот же прогон.
        self._deny_streak: dict = {}
        self._DENY_STREAK_WINDOW_SECONDS = 60

    # ------------------------------------------------------------------
    def _max_denials(self) -> int:
        try:
            from modules import timing_config
            return int(timing_config.get("policy.max_same_policy_denials", 2))
        except Exception:  # noqa: BLE001
            return 2

    def _record_denial(self, tool: str, args: dict, code: str) -> None:
        """Учитывает deny; при превышении бюджета возвращает True (цикл)."""
        import json as _json
        import time as _time

        key = (tool, code, _json.dumps(
            {k: v for k, v in (args or {}).items()
             if not str(k).startswith("__")},
            sort_keys=True, default=str)[:200])
        now = _time.monotonic()
        prev_at, prev_count = self._deny_streak.get(key, (0.0, 0))
        count = prev_count + 1 if now - prev_at < self._DENY_STREAK_WINDOW_SECONDS else 1
        self._deny_streak[key] = (now, count)
        return count > self._max_denials()

    def _loop_detected(self, tool: str, args: dict, code: str,
                       message: str = "", suggestions: Optional[list] = None,
                       ) -> "PolicyDecision":
        """ТЗ §15: жёсткий стоп-сигнал после повторяющихся отказов."""
        log.warning("PolicyGuard LOOP: %s (%s) — повторы отказов исчерпали бюджет",
                    tool, code)
        return _deny(
            "POLICY_LOOP_DETECTED", tool, args,
            message=message or (
                "Обнаружен цикл: один и тот же вызов %s отклонён политикой "
                "более допустимого числа раз. Прекрати повторять это действие; "
                "выбери другой инструмент из suggested_safe_actions или сообщи "
                "пользователю о блокировке." % tool),
            suggestions=suggestions or [],
            confirmation=False)

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

    def _site_search_gate(self, tool: str, args: dict, ctx, s):
        """ТЗ §15 (C7): проверка виртуального browser_navigate_to_search_url
        ДО анти-цикла inplace-навигации.

        Возвращает PolicyDecision (deny) либо None (проверка не применима /
        разрешено). Если допустить сначала inplace-rewrite, второй denied
        new_tab на URL выдачи молча исполнял бы навигацию на search-results
        в обход allow_site_search — порядок проверок нарушен (audit C7).
        """
        if tool not in ("browser_new_tab", "browser_navigate"):
            return None
        url = str(args.get("url") or "")
        if not url or not uv.is_search_results_url(url):
            return None
        site_forbidden = set()
        if ctx is not None:
            site_forbidden = s.site_forbidden_tools(
                getattr(ctx, "site", "") or "")
        virtual_forbidden = (
            "browser_navigate_to_search_url" in site_forbidden)
        global_allowed = bool(s.g("allow_site_search"))
        phrase = args.get("override_phrase")
        if phrase and s.phrase_matches("allow_site_search", phrase):
            return None
        q = _query_from(url)
        if virtual_forbidden:
            code = "POLICY_SITE_SEARCH_NOT_REQUESTED"
            message = (
                "ОШИБКА: Переход на страницу выдачи сайта запрещён политикой "
                "(browser_navigate_to_search_url в forbidden_tools). ЗАПРЕЩЕНО "
                "повторять этот заблокированный вызов и конструировать URL "
                "поиска вручную. НЕМЕДЛЕННО вызови browser_search_on_page с "
                "pattern='%s' (первое действие из suggested_safe_actions) в "
                "этом же ходе, затем перейди по href найденной ссылки." % q)
        elif not global_allowed:
            code = "POLICY_SITE_SEARCH_NOT_REQUESTED"
            message = (
                "Поиск на сайте (URL выдачи) не разрешён: allow_site_search="
                "false. Требуется явная фраза пользователя override_phrase. "
                "ЗАПРЕЩЕНО повторять этот заблокированный вызов и конструировать "
                "URL поиска вручную — НЕМЕДЛЕННО вызови browser_search_on_page "
                "с pattern='%s' (первое действие из suggested_safe_actions) "
                "в этом же ходе." % q)
        else:
            return None
        # ТЗ 2.3/2.4/3.5: первым suggestion — готовый browser_search_on_page
        # с извлечённым из URL pattern и флагом priority="immediate"
        # (немедленное выполнение без переспрашивания пользователя).
        return _deny(
            code, tool, args, message=message,
            suggestions=[_search_page_suggestion(
                q, "Поиск текста на ТЕКУЩЕЙ странице вместо URL выдачи"),
                         {"tool": "browser_find_link", "args": {"pattern": q},
                          "priority": "immediate"}])

    def check_site_search_policy(self, query: str, override_phrase=None,
                                 site: str = "", context=None):
        """Блок 1 ТЗ: политический контроль для виртуальной команды
        site_search (инструмент browser_site_search). Семантика совпадает с
        _site_search_gate для навигации на URL выдачи: если политика сайта
        запрещает browser_navigate_to_search_url и нет дословной фразы
        пользователя (override_phrase / настройка allow_site_search) — deny
        с императивным сообщением и готовыми DOM-альтернативами."""
        s = self.settings
        ctx = context if context is not None else (self._cm.get_active_context()
                                                   if self._cm else None)
        site_forbidden = set()
        if ctx is not None:
            site_forbidden = s.site_forbidden_tools(
                getattr(ctx, "site", "") or "")
        virtual_forbidden = (
            "browser_navigate_to_search_url" in site_forbidden)
        if override_phrase and s.phrase_matches("allow_site_search",
                                                override_phrase):
            return ALLOW
        if not virtual_forbidden and s.g("allow_site_search"):
            return ALLOW
        tool = "browser_site_search"
        args = {"query": query, "override_phrase": override_phrase or ""}
        if virtual_forbidden:
            message = (
                "ОШИБКА: Поиск на сайте (переход на страницу выдачи) "
                "запрещён политикой сайта. НЕМЕДЛЕННО вызови "
                "browser_search_on_page с pattern='%s' (первое действие из "
                "suggested_safe_actions). ЗАПРЕЩЕНО повторять заблокированный "
                "вызов browser_site_search без override_phrase, ЗАПРЕЩЕНО "
                "конструировать URL поиска через browser_navigate и ЗАПРЕЩЕНО "
                "сдаваться — выполни первое действие из suggested_safe_actions "
                "в этом же ходе." % query)
        else:
            message = (
                "Поиск на сайте не разрешён: allow_site_search=false. "
                "Требуется дословная фраза пользователя в override_phrase. "
                "НЕМЕДЛЕННО вызови browser_search_on_page с pattern='%s' "
                "(первое действие из suggested_safe_actions). ЗАПРЕЩЕНО "
                "повторять этот заблокированный вызов и ЗАПРЕЩЕНО сдаваться."
                % query)
        # ТЗ 2.3/2.4/3.5: готовый browser_search_on_page с извлечённым
        # pattern ПЕРВЫМ + флаг немедленного выполнения priority="immediate".
        return _deny(
            "POLICY_SITE_SEARCH_NOT_REQUESTED", tool, args,
            message=message,
            suggestions=[
                _search_page_suggestion(
                    query, "Поиск текста на ТЕКУЩЕЙ странице (без URL выдачи)"),
                {"tool": "browser_find_link", "args": {"pattern": query},
                 "priority": "immediate"},
                {"tool": "browser_get_context", "args": {}},
            ])

    # ------------------------------------------------------------------
    # Policy Guard v2.0: Intercept & Translate (Sprint 2)
    # ------------------------------------------------------------------
    def evaluate_intent(self, tool_name: str, args: dict,
                        context: Optional[dict] = None) -> PolicyDecision:
        """Оценивает НАМЕРЕНИЕ агента до классических hard-rules.

        Вместо возврата сухого `suggested_safe_actions` guard генерирует
        `system_injection` — директиву коррекции с максимальным приоритетом
        для LLM (Recovery Injection). Возвращает PolicyDecision с вердиктом:
          * INTENT_REDIRECT — действие деструктивно для контекста, перехват;
          * ALLOWED         — обычный путь (далее исполняется check()).
        """
        args = args or {}
        if context is None:
            ctx = self.context()
            current_url = getattr(ctx, "url", "") or "" if ctx else ""
        else:
            current_url = str(context.get("current_url")
                              or context.get("url") or "")

        # Случай 1: site search на YouTube — URL-фолбэк /results?search_query=
        # ПЕРЕЗАГРУЖАЕТ страницу и УНИЧТОЖАЕТ текущие результаты.
        if tool_name in ("browser_site_search",
                         "browser_navigate_to_search_results"):
            site = str(args.get("site") or "").lower()
            query = str(args.get("query") or "")
            s = self.settings
            phrase = args.get("override_phrase")
            allowed_by_phrase = bool(
                phrase and s.phrase_matches("allow_site_search", phrase))
            if (site == "youtube" or "youtube" in current_url.lower()) \
                    and not allowed_by_phrase:
                return _redirect(
                    "INTENT_REDIRECT_SITE_SEARCH_DESTRUCTIVE",
                    tool_name, args,
                    system_injection=(
                        "[SYSTEM POLICY OVERRIDE]: %s на YouTube выполняет "
                        "ПЕРЕХОД ПО URL на /results?search_query=, что "
                        "ПЕРЕЗАГРУЖАЕТ страницу и УНИЧТОЖАЕТ текущие "
                        "результаты. Пользователь просит найти видео НА "
                        "ТЕКУЩЕЙ СТРАНИЦЕ. ТВОЙ АЛГОРИТМ:\n"
                        "1. Вызови browser_ui_search_box(query='%s') для "
                        "ввода запроса в поисковую строку YouTube.\n"
                        "2. Дождись загрузки результатов.\n"
                        "3. Используй browser_find_interactive_element("
                        "pattern='%s') для клика по нужному видео.\n"
                        "ЗАПРЕЩЕНО использовать web_search, google-search "
                        "или browser_navigate."
                        % (tool_name, query, query)),
                    suggestions=[
                        {"tool": "browser_ui_search_box",
                         "args": {"query": query},
                         "priority": "immediate", "auto_retry": True},
                        {"tool": "browser_find_interactive_element",
                         "args": {"pattern": query},
                         "priority": "secondary"},
                    ])

        # Случай 2: browser_navigate на поисковый URL выдачи
        if tool_name in ("browser_navigate", "browser_new_tab"):
            url = str(args.get("url") or "")
            if ("search_query=" in url or "/results" in url) \
                    and uv.is_search_results_url(url):
                q = _query_from(url)
                return _redirect(
                    "INTENT_REDIRECT_NAVIGATE_TO_SEARCH_URL",
                    tool_name, args,
                    system_injection=(
                        "[SYSTEM POLICY OVERRIDE]: Прямой переход на URL "
                        "поиска ('%s') запрещён, так как это ПЕРЕЗАГРУЖАЕТ "
                        "страницу и УНИЧТОЖАЕТ контекст текущей страницы. "
                        "Используй browser_ui_search_box(query='%s') для "
                        "поиска через UI сайта, затем "
                        "browser_find_interactive_element(pattern='%s'). "
                        "ЗАПРЕЩЕНО использовать web_search или повторять "
                        "browser_navigate." % (url, q, q)),
                    suggestions=[
                        {"tool": "browser_ui_search_box",
                         "args": {"query": q},
                         "priority": "immediate", "auto_retry": True},
                        {"tool": "browser_find_interactive_element",
                         "args": {"pattern": q}, "priority": "secondary"},
                    ])

            # Случай 3: browser_navigate на главную страницу текущего сайта
            # при активном watch/search контексте (потеря результатов).
            if tool_name == "browser_navigate" and self._is_root_navigation(
                    current_url, url):
                return _redirect(
                    "INTENT_REDIRECT_ROOT_NAVIGATION",
                    tool_name, args,
                    system_injection=(
                        "[SYSTEM POLICY OVERRIDE]: Переход на главную "
                        "страницу запрещён, так как это УНИЧТОЖИТ текущие "
                        "результаты поиска (Context Sanctity). Продолжай "
                        "работу на текущей странице через "
                        "browser_find_interactive_element или "
                        "browser_dom_text_search. ЗАПРЕЩЕНО использовать "
                        "web_search и повторять browser_navigate."),
                    suggestions=[
                        {"tool": "browser_find_interactive_element",
                         "args": {"pattern": ""},
                         "priority": "immediate", "auto_retry": True},
                        {"tool": "browser_dom_text_search",
                         "args": {"pattern": ""}, "priority": "secondary"},
                    ])

        return PolicyDecision(verdict=PolicyVerdict.ALLOWED, allowed=True)

    # ------------------------------------------------------------------
    # Вспомогательные функции v2.0
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_query_from_url(url: str) -> str:
        """Извлекает search_query из URL поиска."""
        return _query_from(url)

    def _is_root_navigation(self, current_url: str, target_url: str) -> bool:
        """Проверяет, является ли переход возвратом на главную того же
        сайта при активном поисковом/просмотровом контексте."""
        if not current_url or not target_url:
            return False
        try:
            cur_parsed = urlparse(current_url)
            tgt_parsed = urlparse(target_url)
        except ValueError:
            return False
        if not tgt_parsed.netloc:
            return False
        cur_host = _norm_host(cur_parsed.netloc)
        tgt_host = _norm_host(tgt_parsed.netloc)
        if not cur_host or cur_host != tgt_host:
            return False
        cur_path = (cur_parsed.path or "/").strip("/")
        destructive_ctx = any(m in cur_path for m in
                              ("results", "watch", "shorts")) \
            or "search_query" in (cur_parsed.query or "") \
            or "v=" in (cur_parsed.query or "")
        tgt_path = (tgt_parsed.path or "/").strip("/")
        return destructive_ctx and tgt_path == ""

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
            decision = self._check_inner(tool, args, context)
        except Exception:  # noqa: BLE001 — guard не должен ронять tool call
            log.exception("PolicyGuard.check(%s) internal error — allow", tool)
            return ALLOW
        # ТЗ §15 (C7): бюджет одинаковых отказов. Каждый deny учитывается;
        # после max_same_policy_denials подряд (окно 60 с) любой дальнейший
        # deny того же вызова заменяется на POLICY_LOOP_DETECTED — жёсткий
        # стоп-сигнал, запрещающий агенту повторять прогон (см. SKILL.md).
        if not decision.allowed and decision.error_code != "POLICY_LOOP_DETECTED":
            if self._record_denial(tool, args, decision.error_code or ""):
                return self._loop_detected(
                    tool, args, decision.error_code or "",
                    suggestions=list(decision.suggested_safe_actions or []))
        return decision

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
                            suggestions=[{"tool": "browser_get_context", "args": {}}],
                        )

        return ALLOW

    # ------------------------------------------------------------------
    def _check_new_tab(self, tool, args, ctx, s) -> PolicyDecision:
        # ТЗ §15 (C7): gate site-search навигации ДО анти-цикла inplace.
        gate = self._site_search_gate(tool, args, ctx, s)
        if gate is not None:
            return gate
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
                # ТЗ 8.3: навигационное suggestion обязано нести флаги
                # немедленного автоповтора без переспрашивания пользователя.
                nav_suggestion = {
                    "tool": "browser_navigate",
                    "args": {"url": url_arg, "new_tab": False},
                    "auto_retry": True,
                    "priority": "immediate",
                }
            return _deny(
                "POLICY_NEW_TAB_NOT_ALLOWED", tool, args,
                message="НОВАЯ ВКЛАДКА ЗАПРЕЩЕНА. Браузер уже открыт. "
                        "НЕМЕДЛЕННО вызови browser_navigate(url='%s', "
                        "new_tab=False) — первое действие из "
                        "suggested_safe_actions (priority='immediate'). "
                        "ЗАПРЕЩЕНО повторять browser_new_tab и ЗАПРЕЩЕНО "
                        "переспрашивать пользователя." % url_arg,
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
            # ТЗ 8.3: флаг немедленного автоповтора обязателен — агент
            # обязан исполнить browser_navigate в этом же ходе без
            # переспрашивания («в новой вкладке или текущей?»).
            nav_suggestion = {
                "tool": "browser_navigate",
                "args": {"url": url_arg, "new_tab": False},
                "auto_retry": True,
                "priority": "immediate",
            }
        suggestions = ([] if nav_suggestion is None else [nav_suggestion]) + [
            {"tool": "browser_find_link", "args": {"pattern": ""}},
            {"tool": "browser_search_on_page", "args": {"pattern": ""}},
        ]
        return _deny(
            "POLICY_NEW_TAB_NOT_ALLOWED", tool, args,
            message="НОВАЯ ВКЛАДКА ЗАПРЕЩЕНА. Браузер уже открыт. "
                    "НЕМЕДЛЕННО вызови browser_navigate(url='%s', "
                    "new_tab=False) — первое действие из "
                    "suggested_safe_actions (priority='immediate'). "
                    "ЗАПРЕЩЕНО повторять browser_new_tab и ЗАПРЕЩЕНО "
                    "переспрашивать пользователя «в новой вкладке или "
                    "текущей?»." % url_arg,
            suggestions=suggestions,
            confirmation=False)

    # ------------------------------------------------------------------
    @staticmethod
    def _norm_nav_url(url: str) -> str:
        """Канонический URL для url_history (совпадает с
        ContextManager._norm_url: host без www + path + query)."""
        try:
            p = urlparse(str(url or ""))
        except ValueError:
            return ""
        if not p.netloc:
            return ""
        host = (p.netloc or "").lower().removeprefix("www.")
        path = (p.path or "/").rstrip("/") or "/"
        q = ("?" + p.query) if p.query else ""
        return host + path + q

    @staticmethod
    def _is_root_path(url: str) -> bool:
        """URL указывает на корень сайта (path пустой или '/')?"""
        try:
            p = urlparse(str(url or ""))
        except ValueError:
            return False
        return (p.path or "/").strip("/") == ""

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
                # Формат отказа (контракт с LLM-агентом): императив в
                # message + browser_find_link ПЕРВЫМ в suggestions.
                # Агент обязан немедленно выполнить первое
                # suggested_safe_action; повтор заблокированного вызова
                # запрещён (см. SKILL.md §Обработка отказов PolicyGuard).
                return _deny(
                    "POLICY_SITE_SEARCH_NOT_REQUESTED", tool, args,
                    message="ОШИБКА: Конструирование URL поиска запрещено! "
                            "Немедленно вызови browser_find_link с аргументом "
                            "pattern=\"%s\", чтобы найти элемент на текущей "
                            "странице. Запрещено повторять этот заблокированный "
                            "вызов и запрещено сдаваться — выполни первое "
                            "действие из suggested_safe_actions."
                            % _query_from(url),
                    suggestions=[{"tool": "browser_find_link",
                                  "args": {"pattern": _query_from(url)}},
                                 {"tool": "browser_search_on_page",
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
        # --- ТЗ блок 6 (6.3): возврат по истории вместо navigate ---------
        # «назад / предыдущая страница» — только browser_history_back;
        # навигация ВПЕРЁД на URL, который уже есть в посещённой истории
        # текущей вкладки (url_history), блокируется с готовым suggestion.
        if tool == "browser_navigate" and not args.get("new_tab") \
                and ctx is not None:
            visited = list(getattr(ctx, "url_history", []) or [])
            target_norm = self._norm_nav_url(url)
            cur_norm = self._norm_nav_url(getattr(ctx, "url", "") or "")
            if target_norm and visited and target_norm in visited[:-1] \
                    and target_norm != cur_norm:
                return _deny(
                    "POLICY_USE_HISTORY_BACK", tool, args,
                    message="Возврат на ранее посещённую страницу '%s' "
                            "выполняется ТОЛЬКО через browser_history_back "
                            "(browser_go_back). browser_navigate — это переход "
                            "ВПЕРЁД по URL; он теряет позицию в истории. "
                            "НЕМЕДЛЕННО вызови browser_history_back — первое "
                            "действие из suggested_safe_actions "
                            "(priority='immediate'). ЗАПРЕЩЕНО повторять "
                            "заблокированный browser_navigate." % url,
                    suggestions=[_history_back_suggestion(url)])
        # --- ТЗ блок 7 (7.2/7.4): запрет деструктивных перезагрузок ------
        # Переход на корень текущего сайта (или тот же URL) при активном
        # watch/search_results контексте уничтожает DOM, кэш и результаты
        # поиска — блокируется; поиск выполняется строго на текущей
        # открытой вкладке через browser_search_on_page.
        if tool == "browser_navigate" and ctx is not None:
            page_type = getattr(ctx, "page_type", "") or ""
            if page_type in DESTRUCTIVE_PAGE_TYPES:
                cur_host = _norm_host(getattr(ctx, "site", "")
                                      or getattr(ctx, "url", "") or "")
                tgt_host = _norm_host(url)
                cur_norm = self._norm_nav_url(getattr(ctx, "url", "") or "")
                tgt_norm = self._norm_nav_url(url)
                root_hit = bool(cur_host) and tgt_host == cur_host \
                        and self._is_root_path(url)
                same_hit = bool(cur_norm) and tgt_norm == cur_norm
                if root_hit or same_hit:
                    what = ("тот же URL" if same_hit
                            else "главную страницу текущего сайта")
                    return _deny(
                        "POLICY_DESTRUCTIVE_RELOAD_FORBIDDEN", tool, args,
                        message="ПЕРЕЗАГРУЗКА СТРАНИЦЫ ЗАПРЕЩЕНА! Переход на "
                                "%s ('%s') при открытой странице типа '%s' "
                                "уничтожит текущий DOM, кэш и результаты "
                                "поиска. Выполняй browser_search_on_page "
                                "СТРОГО на текущей открытой вкладке без "
                                "перезагрузки. НЕМЕДЛЕННО вызови "
                                "browser_search_on_page — первое действие из "
                                "suggested_safe_actions (priority='immediate')."
                                % (what, url, page_type),
                        suggestions=[_search_page_suggestion(
                            "", "Поиск выполняется на ТЕКУЩЕЙ странице без "
                                "перезагрузки"),
                            {"tool": "browser_get_context", "args": {}}])
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
                            suggestions=[{"tool": "browser_get_context", "args": {}},
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
                # Блок 3 ТЗ: агент часто путает эпизоды (плейлист/сериал)
                # и главы (таймкоды одного видео). При отсутствии очереди
                # эпизодов первым предлагаемым действием становится
                # chapter_list — fallback для «списка эпизодов» обычного
                # видео, чтобы агент не парсил DOM костылями.
                return _deny(
                    "NO_EPISODE_CONTEXT", "browser_video", args,
                    message="Нет контекста episodes/playlist. Если вы ищете "
                            "таймкоды (главы) текущего видео — немедленно "
                            "вызови browser_video(action=\"chapter_list\"). "
                            "Если это сериал/плейлист — сначала episode_list. "
                            "Запрещено повторять этот заблокированный вызов.",
                    suggestions=[{"tool": "browser_video",
                                  "args": {"action": "chapter_list"}},
                                 {"tool": "browser_video",
                                  "args": {"action": "episode_list"}},
                                 {"tool": "browser_get_context", "args": {}}])

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
                                 {"tool": "browser_get_context", "args": {}}])

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
                    suggestions=[{"tool": "browser_get_context", "args": {}},
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


__all__ = ["PolicyGuard", "PolicyDecision", "PolicyVerdict", "_redirect",
           "RULE_SOURCES",
           "VIDEO_MUTATION_ACTIONS", "EPISODE_ACTIONS"]
