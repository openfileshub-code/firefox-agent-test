"""ТЗ v2.0 этапы 2–3: регресс-тесты детектора theater и идемпотентности.

Проверяют подтверждённые ошибки аудита:
* C2 — детектор обязан читать ``ytd-watch-flexy[theater]``/``[wide]`` и
  класс ``.theater-mode`` плеера, а противоречивый снапшот
  (ratio_wide + theater_mode:false) должен давать ``mode:'uncertain'``,
  а не уверенный ``default``;
* C1 — ``set_theater_mode`` запрещён при confidence < 0.8 (STATE_UNCERTAIN),
  идемпотентен при already_in_state и НЕ переключает вторым каналом после
  dispatched=true (STATE_VERIFICATION_UNCERTAIN).

JS-детектор исполняется в реальном Playwright только на live-тестах; здесь
``page.evaluate`` фейкуется по структуре возвращаемого объекта — это
проверяет именно Python-логику консолидации каналов (ТЗ §11.3–11.4).
"""
from __future__ import annotations

import asyncio

import pytest

from adapters.base import CONFIDENCE_TRUSTED
from adapters.youtube_adapter import YoutubeAdapter


class FakePage:
    """Минимальная заглушка page: evaluate возвращает заготовленный info."""

    def __init__(self, info: dict):
        self.info = info
        self.url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    async def evaluate(self, script, *args):
        return dict(self.info)

    async def get_player_modes(self):  # не используется, но страхуем super()
        raise AssertionError("super().get_player_modes не должен вызываться")


def make_adapter(info: dict) -> YoutubeAdapter:
    ad = YoutubeAdapter.__new__(YoutubeAdapter)
    ad.page = FakePage(info)
    ad.video_controller = None
    ad.selectors = {}
    ad.platform = "youtube"
    return ad


async def modes_with(monkeypatch, info: dict) -> dict:
    """Прогон get_player_modes с перехватом базовой реализации."""
    ad = make_adapter(info)

    async def fake_base(self):
        return {"fullscreen": False, "picture_in_picture": False}

    monkeypatch.setattr(YoutubeAdapter.__mro__[1], "get_player_modes",
                        fake_base, raising=True)
    return await ad.get_player_modes()


BASE_INFO = {
    "theater": False, "full_window": False, "small_player": True,
    "cinematics": False, "container_class": "html5-video-player",
    "flexy_theater_attr": False, "flexy_wide_attr": False,
    "player_theater_class": False,
    "aria_theater_on": None, "aria_theater_off": True,
    "ratio": 0.66,
}


class TestDetectorChannels:
    def test_flexy_theater_attr_authoritative(self, monkeypatch):
        info = dict(BASE_INFO, flexy_theater_attr=True, ratio=0.98)
        modes = asyncio.run(modes_with(monkeypatch, info))
        assert modes["theater_mode"] is True
        lay = modes["layout_state"]
        assert lay["mode"] == "theater"
        assert "flexy_theater_attr:true" in lay["evidence"]
        assert lay["confidence"] >= CONFIDENCE_TRUSTED

    def test_player_theater_class_detected(self, monkeypatch):
        info = dict(BASE_INFO, player_theater_class=True,
                    theater=True, ratio=0.97)
        modes = asyncio.run(modes_with(monkeypatch, info))
        assert modes["theater_mode"] is True
        assert "player_theater_class" in modes["layout_state"]["evidence"]

    def test_conflict_ratio_wide_but_no_theater_is_uncertain(self, monkeypatch):
        """Регресс R1 (ТЗ 21.9): ratio_wide:1.00 + theater_mode:false -> uncertain."""
        info = dict(BASE_INFO, ratio=1.00,
                    flexy_theater_attr=False, player_theater_class=False,
                    theater=False, aria_theater_on=None, aria_theater_off=None)
        modes = asyncio.run(modes_with(monkeypatch, info))
        lay = modes["layout_state"]
        assert lay["mode"] == "uncertain"
        assert lay["uncertain"] is True
        assert lay["theater_mode"] is False
        # Confidence ниже порога доверия — слепой toggle будет запрещён.
        assert lay["confidence"] < CONFIDENCE_TRUSTED
        assert "ratio_wide:1.00" in lay["evidence"]

    def test_default_state_confident(self, monkeypatch):
        modes = asyncio.run(modes_with(monkeypatch, dict(BASE_INFO)))
        lay = modes["layout_state"]
        assert lay["mode"] == "default"
        assert lay["theater_mode"] is False
        assert lay["confidence"] >= CONFIDENCE_TRUSTED or lay["mode"] != "uncertain"


class TestSetTheaterIdempotency:
    def _run(self, scenario):
        return asyncio.run(scenario)

    def test_already_in_state_noop(self, monkeypatch):
        ad = make_adapter({})

        async def fake_modes():
            return {"theater_mode": True, "container_class": "theater-mode",
                    "layout_state": {"mode": "theater", "theater_mode": True,
                                     "confidence": 0.95, "evidence": []}}
        monkeypatch.setattr(ad, "get_player_modes", fake_modes)
        toggled = []

        async def fake_toggle(target=None):
            toggled.append(target)
            return {"ok": True, "method": "hotkey_t"}
        monkeypatch.setattr(ad, "toggle_theater_mode", fake_toggle)

        res = self._run(ad.set_theater_mode(True))
        assert res["ok"] and res["changed"] is False
        assert res["status"] == "already_in_state"
        assert not toggled  # переключающее действие не выполнялось

    def test_low_confidence_blocks_toggle(self, monkeypatch):
        ad = make_adapter({})

        async def fake_modes():
            return {"theater_mode": False, "container_class": "",
                    "layout_state": {"mode": "uncertain", "theater_mode": False,
                                     "uncertain": True, "confidence": 0.2,
                                     "evidence": ["ratio_wide:1.00"]}}
        monkeypatch.setattr(ad, "get_player_modes", fake_modes)
        calls = []

        async def fake_toggle(target=None):
            calls.append(target)
            return {"ok": True}
        monkeypatch.setattr(ad, "toggle_theater_mode", fake_toggle)

        res = self._run(ad.set_theater_mode(True))
        assert res["ok"] is False
        assert res["error_code"] == "STATE_UNCERTAIN"
        assert not calls  # C1: toggle при низкой уверенности запрещён

    def test_dispatched_no_second_channel(self, monkeypatch):
        """C1/§10.6: после dispatched=true второй переключающий канал не пробуется."""
        ad = make_adapter({})
        seq = [
            # before (достоверно default, high confidence)
            {"theater_mode": False, "container_class": "",
             "layout_state": {"mode": "default", "theater_mode": False,
                              "confidence": 0.9, "evidence": []}},
            # after toggle — состояние не изменилось (dispatched, unverified)
            {"theater_mode": False, "container_class": "",
             "layout_state": {"mode": "default", "theater_mode": False,
                              "confidence": 0.9, "evidence": []}},
        ]
        idx = {"i": 0}

        async def fake_modes():
            r = seq[min(idx["i"], len(seq) - 1)]
            idx["i"] += 1
            return r
        monkeypatch.setattr(ad, "get_player_modes", fake_modes)

        executed_channels: list[str] = []

        async def rec(name, applied=True):
            if applied:
                executed_channels.append(name)
            return applied

        monkeypatch.setattr(ad, "_theater_channel_hotkey",
                            lambda: rec("hotkey_t"))
        monkeypatch.setattr(ad, "_theater_channel_aria",
                            lambda: rec("aria_button"))
        monkeypatch.setattr(ad, "_theater_channel_css",
                            lambda: rec("css_size_button"))
        monkeypatch.setattr(ad, "_theater_channel_js",
                            lambda: rec("js_click"))

        async def fast_wait(self, before_theater, **kw):
            return False, await self.get_player_modes()
        monkeypatch.setattr(YoutubeAdapter, "_wait_theater_change", fast_wait)

        res = self._run(ad.set_theater_mode(True))
        assert res["ok"] is False
        assert res.get("dispatched") is True
        assert res["error"] == "STATE_VERIFICATION_UNCERTAIN"
        # Критично: ровно ОДИН переключающий канал был выполнен.
        assert executed_channels == ["hotkey_t"]

    def test_all_channels_unavailable_raises(self, monkeypatch):
        ad = make_adapter({})

        async def fake_modes():
            return {"theater_mode": False, "container_class": "",
                    "layout_state": {"mode": "default", "theater_mode": False,
                                     "confidence": 0.9, "evidence": []}}
        monkeypatch.setattr(ad, "get_player_modes", fake_modes)
        for ch in ("_theater_channel_hotkey", "_theater_channel_aria",
                   "_theater_channel_css", "_theater_channel_js"):
            async def none_applied():
                return False
            monkeypatch.setattr(ad, ch, none_applied)

        from adapters.base import ElementNotFoundError
        with pytest.raises(ElementNotFoundError):
            asyncio.run(ad.toggle_theater_mode())
