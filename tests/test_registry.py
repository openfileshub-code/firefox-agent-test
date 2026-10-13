"""Unit-тесты реестра адаптеров и базового класса (ТЗ 5.1).

Проверяют маршрутизацию по URL/DOM без запуска браузера.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from adapters import (
    GenericHTML5Adapter,
    REGISTRY,
    RutubeAdapter,
    TwitchAdapter,
    VkAdapter,
    YoutubeAdapter,
    get_adapter_class,
)
from adapters.base import BaseVideoAdapter, load_selector_config


class TestRegistryRouting:
    def test_youtube_watch_url(self):
        cls = get_adapter_class("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert cls is YoutubeAdapter

    def test_youtu_be_short_url(self):
        assert get_adapter_class("https://youtu.be/abc123") is YoutubeAdapter

    def test_twitch_channel_url(self):
        assert get_adapter_class("https://twitch.tv/channel") is TwitchAdapter

    def test_rutube_url(self):
        assert get_adapter_class("https://rutube.ru/video/12345/") is RutubeAdapter

    def test_vk_video_section(self):
        assert get_adapter_class("https://vk.com/video-123_456") is VkAdapter

    def test_vk_video_marker_on_other_path(self):
        # vk.com не в видеоразделе, но DOM-маркер плеера присутствует -> VK
        cls = get_adapter_class(
            "https://vk.com/feed",
            {"selectors_present": [".VideoPlayer"]},
        )
        assert cls is VkAdapter

    def test_vk_plain_page_is_generic(self):
        # Обычная страница vk без видео-маркеров — generic fallback
        assert get_adapter_class("https://vk.com/public123") is GenericHTML5Adapter

    def test_unknown_domain_fallback(self):
        assert get_adapter_class("https://example.com/page") is GenericHTML5Adapter

    def test_empty_url_fallback(self):
        assert get_adapter_class("") is GenericHTML5Adapter

    def test_dom_marker_without_known_domain(self):
        # embed-плеер на стороннем сайте: #movie_player в DOM-образце
        cls = get_adapter_class(
            "https://some-blog.example/embed",
            {"html": '<div id="movie_player"></div><video></video>'},
        )
        assert cls is YoutubeAdapter

    def test_registry_contains_all_platforms(self):
        names = {c.platform for c in REGISTRY}
        assert names == {"youtube", "twitch", "vk", "rutube", "generic_html5"}


class TestBaseAdapter:
    def test_execute_unknown_action_without_controller(self):
        import asyncio

        adapter = BaseVideoAdapter(page=None, video_controller=None)
        res = asyncio.run(adapter.execute("some_nonsense"))
        assert res["ok"] is False
        assert res.get("error")

    def test_unsupported_platform_action(self):
        import asyncio

        adapter = BaseVideoAdapter(page=None, video_controller=None)
        res = asyncio.run(adapter.set_quality("480p"))
        assert res["ok"] is False
        assert res["error"] == "NotSupportedByAdapter"

    def test_selectors_loaded_from_config(self):
        cfg = load_selector_config()
        assert "youtube" in cfg and "twitch" in cfg
        adapter = YoutubeAdapter(page=None)
        # Селекторы берутся из конфига, а не хардкода (DoD ТЗ).
        assert adapter.selectors.get("settings_button_aria")
        assert "#movie_player" in adapter.selectors.get("dom_marker", "")

    def test_no_hardcoded_css_in_python_sources(self):
        """Грубая проверка DoD: ytp-/tw-классы не встречаются вне config/*.json."""
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for py in list(root.glob("adapters/*.py")) + list(root.glob("modules/*.py")):
            text = py.read_text(encoding="utf-8")
            for token in ("ytp-settings-button", "tw-player__"):
                if token in text:
                    offenders.append((py.name, token))
        assert not offenders, offenders
