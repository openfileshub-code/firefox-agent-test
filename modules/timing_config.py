"""Конфигурация тайминг-бюджета и поведения (объединённое ТЗ v2.0, §7, §17).

Единый источник дефолтов для server.py / browserd.py / адаптеров. Читает
``config/behavior.json``; при отсутствии/битости файла использует встроенные
дефолты, совпадающие с целевой таблицей таймаутов ТЗ §7.2:

    обычный цикл видеодействия ............ 150–800 мс (soft target)
    дедлайн подтверждения theater_set ..... 1500 мс (soft deadline)
    жёсткий потолок видеокоманды демона ... 2500 мс
    абсолютный потолок MCP-инструмента .... 5000 мс
    окно тишины пользовательского ввода ... 250 мс (ожидание ≤ 300 мс)
    polling верификации .................. шаг 20 мс, суммарно ≤ 400–800 мс

Модуль не зависит от playwright и браузера — пригоден для unit-тестов.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "behavior.json"

#: Встроенные дефолты = целевые значения ТЗ v2.0 (раздел 7/17).
DEFAULTS: dict[str, Any] = {
    "fast_css": {
        "enabled": True,
        "class_name": "agent-fast-mode",
        "auto_remove_after_ms": 800,
        "force_controls_visible": True,
        "disable_transitions": True,
        "disable_animations": True,
        "keep_fast_mode": False,
    },
    "timing": {
        "video_action_soft_target_ms": 800,
        "video_action_verification_deadline_ms": 1500,
        "video_action_hard_timeout_ms": 2500,
        "mcp_tool_hard_timeout_ms": 5000,
        "element_check_timeout_ms": 300,
        "state_verify_timeout_ms": 800,
        "state_poll_interval_ms": 20,
        "retry_max": 1,
        "retry_pause_ms": 100,
    },
    "user_input": {
        "quiescence_ms": 250,
        "max_wait_for_quiescence_ms": 300,
        "abort_on_activity": True,
        "tracked_events": [
            "mousemove", "mousedown", "keydown", "wheel", "touchstart",
        ],
    },
    "youtube": {
        "theater_channels": [
            "keyboard_t", "aria_button", "ytp_size_button", "js_click",
        ],
        "allow_state_uncertain": True,
        "no_repeat_after_dispatch": True,
        "min_confidence_for_toggle": 0.8,
    },
    "policy": {
        "max_same_policy_denials": 2,
        "auto_execute_first_safe_action": False,
        "return_structured_errors_only": True,
    },
    "daemon": {
        "command_hard_timeout_ms": 2500,
        "heartbeat_interval_ms": 5000,
        "watchdog_probe_interval_ms": 500,
        "watchdog_nonblocking": True,
    },
}

_cache: dict[str, Any] | None = None
_lock = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(force: bool = False) -> dict[str, Any]:
    """Загружает config/behavior.json поверх DEFAULTS (с кэшированием)."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        cfg = dict(DEFAULTS)
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg = _merge(DEFAULTS, user)
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — битый конфиг не должен ронять демон
            pass
        _cache = cfg
        return cfg


def section(name: str) -> dict[str, Any]:
    return load().get(name) or {}


def get(path: str, default: Any = None) -> Any:
    """Доступ по 'section.key', напр. get('timing.video_action_hard_timeout_ms')."""
    cur: Any = load()
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


__all__ = ["CONFIG_PATH", "DEFAULTS", "load", "section", "get"]
