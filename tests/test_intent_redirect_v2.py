"""Тесты Policy Guard v2.0 (Sprint 2/3): Intent Redirect + Recovery Injection.

Проверяют переход от «бетонной стены» к «умному роутеру»:
  * evaluate_intent() возвращает INTENT_REDIRECT вместо plain BLOCKED;
  * ответ содержит system_injection с алгоритмом коррекции и поле
    __system_injection__ (priority=critical) для MCP-хостов;
  * деструктивные намерения (site search на YouTube, навигация на URL
    выдачи, возврат на корень) перехватываются, обычная навигация — нет.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from modules.context_manager import ContextManager
from modules.policy_guard import PolicyGuard, PolicyVerdict
from modules.settings_loader import SettingsLoader


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
    return PolicyGuard(loader, cm)


class TestEvaluateIntentRedirect:
    def test_site_search_on_youtube_returns_intent_redirect(self, guard):
        """browser_site_search на YouTube → INTENT_REDIRECT с injection."""
        context = {"current_url": "https://www.youtube.com"}
        args = {"query": "Джем Стас Пьеха", "site": "youtube"}

        verdict = guard.evaluate_intent("browser_site_search", args, context)

        assert verdict.verdict == PolicyVerdict.INTENT_REDIRECT
        assert not verdict.allowed
        assert verdict.error_code == "INTENT_REDIRECT_SITE_SEARCH_DESTRUCTIVE"
        assert "browser_ui_search_box" in verdict.system_injection
        assert "[SYSTEM POLICY OVERRIDE]" in verdict.system_injection
        # первый suggestion — немедленное recovery-действие
        tools = [a["tool"] for a in verdict.suggested_safe_actions]
        assert tools[0] == "browser_ui_search_box"

    def test_renamed_tool_also_intercepted(self, guard):
        """Новое семантическое имя browser_navigate_to_search_results тоже
        перехватывается (обратная совместимость переименования)."""
        context = {"current_url": "https://www.youtube.com"}
        verdict = guard.evaluate_intent(
            "browser_navigate_to_search_results",
            {"query": "тест", "site": "youtube"}, context)
        assert verdict.verdict == PolicyVerdict.INTENT_REDIRECT

    def test_override_phrase_allows_site_search(self, guard):
        """Дословная whitelist-фраза пользователя снимает перехват."""
        context = {"current_url": "https://www.youtube.com"}
        verdict = guard.evaluate_intent(
            "browser_site_search",
            {"query": "тест", "site": "youtube",
             "override_phrase": "ищи на сайте"}, context)
        assert verdict.verdict == PolicyVerdict.ALLOWED

    def test_navigate_to_search_url_returns_intent_redirect(self, guard):
        """browser_navigate на /results?search_query= → INTENT_REDIRECT."""
        context = {"current_url": "https://www.youtube.com"}
        args = {"url":
                "https://www.youtube.com/results?search_query=%D0%94%D0%B6%D0%B5%D0%BC"}

        verdict = guard.evaluate_intent("browser_navigate", args, context)

        assert verdict.verdict == PolicyVerdict.INTENT_REDIRECT
        assert verdict.error_code == "INTENT_REDIRECT_NAVIGATE_TO_SEARCH_URL"
        assert "browser_ui_search_box" in verdict.system_injection

    def test_root_navigation_returns_intent_redirect(self, guard):
        """Возврат на главную из /results → INTENT_REDIRECT_ROOT_NAVIGATION."""
        context = {"current_url":
                   "https://www.youtube.com/results?search_query=test"}
        args = {"url": "https://www.youtube.com"}

        verdict = guard.evaluate_intent("browser_navigate", args, context)

        assert verdict.verdict == PolicyVerdict.INTENT_REDIRECT
        assert verdict.error_code == "INTENT_REDIRECT_ROOT_NAVIGATION"
        assert "УНИЧТОЖИТ" in verdict.system_injection or \
            "Context Sanctity" in verdict.system_injection

    def test_normal_navigation_allowed(self, guard):
        """Обычная навигация на watch не перехватывается."""
        context = {"current_url": "https://www.youtube.com"}
        args = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}

        verdict = guard.evaluate_intent("browser_navigate", args, context)

        assert verdict.verdict == PolicyVerdict.ALLOWED
        assert verdict.allowed

    def test_dom_tools_never_blocked(self, guard):
        """DOM-инструменты (поиск на странице) не перехватываются."""
        context = {"current_url": "https://www.youtube.com/results"}
        for tool in ("browser_dom_text_search",
                     "browser_find_interactive_element",
                     "browser_ui_search_box"):
            verdict = guard.evaluate_intent(tool, {"pattern": "x"}, context)
            assert verdict.verdict == PolicyVerdict.ALLOWED, tool


class TestRecoveryInjectionFormat:
    def test_to_dict_contains_system_injection_fields(self, guard):
        """Формат ответа для MCP-хостов: system_injection +
        __system_injection__ (priority=critical, required_action)."""
        verdict = guard.evaluate_intent(
            "browser_site_search",
            {"query": "тест", "site": "youtube"},
            {"current_url": "https://www.youtube.com"})
        d = verdict.to_dict()

        assert d["ok"] is False or d.get("allowed") is False
        assert d["error_code"] == "INTENT_REDIRECT_SITE_SEARCH_DESTRUCTIVE"
        assert d["system_injection"]
        inj = d["__system_injection__"]
        assert inj["priority"] == "critical"
        assert inj["required_action"] == "immediate_correction"
        assert inj["message"] == verdict.system_injection
        assert d["verdict"] == "intent_redirect"

    def test_rule_source_links_exist_for_new_codes(self):
        """Каждый новый INTENT_REDIRECT_* код имеет ссылку на SKILL.md."""
        from modules.policy_guard import RULE_SOURCES
        for code in ("INTENT_REDIRECT_SITE_SEARCH_DESTRUCTIVE",
                     "INTENT_REDIRECT_NAVIGATE_TO_SEARCH_URL",
                     "INTENT_REDIRECT_ROOT_NAVIGATION",
                     "PANIC_LOOP_DETECTED"):
            assert code in RULE_SOURCES, code


class TestAntiPanicLock:
    def test_panic_lock_blocks_destructive_tools(self):
        cm = ContextManager()
        cm.set_panic_lock(duration_ms=5000)

        assert cm.is_tool_blocked("web_search") is True
        assert cm.is_tool_blocked("google-search") is True
        assert cm.is_tool_blocked("duckduckgo-search") is True
        assert cm.is_tool_blocked("browser_navigate") is True
        assert cm.is_tool_blocked("browser_site_search") is True
        # безопасные DOM-инструменты НЕ блокируются
        assert cm.is_tool_blocked("browser_find_interactive_element") is False
        assert cm.is_tool_blocked("browser_dom_text_search") is False
        assert cm.is_tool_blocked("browser_ui_search_box") is False

    def test_panic_lock_expires_after_duration(self):
        import time
        cm = ContextManager()
        cm.set_panic_lock(duration_ms=300)

        assert cm.is_tool_blocked("web_search") is True
        time.sleep(0.4)
        assert cm.is_tool_blocked("web_search") is False
        assert cm.panic_lock is None

    def test_clear_panic_lock(self):
        cm = ContextManager()
        cm.set_panic_lock(duration_ms=60000)
        assert cm.is_tool_blocked("browser_navigate") is True
        cm.clear_panic_lock()
        assert cm.is_tool_blocked("browser_navigate") is False

    def test_custom_blocked_tools(self):
        cm = ContextManager()
        cm.set_panic_lock(duration_ms=1000, blocked_tools=["web_search"])
        assert cm.is_tool_blocked("web_search") is True
        assert cm.is_tool_blocked("browser_navigate") is False
