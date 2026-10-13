"""Инвентаризация и приблизительный поиск видимых элементов страницы."""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

logger = logging.getLogger("FirefoxMCP_LinkCache")

INVENTORY_JS = r"""
() => {
  const viewportBuffer = window.innerHeight * 2;
  const visible = el => {
    if (!el || !el.isConnected) return false;
    let node = el, inShadow = false;
    // Элементы внутри Shadow DOM не имеют offsetParent даже когда видны.
    for (let root = el.getRootNode && el.getRootNode();
         root instanceof ShadowRoot; ) { inShadow = true; break; }
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' ||
        Number(s.opacity) <= 0.1) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    // Фильтр по viewport + буфер 2 экрана (экономия памяти на длинных страницах).
    if (r.bottom < -viewportBuffer || r.top > window.innerHeight + viewportBuffer)
      return false;
    void inShadow;
    return true;
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
    // Поднимаемся по дереву (в т.ч. через shadow-границы) до меню-контейнера.
    let cur = el;
    for (let depth = 0; cur && depth < 25; depth++) {
      if (cur.nodeType === 1) {
        const matchesMenuish = (node) =>
          node.matches && node.matches(
            'nav,[role="navigation"],[role="menubar"],[role="menu"],' +
            '[class*="menu" i],[id*="nav" i],[class*="nav" i]'
          );
        if (matchesMenuish(cur)) {
          const cls = typeof cur.className === 'string'
            ? cur.className.trim().split(/\s+/).slice(0, 2).join('.')
            : '';
          return (cur.getAttribute('aria-label') || cur.getAttribute('role') ||
            cur.tagName.toLowerCase() + (cls ? '.' + cls : '')).slice(0, 160);
        }
      }
      cur = cur.parentElement ||
        (cur.getRootNode && cur.getRootNode() instanceof ShadowRoot
          ? cur.getRootNode().host : null);
    }
    return '';
  };

  const INTERACTIVE_ROLES = ['button','link','tab','menuitem','option',
    'checkbox','radio','switch','combobox','textbox','searchbox'];
  const CLASS_MARKERS = /(btn|link|menu-?item|nav-?link|clickable)/i;

  const isInteractive = el => {
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (tag === 'a' && el.hasAttribute('href')) return true;
    if (['button','input','select','textarea','summary'].includes(tag)) return true;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (INTERACTIVE_ROLES.includes(role)) return true;
    if (el.hasAttribute('onclick') || el.hasAttribute('onmouseover')) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && Number(ti) >= 0) return true;
    if (typeof el.className === 'string' && CLASS_MARKERS.test(el.className))
      return true;
    return false;
  };

  const out = [];
  const seen = new Set();
  const MAX_SHADOW_DEPTH = 8;
  const MAX_ELEMENTS = 1200;

  const walkDom = (root, shadowDepth, hostPath) => {
    if (out.length >= MAX_ELEMENTS || shadowDepth > MAX_SHADOW_DEPTH) return;
    const walker = document.createTreeWalker(
      root, NodeFilter.SHOW_ELEMENT, null, false
    );
    let el = walker.currentNode;
    while ((el = walker.nextNode())) {
      if (out.length >= MAX_ELEMENTS) break;
      if (!seen.has(el) && isInteractive(el)) {
        seen.add(el);
        if (visible(el)) {
          const text = nameOf(el);
          if (text || el.matches('input,textarea,select')) {
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
              id: el.id || '',
              in_shadow: shadowDepth > 0,
              shadow_host: hostPath || ''
            });
          }
        }
      }
      // Рекурсия внутрь открытых Shadow Roots.
      if (el.shadowRoot) {
        walkDom(el.shadowRoot, shadowDepth + 1, pathOf(el));
      }
    }
  };

  walkDom(document, 0, '');

  return out.slice(0, MAX_ELEMENTS);
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

    async def scan_interactive_elements(
        self, page: Any, zone_selector: str = ""
    ) -> dict[str, Any]:
        """Универсальный сканер интерактивности (Deep Traversal + Shadow DOM).

        zone_selector — необязательный CSS-селектор корня зоны (например,
        контейнер меню); если пустой, сканируется вся страница. Возвращает
        {'elements': [...], 'partial_scan': bool}. При таймауте/ошибке —
        partial_scan=True с текущим состоянием (согласно ТЗ 5.3).
        """
        js = INVENTORY_JS
        if zone_selector:
            # Оборачиваем: стартуем walkDom не от document, а от узла зоны.
            js = INVENTORY_JS.replace(
                "walkDom(document, 0, '');",
                "const zone = document.querySelector(%r) || document;"
                "walkDom(zone, 0, '');" % zone_selector,
            )
        try:
            elements = await page.evaluate(js)
            return {"elements": elements, "partial_scan": False}
        except Exception as exc:
            logger.warning(
                "scan_interactive_elements: таймаут/ошибка (%s), "
                "возвращаю частичный снимок", exc,
            )
            return {
                "elements": list(self.links),
                "partial_scan": True,
                "error": str(exc),
            }

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
        """Ранжирование через семантическую нормализацию (леммы + синонимы).

        Совпадение считается по пересечению множеств лемм; порядок слов
        учитывается через bigram-бонус. При недоступности SemanticNormalizer
        используется прежняя строковая эвристика (graceful fallback).
        """
        q = cls.normalize(query)
        t = cls.normalize(target)
        if not q or not t:
            return 9, 0.0

        normalizer = None
        try:
            from modules.text_utils import get_normalizer

            normalizer = get_normalizer()
        except Exception:
            logger.warning(
                "SemanticNormalizer недоступен; поиск деградирует до строк"
            )

        if normalizer is not None:
            q_terms = normalizer.normalize(query)
            # Леммы цели считаем от текста (без "шума" из URL-а), но к строке
            # href тоже сравниваем — это повышает recall для технических ссылок.
            text_part = str(target).split(" http")[0] if " http" in target else target
            t_terms = normalizer.normalize(text_part)
            full_terms = normalizer.normalize(target)

            # Строковые быстрые пути сохраняем для точных/подстрочных совпадений.
            # Важно: подстрочные проверки выполняются ТОЛЬКО против текста
            # элемента, чтобы URL-подстроки (например /about в href) не
            # порождали ложных срабатываний bucket 1 для других элементов.
            text_norm = cls.normalize(text_part)
            # Точное совпадение только если target НЕ похож на URL-путь —
            # иначе href "/about" будет ранжироваться так же, как текст "About".
            # Проверка выполняется над СЫРЫМ target: cls.normalize() стирает
            # слеши и другие разделители URL, уничтожая сам признак пути.
            raw_target = (target or "").strip().lower()
            # Признак "это URL, а не текст элемента": абсолютный URL,
            # якорь или путь с несколькими сегментами (/about/settings).
            # Односегментные пути ("/about") нормализуются как обычное
            # слово и ранжируются по общим правилам.
            looks_like_url = raw_target.startswith(("http", "//", "#")) or \
                raw_target.count("/") >= 2
            if looks_like_url:
                # Для URL-подобного target сопоставляем леммы запроса с
                # леммами пути (слова из path/query вместо разделителей).
                # Сигнал слабее текстового на один уровень (min bucket 1):
                # точное текстовое совпадение (bucket 0) всегда выигрывает.
                url_terms = normalizer.normalize(target)
                score = max(
                    normalizer.similarity(q_terms, t_terms),
                    normalizer.similarity(q_terms, url_terms),
                )
                if q in text_norm or score >= 0.99:
                    return 1, score
                if score >= 0.48:
                    return 3, score
                return 9, score
            if q == text_norm or q == t:
                return 0, 1.0
            if q in text_norm:
                return 1, len(q) / max(1, len(text_norm))

            score = max(
                normalizer.similarity(q_terms, t_terms),
                normalizer.similarity(q_terms, full_terms),
            )
            if score >= 0.99:
                return 2, score
            if score >= 0.48:
                return 3, score
            # Fallback на raw-string similarity только против ТЕКСТА элемента,
            # чтобы URL-подстроки не порождали ложных срабатываний.
            raw_sim = difflib.SequenceMatcher(None, q, t).ratio()
            text_only = cls.normalize(text_part)
            if text_only and text_only in t:
                raw_sim = max(raw_sim, difflib.SequenceMatcher(None, q, text_only).ratio())
            else:
                raw_sim = difflib.SequenceMatcher(None, q, text_only or t).ratio()
            if raw_sim >= 0.75:
                return 3, raw_sim
            return 9, max(score, raw_sim)

        # --- Legacy path (без pymorphy3 / словаря синонимов) ---
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
            # Текст и href ранжируем раздельно: URL не должен "загрязнять"
            # леммы текста (и наоборот), но учитываем оба сигнала.
            # При равных bucket/score текстовое совпадение приоритетнее
            # URL-совпадения (href даёт +0.5 к сортировочному ключу bucket).
            text = str(item.get("text", "") or "")
            href = str(item.get("href", "") or "")
            bucket, score = self._rank(query, text)
            sort_bucket = float(bucket)
            if href:
                alt_bucket, alt_score = self._rank(query, href)
                if alt_bucket < bucket:
                    bucket, score = alt_bucket, alt_score
                    sort_bucket = alt_bucket + 0.5
            if bucket < 9:
                results.append((sort_bucket, -score, item))

        results.sort(key=lambda row: (row[0], row[1]))
        return [
            {**item, "match_score": round(-negative_score, 3)}
            for _bucket, negative_score, item in results[:30]
        ]