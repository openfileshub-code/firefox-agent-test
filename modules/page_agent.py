"""Исследование страницы, текстовый поиск и безопасные действия с элементами."""

from __future__ import annotations

from typing import Any


INTERACTIVE_JS = r"""
() => {
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
  };
  const label = el => (
    el.getAttribute('aria-label') ||
    el.getAttribute('title') ||
    (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()
  ).slice(0, 200);

  const selector = [
    'a[href]', 'button', 'input', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="tab"]',
    '[role="menuitem"]', '[onclick]'
  ].join(',');

  return [...document.querySelectorAll(selector)]
    .filter(visible)
    .slice(0, 600)
    .map(el => ({
      name: label(el),
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      href: el.href || '',
      disabled: !!el.disabled ||
        el.getAttribute('aria-disabled') === 'true',
      expanded: el.getAttribute('aria-expanded'),
      selected: el.getAttribute('aria-selected')
    }));
}
"""


class PageAgent:
    async def inspect(self, page: Any, include_frames: bool = True) -> dict:
        frames = page.frames if include_frames else [page.main_frame]
        result = []

        for index, frame in enumerate(frames[:30]):
            try:
                elements = await frame.evaluate(INTERACTIVE_JS)
                result.append({
                    "frame_index": index,
                    "url": frame.url,
                    "elements": elements,
                })
            except Exception as exc:
                result.append({
                    "frame_index": index,
                    "url": getattr(frame, "url", ""),
                    "error": str(exc)[:300],
                    "elements": [],
                })

        try:
            locale = await page.evaluate(
                "() => ({language:navigator.language,languages:navigator.languages})"
            )
        except Exception:
            locale = {}

        return {
            "url": page.url,
            "title": await page.title(),
            "locale": locale,
            "frames": result,
        }

    async def search_text(
        self,
        page: Any,
        query: str,
        include_frames: bool = True,
        limit: int = 30,
    ) -> list[dict]:
        if not query.strip():
            return []

        frames = page.frames if include_frames else [page.main_frame]
        results = []
        needle = query.casefold().replace("ё", "е")

        for index, frame in enumerate(frames[:30]):
            try:
                text = await frame.locator("body").inner_text(timeout=3000)
            except Exception:
                continue

            normalized = text.casefold().replace("ё", "е")
            pos = 0
            while len(results) < limit:
                found = normalized.find(needle, pos)
                if found < 0:
                    break
                results.append({
                    "frame_index": index,
                    "frame_url": frame.url,
                    "context": text[max(0, found - 120):found + len(query) + 180],
                })
                pos = found + max(1, len(needle))

        return results

    async def scroll(self, page: Any, direction: str, amount: int) -> dict:
        if direction not in ("up", "down", "left", "right"):
            raise ValueError("Направление должно быть up/down/left/right")

        amount = max(1, min(abs(int(amount)), 100_000))
        sign = -1 if direction in ("up", "left") else 1
        dx = sign * amount if direction in ("left", "right") else 0
        dy = sign * amount if direction in ("up", "down") else 0

        await page.evaluate(
            "([x,y]) => window.scrollBy({left:x,top:y,behavior:'smooth'})",
            [dx, dy],
        )
        return {"direction": direction, "amount": amount}

    async def scroll_to_text(self, page: Any, text: str) -> dict:
        if not text.strip():
            raise ValueError("Не задан текст для поиска")

        locator = page.get_by_text(text, exact=False).first
        if await locator.count() == 0:
            raise LookupError("Текст не найден в основном документе")

        await locator.scroll_into_view_if_needed(timeout=8000)
        return {"text": text, "visible": await locator.is_visible()}

    async def click_text(
        self,
        page: Any,
        text: str,
        index: int = 0,
        timeout_ms: int = 8000,
    ) -> dict:
        if not text.strip():
            raise ValueError("Не задан текст")

        locator = page.get_by_text(text, exact=False)
        count = await locator.count()
        if count == 0:
            raise LookupError("Элемент с таким текстом не найден")
        if index < 0 or index >= count:
            raise IndexError("Индекс вне диапазона; найдено: {}".format(count))

        target = locator.nth(index)
        if not await target.is_visible():
            raise RuntimeError("Найденный элемент скрыт")
        if not await target.is_enabled():
            raise RuntimeError("Найденный элемент отключён")

        await target.scroll_into_view_if_needed(timeout=timeout_ms)
        await target.click(timeout=timeout_ms)
        return {"clicked": text, "index": index}