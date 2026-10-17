"""Регресс-тесты по итогам глубокого анализа (Блоки 1–5 ТЗ-доработки).

Проверяют закрытие четырёх проблем из лога пользовательского чата:
  Блок 1: browser_site_search физически не мог передать override_phrase —
          middleware блокировал вызов POLICY_SITE_SEARCH_NOT_REQUESTED.
  Блок 2: cmd_schedule_action падал с AttributeError, если payload/options
          пришли от LLM как JSON-строка.
  Блок 3: NO_EPISODE_CONTEXT не предлагал fallback на chapter_list —
          агент «костылил» парсинг таймкодов через search_on_page(":").
  Блок 4/5: deny-сообщения должны быть императивными для LLM (ЗАПРЕЩЕНО
          повторять заблокированный вызов + немедленно выполнить первый
          suggested_safe_action) и нести извлечённый query/pattern.
"""

import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from modules.context_manager import ContextManager
from modules.policy_guard import PolicyGuard
from modules.settings_loader import SettingsLoader

ROOT = Path(__file__).resolve().parent.parent

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
        "allow_site_search": ["ищи на сайте", "найди на ютубе"],
    },
    "sites": {
        "youtube.com": {
            "intents": {
                "open_item_on_current_page": {
                    "forbidden_tools": ["browser_navigate_to_search_url"],
                },
            },
        },
    },
}


def make_stack(tmp_path, data=None):
    p = tmp_path / "user_tool_settings.json"
    p.write_text(json.dumps(data or BASE_SETTINGS, ensure_ascii=False),
                 encoding="utf-8")
    loader = SettingsLoader(explicit_path=str(p), include_project_config=False)
    loader.load()
    cm = ContextManager(settings=loader.get())
    guard = PolicyGuard(loader, cm)
    return guard, cm, loader


# ---------------------------------------------------------------------------
# Блок 1: сигнатура browser_site_search обязана принимать override_phrase
# ---------------------------------------------------------------------------
class TestBlock1SiteSearchSignature:
    def test_tool_signature_has_override_phrase(self):
        server = importlib.import_module("server")
        sig = inspect.signature(server.browser_site_search)
        assert "override_phrase" in sig.parameters, (
            "Блок 1: без параметра override_phrase агент не мог пройти "
            "политический контроль site_search")

    def test_guard_policy_denies_without_phrase(self, tmp_path):
        guard, _, _ = make_stack(tmp_path)
        d = guard.check_site_search_policy("lofi", None)
        assert not d.allowed
        assert d.error_code == "POLICY_SITE_SEARCH_NOT_REQUESTED"
        # императив для LLM (Блок 5)
        assert "ЗАПРЕЩЕНО повторять этот заблокированный вызов" in d.message
        assert d.suggested_safe_actions and \
               d.suggested_safe_actions[0]["args"]["pattern"] == "lofi"

    def test_guard_policy_allows_with_exact_phrase(self, tmp_path):
        guard, _, _ = make_stack(tmp_path)
        d = guard.check_site_search_policy("lofi", "найди на ютубе lofi")
        assert d.allowed

    def test_middleware_passes_override_phrase(self, tmp_path, monkeypatch):
        """call('site_search') с override_phrase НЕ должен блокироваться,
        а без него — возвращать структурированный deny (не исключение)."""
        server = importlib.import_module("server")
        loader = make_stack(tmp_path)[2]
        monkeypatch.setattr(server, "SETTINGS_LOADER", loader)
        monkeypatch.setattr(
            server, "POLICY_GUARD",
            PolicyGuard(loader, ContextManager(settings=loader.get())))

        sent = []

        class FakeConnector:
            async def ensure_connected(self):
                return True

            async def send(self, command, params):
                sent.append((command, params))
                return {"ok": True, "action": command}

        monkeypatch.setattr(server, "connector", FakeConnector())

        blocked = asyncio.run(server.call("site_search", {"query": "x"}))
        assert blocked["ok"] is False
        assert blocked["error_code"] == "POLICY_SITE_SEARCH_NOT_REQUESTED"

        ok = asyncio.run(server.call(
            "site_search",
            {"query": "x", "override_phrase": "ищи на сайте x"}))
        assert ok["ok"] is True
        assert sent and sent[-1][0] == "site_search"


# ---------------------------------------------------------------------------
# Блок 2: cmd_schedule_action не падает на payload/options как JSON-строке
# ---------------------------------------------------------------------------
class TestBlock2ScheduleActionCoercion:
    def test_coerce_dict_variants(self):
        from browserd import BrowserController
        c = BrowserController._coerce_dict
        assert c({"url": "https://x"}) == {"url": "https://x"}
        assert c('{"url": "https://x"}') == {"url": "https://x"}
        assert c(None) == {}
        assert c("") == {}
        assert c("[1,2]") == {}      # валидный JSON, но не dict
        assert c("{not json") == {}  # битая строка -> {} без падения

    @pytest.mark.asyncio
    async def test_schedule_action_accepts_string_payload(self, tmp_path):
        """Ключевая проверка Блока 2: до coerce строки payload/options
        приводили к AttributeError на .get() сразу после валидации URL.
        Проверяем участок кода БЕЗ браузера: require_page заглушен."""
        from browserd import BrowserController
        ctrl = BrowserController(profile_dir=tmp_path, headless=True)

        class FakePage:
            async def evaluate(self, script, arg=None):
                return None

        async def fake_require_page(params=None):
            return FakePage()

        ctrl.require_page = fake_require_page  # type: ignore[assignment]

        res = await ctrl.cmd_schedule_action({
            "trigger": "video_ended",
            "action": "navigate",
            "payload": '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}',
            "options": '{"once": true}',
        })
        # Без coercing здесь падало с AttributeError: 'str' object has
        # no attribute 'get'. Сейчас — вооружённое расписание либо
        # осмысленный структурированный отказ, но НЕ исключение/AttributeError.
        text = json.dumps(res, default=str)
        assert "AttributeError" not in text
        assert res.get("ok") is True or res.get("error"), \
            f"Ожидался структурированный ответ, получено: {text}"


# ---------------------------------------------------------------------------
# Блок 3: NO_EPISODE_CONTEXT → fallback на chapter_list первым
# ---------------------------------------------------------------------------
class TestBlock3EpisodesVsChapters:
    def test_deny_suggests_chapter_list_first(self, tmp_path):
        guard, cm, _ = make_stack(tmp_path)
        # Симулируем реальную ситуацию: на странице обычное видео без
        # плейлиста (has_episode_queue=False, list_id=None).
        ctx = cm.get_active_context()
        assert ctx is not None
        ctx.has_episode_queue = False
        ctx.list_id = None
        d = guard.check("browser_video", {"action": "episode_next"},
                        context=ctx)
        assert not d.allowed
        assert d.error_code == "NO_EPISODE_CONTEXT"
        args = [a["args"].get("action") for a in d.suggested_safe_actions]
        assert args[0] == "chapter_list", (
            "Блок 3: при отсутствии очереди эпизодов первый suggestion — "
            "главы текущего видео, чтобы агент не парсил DOM костылями")
        assert "chapter_list" in d.message

    def test_skill_md_documents_split(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "РАЗДЕЛЕНИЕ ПОНЯТИЙ" in text
        # Регрессия на точную пунктуацию ТЗ-формулировки (без ложных падений
        # из-за переносов/кавычек в документе):
        assert "NO_EPISODE_CONTEXT" in text
        assert "немедленно вызывай `chapter_list`" in text


# ---------------------------------------------------------------------------
# Блок 4/5: императивные deny-сообщения и готовые pattern в suggestions
# ---------------------------------------------------------------------------
class TestBlock45ImperativeDeny:
    IMPERATIVE = "ЗАПРЕЩЕНО повторять этот заблокированный вызов"

    def test_navigate_search_url_deny_is_imperative(self, tmp_path):
        guard, _, _ = make_stack(tmp_path)
        d = guard.check("browser_navigate", {
            "url": "https://www.youtube.com/results?search_query=%D0%B4%D0%B6"
                   "%D0%B5%D0%BC"})
        assert not d.allowed
        assert d.error_code == "POLICY_SITE_SEARCH_NOT_REQUESTED"
        # case-insensitive: в разных deny-ветках фраза начинается с
        # «ЗАПРЕЩЕНО»/«Запрещено» — императив обязателен в любом регистре
        assert self.IMPERATIVE.lower() in d.message.lower()
        q = d.suggested_safe_actions[0]["args"]["pattern"]
        assert q  # извлечённый decode-запрос, не пустой

    def test_new_tab_deny_is_imperative(self, tmp_path):
        guard, _, _ = make_stack(tmp_path)
        d = guard.check("browser_new_tab", {"url": "https://example.com"})
        assert not d.allowed
        assert d.error_code == "POLICY_NEW_TAB_NOT_ALLOWED"
        assert self.IMPERATIVE in d.message

    def test_skill_md_flowchart_and_rule(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert "АЛГОРИТМ «Открой [X]»" in text
        assert 'browser_search_on_page(pattern="[X]")' in text
        assert "actionable: true" in text
        assert ('используй **ТОЛЬКО** `browser_site_search(query="[X]",'
                ' override_phrase="<дословная фраза пользователя>")`') in text
