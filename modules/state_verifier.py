"""State verifier (ТЗ этап 7).

Пост-проверка mutate-действий: после browser_video / theater_set / volume_set
состояние страницы СЧИТАЕТСЯ, а не предполагается. Результат — структурированный
`VerificationResult` с полем `passed`; при расхождении — error_code VERIFY_FAILED
(см. docs/policy-errors.md) и фактические evidence.

Модуль не зависит от playwright: принимает «сырой» снимок состояния (dict),
который собирает вызывающий код (server.py middleware / адаптер). Это делает
логику верификации тестируемой без браузера.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class VerificationResult:
    action: str
    passed: bool
    checked_fields: list = field(default_factory=list)
    mismatches: list = field(default_factory=list)   # [{field, expected, actual}]
    error_code: Optional[str] = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "verified": self.passed,
            "action": self.action,
            "checked_fields": self.checked_fields,
            "mismatches": self.mismatches,
            **({"error_code": self.error_code, "message": self.message}
               if not self.passed else {}),
        }


def _get_path(snapshot: dict, dotted: str) -> Any:
    cur: Any = snapshot
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def verify_state(action: str,
                 snapshot: dict,
                 *,
                 expected_theater: Optional[bool] = None,
                 expected_volume: Optional[float] = None,
                 expected_paused: Optional[bool] = None,
                 expected_video_id: Optional[str] = None,
                 volume_tolerance: float = 0.005,
                 min_confidence: float = 0.6) -> VerificationResult:
    """Сверить снимок post-state с ожидаемым результатом действия.

    Проверяются только те поля, которые заданы (не None) — остальные
    просто не участвуют. Поля читаются из snapshot по путям:
      layout.theater_mode / layout.confidence / state.volume /
      state.paused / video_id.
    """
    res = VerificationResult(action=action, passed=True)
    snap = snapshot or {}

    def mismatch(fname: str, expected: Any, actual: Any) -> None:
        res.mismatches.append(
            {"field": fname, "expected": expected, "actual": actual})

    # --- theater -----------------------------------------------------
    if expected_theater is not None:
        lay = snap.get("layout") or {}
        actual = lay.get("theater_mode",
                         (snap.get("state") or {}).get("theater_mode"))
        conf = lay.get("confidence")
        res.checked_fields.append("layout.theater_mode")
        if actual is None:
            mismatch("layout.theater_mode", expected_theater, None)
            res.error_code = "THEATER_STATE_UNKNOWN"
        elif bool(actual) != expected_theater:
            mismatch("layout.theater_mode", expected_theater, actual)
            res.error_code = "VERIFY_FAILED"
        elif conf is not None and float(conf) < min_confidence:
            mismatch("layout.confidence", f">={min_confidence}", conf)
            res.error_code = "THEATER_STATE_UNKNOWN"

    # --- volume ------------------------------------------------------
    if expected_volume is not None:
        actual = (snap.get("state") or {}).get("volume", snap.get("volume"))
        res.checked_fields.append("state.volume")
        if actual is None or abs(float(actual) - float(expected_volume)) > volume_tolerance:
            mismatch("state.volume", expected_volume, actual)
            res.error_code = "VERIFY_FAILED"

    # --- paused ------------------------------------------------------
    if expected_paused is not None:
        actual = (snap.get("state") or {}).get("paused", snap.get("paused"))
        res.checked_fields.append("state.paused")
        if actual is None or bool(actual) != expected_paused:
            mismatch("state.paused", expected_paused, actual)
            res.error_code = "VERIFY_FAILED"

    # --- video context ----------------------------------------------
    if expected_video_id is not None and VIDEO_ID_RE.match(str(expected_video_id)):
        actual = snap.get("video_id") or (snap.get("state") or {}).get("video_id")
        res.checked_fields.append("video_id")
        if actual and actual != expected_video_id:
            mismatch("video_id", expected_video_id, actual)
            res.error_code = "VIDEO_CONTEXT_CHANGED"

    res.passed = not res.mismatches
    if not res.passed and not res.error_code:
        res.error_code = "VERIFY_FAILED"
    if not res.passed:
        res.message = "Пост-проверка '%s' не пройдена: %s" % (
            action, "; ".join("%s: ожидалось %r, получено %r" %
                              (m["field"], m["expected"], m["actual"])
                              for m in res.mismatches))
    return res


def verify_video_action_result(action: str, result: dict,
                               settings=None) -> VerificationResult:
    """Верификация по ответу демона на browser_video (для middleware).

    Берёт то, что адаптер уже посчитал (applied/changed/state/layout), и
    сверяет с ожиданиями из параметров. Если данных для проверки нет —
    passed=True с пустым checked_fields (нет false-negative на старых
    адаптерах).
    """
    g = settings.g if settings is not None else (lambda k: None)
    tol = g("volume_tolerance") or 0.005
    snap = {
        "state": result.get("state") or {},
        "layout": result.get("layout") or {},
        "video_id": result.get("video_id"),
    }
    exp_theater = None
    if action == "theater_set":
        exp_theater = bool(result.get("target", True)) \
            if "target" in result else None
    exp_volume = None
    if action in ("volume_set", "set_volume", "volume") and \
            result.get("target_volume") is not None:
        exp_volume = float(result["target_volume"])
    exp_paused = None
    if action in ("play", "pause"):
        exp_paused = (action == "pause")
    return verify_state(action, snap,
                        expected_theater=exp_theater,
                        expected_volume=exp_volume,
                        expected_paused=exp_paused,
                        expected_video_id=result.get("expected_video_id"),
                        volume_tolerance=float(tol))


__all__ = ["VerificationResult", "verify_state", "verify_video_action_result"]
