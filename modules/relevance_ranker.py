"""Ранжирование кандидатов ссылок/кнопок по релевантности запросу (Этап 6 ТЗ).

Модуль оборачивает ``SemanticNormalizer`` и возвращает для ``browser_find_link``
список ``LinkCandidate`` с полями ``candidate_id / score / video_id``, а также
признак ``confident`` — если лучший кандидат ниже ``relevance_threshold``,
сервер обязан ответить ``NO_CONFIDENT_MATCH_ON_CURRENT_PAGE`` вместо слепого
клика (RC5).

Сигналы скоринга (взвешенная сумма, нормирована к [0..1]):
- overlap лемм запроса и текста элемента          — вес 0.55
- точное/подстрочное совпадение (casefold)       — бонус до 0.25
- совпадение по href (id видео / ключ запроса)   — 0.10
- тип элемента (a/button > прочие)               — 0.05
- видимость на странице                          — 0.05
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote

from modules.text_utils import SemanticNormalizer

DEFAULT_THRESHOLD = 0.55
VIDEO_ID_RE = re.compile(
    r"(?:v=|vi=/|embed/|shorts/|live/)([A-Za-z0-9_-]{6,20})"
)


def extract_video_id(url: str) -> str:
    """Достаёт YouTube video_id из href (или '' если не найден)."""
    if not url:
        return ""
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else ""


@dataclass
class LinkCandidate:
    candidate_id: str
    text: str
    href: str = ""
    video_id: str = ""
    element_type: str = ""
    visible: bool = True
    source: str = "live_dom"
    position: int = 0
    score: float = 0.0
    matched_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "text": self.text,
            "href": self.href,
            "video_id": self.video_id,
            "element_type": self.element_type,
            "visible": self.visible,
            "source": self.source,
            "position": self.position,
            "score": round(self.score, 4),
            "matched_terms": self.matched_terms,
        }


class RelevanceRanker:
    def __init__(self, normalizer: Optional[SemanticNormalizer] = None,
                 threshold: float = DEFAULT_THRESHOLD) -> None:
        self.norm = normalizer or SemanticNormalizer()
        self.threshold = float(threshold)

    # ------------------------------------------------------------------
    def _candidate_id(self, item: dict[str, Any], idx: int) -> str:
        base = "|".join((str(item.get("text", ""))[:80],
                         str(item.get("href", ""))[:120]))
        digest = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:8]
        return f"c{idx:03d}-{digest}"

    def rank(self, query: str, items: list[dict[str, Any]],
             source: str = "live_dom") -> list[LinkCandidate]:
        q_terms = self.norm.normalize(query)
        q_clean = (query or "").casefold().replace("ё", "е").strip()
        out: list[LinkCandidate] = []
        for idx, item in enumerate(items):
            text = str(item.get("text", "") or "")
            href = str(item.get("href", "") or "")
            c = LinkCandidate(
                candidate_id=self._candidate_id(item, idx),
                text=text, href=href,
                video_id=extract_video_id(unquote(href)),
                element_type=str(item.get("type")
                                 or item.get("element_type") or ""),
                visible=bool(item.get("visible", True)),
                source=source, position=int(item.get("position", idx)),
            )
            if not q_clean:
                c.score = 0.0 if not c.visible else 0.1
                out.append(c)
                continue

            t_terms = self.norm.normalize(text)
            overlap = set(q_terms) & set(t_terms)
            score = 0.0
            if q_terms:
                score += 0.55 * (len(overlap) / len(q_terms))
            if text and q_clean in text.casefold().replace("ё", "е"):
                score += 0.25
            if href and q_clean in unquote(href).casefold():
                score += 0.10
            if c.element_type in ("a", "button"):
                score += 0.05
            if c.visible:
                score += 0.05
            c.score = min(score, 1.0)
            c.matched_terms = sorted(overlap)
            out.append(c)
        out.sort(key=lambda x: (-x.score, x.position))
        return out

    def confident(self, candidates: list[LinkCandidate]) -> bool:
        return bool(candidates) and candidates[0].score >= self.threshold

    def rank_and_wrap(self, query: str, items: list[dict[str, Any]],
                      source: str = "live_dom") -> dict[str, Any]:
        """Готовый ответ для browser_find_link (ТЗ §Э6)."""
        cands = self.rank(query, items, source=source)
        top = [c.to_dict() for c in cands[:10]]
        confident = self.confident(cands)
        resp: dict[str, Any] = {
            "ok": True,
            "query": query,
            "count": len(cands),
            "candidates": top,
            "relevance_threshold": self.threshold,
            "best_score": round(cands[0].score, 4) if cands else 0.0,
            "confident": confident,
        }
        if not confident:
            resp["error_code"] = "NO_CONFIDENT_MATCH_ON_CURRENT_PAGE"
            resp["message"] = (
                "Ни один элемент текущей страницы не соответствует запросу "
                "уверенно (лучший score=%.2f < %.2f). Не кликай вслепую: "
                "раскрой меню (browser_expand_menu), уточни формулировку или "
                "используй нативные команды сайта."
                % (cands[0].score if cands else 0.0, self.threshold))
            resp["suggested_safe_actions"] = [
                {"tool": "browser_expand_menu"},
                {"tool": "browser_get_content"},
                {"tool": "browser_screenshot"},
            ]
        return resp
