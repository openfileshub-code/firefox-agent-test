"""Тесты context_manager (Этап 3) — по критериям ТЗ:
корректный контекст для watch/playlist/radio/home, radio-drift, same-tab.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.context_manager import (
    ContextManager, detect_page_type, VIDEO_ID_RE,
)


def cm():
    return ContextManager()


def test_watch_page_context():
    m = cm()
    ctx = m.update_from_result("browser_navigate", {"url": "x"}, {
        "ok": True,
        "url": "https://www.youtube.com/watch?v=U8A9YEsj3zY&list=PLabc123",
    })
    assert ctx.site == "youtube.com"
    assert ctx.page_type == "watch"
    assert ctx.video_id == "U8A9YEsj3zY"
    assert VIDEO_ID_RE.match(ctx.video_id)
    assert ctx.list_id == "PLabc123"
    assert ctx.expected_video_id == "U8A9YEsj3zY"
    assert ctx.is_radio is False


def test_playlist_and_home_and_radio():
    assert detect_page_type("https://www.youtube.com/playlist?list=PL1") == "playlist"
    assert detect_page_type("https://www.youtube.com/") == "home"
    assert detect_page_type(
        "https://www.youtube.com/watch?v=U8A9YEsj3zY&start_radio=1") == "radio"
    assert detect_page_type(
        "https://music.youtube.com/watch?v=U8A9YEsj3zY&list=RDabc"
    ) == "radio"


def test_radio_drift_detection():
    m = cm()
    m.update_from_result("browser_navigate", {}, {
        "ok": True, "url": "https://www.youtube.com/watch?v=r3TzbOP7-YA&start_radio=1"})
    assert m.get_active_context().is_radio
    # autoplay сменил видео БЕЗ явной навигации агента (state-обновление)
    m.update_from_result("browser_video", {"action": "state"}, {
        "ok": True, "video_id": "J3eqAxYPkmA"})
    check = m.check_video_context("J3eqAxYPkmA")
    assert check.ok is False
    assert check.error_code == "VIDEO_CONTEXT_CHANGED"
    assert check.expected_video_id == "r3TzbOP7-YA"


def test_check_video_context_ok():
    m = cm()
    m.set_expected_video_id("U8A9YEsj3zY")
    assert m.check_video_context("U8A9YEsj3zY").ok
    assert m.check_video_context(None).ok  # unknown actual -> skip


def test_same_tab_lock_unlock():
    from modules.settings_loader import SettingsLoader
    m = cm()
    m.lock_same_tab("test")
    assert m.get_active_context().same_tab_required
    loader = SettingsLoader(include_project_config=False)
    s = loader.load()
    assert m.unlock_same_tab("приблизительная фраза", s) is False  # не whitelist
    assert m.unlock_same_tab("открой в новой вкладке", s) is True


def test_hidden_element_marker_flow():
    m = cm()
    m.update_from_result("browser_click_text", {"text": "Сопрано"},
                         {"ok": False, "error": "элемент скрыт (hidden)"})
    ctx = m.get_active_context()
    assert ctx.pending_hidden_element == "Сопрано"
    assert ctx.expanded_since_hidden is False
    m.update_from_result("browser_expand_menu", {"trigger_text": "Меню"},
                         {"ok": True})
    assert m.get_active_context().expanded_since_hidden is True


def test_layout_state_captured():
    m = cm()
    m.update_from_result("browser_video", {"action": "layout_state"}, {
        "ok": True,
        "layout": {"mode": "theater", "confidence": 0.85,
                   "evidence": ["css:ytp-theater-mode"]},
    })
    lay = m.get_active_context().layout
    assert lay["mode"] == "theater"
    assert lay["confidence"] == 0.85


def test_blind_retry_history():
    m = cm()
    args = {"direction": "down", "amount": 800}
    for _ in range(3):
        m.update_from_result("browser_scroll", args,
                             {"ok": True, "url": "https://example.com/"})
    assert m.count_recent_identical("browser_scroll", args) == 3
