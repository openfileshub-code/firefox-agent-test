"""Тесты chapter_list/chapter_seek (ТЗ этап 9).

Мок-страница эмулирует page.evaluate: возвращает главы для _CHAPTER_LIST_JS
и записывает seek-вызовы. cmd_video_action перехватывается, чтобы проверить
маршрутизацию chapter_seek -> seek без запуска браузера.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from browserd import BrowserController


CHAPTERS = [
    {"index": 0, "title": "Введение", "start_seconds": 0},
    {"index": 1, "title": "Основная часть", "start_seconds": 125},
    {"index": 2, "title": "Итоги", "start_seconds": 640},
]


class FakePage:
    url = "https://www.youtube.com/watch?v=abcdefghijk"

    def is_closed(self):
        return False

    async def evaluate(self, script: str, *args: Any):
        if "chapteredActions" in script or "playerDescriptors" in script:
            return {"chapters": CHAPTERS, "video_id": "abcdefghijk",
                    "source": "ytInitialPlayerResponse"}
        return {}


@pytest.fixture()
def ctrl(tmp_path: Path) -> BrowserController:
    c = BrowserController(profile_dir=tmp_path, headless=True)
    page = FakePage()
    c._resolve_page = lambda params=None: page  # type: ignore[assignment]
    c.video_controller = None
    return c


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_chapter_list_returns_chapters(ctrl):
    res = run(ctrl.cmd_chapter_list({}))
    assert res["ok"] is True
    assert res["action"] == "chapter_list"
    assert res["count"] == 3
    assert res["chapters"][1]["title"] == "Основная часть"
    assert res["video_id"] == "abcdefghijk"


def test_chapter_seek_by_index(ctrl, monkeypatch):
    calls = []

    async def fake_video_action(params):
        calls.append(params)
        return {"ok": True, "action": "seek"}

    monkeypatch.setattr(ctrl, "cmd_video_action", fake_video_action)
    res = run(ctrl.cmd_chapter_seek({"value": 1}))
    assert res["ok"] is True
    assert res["action"] == "chapter_seek"
    assert res["chapter_title"] == "Основная часть"
    assert res["start_seconds"] == 125
    assert calls == [{"action": "seek", "value": 125}]


def test_chapter_seek_by_title_fuzzy(ctrl, monkeypatch):
    calls = []

    async def fake_video_action(params):
        calls.append(params)
        return {"ok": True, "action": "seek"}

    monkeypatch.setattr(ctrl, "cmd_video_action", fake_video_action)
    res = run(ctrl.cmd_chapter_seek({"value": "итоги"}))
    assert res["ok"] is True
    assert res["chapter_index"] == 2
    assert res["start_seconds"] == 640


def test_chapter_seek_raw_seconds(ctrl, monkeypatch):
    calls = []

    async def fake_video_action(params):
        calls.append(params)
        return {"ok": True, "action": "seek"}

    monkeypatch.setattr(ctrl, "cmd_video_action", fake_video_action)
    res = run(ctrl.cmd_chapter_seek({"value": "42.5"}))
    assert res["ok"] is True
    assert res["chapter_index"] is None
    assert res["start_seconds"] == 42.5
    assert calls[0]["value"] == 42.5


def test_chapter_not_found(ctrl, monkeypatch):
    async def fake_video_action(params):
        raise AssertionError("seek не должен вызываться")

    monkeypatch.setattr(ctrl, "cmd_video_action", fake_video_action)
    res = run(ctrl.cmd_chapter_seek({"value": "несуществующая глава xyz"}))
    assert res["ok"] is False
    assert res["error"] == "CHAPTER_NOT_FOUND"


def test_no_chapter_context(ctrl, monkeypatch):
    class NoChapters(FakePage):
        async def evaluate(self, script, *a):
            return {"chapters": [], "video_id": None, "source": None}

    ctrl._resolve_page = lambda params=None: NoChapters()  # type: ignore[assignment]
    res = run(ctrl.cmd_chapter_list({}))
    assert res["ok"] is False
    assert res["error"] == "NO_CHAPTER_CONTEXT"
    res2 = run(ctrl.cmd_chapter_seek({"value": 0}))
    assert res2["ok"] is False
    assert res2["error"] == "NO_CHAPTER_CONTEXT"
    assert res2["suggested_safe_actions"]


def test_episode_actions_routing_includes_chapters():
    assert "chapter_list" in BrowserController._EPISODE_ACTIONS
    assert "chapter_seek" in BrowserController._EPISODE_ACTIONS
