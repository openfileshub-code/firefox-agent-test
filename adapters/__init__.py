"""Система адаптеров видеоплееров (ТЗ раздел 2).

Единая точка входа: ``get_adapter`` (реестр) и базовые классы.
Все селекторы берутся из ``config/player_selectors.json`` — в Python-коде
нет жёстко зашитых CSS-классов (ТЗ, DoD).
"""

from adapters.base import (
    AdapterError,
    BaseVideoAdapter,
    ElementNotFoundError,
    load_selector_config,
)
from adapters.registry import REGISTRY, get_adapter, get_adapter_class
from adapters.youtube_adapter import YoutubeAdapter
from adapters.twitch_adapter import TwitchAdapter
from adapters.vk_adapter import VkAdapter
from adapters.rutube_adapter import RutubeAdapter
from adapters.generic_html5 import GenericHTML5Adapter

__all__ = [
    "BaseVideoAdapter",
    "ElementNotFoundError",
    "AdapterError",
    "load_selector_config",
    "get_adapter",
    "get_adapter_class",
    "REGISTRY",
    "YoutubeAdapter",
    "TwitchAdapter",
    "VkAdapter",
    "RutubeAdapter",
    "GenericHTML5Adapter",
]
