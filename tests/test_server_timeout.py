"""Этап 7 ТЗ v2.0: жёсткий MCP-потолок и структурированный COMMAND_TIMEOUT.

server.call() должен:
- ограничивать ожидание ответа демона значением
  max(timing.mcp_tool_hard_timeout_ms, штатный TIMEOUTS[cmd]);
- при превышении возвращать ok:false + error_code=COMMAND_TIMEOUT +
  state_uncertain:true (вместо зависания/сырого исключения);
- писать per-call лог ключ-значение action/duration_ms/ok/error_code;
- не ломать обычный путь (ответ демона проходит как раньше).
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import server  # noqa: E402


class SlowConnector:
    """Заглушка: send() висит дольше потолка."""

    def __init__(self):
        self.calls = []

    async def ensure_connected(self):
        return None

    async def send(self, command, params=None, timeout=None):
        self.calls.append((command, params))
        await asyncio.sleep(30)
        return {"ok": True}


class FastConnector(SlowConnector):
    async def send(self, command, params=None, timeout=None):
        self.calls.append((command, params))
        return {"ok": True, "value": 42}


def _run(monkeypatch, connector_cls):
    conn = connector_cls()
    monkeypatch.setattr(server, "connector", conn)
    return asyncio.run(server.call("tabs"))


def test_command_timeout_structured(monkeypatch):
    """Медленный демон -> COMMAND_TIMEOUT вместо зависания."""
    # ужимаем потолок до минимума, не трогаяTIMEOUTS["tabs"]=15:
    monkeypatch.setattr(server.timing_config, "get",
                        lambda path, default=None: 200.0
                        if path.endswith("mcp_tool_hard_timeout_ms")
                        else default)
    # штатный таймаут tabs=15c тоже больше 0.2c — берём hard=max(0.2,15)=15,
    # поэтому дополнительно переопределяем TIMEOUTS для теста.
    saved = dict(server.TIMEOUTS)
    server.TIMEOUTS["tabs"] = 0.2
    try:
        result = _run(monkeypatch, SlowConnector)
    finally:
        server.TIMEOUTS.clear()
        server.TIMEOUTS.update(saved)
    assert result["ok"] is False
    assert result["error_code"] == "COMMAND_TIMEOUT"
    assert result["state_uncertain"] is True
    assert result["retry_allowed"] is False
    assert "duration_ms" in result and "budget_ms" in result


def test_fast_path_passthrough(monkeypatch):
    """Быстрый ответ демона возвращается без изменений семантики."""
    result = _run(monkeypatch, FastConnector)
    assert result["ok"] is True
    assert result.get("value") == 42
    # per-call метрика добавлена
    assert isinstance(result.get("duration_ms"), int)


def test_ceiling_at_least_mcp_default():
    """Потолок по умолчанию >= 5000 мс из behavior.json."""
    ceiling = server.timing_config.get("timing.mcp_tool_hard_timeout_ms", 0)
    assert float(ceiling) >= 5000.0
