"""Реестр адаптеров видеоплееров (ТЗ 1.3).

``get_adapter_class(page_url, page_dom_sample) -> Type[BaseVideoAdapter]``:

1. Парсит домен (youtube.com, twitch.tv, vk.com/video, rutube.ru).
2. Проверяет наличие характерных DOM-узлов (``#movie_player`` для YouTube,
   ``[data-a-target*='player']`` для Twitch и т.д.) — маркеры берутся из
   config/player_selectors.json, а не зашиты в код.
3. Возвращает класс адаптера; если ничего не найдено — GenericHTML5Adapter.

Каждый выбор сопровождается логом с указанием причины (primary по домену /
primary по DOM-маркеру / fallback), как требует DoD ТЗ.

``page_dom_sample`` — необязательный dict вида {"selectors_present": [...]}
или set/list присутствующих селекторов/подстрок HTML; может быть None
(тогда маршрутизация только по URL).
"""

from __future__ import annotations

import logging
from typing import Any, Type
from urllib.parse import urlparse

from adapters.base import BaseVideoAdapter, load_selector_config
from adapters.generic_html5 import GenericHTML5Adapter
from adapters.rutube_adapter import RutubeAdapter
from adapters.twitch_adapter import TwitchAdapter
from adapters.vk_adapter import VkAdapter
from adapters.youtube_adapter import YoutubeAdapter

logger = logging.getLogger("FirefoxMCP_Adapters.Registry")

#: Порядок важен при равных совпадениях; generic — всегда последний.
REGISTRY: list[Type[BaseVideoAdapter]] = [
    YoutubeAdapter,
    TwitchAdapter,
    VkAdapter,
    RutubeAdapter,
    GenericHTML5Adapter,
]


def _dom_markers_for(cls: Type[BaseVideoAdapter]) -> list[str]:
    """DOM-маркеры платформы из конфига (не из кода — DoD ТЗ)."""
    cfg = load_selector_config().get(cls.platform, {})
    markers = cfg.get("dom_marker") or cfg.get("dom_markers") or []
    if isinstance(markers, str):
        markers = [markers]
    return list(markers)


def _marker_present(sample_text: str, marker: str) -> bool:
    """Проверяет присутствие CSS-маркера в HTML-тексте без cssselect.

    Эвристика по атрибутам id/class: '#foo' ищет id="foo", '.bar' ищет
    владение классом bar; селекторы вида [attr='v'] ищутся как подстрока.
    """
    if not sample_text:
        return False
    m = marker.strip()
    if m.startswith("#"):
        mid = m[1:].split()[0] if " " not in m else m[1:]
        import re
        return bool(re.search(rf'id\s*=\s*["\']?{re.escape(mid)}["\']?\b', sample_text))
    if m.startswith(".") and "[" not in m:
        cls_name = m[1:].split()[0]
        import re
        return bool(re.search(
            rf'class\s*=\s*["\'][^"\']*\b{re.escape(cls_name)}\b[^"\']*["\']',
            sample_text))
    # Атрибутные/прочие селекторы — прямое вхождение подстроки или значения.
    if m in sample_text:
        return True
    # [data-a-target*='player'] -> ищем значение атрибута 'player'...
    import re
    attr_match = re.match(r"\[([\w-]+)([*^$]?=)['\"]?([^'\"\]]+)", m)
    if attr_match:
        attr, _, val = attr_match.groups()
        return bool(re.search(rf'{re.escape(attr)}\s*=\s*["\'][^"\']*{re.escape(val)}',
                              sample_text))
    return False


def _sample_contains(dom_sample: Any, marker: str) -> bool:
    """Проверяет присутствие маркера в образце DOM произвольной формы."""
    if dom_sample is None:
        return False
    if isinstance(dom_sample, dict):
        present = dom_sample.get("selectors_present") or dom_sample.get("present") or []
        if isinstance(present, (list, tuple, set)) and any(marker in p for p in present):
            return True
        html = dom_sample.get("html") or dom_sample.get("dom") or ""
        return _marker_present(html, marker)
    if isinstance(dom_sample, (list, tuple, set)):
        return any(marker in str(item) for item in dom_sample)
    if isinstance(dom_sample, str):
        return _marker_present(dom_sample, marker)
    return False


def get_adapter_class(
    page_url: str,
    page_dom_sample: Any = None,
) -> Type[BaseVideoAdapter]:
    """Возвращает класс адаптера для страницы. См. docstring модуля."""
    host = ""
    try:
        host = (urlparse(page_url or "").netloc or "").lower()
        # vk: различаем разделы видео от остального сайта
        path = (urlparse(page_url or "").path or "").lower()
    except Exception:  # noqa: BLE001
        path = ""

    for cls in REGISTRY:
        if cls is GenericHTML5Adapter:
            continue
        domain_hit = any(h in host for h in cls.domain_hints)
        # Специфика VK: матчимся только на видеоразделы либо при DOM-маркере.
        if cls is VkAdapter and domain_hit and "video" not in path and "/clips" not in path:
            if not any(_sample_contains(page_dom_sample, m)
                       for m in _dom_markers_for(cls)):
                logger.debug("VK: домен %s, но путь %s не видео-раздел", host, path)
                domain_hit = False

        marker_hit = any(_sample_contains(page_dom_sample, m)
                         for m in _dom_markers_for(cls))

        if domain_hit and marker_hit:
            logger.info("Адаптер выбран: %s (primary: домен %s + DOM-маркер)",
                        cls.__name__, host)
            return cls
        if domain_hit:
            logger.info("Адаптер выбран: %s (primary: домен %s)", cls.__name__, host)
            return cls
        if marker_hit:
            logger.info("Адаптер выбран: %s (primary: DOM-маркер без известного домена %s)",
                        cls.__name__, host or "?")
            return cls

    logger.info("Адаптер выбран: GenericHTML5Adapter (fallback: платформа не распознана "
                "по URL '%s' и DOM-образцу)", page_url)
    return GenericHTML5Adapter


def get_adapter(
    page: Any,
    page_url: str | None = None,
    page_dom_sample: Any = None,
    video_controller: Any = None,
) -> BaseVideoAdapter:
    """Создаёт экземпляр адаптера для страницы Playwright.

    Если ``page_url`` не передан — берётся ``page.url``.
    """
    url = page_url or getattr(page, "url", "") or ""
    cls = get_adapter_class(url, page_dom_sample)
    return cls(page, video_controller=video_controller)
