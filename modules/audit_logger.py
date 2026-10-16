"""Audit logger (ТЗ этап 7, вспомогательный).

Пишет в JSONL каждый tool call: tool, args-hash, решение guard'а
(allow/deny + error_code + rule_source), итог команды демона и снимок
контекста. Даёт доказательную базу для разбора «почему агент поступил
именно так» (см. docs/contract-audit.md).

Файл: logs/tool_audit.jsonl (RotatingFileHandler-подобная ротация по
размеру реализована вручную, чтобы не тянуть logging-конвейер).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("FirefoxMCP_Audit")

MAX_BYTES = 5 * 1024 * 1024  # 5 MB, затем .1 ротация


def _args_hash(args: dict) -> str:
    blob = json.dumps(args or {}, sort_keys=True, ensure_ascii=False,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class AuditLogger:
    def __init__(self, path: Optional[str] = None, enabled: bool = True):
        self.path = Path(path) if path else Path("logs") / "tool_audit.jsonl"
        self.enabled = enabled
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: BLE001
            log.warning("audit dir create failed: %s", exc)

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                rot = self.path.with_suffix(".jsonl.1")
                if rot.exists():
                    rot.unlink()
                self.path.rename(rot)
        except OSError as exc:  # noqa: BLE001
            log.warning("audit rotate failed: %s", exc)

    def record(self, tool: str, args: dict, *,
               decision: Optional[dict] = None,
               result: Optional[dict] = None,
               context: Optional[dict] = None) -> None:
        """Записать один tool call. Никогда не бросает (fail-open)."""
        if not self.enabled:
            return
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "args_hash": _args_hash(args),
            "guard": {
                "allowed": bool((decision or {}).get("allowed", True)),
                "error_code": (decision or {}).get("error_code"),
                "rule_source": (decision or {}).get("rule_source"),
            } if decision is not None else None,
            "ok": (result or {}).get("ok") if isinstance(result, dict) else None,
            "result_error": (result or {}).get("error")
            if isinstance(result, dict) else None,
            "context": {
                "site": (context or {}).get("site"),
                "video_id": (context or {}).get("video_id"),
                "list_id": (context or {}).get("list_id"),
                "is_radio": (context or {}).get("is_radio"),
            } if context is not None else None,
        }
        try:
            with self._lock:
                self._ensure_dir()
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001
            log.warning("audit write failed: %s", exc)


_GLOBAL: Optional[AuditLogger] = None


def get_audit_logger(path: Optional[str] = None) -> AuditLogger:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = AuditLogger(path=path)
    return _GLOBAL


__all__ = ["AuditLogger", "get_audit_logger"]
