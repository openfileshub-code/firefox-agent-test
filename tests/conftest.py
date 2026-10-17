"""Общие фикстуры тестов.

server.py держит глобальный singleton CONTEXT_MANAGER, поэтому состояние
Anti-Panic Lock и история PolicyGuard «протекают» между независимыми
тестами (например, test_deny_short_circuits_connector ставит лок через
INTENT_REDIRECT, а следующий test_allowed_command_passes_through получает
PANIC_LOOP_DETECTED). Autouse-фикстура сбрасывает состояние сервера перед
каждым тестом, обеспечивая изоляцию.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def reset_server_state():
    """Сброс глобального состояния server (panic lock, deny history) до теста."""
    try:
        import server
    except Exception:  # pragma: no cover - server может требовать опциональные зависимости
        yield
        return

    cm = getattr(server, "CONTEXT_MANAGER", None)
    guard = getattr(server, "POLICY_GUARD", None)
    if cm is not None and hasattr(cm, "clear_panic_lock"):
        cm.clear_panic_lock()
    if guard is not None:
        # очищаем накопленное состояние deny/retry-истории, если оно есть
        for attr in ("_newtab_deny_at", "_deny_streak"):
            value = getattr(guard, attr, None)
            if isinstance(value, dict):
                value.clear()
            elif isinstance(value, list):
                del value[:]
    yield
    if cm is not None and hasattr(cm, "clear_panic_lock"):
        cm.clear_panic_lock()
