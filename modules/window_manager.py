"""
Window persistence module for Firefox/Camoufox MCP server.

Saves and loads window position/size from config/window.json.
Applies settings at browser launch via Firefox CLI args.
"""

import json
import logging
import os
import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger("FirefoxMCP_Window")


class WindowManager:
    """Manages browser window size and position persistence."""

    DEFAULT_CONFIG: Dict[str, Any] = {
        "width": 1280,
        "height": 800,
        "maximized": False,
        "use_viewport": False,
        "save_interval_seconds": 60,
        "last_saved": None,
    }

    _JS_WINDOW = r"""
() => {
  return {
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    outerWidth: window.outerWidth,
    outerHeight: window.outerHeight,
    screenX: window.screenX,
    screenY: window.screenY,
    devicePixelRatio: window.devicePixelRatio
  };
}
"""

    def __init__(self, config_path: Optional[str] = None) -> None:
        if config_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base_dir, "config", "window.json")
        self.config_path = config_path
        self.config: Dict[str, Any] = dict(self.DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        """Load window configuration from JSON file."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.config.update(loaded)
        except Exception as e:
            logger.error(f"WindowManager load error: {e}")

    def save(self) -> None:
        """Save current window configuration to JSON file."""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"WindowManager save error: {e}")

    def get_launch_args(self) -> list:
        """
        Return Firefox CLI args for window sizing.
        Currently returns empty list because -width/-height args cause
        Firefox to interpret numbers as URLs (bug). Window size is
        restored from profile's xulstore.json via persistent_context=True.
        """
        return []

    def get_viewport(self) -> Optional[Dict[str, int]]:
        """Return viewport dict if use_viewport is enabled in config."""
        if not self.config.get("use_viewport"):
            return None
        width = self.config.get("width")
        height = self.config.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return {"width": width, "height": height}
        return None

    def should_enforce_os_window(self) -> bool:
        """ТЗ 7.6: enforce_os_window + валидные размеры (headless — нет).

        Ограничено режимом "apply": геометрия из конфига принудительно
        применяется к окну ОС. В режиме "track" (по умолчанию) менеджер
        только отслеживает окно и ЗАПИСЫВАЕТ его геометрию в конфиг,
        ничего не двигая.
        """
        if str(self.config.get("geometry_mode", "track")).lower() != "apply":
            return False
        if not self.config.get("enforce_os_window"):
            return False
        width = self.config.get("width")
        height = self.config.get("height")
        return (isinstance(width, int) and isinstance(height, int)
                and width > 0 and height > 0)

    def should_track_geometry(self) -> bool:
        """Режим "track": watchdog отслеживает окно и пишет геометрию в конфиг."""
        return str(self.config.get("geometry_mode", "track")).lower() == "track"

    def update_geometry(self, geo: Dict[str, Any]) -> bool:
        """Сохранить фактическую геометрию окна в конфиг (и на диск).

        Принимает словарь с ключами outerWidth/outerHeight/screenX/screenY
        (значения — CSS px из window.* браузера). Возвращает True, если
        конфиг изменился и был сохранён.
        """
        try:
            width = int(geo.get("outerWidth") or 0)
            height = int(geo.get("outerHeight") or 0)
            x = int(geo.get("screenX") or 0)
            y = int(geo.get("screenY") or 0)
        except (TypeError, ValueError):
            return False
        if width <= 0 or height <= 0:
            return False
        cfg = self.config
        changed = (
            cfg.get("width") != width
            or cfg.get("height") != height
            or cfg.get("x") != x
            or cfg.get("y") != y
        )
        if not changed:
            return False
        cfg["width"] = width
        cfg["height"] = height
        cfg["x"] = x
        cfg["y"] = y
        cfg["last_saved"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.save()
        logger.info(
            "WindowManager: геометрия окна сохранена в конфиг: "
            "%dx%d @ (%d,%d)", width, height, x, y,
        )
        return True