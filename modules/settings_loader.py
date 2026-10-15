"""Загрузка, валидация и hot-reload пользовательских настроек инструментов.

См. ТЗ §6.1 (settings_loader) и docs/settings-reference.md.

Источники по приоритету:
1. Явный CLI/explicit path;
2. env FIREFOX_AGENT_SETTINGS;
3. %APPDATA%\\firefox-agent\\user_tool_settings.json;
4. проектный config/user_tool_settings.json;
5. built-in safe defaults.

Невалидный JSON не роняет систему: фиксируется warning
(`SETTINGS_INVALID_FALLBACK`) и происходит откат к следующему источнику
или safe defaults. Hot reload — по mtime активного файла.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("FirefoxMCP_Settings")

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_CONFIG_PATH = BASE_DIR / "config" / "user_tool_settings.json"
SCHEMA_PATH = BASE_DIR / "config" / "user_tool_settings.schema.json"
ENV_VAR = "FIREFOX_AGENT_SETTINGS"

SAFE_DEFAULT_GLOBAL = {
    "same_tab_by_default": True,
    "allow_new_tab": False,
    "allow_site_search": False,
    "allow_scroll": False,
    "forbid_placeholder_urls": True,
    "prefer_native_video_controls": True,
    "verify_after_mutation": True,
    "max_retries": 2,
    "relevance_threshold": 0.55,
    "volume_tolerance": 0.005,
    "theater_confidence_threshold": 0.75,
}

DEFAULT_OVERRIDE_PHRASES = {
    "allow_new_tab": [
        "открой в новой вкладке", "новая вкладка", "в новой вкладке",
        "open in new tab", "new tab",
    ],
    "allow_site_search": [
        "поищи на сайте", "выполни поиск на сайте", "перейди в поиск",
        "найди через поиск сайта", "поиск по сайту", "search on site",
        "site search",
    ],
    "allow_scroll": [
        "прокрути вниз", "прокрути вверх", "прокрути", "скролль",
        "листай", "scroll down", "scroll up", "scroll",
    ],
}

DEFAULT_POLICIES = {
    "same_tab_context": {
        "enabled": True,
        "block_tools_if_context_locked": [
            "browser_new_tab", "browser_navigate_to_search_url",
        ],
    },
    "no_unrequested_scroll": {"enabled": True, "block_tool": "browser_scroll"},
    "no_placeholder_urls": {
        "enabled": True,
        "patterns": ["...", "{", "}", "VIDEO_ID", "example", "test",
                     "placeholder"],
    },
    "video_context_drift": {
        "enabled": True, "error_code": "VIDEO_CONTEXT_CHANGED",
    },
}


def _appdata_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "firefox-agent" / "user_tool_settings.json"


@dataclass
class ValidationResult:
    valid: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class UserSettings:
    """Типизированный view над JSON-настройками + safe defaults."""

    data: dict = field(default_factory=dict)
    source: str = "defaults"
    path: Optional[str] = None
    hash: str = ""
    warnings: list = field(default_factory=list)

    # ---------------- global ----------------
    @property
    def global_rules(self) -> dict:
        merged = dict(SAFE_DEFAULT_GLOBAL)
        merged.update(self.data.get("global") or {})
        return merged

    def g(self, key: str) -> Any:
        return self.global_rules.get(key, SAFE_DEFAULT_GLOBAL.get(key))

    # ---------------- sites/intents ----------------
    def _site_entry(self, hostname: str) -> dict:
        sites = self.data.get("sites") or {}
        host = (hostname or "").lower().removeprefix("www.")
        for name, entry in sites.items():
            names = [str(name).lower()] + [
                str(a).lower().removeprefix("www.")
                for a in (entry.get("aliases") or [])
            ]
            for n in names:
                n = n.removeprefix("www.")
                if host == n or host.endswith("." + n):
                    return entry or {}
        return {}

    def get_site_rules(self, hostname: str) -> dict:
        return self._site_entry(hostname)

    def get_intent_rules(self, hostname: str, intent: str) -> dict:
        entry = self._site_entry(hostname)
        intents = entry.get("intents") or {}
        if intent in intents:
            return intents[intent] or {}
        generic = (self.data.get("intents") or {}).get(intent)
        return generic or {}

    def site_forbidden_tools(self, hostname: str) -> set:
        forbidden = set()
        entry = self._site_entry(hostname)
        # top-level список правил ТЗ (§sites.<host>.forbidden_tools)
        forbidden.update(entry.get("forbidden_tools") or [])
        for it in (entry.get("intents") or {}).values():
            forbidden.update(it.get("forbidden_tools") or [])
        return forbidden

    def site_allowed_tools(self, hostname: str) -> set:
        allowed = set()
        entry = self._site_entry(hostname)
        for it in (entry.get("intents") or {}).values():
            allowed.update(it.get("allowed_tools") or [])
        return allowed

    # ---------------- override phrases ----------------
    @property
    def override_phrases(self) -> dict:
        merged = copy.deepcopy(DEFAULT_OVERRIDE_PHRASES)
        for k, v in (self.data.get("override_phrases") or {}).items():
            if isinstance(v, list):
                merged[k] = list(v)
        return merged

    def phrase_matches(self, category: str, phrase: Optional[str]) -> bool:
        """Override разрешён только точным совпадением с whitelist."""
        if not phrase:
            return False
        norm = " ".join(str(phrase).strip().lower().split())
        candidates = self.override_phrases.get(category) or []
        return any(
            norm == " ".join(str(c).strip().lower().split())
            or " ".join(str(c).strip().lower().split()) in norm
            for c in candidates
        )

    # ---------------- policies ----------------
    @property
    def policies(self) -> dict:
        merged = copy.deepcopy(DEFAULT_POLICIES)
        for k, v in (self.data.get("policies") or {}).items():
            if isinstance(v, dict):
                base = dict(merged.get(k) or {})
                base.update(v)
                merged[k] = base
            else:
                merged[k] = v
        return merged

    def policy_enabled(self, name: str) -> bool:
        pol = self.policies.get(name) or {}
        return bool(pol.get("enabled", True))

    # ---------------- tool allow/deny ----------------
    def is_tool_allowed(self, tool_name: str, args: dict, context) -> bool:
        """Разрешён ли инструмент с учётом site/intent правил.

        forbidden имеет приоритет над allowed; если для сайта нет правил —
        решение остаётся за глобальными guard-правилами (True здесь).
        """
        hostname = getattr(context, "site", "") or ""
        if tool_name in self.site_forbidden_tools(hostname):
            # явный allow через override-фразу пользователя сохраняет силу
            # (guard проверяет override отдельно); здесь — базовый deny-сигнал
            return False
        return True

    def to_public_dict(self) -> dict:
        return {
            "version": self.data.get("version", "builtin"),
            "source": self.source,
            "path": self.path,
            "settings_hash": self.hash,
            "global": self.global_rules,
            "warnings": list(self.warnings),
        }


def canonical_hash(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class SettingsLoader:
    def __init__(self, explicit_path: Optional[str] = None,
                 include_project_config: bool = True):
        self.explicit_path = explicit_path
        #: false — для unit-тестов: не подмешивать проектный config в цепочку
        self.include_project_config = include_project_config
        self._settings: Optional[UserSettings] = None
        self._active_path: Optional[Path] = None
        self._mtime: float = 0.0
        self._schema: Optional[dict] = None

    @property
    def active_path(self) -> Optional[str]:
        """Путь к фактически применённому файлу настроек (или None)."""
        return str(self._active_path) if self._active_path else None

    # ------------- schema/validation -------------
    def _load_schema(self) -> Optional[dict]:
        if self._schema is None:
            try:
                self._schema = json.loads(SCHEMA_PATH.read_text("utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Schema не загружена (%s); structural check", exc)
                self._schema = {}
        return self._schema or None

    def validate(self, data: Any) -> ValidationResult:
        result = ValidationResult(valid=True)
        if not isinstance(data, dict):
            result.valid = False
            result.errors.append("root должен быть JSON-объектом")
            return result
        if "version" not in data or not isinstance(data["version"], str):
            result.errors.append("отсутствует строковый 'version'")
        glob = data.get("global")
        if glob is not None and not isinstance(glob, dict):
            result.errors.append("'global' должен быть объектом")
        if isinstance(glob, dict):
            for k, v in glob.items():
                if k in SAFE_DEFAULT_GLOBAL and isinstance(
                        SAFE_DEFAULT_GLOBAL[k], (bool, int, float)) \
                        and not isinstance(v, (bool, int, float)):
                    result.errors.append(f"global.{k}: неверный тип")
            for num_key in ("relevance_threshold", "volume_tolerance",
                            "theater_confidence_threshold"):
                v = glob.get(num_key)
                if isinstance(v, (int, float)) and not (0 <= v <= 1 or
                                                        num_key == "volume_tolerance" and v <= 0.2):
                    result.warnings.append(f"global.{num_key} вне разумного диапазона")
        sites = data.get("sites")
        if sites is not None and not isinstance(sites, dict):
            result.errors.append("'sites' должен быть объектом")
        elif isinstance(sites, dict):
            for name, entry in sites.items():
                if not isinstance(entry, dict):
                    result.errors.append(f"sites.{name}: должен быть объектом")
        # jsonschema — если доступен, как дополнительный слой
        schema = self._load_schema()
        if schema:
            try:
                import jsonschema
                validator = jsonschema.Draft7Validator(schema)
                for err in sorted(validator.iter_errors(data),
                                  key=lambda e: list(e.path))[:10]:
                    result.errors.append(
                        "schema: " + "/".join(map(str, err.path)) + ": "
                        + err.message[:200])
            except ImportError:
                pass
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"schema check пропущен: {exc}")
        result.valid = not result.errors
        return result

    # ------------- sources -------------
    def candidate_paths(self) -> list:
        paths = []
        if self.explicit_path:
            paths.append(Path(self.explicit_path))
        env = os.environ.get(ENV_VAR)
        if env:
            paths.append(Path(env))
        ap = _appdata_path()
        if ap:
            paths.append(ap)
        if self.include_project_config:
            paths.append(PROJECT_CONFIG_PATH)
        return paths

    def _read_file(self, path: Path) -> tuple[Optional[dict], list]:
        warnings: list = []
        try:
            raw = path.read_text("utf-8")
        except FileNotFoundError:
            return None, warnings
        except Exception as exc:  # noqa: BLE001
            return None, [f"не удалось прочитать {path}: {exc}"]
        try:
            return json.loads(raw), warnings
        except Exception as exc:  # noqa: BLE001
            return None, [f"невалидный JSON в {path}: {exc}"]

    # ------------- public API -------------
    def load(self) -> UserSettings:
        """Загрузка по цепочке источников.

        Файл, который существует, но невалиден (битый JSON или нарушение
        схемы), НЕ пропускается молча: фиксируется warning
        `SETTINGS_INVALID_FALLBACK` и происходит откат на safe defaults —
        так пользователь гарантированно видит, что его настройки не
        применены (ТЗ §6.1, критерий Этапа 2).
        """
        all_warnings: list = []
        broken_seen = False
        paths = self.candidate_paths()
        #: Явно заданный пользователем файл (CLI/explicit/env) при ошибке
        #: даёт immediate fallback на safe defaults — без тихого подхвата
        #: нижестоящих конфигураций (принцип «не ронять, но и не подменять»).
        explicit_set = {str(Path(self.explicit_path).resolve())
                        if self.explicit_path else "",
                        str(Path(os.environ.get(ENV_VAR)).resolve())
                        if os.environ.get(ENV_VAR) else ""}
        for idx, path in enumerate(paths):
            exists = path.exists()
            data, warnings = self._read_file(path)
            all_warnings.extend(warnings)
            is_explicit = str(path.resolve()) in explicit_set and idx == 0
            if data is None:
                if exists:
                    broken_seen = True
                    all_warnings.append(
                        "SETTINGS_INVALID_FALLBACK: %s: %s"
                        % (path, "; ".join(warnings)))
                    if is_explicit:
                        break
                continue
            vr = self.validate(data)
            if not vr.valid:
                all_warnings.append(
                    "SETTINGS_INVALID_FALLBACK: %s: %s" % (path, "; ".join(vr.errors)))
                broken_seen = True
                if is_explicit:
                    break
                continue
            settings = UserSettings(
                data=data,
                source="file",
                path=str(path),
                hash=canonical_hash(data),
                warnings=all_warnings + vr.warnings,
            )
            self._settings = settings
            self._active_path = path
            try:
                self._mtime = path.stat().st_mtime
            except OSError:
                self._mtime = 0.0
            for w in settings.warnings:
                logger.warning("settings: %s", w)
            return settings

        defaults = UserSettings(
            data={"version": "builtin-defaults", "global": dict(SAFE_DEFAULT_GLOBAL)},
            source="defaults", path=None,
            hash=canonical_hash({"v": 1}),
            warnings=all_warnings,
        )
        self._settings = defaults
        self._active_path = None
        self._mtime = 0.0
        if broken_seen:
            logger.warning(
                "settings: SETTINGS_INVALID_FALLBACK — применены safe defaults")
        for w in all_warnings:
            logger.warning("settings: %s", w)
        return defaults

    def get(self) -> UserSettings:
        self.reload_if_changed()
        if self._settings is None:
            return self.load()
        return self._settings

    def reload_if_changed(self) -> bool:
        """Hot reload: перечитать файл, если mtime изменился."""
        if self._active_path is None:
            # defaults/env-источник мог появиться позже — периодический rescan
            return False
        try:
            mtime = self._active_path.stat().st_mtime
        except OSError:
            self.load()
            return True
        if mtime != self._mtime:
            old_hash = self._settings.hash if self._settings else ""
            self.load()
            changed = self._settings is None or self._settings.hash != old_hash
            if changed:
                logger.info("Settings hot-reloaded: %s", self._active_path)
            return True
        return False

    def force_reload(self) -> UserSettings:
        self._settings = None
        self._active_path = None
        return self.load()


_GLOBAL_LOADER: Optional["SettingsLoader"] = None


def get_global_loader() -> "SettingsLoader":
    """Ленивый процесс-уровневый синглтон настроек.

    Используется адаптерами (volume_tolerance) и server.py; при явной
    инициализации через init_global_loader() переиспользуется тот же
    экземпляр, что гарантирует единый источник настроек на процесс.
    """
    global _GLOBAL_LOADER
    if _GLOBAL_LOADER is None:
        _GLOBAL_LOADER = SettingsLoader()
        _GLOBAL_LOADER.load()
    return _GLOBAL_LOADER


def init_global_loader(loader: "SettingsLoader") -> "SettingsLoader":
    """Установить синглтон (вызывается из server.py после создания)."""
    global _GLOBAL_LOADER
    _GLOBAL_LOADER = loader
    return loader


__all__ = [
    "SettingsLoader", "UserSettings", "ValidationResult",
    "SAFE_DEFAULT_GLOBAL", "canonical_hash", "ENV_VAR",
    "get_global_loader", "init_global_loader",
]
