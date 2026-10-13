"""Инвентаризация и приблизительный поиск видимых элементов страницы."""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

logger = logging.getLogger("FirefoxMCP_LinkCache")

INVENTORY_JS = r"""
() => {
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' ||
        Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const nameOf = el => (
    el.getAttribute('aria-label') ||
    el.getAttribute('title') ||
    (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() ||
    el.getAttribute('alt') ||
    el.getAttribute('placeholder') ||
    ''
  ).slice(0, 240);

  const pathOf = el => {
    const parts = [];
    let cur = el;
    for (let depth = 0; cur && cur.nodeType === 1 && depth < 8; depth++) {
      let part = cur.tagName.toLowerCase();
      if (cur.id) {
        part += '#' + CSS.escape(cur.id);
        parts.unshift(part);
        break;
      }
      const parent = cur.parentElement;
      if (parent) {
        const same = [...parent.children].filter(x => x.tagName === cur.tagName);
        if (same.length > 1)
          part += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  };

  const menuOf = el => {
    const m = el.closest(
      'nav,[role="navigation"],[role="menubar"],[role="menu"],' +
      'header,footer,aside,ul,ol,[class*="menu" i],[class*="nav" i]'
    );
    if (!m) return '';
    const cls = typeof m.className === 'string'
      ? m.className.trim().split(/\s+/).slice(0, 2).join('.')
      : '';
    return (m.getAttribute('aria-label') || m.getAttribute('role') ||
      m.tagName.toLowerCase() + (cls ? '.' + cls : '')).slice(0, 160);
  };

  const selectors = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="tab"]',
    '[role="menuitem"]', '[role="option"]',
    '[tabindex]:not([tabindex="-1"])', '[onclick]'
  ];

  const found = new Set();
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) found.add(el);
  }

  const out = [];
  for (const el of found) {
    if (!visible(el)) continue;

    const text = nameOf(el);
    if (!text && !el.matches('input,textarea,select')) continue;

    const role = el.getAttribute('role') || '';
    const type = el.matches('a[href]') ? 'link' :
      role === 'tab' ? 'tab' :
      role === 'menuitem' ? 'menuitem' :
      el.matches('button,[role="button"],[onclick]') ? 'button' :
      el.matches('input,textarea,select') ? 'form_control' : 'interactive';

    out.push({
      text,
      href: el.href || '',
      type,
      role,
      tag: el.tagName.toLowerCase(),
      selector: pathOf(el),
      menu: menuOf(el),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      expanded: el.getAttribute('aria-expanded'),
      selected: el.getAttribute('aria-selected'),
      id: el.id || ''
    });
  }

  return out.slice(0, 1200);
}
"""


class LinkCache:
    """Сбор и ранжирование найденных на странице интерактивных элементов."""

    def __init__(self) -> None:
        self.links: list[dict[str, Any]] = []
        self.current_url = ""
        self._dirty = True

    async def refresh(self, page: Any) -> None:
        try:
            self.links = await page.evaluate(INVENTORY_JS)
            self.current_url = page.url
            self._dirty = False
        except Exception:
            logger.exception("Не удалось собрать элементы страницы")
            self.links = []
            self._dirty = True

    async def ensure_fresh(self, page: Any) -> None:
        # Обновляем при каждом поиске, так как SPA может менять DOM без смены URL.
        await self.refresh(page)

    def mark_dirty(self) -> None:
        self._dirty = True

    @staticmethod
    def normalize(text: str) -> str:
        text = (text or "").casefold().replace("ё", "е")
        text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _rank(cls, query: str, target: str) -> tuple[int, float]:
        q = cls.normalize(query)
        t = cls.normalize(target)
        if not q or not t:
            return 9, 0.0
        if q == t:
            return 0, 1.0
        if q in t:
            return 1, len(q) / max(1, len(t))

        qw, tw = set(q.split()), set(t.split())
        if qw and qw.issubset(tw):
            return 2, len(qw) / max(1, len(tw))

        overlap = len(qw & tw) / max(1, len(qw))
        similarity = difflib.SequenceMatcher(None, q, t).ratio()
        score = max(overlap, similarity)
        if score >= 0.48:
            return 3, score
        return 9, score

    def search(self, query: str) -> list[dict[str, Any]]:
        if not query.strip():
            return list(self.links[:100])

        results = []
        for item in self.links:
            target = "{} {}".format(
                item.get("text", ""),
                item.get("href", ""),
            )
            bucket, score = self._rank(query, target)
            if bucket < 9:
                results.append((bucket, -score, item))

        results.sort(key=lambda row: (row[0], row[1]))
        return [
            {**item, "match_score": round(-negative_score, 3)}
            for _bucket, negative_score, item in results[:30]
        ]