"""Менеджер активного контекста браузера (ТЗ §6.3, Этап 3).

Хранит зафиксированный контекст текущей вкладки/сайта/видео и обновляет
его после каждого tool result. Используется policy_guard для:
- same-tab lock (запрет выхода из текущей вкладки без override);
- expected_video_id (детект radio autoplay drift);
- allowed/forbidden tools для `browser_get_context`.

Контекст строится из ответов демона (`context.get` / `navigate` /
`video_action`), поэтому модуль не зависит от Playwright и тестируется
на чистых словарях.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Инструменты, блокируемые при same_tab_required (policy same_tab_context)
SAME_TAB_BLOCKED_TOOLS = (
    "browser_new_tab",
    "browser_switch_tab",
    "browser_navigate_to_search_url",
)

#: Anti-Panic Lock (Policy Guard v2.0, Sprint 3): инструменты, временно
#: блокируемые после INTENT_REDIRECT, чтобы агент не ушёл в паническую
#: петлю («сбежать» во внешний поиск или на главную страницу).
PANIC_LOCK_DEFAULT_TOOLS = (
    "web_search",
    "google-search",
    "duckduckgo-search",
    "browser_site_search",
    "browser_navigate_to_search_results",
    "browser_navigate",
)


def _hostname(url: str) -> str:
    try:
        host = (urlparse(url or "").netloc or "").lower()
        return host.removeprefix("www.")
    except ValueError:
        return ""


def detect_page_type(url: str, dom_markers: Optional[dict] = None) -> str:
    """home|watch|playlist|radio|channel|search|shorts|live|series|other."""
    dom = dom_markers or {}
    u = (url or "").lower()
    host = _hostname(u)
    if not host:
        return "blank"
    if any(h in host for h in YOUTUBE_HOSTS):
        qs = parse_qs(urlparse(u).query)
        # Radio-плейлист: явный start_radio=1 либо RD*-list. Список берётся
        # из НЕНОРМАЛИЗОВАНного URL — префикс «RD» регистрозависимый, а u
        # ниже по функции приведён к нижнему регистру.
        raw_qs = parse_qs(urlparse(url or "").query)
        if raw_qs.get("start_radio", [""])[0] == "1" or \
                (raw_qs.get("list", [""])[0].startswith("RD")):
            return "radio"
        if "/results" in u or "search_query" in qs:
            return "search"
        if "/shorts/" in u:
            return "shorts"
        if "/watch" in u and qs.get("v"):
            return "watch"
        if "/playlist" in u or (qs.get("list") and not dom.get("on_watch")):
            return "playlist"
        if "/live" in u:
            return "live"
        if ("/@" in u) or "/channel/" in u or "/user/" in u or "/c/" in u:
            return "channel"
        if dom.get("on_watch"):
            return "watch"
        path = urlparse(u).path.rstrip("/") or "/"
        if path in ("", "/") and not qs.get("vl"):
            return "home"
        return "other"
    if dom.get("on_watch"):
        return "watch"
    if "/search" in u or "q=" in u:
        return "search"
    return "other"


@dataclass
class ContextCheck:
    """Результат проверки video-контекста (radio drift)."""

    ok: bool
    error_code: str = ""
    expected_video_id: Optional[str] = None
    actual_video_id: Optional[str] = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error_code": self.error_code,
            "expected_video_id": self.expected_video_id,
            "actual_video_id": self.actual_video_id,
            "message": self.message,
        }


@dataclass
class BrowserContext:
    tab_id: str = ""
    url: str = ""
    site: str = ""
    page_type: str = "blank"
    video_id: Optional[str] = None
    list_id: Optional[str] = None
    index: Optional[int] = None
    is_radio: bool = False
    same_tab_required: bool = True
    same_tab_locked_by: str = ""
    allowed_tools: list = field(default_factory=list)
    forbidden_tools: list = field(default_factory=list)
    expected_video_id: Optional[str] = None
    last_verified_state: dict = field(default_factory=dict)
    layout: dict = field(default_factory=dict)
    title: str = ""
    has_episode_queue: bool = False
    history: list = field(default_factory=list)  # последние (tool, sig, ok)
    #: последние посещённые URL вкладки — для POLICY_USE_HISTORY_BACK (ТЗ 6.3)
    url_history: list = field(default_factory=list)  # список norm-URL, без повторов
    #: маркер «элемент скрыт» для POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND
    pending_hidden_element: str = ""
    expanded_since_hidden: bool = False

    # ------------------------------------------------------------------
    @property
    def hostname(self) -> str:
        return self.site

    def is_youtube(self) -> bool:
        return any(h in (self.site or "") for h in YOUTUBE_HOSTS)

    def to_public_dict(self) -> dict:
        return {
            "tab_id": self.tab_id,
            "url": self.url,
            "site": self.site,
            "page_type": self.page_type,
            "video_id": self.video_id,
            "list_id": self.list_id,
            "index": self.index,
            "is_radio": self.is_radio,
            "same_tab_required": self.same_tab_required,
            "same_tab_locked_by": self.same_tab_locked_by,
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "expected_video_id": self.expected_video_id,
            "layout": dict(self.layout),
            "title": self.title,
            "has_episode_queue": self.has_episode_queue,
            "last_verified_state": dict(self.last_verified_state),
        }

    # ------------------------------------------------------------------
    def apply_settings(self, settings) -> None:
        """Применить user_tool_settings: same_tab default + allowed/forbidden."""
        if settings is None:
            return
        try:
            self.same_tab_required = bool(settings.g("same_tab_by_default")) \
                if not self.same_tab_locked_by else True
            site_rules = settings.get_site_rules(self.site) or {}
            intents = site_rules.get("intents") or {}
            allowed: set = set()
            forbidden: set = set()
            for it in intents.values():
                allowed.update(it.get("allowed_tools") or [])
                forbidden.update(it.get("forbidden_tools") or [])
            self.allowed_tools = sorted(allowed) if allowed else []
            self.forbidden_tools = sorted(forbidden)
        except Exception:  # noqa: BLE001 - настройки не должны ронять контекст
            pass


class ContextManager:
    def __init__(self, settings=None):
        self._ctx = BrowserContext()
        self._settings = settings
        #: Anti-Panic Lock (Policy Guard v2.0, Sprint 3):
        #: {"until": float, "blocked_tools": tuple[str, ...]} или None.
        self.panic_lock: Optional[dict] = None

    # ------------- basic API -------------
    def get_active_context(self) -> BrowserContext:
        return self._ctx

    # ------------- Anti-Panic Lock (Policy Guard v2.0) -------------
    def set_panic_lock(self, duration_ms: int = 10000,
                      blocked_tools: Optional[list] = None) -> None:
        """Устанавливает временный лок на деструктивные инструменты.

        Срабатывает после INTENT_REDIRECT: агент не должен «сбегать»
        во внешний поиск (web_search / google-search) или на главную
        страницу (browser_navigate) — это паническая петля, которая
        УНИЧТОЖАЕТ контекст текущей страницы (Context Sanctity).
        """
        try:
            duration_s = max(0.0, float(duration_ms) / 1000.0)
        except (TypeError, ValueError):
            duration_s = 10.0
        self.panic_lock = {
            "until": time.time() + duration_s,
            "blocked_tools": tuple(blocked_tools) if blocked_tools else tuple(
                PANIC_LOCK_DEFAULT_TOOLS),
        }

    def clear_panic_lock(self) -> None:
        """Снять лок досрочно (например, после успешного recovery-действия)."""
        self.panic_lock = None

    def is_tool_blocked(self, tool_name: str) -> bool:
        """Проверяет, заблокирован ли инструмент активным Anti-Panic Lock."""
        lock = self.panic_lock
        if not lock:
            return False
        if time.time() > lock["until"]:
            self.panic_lock = None
            return False
        return tool_name in lock["blocked_tools"]

    def set_settings(self, settings) -> None:
        self._settings = settings
        self._ctx.apply_settings(settings)

    # ------------- history of visited URLs -------------
    @staticmethod
    def _norm_url(url: str) -> str:
        """Канонический URL для url_history: hostname без www + path + query."""
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

    def note_pending_navigation(self, url: str) -> None:
        """Перед фактической навигацией записать ТЕКУЩИЙ URL в историю
        посещений вкладки — это база для POLICY_USE_HISTORY_BACK (ТЗ 6.3):
        guard видит, что целевой URL уже был посещён, и требует
        browser_history_back вместо перехода вперёд."""
        ctx = self._ctx
        cur_norm = self._norm_url(ctx.url)
        nxt_norm = self._norm_url(url)
        if not cur_norm or cur_norm == nxt_norm:
            return
        if cur_norm in ctx.url_history:
            ctx.url_history.remove(cur_norm)
        ctx.url_history.append(cur_norm)
        ctx.url_history = ctx.url_history[-25:]

    def record_visited_url(self, url: str) -> None:
        """Зафиксировать успешный переход по URL (после cmd_navigate)."""
        ctx = self._ctx
        norm = self._norm_url(url)
        if not norm:
            return
        for h in list(ctx.url_history):
            if h == norm:
                ctx.url_history.remove(h)
        ctx.url_history.append(norm)
        ctx.url_history = ctx.url_history[-25:]

    # ------------- updates -------------
    def update_from_result(self, tool: str, args: dict, result: dict) -> BrowserContext:
        """Обновить контекст по ответу демона на tool call."""
        ctx = self._ctx
        if not isinstance(result, dict):
            return ctx
        ok = bool(result.get("ok", True))

        url = result.get("url") or (result.get("state") or {}).get("url") \
            or result.get("page_url")
        if isinstance(url, str) and url:
            ctx.url = url
            ctx.site = _hostname(url)
            qs = parse_qs(urlparse(url).query)
            vid = (qs.get("v") or [""])[0] or None
            if vid and VIDEO_ID_RE.match(vid):
                ctx.video_id = vid
            elif "/watch" not in url and "/shorts/" not in url:
                # ушли с watch-страницы — видео больше нет
                if ctx.site != _hostname(url) or "/watch" not in url:
                    if not (qs.get("v") or [""])[0]:
                        ctx.video_id = None
            lid = (qs.get("list") or [""])[0] or None
            ctx.list_id = lid or (None if "/watch" in url and not lid else ctx.list_id)
            idx = (qs.get("index") or [""])[0]
            ctx.index = int(idx) if idx.isdigit() else ctx.index
            ctx.is_radio = (
                (qs.get("start_radio", [""])[0] == "1")
                or bool(lid and lid.startswith("RD"))
            )

        # видео из payload адаптера/state
        for key in ("video_id", "videoId"):
            v = result.get(key)
            if isinstance(v, str) and VIDEO_ID_RE.match(v):
                ctx.video_id = v
        state = result.get("state")
        if not isinstance(state, dict):
            state = {}
        v = state.get("video_id") or result.get("video_id")
        if isinstance(v, str) and VIDEO_ID_RE.match(v):
            ctx.video_id = v
        lay = result.get("layout") or state.get("layout") or {}
        if isinstance(lay, dict) and lay:
            ctx.layout = {
                "mode": lay.get("mode") or state.get("layout_mode") or "unknown",
                "theater_mode": lay.get("theater_mode",
                                        state.get("theater_mode")),
                "fullscreen": lay.get("fullscreen", state.get("fullscreen")),
                "confidence": lay.get("confidence"),
                "evidence": lay.get("evidence", []),
            }
        elif "theater_mode" in state:
            ctx.layout = {
                "mode": ("theater" if state.get("theater_mode") else "default"),
                "theater_mode": state.get("theater_mode"),
                "fullscreen": state.get("fullscreen"),
                "confidence": None, "evidence": ["legacy_state_class"],
            }
        title = result.get("title")
        if isinstance(title, str) and title and tool != "browser_tabs":
            ctx.title = title[:240]

        if isinstance(result.get("episodes"), list):
            ctx.has_episode_queue = bool(result["episodes"])

        tab_id = result.get("tab_id") or result.get("page_id")
        if isinstance(tab_id, str) and tab_id:
            ctx.tab_id = tab_id

        ctx.page_type = detect_page_type(ctx.url, {
            "on_watch": bool(ctx.video_id),
        })

        # expected_video_id фиксируется автоматически на watch/radio —
        # это база для детекта radio drift (§6.3 ТЗ).
        if ctx.video_id and (ctx.expected_video_id is None or ok):
            if ctx.expected_video_id != ctx.video_id and ctx.expected_video_id \
                    and ctx.video_id != ctx.expected_video_id \
                    and tool in ("browser_navigate", "browser_new_tab",
                                 "episode_open"):
                pass  # явная навигация — ожидаем новое видео
            if tool in ("browser_navigate", "browser_new_tab",
                        "browser_click_text", "browser_click_selector",
                        "episode_open") or ctx.expected_video_id is None:
                ctx.expected_video_id = ctx.video_id

        if ok and isinstance(result.get("verified"), dict):
            ctx.last_verified_state = result["verified"]

        sig = self._mutation_signature(tool, args)
        if sig:
            ctx.history.append((tool, sig, ok))
            ctx.history = ctx.history[-20:]

        # маркер «скрытого элемента»: клик завершился ошибкой visibility —
        # повторный клик по тому же тексту без browser_expand_menu запрещён
        err_text = str(result.get("error") or result.get("message") or "")
        if tool in ("browser_click_text", "browser_click_selector") and not ok:
            if any(m in err_text.lower() for m in
                   ("hidden", "скрыт", "not visible", "visible")):
                ctx.pending_hidden_element = str(args.get("text")
                                                 or args.get("selector") or "")
                ctx.expanded_since_hidden = False
        if tool == "browser_expand_menu" and ok:
            ctx.expanded_since_hidden = True
        if tool in ("browser_click_text", "browser_click_selector") and ok:
            ctx.pending_hidden_element = ""
            ctx.expanded_since_hidden = False

        ctx.apply_settings(self._settings)
        return ctx

    @staticmethod
    def _mutation_signature(tool: str, args: dict) -> Optional[str]:
        mutate = {
            "browser_scroll": lambda a: f"{a.get('direction')}|{a.get('amount')}",
            "browser_new_tab": lambda a: str(a.get("url") or ""),
            "browser_navigate": lambda a: str(a.get("url") or ""),
            "browser_click_text": lambda a: f"{a.get('text')}|{a.get('selector')}",
            "browser_click_selector": lambda a: str(a.get("selector")),
            "browser_video": lambda a: f"{a.get('action')}|{a.get('value')}",
            "browser_press_key": lambda a: str(a.get("key")),
        }
        fn = mutate.get(tool)
        return fn(args or {}) if fn else None

    def last_mutate_ok(self, tool: str, args: dict) -> Optional[bool]:
        sig = self._mutation_signature(tool, args)
        if not sig:
            return None
        for t, s, ok in reversed(self._ctx.history):
            if t == tool and s == sig:
                return ok
        return None

    def count_recent_identical(self, tool: str, args: dict) -> int:
        sig = self._mutation_signature(tool, args)
        if not sig:
            return 0
        n = 0
        for t, s, _ok in reversed(self._ctx.history):
            if t == tool and s == sig:
                n += 1
            else:
                break
        return n

    # ------------- same-tab lock -------------
    def lock_same_tab(self, reason: str = "") -> None:
        self._ctx.same_tab_required = True
        self._ctx.same_tab_locked_by = reason or "policy"

    def unlock_same_tab(self, override_phrase: Optional[str], settings) -> bool:
        """Снять lock только при whitelist override-фразе (ТЗ §risk)."""
        if settings is None or not settings.phrase_matches("allow_new_tab",
                                                           override_phrase):
            return False
        self._ctx.same_tab_required = False
        self._ctx.same_tab_locked_by = ""
        return True

    # ------------- video id -------------
    def set_expected_video_id(self, video_id: Optional[str]) -> None:
        self._ctx.expected_video_id = video_id if (
            video_id and VIDEO_ID_RE.match(video_id)) else None

    def check_video_context(self, actual_id: Optional[str]) -> ContextCheck:
        exp = self._ctx.expected_video_id
        if not exp:
            return ContextCheck(ok=True, actual_video_id=actual_id,
                                message="expected_video_id не зафиксирован")
        if not actual_id:
            return ContextCheck(ok=True, expected_video_id=exp,
                                message="actual video_id недоступен — skip")
        if actual_id == exp:
            return ContextCheck(ok=True, expected_video_id=exp,
                                actual_video_id=actual_id)
        return ContextCheck(
            ok=False, error_code="VIDEO_CONTEXT_CHANGED",
            expected_video_id=exp, actual_video_id=actual_id,
            message=("Radio autoplay сменил видео: expected=%s actual=%s. "
                     "Действия к чужому видео не применяются." % (exp, actual_id)),
        )


__all__ = [
    "ContextManager", "BrowserContext", "ContextCheck",
    "detect_page_type", "VIDEO_ID_RE", "SAME_TAB_BLOCKED_TOOLS",
]
