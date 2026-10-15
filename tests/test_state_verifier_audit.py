"""ТЗ этап 7: модульные тесты state_verifier и audit_logger."""
import json


from modules.audit_logger import AuditLogger
from modules.state_verifier import verify_state, verify_video_action_result


# ---------------------------------------------------------------- verify_state

class TestVerifyState:
    def test_theater_matches(self):
        snap = {"layout": {"theater_mode": True, "confidence": 0.9}}
        res = verify_state("theater_set", snap, expected_theater=True)
        assert res.passed and not res.mismatches

    def test_theater_mismatch_verify_failed(self):
        # RC10-сценарий: просили theater, факт False
        snap = {"layout": {"theater_mode": False, "confidence": 0.9}}
        res = verify_state("theater_set", snap, expected_theater=True)
        assert not res.passed
        assert res.error_code == "VERIFY_FAILED"
        assert res.mismatches[0]["field"] == "layout.theater_mode"

    def test_theater_low_confidence_unknown(self):
        snap = {"layout": {"theater_mode": True, "confidence": 0.3}}
        res = verify_state("theater_set", snap, expected_theater=True,
                           min_confidence=0.6)
        assert not res.passed
        assert res.error_code == "THEATER_STATE_UNKNOWN"

    def test_theater_missing_field(self):
        res = verify_state("theater_set", {}, expected_theater=True)
        assert not res.passed
        assert res.error_code == "THEATER_STATE_UNKNOWN"

    def test_volume_tolerance_settings_value(self):
        # RC9: target 0.1, факт 0.08015735 — при допуске 0.005 это ПРОВАЛ
        snap = {"state": {"volume": 0.08015735}}
        res = verify_state("volume_set", snap, expected_volume=0.1,
                           volume_tolerance=0.005)
        assert not res.passed
        assert res.error_code == "VERIFY_FAILED"

    def test_volume_within_tolerance_passes(self):
        snap = {"state": {"volume": 0.0998}}
        res = verify_state("volume_set", snap, expected_volume=0.1,
                           volume_tolerance=0.005)
        assert res.passed

    def test_paused_check(self):
        res = verify_state("pause", {"state": {"paused": False}},
                           expected_paused=True)
        assert not res.passed and res.error_code == "VERIFY_FAILED"
        res2 = verify_state("pause", {"state": {"paused": True}},
                            expected_paused=True)
        assert res2.passed

    def test_video_context_changed(self):
        # RC11: radio autoplay подменил видео
        res = verify_state("play", {"video_id": "BBBBBBBBBBB"},
                           expected_video_id="AAAAAAAAAAA")
        assert not res.passed
        assert res.error_code == "VIDEO_CONTEXT_CHANGED"

    def test_no_expectations_passes(self):
        res = verify_state("state", {"whatever": 1})
        assert res.passed and res.checked_fields == []

    def test_to_dict_shape_on_fail(self):
        res = verify_state("theater_set", {}, expected_theater=True)
        d = res.to_dict()
        assert d["verified"] is False
        assert d["error_code"] == "THEATER_STATE_UNKNOWN"
        assert "action" in d and "checked_fields" in d


# --------------------------------------------------- verify_video_action_result

class TestVerifyVideoActionResult:
    def test_theater_set_expected_from_target(self):
        result = {"ok": True, "target": True,
                  "layout": {"theater_mode": False, "confidence": 0.9},
                  "state": {}}
        res = verify_video_action_result("theater_set", result)
        assert not res.passed
        assert res.error_code == "VERIFY_FAILED"

    def test_theater_set_noop_still_consistent(self):
        result = {"ok": True, "target": True,
                  "layout": {"theater_mode": True, "confidence": 0.85},
                  "state": {}}
        assert verify_video_action_result("theater_set", result).passed

    def test_volume_target_from_payload(self):
        result = {"ok": True, "target_volume": 0.1,
                  "state": {"volume": 0.08015735}, "layout": {}}
        res = verify_video_action_result("volume_set", result)
        assert not res.passed
        assert res.error_code == "VERIFY_FAILED"

    def test_pause_expected_paused_true(self):
        result = {"ok": True, "state": {"paused": True}, "layout": {}}
        assert verify_video_action_result("pause", result).passed

    def test_play_expected_paused_false(self):
        result = {"ok": True, "state": {"paused": True}, "layout": {}}
        res = verify_video_action_result("play", result)
        assert not res.passed

    def test_expected_video_id_drift(self):
        result = {"ok": True, "expected_video_id": "AAAAAAAAAAA",
                  "video_id": "ZZZZZZZZZZZ", "state": {}, "layout": {}}
        res = verify_video_action_result("episode_next", result)
        assert not res.passed
        assert res.error_code == "VIDEO_CONTEXT_CHANGED"

    def test_old_adapter_no_data_no_false_negative(self):
        # нет layout/state/target -> passed=True (не блокируем старые ответы)
        result = {"ok": True}
        res = verify_video_action_result("toggle_theater_mode", result)
        assert res.passed

    def test_settings_loader_object_accepted(self):
        from modules.settings_loader import SettingsLoader
        loader = SettingsLoader(explicit_path="__no_such_file__", include_project_config=False)
        loader.load()
        result = {"ok": True, "target_volume": 0.1,
                  "state": {"volume": 0.08}, "layout": {}}
        res = verify_video_action_result("volume_set", result,
                                         settings=loader)
        assert not res.passed  # толерантность из safe defaults 0.005


# ------------------------------------------------------------------ AuditLogger

class TestAuditLogger:
    def test_record_writes_jsonl(self, tmp_path):
        log = AuditLogger(path=str(tmp_path / "a" / "audit.jsonl"))
        log.record("browser_scroll", {"amount": 5},
                   decision={"allowed": False, "error_code": "POLICY_SCROLL_FORBIDDEN",
                             "rule_source": "§Повторы и слепые ретраи"},
                   result={"ok": False},
                   context={"site": "youtube.com", "video_id": "AAAAAAAAAAA",
                            "list_id": None, "is_radio": False})
        rows = [json.loads(l) for l in
                (tmp_path / "a" / "audit.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert len(rows) == 1
        row = rows[0]
        assert row["tool"] == "browser_scroll"
        assert row["guard"]["allowed"] is False
        assert row["guard"]["error_code"] == "POLICY_SCROLL_FORBIDDEN"
        assert row["context"]["video_id"] == "AAAAAAAAAAA"
        assert row["args_hash"] and len(row["args_hash"]) == 16

    def test_args_not_leaked_in_plaintext(self, tmp_path):
        log = AuditLogger(path=str(tmp_path / "audit.jsonl"))
        secret = "https://example.com/watch?v=AAAAAAAAAAA&token=SECRET"
        log.record("browser_navigate", {"url": secret})
        text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "SECRET" not in text  # аргументы — только хэш

    def test_disabled_logger_writes_nothing(self, tmp_path):
        p = tmp_path / "audit.jsonl"
        log = AuditLogger(path=str(p), enabled=False)
        log.record("browser_tabs", {})
        assert not p.exists()

    def test_fail_open_on_bad_path(self):
        log = AuditLogger(path="/proc/nonexistent-dir/audit.jsonl")
        log.record("browser_tabs", {})  # не бросает

    def test_rotation_keeps_one_backup(self, tmp_path):
        from modules.audit_logger import MAX_BYTES
        p = tmp_path / "audit.jsonl"
        log = AuditLogger(path=str(p))
        # переполняем файл сверх лимита
        p.write_text("x" * (MAX_BYTES + 1), encoding="utf-8")
        log.record("browser_tabs", {})
        assert p.exists()
        assert (tmp_path / "audit.jsonl.1").exists()
        # свежая запись осталась в основном файле
        assert json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
