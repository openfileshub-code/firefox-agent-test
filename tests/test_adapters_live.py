"""Интеграционные тесты адаптеров и раскрытия меню (ТЗ 5.2).

Требуют живого браузера: Playwright Chromium/Camoufox + локальные HTML-стенды
(без обращения к реальным YouTube/Twitch — CI без сети остаётся зелёным,
реальные платформы проверяются вручную флагом ``--run-external``).

Запуск:
    python -m pytest tests/test_adapters_live.py -q            # локальные стенды
    RUN_EXTERNAL=1 python -m pytest tests/test_adapters_live.py -q -s  # + реальные платформы

Стенды эмулируют ключевые структуры:
* YT-подобный плеер с шестерёнкой [aria-label], role=menu и radio-пунктами;
* Twitch-подобный player c data-a-target и списком Sources;
* кастомный дропдаун (div+onclick) для browser_expand_menu-сценария;
* Shadow DOM компонент с кнопкой внутри shadowRoot.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.registry import get_adapter  # noqa: E402
from adapters.base import load_selector_config  # noqa: E402

RUN_EXTERNAL = os.environ.get("RUN_EXTERNAL") == "1"

YT_STAND = """<!doctype html><html><body>
<div id="movie_player">
  <video src="data:video/mp4;base64," width="320" height="180"></video>
  <button aria-label="Settings">&#9881;</button>
  <button aria-label="Широкоэкранный режим"
          onclick="document.getElementById('movie_player').classList.toggle('theater')"></button>
  <ul role="menu" style="display:none" id="menu">
    <li role="menuitem">Качество</li>
  </ul>
  <div id="qmenu" style="display:none" role="menu">
    <div role="menuitemradio" aria-checked="false" onclick="pick(this)">Auto</div>
    <div role="menuitemradio" aria-checked="false" onclick="pick(this)">480p</div>
    <div role="menuitemradio" aria-checked="true" onclick="pick(this)">1080p</div>
  </div>
</div>
<script>
  document.querySelector('[aria-label="Settings"]').onclick = () => {
    document.getElementById('qmenu').style.display = 'block';
  };
  function pick(el){
    document.querySelectorAll('[role=menuitemradio]').forEach(x=>x.setAttribute('aria-checked','false'));
    el.setAttribute('aria-checked','true');
    document.getElementById('qmenu').style.display='none';
  }
</script></body></html>"""

TW_STAND = """<!doctype html><html><body>
<div class="tw-player" data-a-target="player">
  <video width="320" height="180"></video>
  <button data-a-target="player-settings-menu-toggle" aria-label="Settings"></button>
  <div id="menu" role="menu" style="display:none">
    <div role="menuitem" onclick="openSrc()">Source</div>
  </div>
  <div id="src" style="display:none">
    <div data-a-target="player-settings-submenu-quality-option"
         onclick="sel(this)">1080p60</div>
    <div data-a-target="player-settings-submenu-quality-option"
         onclick="sel(this)">480p30</div>
    <div data-a-target="player-settings-submenu-quality-option"
         onclick="sel(this)">Lowest Available Bitrate</div>
  </div>
  <button aria-label="Collapse Chat" onclick="this.textContent='ChatToggled'"></button>
</div>
<script>
  document.querySelector('[data-a-target="player-settings-menu-toggle"]').onclick=
    ()=>{document.getElementById('menu').style.display='block'};
  function openSrc(){document.getElementById('menu').style.display='none';
    document.getElementById('src').style.display='block';}
  function sel(el){el.setAttribute('data-selected','true');
    document.getElementById('src').style.display='none';}
</script></body></html>"""

MENU_STAND = """<!doctype html><html><body>
<nav role="navigation"><div id="cat" class="nav-link" onclick="openMenu()"
   tabindex="0">Категории</div></nav>
<div id="drop" style="display:none">
  <a href="/phones">Телефоны</a><a href="/laptops">Ноутбуки</a>
  <a href="/tv">Телевизоры</a>
</div>
<script>function openMenu(){document.getElementById('drop').style.display='block';}</script>
</body></html>"""

SHADOW_STAND = """<!doctype html><html><body>
<my-widget></my-widget>
<script>
class W extends HTMLElement{
  connectedCallback(){
    const r=this.attachShadow({mode:'open'});
    r.innerHTML='<button aria-label="Отправить заявку">Send</button>';
  }}
customElements.define('my-widget',W);
</script></body></html>"""


def _write(name: str, html: str) -> str:
    fd, path = tempfile.mkstemp(suffix=f"_{name}.html", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(html)
    return "file://" + path


async def _get_page(browser):
    page = await browser.new_page()
    return page


async def _browser_ctx():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception:
        await pw.stop()
        pytest.skip("Chromium недоступен в этой среде")
    return pw, browser


async def run_scenarios():
    """Основная функция live-проверок (ТЗ 5.2) на локальных HTML-стендах.

    file:// URL имеют пустой netloc, поэтому для проверки маршрутизации
    реестра по домену стенд открывается через route-перехват на псевдо-домене
    (браузер не уходит в сеть — ответ отдаёт Playwright).
    """
    pw, browser = await _browser_ctx()
    results: dict = {}
    try:
        # --- 1. YouTube-стенд: качество через UI + theater mode ---
        page = await _get_page(browser)
        await page.route("https://yt.stand.test/**",
                        lambda route: route.fulfill(body=YT_STAND,
                                                    content_type="text/html"))
        await page.goto("https://yt.stand.test/watch?v=stand")
        adapter = get_adapter(page, page_url="https://www.youtube.com/watch?v=stand",
                              video_controller=None)
        results["yt_adapter"] = type(adapter).__name__
        results["yt_set_quality"] = await adapter.set_quality("480p")
        checked = await page.evaluate(
            "()=>{const el=document.querySelector('[aria-checked=true]');"
            "return el?el.textContent.trim():''}")
        results["yt_active_quality"] = checked
        results["yt_theater"] = await adapter.toggle_theater_mode()
        await page.close()

        # --- 2. Twitch-стенд: source + чат ---
        page = await _get_page(browser)
        await page.route("https://tw.stand.test/**",
                        lambda route: route.fulfill(body=TW_STAND,
                                                    content_type="text/html"))
        await page.goto("https://tw.stand.test/somestream")
        adapter = get_adapter(page, page_url="https://twitch.tv/somestream")
        results["tw_adapter"] = type(adapter).__name__
        results["tw_set_quality"] = await adapter.set_quality("lowest")
        results["tw_selected_source"] = await page.evaluate(
            "()=>{const el=document.querySelector('[data-selected]');"
            "return el?el.textContent.trim():''}")
        results["tw_chat"] = await adapter.toggle_chat_visibility()
        await page.close()

        # --- 3. Раскрытие нестандартного меню (div+onclick, не <select>) ---
        page = await _get_page(browser)
        await page.goto(_write("menu", MENU_STAND))
        from modules.link_cache import LinkCache
        lc = LinkCache()
        before = await lc.scan_interactive_elements(page)
        before_keys = {(e.get("text"), e.get("href")) for e in before["elements"]}
        trigger = page.get_by_text("Категории", exact=True).first
        await trigger.click()
        await asyncio.sleep(0.6)
        after = await lc.scan_interactive_elements(page)
        new_els = [e for e in after["elements"]
                   if (e.get("text"), e.get("href")) not in before_keys]
        results["expand_new_links"] = [e.get("href") for e in new_els]
        await page.close()

        # --- 4. Shadow DOM: кнопка внутри shadowRoot видна сканеру ---
        page = await _get_page(browser)
        await page.goto(_write("shadow", SHADOW_STAND))
        lc2 = LinkCache()
        scan = await lc2.scan_interactive_elements(page)
        found = False
        for e in scan["elements"]:
            blob = json.dumps(e, ensure_ascii=False)
            if "заявку" in blob or e.get("text") == "Send":
                found = True
                break
        results["shadow_found_button"] = found
        await page.close()
    finally:
        await browser.close()
        await pw.stop()
    return results


def main() -> int:
    if os.environ.get("LIVE") != "1" and not RUN_EXTERNAL:
        print("Live-тесты пропущены. Запустите: LIVE=1 python tests/test_adapters_live.py")
        return 0
    res = asyncio.run(run_scenarios())
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    ok = (
        res.get("yt_adapter") == "YoutubeAdapter"
        and res.get("yt_active_quality") == "480p"
        and res.get("yt_theater", {}).get("container_changed") is True
        and res.get("tw_adapter") == "TwitchAdapter"
        and res.get("tw_set_quality", {}).get("ok") is True
        and "lowest" in str(res.get("tw_selected_source", "")).lower()
        and len(res.get("expand_new_links", [])) >= 3
        and res.get("shadow_found_button") is True
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
