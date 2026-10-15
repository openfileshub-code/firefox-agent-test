"""Тесты modules/url_validator.py (Этап 5 ТЗ).

API модуля: validate_navigation_url возвращает UrlValidationResult
(поля valid/error_code/rule_source/normalized_url/video_id/is_search_url),
а не dict — тесты используют атрибуты и to_dict().
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.url_validator import (  # noqa: E402
    extract_youtube_video_id,
    is_placeholder_url,
    is_search_results_url,
    is_valid_youtube_video_id,
    normalize_youtube_watch_url,
    validate_navigation_url,
)


class TestPlaceholderDetection:
    def test_angle_brackets(self):
        r = validate_navigation_url("https://site.ru/<id>")
        assert not r.valid and r.error_code == "POLICY_PLACEHOLDER_URL_FORBIDDEN"

    def test_template_braces(self):
        r = validate_navigation_url("https://site.ru/watch?v={video_id}")
        assert not r.valid and r.error_code == "POLICY_PLACEHOLDER_URL_FORBIDDEN"

    def test_your_marker(self):
        r = validate_navigation_url("https://site.ru/[your-video-id]")
        assert not r.valid

    def test_site_tld(self):
        r = validate_navigation_url("https://mysite.site.tld/page")
        assert not r.valid and r.error_code == "POLICY_PLACEHOLDER_URL_FORBIDDEN"

    def test_ellipsis(self):
        r = validate_navigation_url("https://site.ru/path/...")
        assert not r.valid

    def test_placeholder_word_in_query_value(self):
        r = validate_navigation_url("https://site.ru/?q=placeholder")
        assert not r.valid and r.error_code == "POLICY_PLACEHOLDER_URL_FORBIDDEN"

    def test_legit_urls_pass(self):
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://example.com",
                    "https://ru.wikipedia.org/wiki/Python"):
            r = validate_navigation_url(url)
            assert r.valid, url

    def test_contest_not_false_positive(self):
        """'test' внутри слова (contest) не является placeholder-лексикой."""
        assert validate_navigation_url("https://site.ru/contest").valid


class TestVideoIdValidation:
    def test_invalid_video_id_rejected(self):
        r = validate_navigation_url("https://www.youtube.com/watch?v=abc")
        assert not r.valid
        assert r.error_code == "POLICY_INVALID_YOUTUBE_VIDEO_ID"

    def test_valid_video_id_accepted(self):
        r = validate_navigation_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123")
        assert r.valid
        assert r.video_id == "dQw4w9WgXcQ"

    def test_shorts_id_accepted(self):
        r = validate_navigation_url("https://youtube.com/shorts/abcdefghijk")
        assert r.valid

    def test_youtu_be_extract(self):
        assert extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ") \
            == "dQw4w9WgXcQ"

    def test_search_url_blocked_without_override(self):
        r = validate_navigation_url(
            "https://www.youtube.com/results?search_query=python+3")
        assert not r.valid
        assert r.error_code == "POLICY_SITE_SEARCH_NOT_REQUESTED"
        assert r.is_search_url is True

    def test_search_url_allowed_with_override(self):
        r = validate_navigation_url(
            "https://www.youtube.com/results?search_query=python",
            allow_search_override=True)
        assert r.valid and r.is_search_url


class TestHelpers:
    def test_empty_url(self):
        r = validate_navigation_url("")
        assert not r.valid

    def test_non_http_garbage(self):
        r = validate_navigation_url("javascriptalert")
        assert not r.valid

    def test_is_placeholder_helper(self):
        assert is_placeholder_url("https://x.ru/{id}")
        assert not is_placeholder_url("https://x.ru/page?id=123")

    def test_is_search_results_url(self):
        assert is_search_results_url("https://site.ru/search?q=abc")
        assert not is_search_results_url("https://site.ru/watch?v=abc")

    def test_valid_id_format(self):
        assert is_valid_youtube_video_id("dQw4w9WgXcQ")
        assert not is_valid_youtube_video_id("short")
        assert not is_valid_youtube_video_id("bad!charact")

    def test_normalize_watch_url(self):
        u = normalize_youtube_watch_url("dQw4w9WgXcQ", list_id="PL1", index=3)
        assert "v=dQw4w9WgXcQ" in u and "list=PL1" in u and "index=3" in u

    def test_normalize_rejects_bad_id(self):
        try:
            normalize_youtube_watch_url("abc")
            assert False, "ожидался ValueError"
        except ValueError:
            pass

    def test_to_dict_shape(self):
        d = validate_navigation_url("https://site.ru/{x}").to_dict()
        assert d["valid"] is False and "error_code" in d
