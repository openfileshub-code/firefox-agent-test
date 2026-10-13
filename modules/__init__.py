"""Модули автоматизации браузера."""

from modules.forms import Forms
from modules.link_cache import LinkCache
from modules.page_agent import PageAgent
from modules.video_controller import VideoController
from modules.websocket_monitor import WebSocketMonitor
from modules.window_manager import WindowManager

__all__ = [
    "Forms",
    "LinkCache",
    "PageAgent",
    "VideoController",
    "WebSocketMonitor",
    "WindowManager",
]