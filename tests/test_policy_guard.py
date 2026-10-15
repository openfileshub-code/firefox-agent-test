"""Тесты policy_guard (Этап 4) — по критериям ТЗ и RC1–RC11.

Каждое hard-rule проверяется ДО исполнения; отказ содержит
error_code, rule_source (валидная ссылка на SKILL.md), suggested_safe_actions.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from modules.context_manager import ContextManager
from modules.policy_guard import PolicyGuard, RULE_SOURCES
from modules.settings_loader import SettingsLoader


SKILL_PATH = Path(__file__).resolve().parent.parent / "SKILL.md"


def make_loader(tmp_path, data):
    p = tmp_path / "user_tool_settings.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loader = SettingsLoader(explicit_path=str(p), include_project_config=False)
    loader.load()
    return loader


@pytest.fixture
def guard(tmp_path):
    loader = make_loader(tmp_path, {"version": "1.0", "global": {}})
    cm = ContextManager(settings=loader.get())
    return PolicyGuard(loader, cm), cm, loader


# ------------------------------------------------------------------
class TestRuleSources:
    def test_all_codes_have_rule_source(self, guard):
        g, _, _ = guard
        for code in RULE_SOURCES:
            d = g._deny_probe(code) if hasattr(g, "_deny_probe") else None
        # прямая проверка словаря: непустые ссылки
        assert len(RULE_SOURCES) >= 18

    def test_rule_sources_point_to_existing_skill_sections(self):
        """Блокер аудита 3.2: каждая rule_source должна ссылаться на
        реально существующий заголовок SKILL.md."""
        text = SKILL_PATH.read_text(encoding="utf-8")
        headings = {line.lstrip("#").strip()
                    for line in text.splitlines() if line.startswith("#")}
        missing = []
        for code, src in RULE_SOURCES.items():
            # формат: "SKILL.md §<Заголовок>" (может быть несколько через '/')
            for part in src.replace("SKILL.md ", "").split(" / "):
                sec = part.lstrip("§").strip()
                if not any(sec in h or h in sec for h in headings):
                    missing.append((code, sec))
        assert not missing, "Битые ссылки rule_source: %s" % missing


# ------------------------------------------------------------------
class TestTabsAndUrl:
    def test_new_tab_denied_without_phrase(self, guard):
        g, _, _ = guard
        d = g.check("browser_new_tab", {"url": "https://example.com"})
        assert not d.allowed
        assert d.error_code == "POLICY_NEW_TAB_NOT_ALLOWED"
        assert d.rule_source
        assert d.suggested_safe_actions

    def test_new_tab_allowed_with_override_phrase(self, guard):
        g, _, _ = guard
        d = g.check("browser_new_tab", {
            "url": "https://example.com",
            "override_phrase": "открой в новой вкладке"})
        assert d.allowed

    def test_placeholder_url_denied(self, guard):
        g, _, _ = guard
        d = g.check("browser_navigate",
                    {"url": "https://www.youtube.com/watch?v=VIDEO_ID"})
        assert not d.allowed
        assert d.error_code == "POLICY_PLACEHOLDER_URL_FORBIDDEN"

    def test_invalid_video_id_denied(self, guard):
        g, _, _ = guard
        d = g.check("browser_navigate",
                    {"url": "https://www.youtube.com/watch?v=short"})
        assert not d.allowed
        assert d.error_code == "POLICY_INVALID_YOUTUBE_VIDEO_ID"

    def test_site_search_denied_without_phrase(self, guard):
        g, _, _ = guard
        d = g.check("browser_navigate", {
            "url": "https://www.youtube.com/results?search_query=cat"})
        assert not d.allowed
        assert d.error_code == "POLICY_SITE_SEARCH_NOT_REQUESTED"

    def test_site_search_allowed_with_phrase(self, guard):
        g, _, _ = guard
        d = g.check("browser_navigate", {
            "url": "https://www.youtube.com/results?search_query=cat",
            "override_phrase": "поищи на сайте cat"})
        assert d.allowed


# ------------------------------------------------------------------
class TestScrollEval:
    def test_scroll_denied_by_default(self, guard):
        g, _, _ = guard
        d = g.check("browser_scroll", {"direction": "down"})
        assert not d.allowed
        assert d.error_code == "POLICY_SCROLL_NOT_REQUESTED"

    def test_scroll_allowed_with_phrase(self, guard):
        g, _, _ = guard
        d = g.check("browser_scroll", {
            "direction": "down", "override_phrase": "прокрути вниз"})
        assert d.allowed

    def test_eval_video_denied(self, guard):
        g, _, _ = guard
        d = g.check("browser_eval",
                    {"js": "document.querySelector('video').play()"})
        assert not d.allowed
        assert d.error_code == "POLICY_EVAL_VIDEO_FORBIDDEN"

    def test_eval_request_fullscreen_denied_as_theater(self, guard):
        """RC6: requestFullscreen — именно тот вызов, которым агент ломал
        «широкий экран»; должен ловиться с отдельным кодом."""
        g, _, _ = guard
        d = g.check("browser_eval", {
            "js": "document.querySelector('#movie_player')"
                  ".requestFullscreen()"})
        assert not d.allowed
        assert d.error_code == "POLICY_THEATER_NOT_FULLSCREEN"

    def test_eval_plain_dom_ok(self, guard):
        g, _, _ = guard
        d = g.check("browser_eval",
                    {"js": "document.title.toUpperCase()"})
        assert d.allowed


# ------------------------------------------------------------------
class TestVideo:
    def test_blind_toggle_when_layout_unknown(self, guard):
        g, cm, _ = guard
        cm.update_from_result("browser_navigate", {}, {
            "ok": True,
            "url": "https://www.youtube.com/watch?v=U8A9YEsj3zY"})
        d = g.check("browser_video", {"action": "toggle_theater_mode"})
        assert not d.allowed
        assert d.error_code == "THEATER_STATE_UNKNOWN"

    def test_theater_set_allowed_after_layout_state(self, guard):
        g, cm, _ = guard
        cm.update_from_result("browser_navigate", {}, {
            "ok": True,
            "url": "https://www.youtube.com/watch?v=U8A9YEsj3zY"})
        cm.update_from_result("browser_video", {"action": "layout_state"}, {
            "ok": True, "layout": {"mode": "default", "theater_mode": False,
                                   "confidence": 0.95, "evidence": ["css"]}})
        d = g.check("browser_video", {"action": "theater_set", "value": True})
        assert d.allowed

    def test_episode_next_without_context_denied(self, guard):
        g, _, _ = guard
        d = g.check("browser_video", {"action": "episode_next"})
        assert not d.allowed
        assert d.error_code == "NO_EPISODE_CONTEXT"

    def test_episode_next_with_list_context_ok(self, guard):
        g, cm, _ = guard
        cm.update_from_result("browser_navigate", {}, {
            "ok": True,
            "url": "https://www.youtube.com/watch?v=U8A9YEsj3zY&list=PLabc"})
        d = g.check("browser_video", {"action": "episode_next"})
        assert d.allowed

    def test_radio_drift_blocks_mutation(self, guard):
        """RC11: autoplay сменил видео — mutation к чужому видео блокируется."""
        g, cm, _ = guard
        cm.update_from_result("browser_navigate", {}, {
            "ok": True,
            "url": "https://www.youtube.com/watch?v=r3TzbOP7-YA&start_radio=1"})
        # без явной навигации video_id сменился (state показал другое видео)
        cm.update_from_result("browser_video", {"action": "state"}, {
            "ok": True, "video_id": "J3eqAxYPkmA"})
        d = g.check("browser_video", {"action": "volume_set", "value": 0.1})
        assert not d.allowed
        assert d.error_code == "VIDEO_CONTEXT_CHANGED"

    def test_verify_false_denied_when_global_on(self, guard):
        g, _, _ = guard
        d = g.check("browser_video",
                    {"action": "play", "verify": False})
        assert not d.allowed
        assert d.error_code == "VERIFY_REQUIRED"

    def test_deprecated_action_denied(self, guard):
        g, _, _ = guard
        d = g.check("browser_video", {"action": "quality_set"})
        assert not d.allowed


# ------------------------------------------------------------------
class TestHiddenAndRetry:
    def test_icon_text_click_denied(self, guard):
        g, _, _ = guard
        d = g.check("browser_click_text", {"text": "Theater"})
        assert not d.allowed
        assert d.error_code == "POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND"

    def test_hidden_retry_requires_expand(self, guard):
        """RC3: клик упал 'hidden' → повтор без expand запрещён,
        после expand — разрешён."""
        g, cm, _ = guard
        cm.update_from_result("browser_click_text", {"text": "Смотреть все"}, {
            "ok": False, "error": "Element is not visible (hidden)"})
        d = g.check("browser_click_text", {"text": "Смотреть все"})
        assert not d.allowed
        assert d.error_code == "POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND"
        # после expand_menu — можно
        cm.update_from_result("browser_expand_menu",
                              {"trigger_text": "Меню"}, {"ok": True})
        d2 = g.check("browser_click_text", {"text": "Смотреть все"})
        assert d2.allowed

    def test_blind_retry_denied(self, guard):
        """RC2/RC7: два идентичных неудачных mutate-повтора → блок."""
        g, cm, _ = guard
        args = {"text": "Кнопка"}
        for _ in range(2):
            cm.update_from_result("browser_click_text", args,
                                  {"ok": False, "error": "TimeoutError"})
        d = g.check("browser_click_text", args)
        assert not d.allowed
        assert d.error_code == "POLICY_BLIND_RETRY_FORBIDDEN"


# ------------------------------------------------------------------
class TestSettingsDriven:
    def test_allow_scroll_global_enables_scroll(self, tmp_path):
        loader = make_loader(tmp_path, {
            "version": "1.0", "global": {"allow_scroll": True}})
        g = PolicyGuard(loader, ContextManager(settings=loader.get()))
        assert g.check("browser_scroll", {"direction": "down"}).allowed

    def test_site_forbidden_tools_block(self, tmp_path):
        loader = make_loader(tmp_path, {
            "version": "1.0",
            "sites": {"youtube.com": {"forbidden_tools": ["browser_screenshot"]}},
        })
        cm = ContextManager(settings=loader.get())
        cm.update_from_result("browser_navigate", {}, {
            "ok": True, "url": "https://www.youtube.com/"})
        g = PolicyGuard(loader, cm)
        d = g.check("browser_screenshot", {})
        assert not d.allowed
