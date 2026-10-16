"""Регрессионные тесты RC1–RC11 (ТЗ этап 13).

Каждый тест воспроизводит дефект из пользовательских чатов и проверяет,
что он закрыт на уровне:
  * PolicyGuard / SettingsLoader / ContextManager (unit-уровень правил),
  * middleware server.call() (интеграция: guard реально исполняется),
  * browserd / adapters (поведение команд и ответы).

Запуск без браузера: playwright не требуется; для интеграционных тестов
подменяется connector.send в server.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from modules import url_validator as uv
from modules.context_manager import ContextManager
from modules.policy_guard import PolicyGuard
from modules.settings_loader import SettingsLoader

ROOT = Path(__file__).resolve().parent.parent


def make_loader(tmp_path, data):
    p = tmp_path / "user_tool_settings.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loader = SettingsLoader(explicit_path=str(p), include_project_config=False)
    loader.load()
    return loader


BASE_SETTINGS = {
    "version": "1.0",
    "global": {
        "volume_tolerance": 0.005,
        "allow_new_tab": False,
        "allow_site_search": False,
        "allow_scroll": False,
    },
    "override_phrases": {
        "allow_new_tab": ["открой новую вкладку", "можно новый таб"],
        "allow_scroll": ["прокрути страницу", "листай вниз"],
        "allow_site_search": ["ищи на сайте"],
    },
}


@pytest.fixture
def stack(tmp_path):
    loader = make_loader(tmp_path, BASE_SETTINGS)
    cm = ContextManager(settings=loader.get())
    guard = PolicyGuard(loader, cm)
    return guard, cm, loader


# ---------------------------------------------------------------------------
# RC1: new_tab + site-search вместо поиска на текущей странице
# ---------------------------------------------------------------------------
class TestRC1NewTabSiteSearch:
    def test_new_tab_denied_without_phrase(self, stack):
        guard, cm, _ = stack
        d = guard.check("browser_new_tab", {"url": "https://www.youtube.com/results?search_query=джаз"})
        assert not d.allowed
        assert d.error_code in ("POLICY_NEW_TAB_NOT_ALLOWED",
                                "POLICY_SITE_SEARCH_NOT_REQUESTED")
        assert d.rule_source.startswith("SKILL.md §")
        assert d.suggested_safe_actions

    def test_site_search_url_denied_on_navigate(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_navigate",
                        {"url": "https://www.youtube.com/results?search_query=foo"})
        assert not d.allowed
        assert d.error_code == "POLICY_SITE_SEARCH_NOT_REQUESTED"

    def test_site_search_allowed_with_override_phrase(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_navigate",
                        {"url": "https://www.youtube.com/results?search_query=foo",
                         "override_phrase": "ищи на сайте foo"})
        assert d.allowed


# ---------------------------------------------------------------------------
# RC2: повтор действия после замечания пользователя (слепой ретрай)
# ---------------------------------------------------------------------------
class TestRC2BlindRetry:
    def test_identical_retry_denied(self, stack):
        """Последовательность как в middleware: check → запись результата в ctx."""
        guard, cm, _ = stack
        args = {"text": "Подписаться"}
        for _ in range(2):  # два неудачных повтора записаны в историю
            d = guard.check("browser_click_text", args)
            assert d.allowed
            cm.update_from_result("browser_click_text", args, {"ok": False})
        d3 = guard.check("browser_click_text", args)
        assert not d3.allowed
        assert d3.error_code == "POLICY_BLIND_RETRY_FORBIDDEN"

    def test_retry_after_user_note_allowed(self, stack):
        """После осознанного изменения аргумента повтор разрешён."""
        guard, cm, _ = stack
        a1 = {"text": "Сохранить"}
        guard.check("browser_click_text", a1)
        cm.update_from_result("browser_click_text", a1, {"ok": True})
        a2 = {"text": "Сохранить", "index": 1}
        d = guard.check("browser_click_text", a2)
        assert d.allowed


# ---------------------------------------------------------------------------
# RC3: скрытый элемент → scroll → слепой клик (нужен expand_menu)
# ---------------------------------------------------------------------------
class TestRC3HiddenElement:
    def test_scroll_denied_without_phrase(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_scroll", {"direction": "down"})
        assert not d.allowed
        assert d.error_code == "POLICY_SCROLL_NOT_REQUESTED"
        assert any("expand_menu" in str(a) or "browser_expand_menu" in str(a)
                   for a in d.suggested_safe_actions)

    def test_scroll_allowed_with_phrase(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_scroll",
                        {"direction": "down", "override_phrase": "прокрути страницу вниз"})
        assert d.allowed


# ---------------------------------------------------------------------------
# RC4: placeholder URL
# ---------------------------------------------------------------------------
class TestRC4PlaceholderUrl:
    def test_placeholder_denied(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_navigate", {"url": "https://site.tld/watch?v=[your id]"})
        assert not d.allowed
        assert d.error_code == "POLICY_PLACEHOLDER_URL_FORBIDDEN"

    def test_server_local_and_module_agree(self):
        """Локальная validate_url в server.py и модуль оба ловят placeholder."""
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        assert "validate_navigation_url" in src
        r = uv.validate_navigation_url("https://example.com/<placeholder>")
        assert not r.valid


# ---------------------------------------------------------------------------
# RC5: реальный video_id не извлекается (find_link / state)
# ---------------------------------------------------------------------------
class TestRC5VideoIdExtraction:
    def test_find_link_wraps_candidates(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        assert "relevance_ranker" in src or "RelevanceRanker" in src
        assert "candidate_id" in src or "rank_and_wrap" in src

    def test_youtube_state_returns_video_id(self):
        """Адаптер state() обогащён извлечением video_id/list_id/is_radio."""
        src = (ROOT / "adapters" / "youtube_adapter.py").read_text(encoding="utf-8")
        assert "getVideoData" in src
        assert '"video_id"' in src or "video_id" in src

    def test_browserd_navigate_returns_ids(self):
        src = (ROOT / "browserd.py").read_text(encoding="utf-8")
        assert re.search(r'result\["video_id"\]', src)


# ---------------------------------------------------------------------------
# RC6: «широкий экран» → requestFullscreen через eval должен блокироваться
# ---------------------------------------------------------------------------
class TestRC6FullscreenBlocked:
    def test_eval_fullscreen_denied_by_guard(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_eval",
                        {"js": "document.querySelector('video').requestFullscreen()"})
        assert not d.allowed
        assert d.error_code in ("POLICY_THEATER_NOT_FULLSCREEN",
                                "POLICY_EVAL_VIDEO_FORBIDDEN")

    def test_server_video_js_re_covers_fullscreen(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        m = re.search(r'VIDEO_JS_RE\s*=\s*re\.compile\((.*?)\n\)', src, re.S)
        assert m and "requestFullscreen" in m.group(1)


# ---------------------------------------------------------------------------
# RC7: повторный запрос «включить театр» = no-op (идемпотентный theater_set)
# ---------------------------------------------------------------------------
class TestRC7IdempotentTheaterSet:
    def test_theater_set_is_not_toggle_alias(self):
        src = (ROOT / "browserd.py").read_text(encoding="utf-8")
        aliases_block = re.search(r'_VIDEO_ACTION_ALIASES\s*=\s*\{(.*?)\}', src, re.S)
        assert aliases_block
        body = aliases_block.group(1)
        # theater_set НЕ должен маппиться на toggle_theater_mode
        assert not re.search(r'"theater_set"\s*:\s*"toggle_theater_mode"', body)

    def test_adapter_has_idempotent_set(self):
        ysrc = (ROOT / "adapters" / "youtube_adapter.py").read_text(encoding="utf-8")
        assert "def set_theater_mode" in ysrc
        assert "changed" in ysrc  # ответ с changed:false при уже включённом режиме

    def test_action_map_resolves_to_theater_set(self):
        cfg = json.loads((ROOT / "config" / "user_tool_settings.json").read_text(encoding="utf-8"))
        am = json.dumps(cfg, ensure_ascii=False)
        assert "theater_set:true" in am or "theater_set" in am


# ---------------------------------------------------------------------------
# RC8: fallback-цепочка theater вместо текстового поиска иконок
# ---------------------------------------------------------------------------
class TestRC8IconTextSearch:
    def test_icon_text_click_denied(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_click_text", {"text": "theater"})
        assert not d.allowed
        assert d.error_code in ("POLICY_ICON_TEXT_SEARCH_FORBIDDEN",
                                "POLICY_HIDDEN_ELEMENT_REQUIRES_EXPAND",
                                "POLICY_THEATER_NOT_FULLSCREEN")

    def test_fallback_chain_present(self):
        src = (ROOT / "adapters" / "youtube_adapter.py").read_text(encoding="utf-8")
        for ch in ("_theater_channel_aria", "_theater_channel_css",
                   "_theater_channel_hotkey", "_theater_channel_js"):
            assert ch in src


# ---------------------------------------------------------------------------
# RC9: volume 10% при фактических 8.01% не должен считаться успехом
# ---------------------------------------------------------------------------
class TestRC9VolumeTolerance:
    def test_base_reads_tolerance_from_settings(self):
        src = (ROOT / "adapters" / "base.py").read_text(encoding="utf-8")
        assert "_volume_tolerance" in src
        assert "volume_tolerance" in src
        assert "get_global_loader" in src

    def test_tolerance_0005_rejects_008_at_target_01(self, tmp_path):
        loader = make_loader(tmp_path, BASE_SETTINGS)
        tol = loader.get().g("volume_tolerance")
        assert abs(tol - 0.005) < 1e-9
        actual, target = 0.08015735, 0.1
        assert abs(actual - target) > tol  # старый допуск 0.02 признавал это успехом

    def test_no_hardcoded_002_in_verify(self):
        src = (ROOT / "adapters" / "base.py").read_text(encoding="utf-8")
        assert "<= 0.02" not in src and "<=0.02" not in src


# ---------------------------------------------------------------------------
# RC10: theater_mode:false при визуальном theater (multi-signal layout)
# ---------------------------------------------------------------------------
class TestRC10MultiSignalLayout:
    def test_get_player_modes_multi_signal(self):
        src = (ROOT / "adapters" / "youtube_adapter.py").read_text(encoding="utf-8")
        assert "layout_state" in src
        assert "confidence" in src
        assert "evidence" in src
        # aria-label сигнал присутствует
        assert re.search(r"[Tt]heater|театральн", src)

    def test_layout_state_action_available(self):
        bsrc = (ROOT / "browserd.py").read_text(encoding="utf-8")
        ysrc = (ROOT / "adapters" / "youtube_adapter.py").read_text(encoding="utf-8")
        assert "layout_state" in bsrc and "def layout_state" in ysrc


# ---------------------------------------------------------------------------
# RC11: radio autoplay уводит контекст — check_video_context должен сработать
# ---------------------------------------------------------------------------
class TestRC11RadioDrift:
    def test_context_changed_detected(self, stack):
        guard, cm, _ = stack
        cm.set_expected_video_id("abcdefghijk")
        d = guard.check("browser_video",
                        {"action": "episode_next", "expected_video_id": "abcdefghijk"})
        # сам по себе запрос разрешён; проверка дрейфа — через context check
        check = cm.check_video_context("zyxwvutsrqp")
        assert not check.ok
        assert check.error_code == "VIDEO_CONTEXT_CHANGED"

    def test_middleware_enriches_context_from_state(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        assert "check_video_context" in src
        assert "update_from_result" in src


# ---------------------------------------------------------------------------
# Интеграция middleware: call() реально исполняет guard (главный блокер аудита)
# ---------------------------------------------------------------------------
class TestMiddlewareIntegration:
    def test_call_imports_and_wraps_guard(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        for imp in ("PolicyGuard", "ContextManager", "SettingsLoader"):
            assert imp in src
        assert "POLICY_GUARD.check" in src
        assert "reload_if_changed" in src

    def test_deny_short_circuits_connector(self, monkeypatch):
        """Отказанный tool call не должен доходить до демона."""
        import importlib

        server = importlib.import_module("server")

        async def fake_send(command, params=None):
            raise AssertionError("connector.send вызван для отказанной команды!")

        monkeypatch.setattr(server.connector, "send", fake_send)

        async def run():
            res = await server.call(
                "new_tab",
                {"url": "https://www.youtube.com/results?search_query=x"},
                tool="browser_new_tab",
            )
            return res

        result = asyncio.run(run())
        assert result["ok"] is False
        assert "error_code" in result
        assert "rule_source" in result

    def test_allowed_command_passes_through(self, monkeypatch):
        import importlib

        server = importlib.import_module("server")

        async def fake_send(command, params=None):
            return {"ok": True, "url": params.get("url"),
                    "video_id": None}

        monkeypatch.setattr(server.connector, "send", fake_send)

        async def run():
            return await server.call(
                "navigate", {"url": "https://example.com/page"},
                tool="browser_navigate")

        result = asyncio.run(run())
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# RC12: deny POLICY_SITE_SEARCH_NOT_REQUESTED должен вести LLM к действию:
# императивный message + browser_find_link ПЕРВЫМ в suggested_safe_actions
# (агент механически выполняет первый suggestion; порядок = приоритет).
# ---------------------------------------------------------------------------
class TestRC12DenyActionability:
    def test_site_search_deny_suggestions_order_and_message(self, stack):
        guard, _, _ = stack
        d = guard.check("browser_navigate", {
            "url": "https://www.youtube.com/search?q=%D0%BC%D0%BE%D0%B9"
                   "+%D0%B4%D0%B6%D0%B5%D0%BC"})
        assert not d.allowed
        assert d.error_code == "POLICY_SITE_SEARCH_NOT_REQUESTED"
        tools = [a["tool"] for a in d.suggested_safe_actions]
        assert tools[0] == "browser_find_link"
        assert "browser_search_on_page" in tools
        pattern = d.suggested_safe_actions[0]["args"]["pattern"]
        assert pattern  # запрос извлечён из URL, не пустой
        # message содержит прямой императив для LLM
        assert "browser_find_link" in d.message
        assert "Немедленно" in d.message

    def test_project_settings_forbid_virtual_search_tool(self):
        """config/user_tool_settings.json: intent open_item_on_current_page
        активен и forbidden_tools включает виртуальный
        browser_navigate_to_search_url (перехват «хитрых» search-URL)."""
        data = json.loads((ROOT / "config" / "user_tool_settings.json")
                          .read_text(encoding="utf-8"))
        intents = data["sites"]["youtube.com"]["intents"]
        assert "open_item_on_current_page" in intents
        forbidden = intents["open_item_on_current_page"]["forbidden_tools"]
        assert "browser_navigate_to_search_url" in forbidden

    def test_skill_md_has_guard_deny_and_open_rules(self):
        """SKILL.md обязан содержать директивы: всегда выполняй
        suggested_safe_actions; «открой [название]» → сначала find_link."""
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "### Обработка отказов PolicyGuard" in text
        assert "suggested_safe_actions" in text
        assert "ЗАПРЕЩЕНО повторять заблокированное действие" in text
        assert "### Логика команды «Открой [название]»" in text
        assert "browser_save_alias" in text
        assert "### Пользовательские алиасы" in text


# ---------------------------------------------------------------------------
# RC13: пользовательские алиасы — MCP-инструменты browser_save_alias /
# browser_list_aliases / browser_delete_alias (config/user_aliases.json).
# ---------------------------------------------------------------------------
class TestRC13UserAliases:
    def _use_tmp_aliases(self, monkeypatch, tmp_path):
        server = importlib.import_module("server")
        monkeypatch.setattr(server, "ALIASES_PATH",
                            tmp_path / "user_aliases.json")
        return server

    def test_save_list_delete_roundtrip(self, monkeypatch, tmp_path):
        server = self._use_tmp_aliases(monkeypatch, tmp_path)
        res = asyncio.run(server.browser_save_alias(
            "Мой Джем", target_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        assert res["ok"] is True
        assert res["alias_name"] == "мой джем"
        lst = asyncio.run(server.browser_list_aliases())
        assert lst["count"] == 1
        assert lst["aliases"][0]["target_url"].startswith("https://")
        dele = asyncio.run(server.browser_delete_alias("мой джем"))
        assert dele["ok"] is True
        assert asyncio.run(server.browser_list_aliases())["count"] == 0

    def test_save_requires_target(self, monkeypatch, tmp_path):
        server = self._use_tmp_aliases(monkeypatch, tmp_path)
        res = asyncio.run(server.browser_save_alias("что-то"))
        assert res["ok"] is False
        assert res["error_code"] == "ALIAS_TARGET_REQUIRED"

    def test_save_rejects_search_results_url(self, monkeypatch, tmp_path):
        server = self._use_tmp_aliases(monkeypatch, tmp_path)
        res = asyncio.run(server.browser_save_alias(
            "x", target_url="https://www.youtube.com/results?search_query=foo"))
        assert res["ok"] is False
        assert res["error_code"] == "POLICY_SITE_SEARCH_NOT_REQUESTED"

    def test_save_with_search_query_ok(self, monkeypatch, tmp_path):
        server = self._use_tmp_aliases(monkeypatch, tmp_path)
        res = asyncio.run(server.browser_save_alias(
            "мой джем", search_query="lofi hip hop radio"))
        assert res["ok"] is True
        lst = asyncio.run(server.browser_list_aliases())
        assert lst["aliases"][0]["search_query"] == "lofi hip hop radio"
