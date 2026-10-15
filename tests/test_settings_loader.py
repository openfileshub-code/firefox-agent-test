"""Тесты settings_loader (Этап 2).

Критерии приёмки ТЗ:
- невалидный JSON не роняет загрузчик (fallback на safe defaults);
- изменение allow_new_tab влияет на policy decision;
- hot reload работает без перезапуска процесса.
"""

import json
import time

import pytest

from modules.settings_loader import (
    SettingsLoader, SAFE_DEFAULT_GLOBAL, ENV_VAR,
)


GOOD = {
    "version": "1.0",
    "global": {"allow_new_tab": False, "max_retries": 2},
    "sites": {
        "youtube.com": {
            "aliases": ["youtu.be", "music.youtube.com"],
            "intents": {
                "open_item_on_current_page": {
                    "allowed_tools": ["browser_find_link"],
                    "forbidden_tools": ["browser_new_tab", "browser_scroll"],
                },
            },
        },
    },
    "override_phrases": {"allow_new_tab": ["открой в новой вкладке"]},
}


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_missing_file_falls_back_to_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "nonexistent"))
    loader = SettingsLoader(explicit_path=str(tmp_path / "absent.json"),
                           include_project_config=False)
    s = loader.load()
    assert s.source == "defaults"
    assert s.g("allow_new_tab") is False
    assert s.g("same_tab_by_default") is True
    assert s.g("relevance_threshold") == SAFE_DEFAULT_GLOBAL["relevance_threshold"]


def test_invalid_json_does_not_crash_and_warns(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json !!!", encoding="utf-8")
    loader = SettingsLoader(explicit_path=str(bad), include_project_config=False)
    s = loader.load()
    assert s.source == "defaults"
    assert any("SETTINGS_INVALID_FALLBACK" in w for w in s.warnings)


def test_schema_violation_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    p = tmp_path / "s.json"
    write(p, {"global": "should be object"})  # нет version + неверный тип
    loader = SettingsLoader(explicit_path=str(p), include_project_config=False)
    s = loader.load()
    assert s.source == "defaults"
    assert any("SETTINGS_INVALID_FALLBACK" in w for w in s.warnings)


def test_valid_file_loaded_and_hash_stable(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    p = tmp_path / "s.json"
    write(p, GOOD)
    loader = SettingsLoader(explicit_path=str(p), include_project_config=False)
    s1 = loader.load()
    s2 = loader.load()
    assert s1.source == "file"
    assert s1.hash and s1.hash == s2.hash


def test_allow_new_tab_change_affects_decision(tmp_path, monkeypatch):
    """allow_new_tab:true + override-фраза => guard должен разрешить."""
    from modules.policy_guard import PolicyGuard

    monkeypatch.delenv(ENV_VAR, raising=False)
    p = tmp_path / "s.json"
    write(p, GOOD)
    loader = SettingsLoader(explicit_path=str(p), include_project_config=False)
    guard = PolicyGuard(loader)

    class Ctx:
        site = "youtube.com"
        same_tab_required = True
        url = "https://www.youtube.com/"
        video_id = None

    blocked = guard.check("browser_new_tab", {"url": ""}, Ctx())
    assert blocked.allowed is False
    assert blocked.error_code == "POLICY_NEW_TAB_NOT_ALLOWED"

    good = json.loads(json.dumps(GOOD))
    good["global"]["allow_new_tab"] = True
    write(p, good)
    time.sleep(0.01)
    allowed = guard.check(
        "browser_new_tab",
        {"url": "", "override_phrase": "открой в новой вкладке"},
        Ctx(),
    )
    assert allowed.allowed is True


def test_override_phrase_whitelist_enforced(tmp_path, monkeypatch):
    from modules.policy_guard import PolicyGuard

    monkeypatch.delenv(ENV_VAR, raising=False)
    p = tmp_path / "s.json"
    write(p, GOOD)
    guard = PolicyGuard(SettingsLoader(explicit_path=str(p), include_project_config=False))

    class Ctx:
        site = "youtube.com"
        same_tab_required = True
        url = "https://www.youtube.com/"
        video_id = None

    # выдуманная агентом фраза не является override
    d = guard.check("browser_new_tab",
                    {"url": "", "override_phrase": "пользователь хотел"}, Ctx())
    assert d.allowed is False


def test_hot_reload_by_mtime(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    p = tmp_path / "s.json"
    write(p, GOOD)
    loader = SettingsLoader(explicit_path=str(p), include_project_config=False)
    s = loader.load()
    assert s.g("allow_site_search") is False

    changed = json.loads(json.dumps(GOOD))
    changed["global"]["allow_site_search"] = True
    time.sleep(0.01)
    write(p, changed)
    os_times = p.stat().st_mtime + 1
    import os
    os.utime(p, (os_times, os_times))

    assert loader.reload_if_changed() is True
    assert loader.get().g("allow_site_search") is True


def test_site_alias_matching(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    p = tmp_path / "s.json"
    write(p, GOOD)
    s = SettingsLoader(explicit_path=str(p), include_project_config=False).load()
    assert s.get_intent_rules("music.youtube.com", "open_item_on_current_page")
    assert s.get_intent_rules("m.youtube.com", "open_item_on_current_page")
    assert not s.get_site_rules("example.org")


def test_priority_env_over_project(tmp_path, monkeypatch):
    env_file = tmp_path / "env.json"
    custom = json.loads(json.dumps(GOOD))
    custom["global"]["max_retries"] = 5
    write(env_file, custom)
    monkeypatch.setenv(ENV_VAR, str(env_file))
    monkeypatch.setenv("APPDATA", str(tmp_path / "none"))
    loader = SettingsLoader(include_project_config=False)
    s = loader.load()
    assert s.path == str(env_file)
    assert s.g("max_retries") == 5
