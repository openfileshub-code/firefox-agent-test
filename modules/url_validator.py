"""Валидация URL перед навигацией (ТЗ §6.4, Этап 5).

Правила:
- placeholder-URL блокируется всегда;
- YouTube video id проверяется по `^[A-Za-z0-9_-]{11}$`;
- search-results URL считается site-search и требует override;
- canonical watch URL разрешён только с реальным video_id;
- поддерживаются youtube.com / youtu.be / music.youtube.com.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, parse_qs, urlencode

YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

SEARCH_URL_MARKERS = (
    "/results?search_query=",
    "/search?q=",
    "/search?query=",
    "/query?",
    "?search=",
    "&search=",
)

PLACEHOLDER_TOKENS = ("...", "{", "}", "[", "]", "<", ">", "$")
PLACEHOLDER_WORDS = ("VIDEO_ID", "video_id", "placeholder",
                     "your_", "site.tld", "xxx", "yyy")

_TEST_WORD_RE = re.compile(r"(^|[^a-z0-9_-])test([^a-z0-9_-]|$)", re.I)


def _norm(url: str) -> str:
    return (url or "").strip()


def is_youtube_host(url: str) -> bool:
    try:
        host = (urlsplit(_norm(url)).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def extract_youtube_video_id(url: str) -> Optional[str]:
    """Извлечь v-параметр из watch/embed/shorts/live/youtu.be URL."""
    u = _norm(url)
    if not u:
        return None
    try:
        parts = urlsplit(u if "//" in u else "https://" + u)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    # youtu.be/<id>
    if host == "youtu.be":
        vid = parts.path.lstrip("/").split("/")[0]
        return vid if YOUTUBE_VIDEO_ID_RE.match(vid) else None
    qs = parse_qs(parts.query)
    for key in ("v", "video_id"):
        for value in qs.get(key, []):
            if YOUTUBE_VIDEO_ID_RE.match(value):
                return value
    m = re.search(r"/(?:embed|shorts|live|v)/([A-Za-z0-9_-]{11})(?:[/?#]|$)",
                  parts.path or "")
    return m.group(1) if m else None


def is_valid_youtube_video_id(video_id: str) -> bool:
    return bool(video_id and YOUTUBE_VIDEO_ID_RE.match(str(video_id)))


def is_placeholder_url(url: str) -> bool:
    u = _norm(url)
    if not u:
        return True
    low = u.lower()
    for token in PLACEHOLDER_TOKENS:
        if token in u:
            return True
    for word in PLACEHOLDER_WORDS:
        if word.lower() in low:
            return True
    if _TEST_WORD_RE.search(low):
        return True
    # placeholder-значения в v-/q-параметрах (VIDEO_ID, your_id и т.п.) —
    # проверяются ДО формата: невалидный по длине id = реальный id с ошибкой,
    # его ловит POLICY_INVALID_YOUTUBE_VIDEO_ID, а не placeholder-фильтр
    for m in re.finditer(r"[?&](?:v|video_id|search_query|q)=([^&#]*)", u):
        val = m.group(1)
        vl = val.lower()
        if any(w in vl for w in ("video_id", "your_", "placeholder", "xxx",
                                 "yyy", "example")) or "<" in val or ">" in val:
            return True
    return False


def is_search_results_url(url: str) -> bool:
    u = _norm(url).lower()
    return any(marker in u for marker in SEARCH_URL_MARKERS)


def normalize_youtube_watch_url(video_id: str, list_id: Optional[str] = None,
                                index: Optional[int] = None,
                                start_radio: Optional[bool] = None) -> str:
    if not is_valid_youtube_video_id(video_id):
        raise ValueError(f"Невалидный YouTube video_id: {video_id!r}")
    params = {"v": video_id}
    if list_id:
        params["list"] = list_id
    if index is not None:
        params["index"] = str(int(index))
    if start_radio:
        params["start_radio"] = "1"
    return "https://www.youtube.com/watch?" + urlencode(params)


@dataclass
class UrlValidationResult:
    valid: bool
    error_code: Optional[str] = None
    rule_source: str = ""
    message: str = ""
    normalized_url: Optional[str] = None
    video_id: Optional[str] = None
    is_search_url: bool = False
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "valid": self.valid,
            "error_code": self.error_code,
            "message": self.message,
            "normalized_url": self.normalized_url,
            "video_id": self.video_id,
            "is_search_url": self.is_search_url,
        }
        if self.rule_source:
            out["rule_source"] = self.rule_source
        if self.details:
            out["details"] = self.details
        return out


def validate_navigation_url(url: str, context=None, settings=None, *,
                            allow_search_override: bool = False) -> UrlValidationResult:
    """Проверить URL для browser_navigate/browser_new_tab.

    Args:
        url: целевой URL.
        context: ActionContext (используется site для правил).
        settings: UserSettings (policies.no_placeholder_urls, global flags).
        allow_search_override: guard уже проверил override-фразу пользователя
            на site-search → True снимает запрет search URL.
    """
    u = _norm(url)
    forbid_placeholder = True
    if settings is not None:
        forbid_placeholder = (
            settings.policy_enabled("no_placeholder_urls")
            and settings.g("forbid_placeholder_urls")
        )

    if not u or "://" not in u and "." not in u:
        return UrlValidationResult(False, "POLICY_PLACEHOLDER_URL_FORBIDDEN",
                                   "SKILL.md §Запрет placeholder-URL / §Placeholder URL Prohibition",
                                   "URL пуст или не похож на адрес")

    if forbid_placeholder and is_placeholder_url(u):
        return UrlValidationResult(
            False, "POLICY_PLACEHOLDER_URL_FORBIDDEN",
            "SKILL.md §Запрет placeholder-URL / §Placeholder URL Prohibition",
            "URL содержит placeholder ('...', '{}', VIDEO_ID, example, test); "
            "извлеки реальный video_id со страницы и построй canonical URL",
            details={"url": u},
        )

    result = UrlValidationResult(True, normalized_url=u)

    if is_search_results_url(u):
        result.is_search_url = True
        if not allow_search_override:
            result.valid = False
            result.error_code = "POLICY_SITE_SEARCH_NOT_REQUESTED"
            result.rule_source = ("SKILL.md §Поиск на сайте / §No site search "
                                  "without explicit request")
            result.message = ("Переход на страницу поиска сайта запрещён без "
                              "явного запроса пользователя")
        return result

    if is_youtube_host(u):
        vid = extract_youtube_video_id(u)
        result.video_id = vid
        watch_like = ("/watch" in u or "youtu.be/" in u or "/shorts/" in u
                      or "/live/" in u or "/embed/" in u)
        if watch_like:
            m = re.search(r"[?&]v=([^&#]*)", u)
            raw_v = m.group(1) if m else (u.split("youtu.be/")[-1].split("?")[0]
                                          .split("/")[0] if "youtu.be/" in u else "")
            if raw_v and not is_valid_youtube_video_id(raw_v):
                result.valid = False
                result.error_code = "POLICY_INVALID_YOUTUBE_VIDEO_ID"
                result.rule_source = ("SKILL.md §Запрет placeholder-URL / "
                                      "§Placeholder URL Prohibition")
                result.message = ("YouTube video_id не соответствует формату "
                                  "11 символов [A-Za-z0-9_-]")
                result.details = {"video_id_raw": raw_v[:32]}
            elif raw_v:
                result.normalized_url = normalize_youtube_watch_url(
                    raw_v,
                    *(parse_qs(urlsplit(u).query).get("list", [None])[0],),
                    None,
                ) if "list" in parse_qs(urlsplit(u).query) else u
    return result


__all__ = [
    "YOUTUBE_VIDEO_ID_RE", "is_valid_youtube_video_id", "is_placeholder_url",
    "is_search_results_url", "is_youtube_host", "extract_youtube_video_id",
    "normalize_youtube_watch_url", "validate_navigation_url",
    "UrlValidationResult",
]
