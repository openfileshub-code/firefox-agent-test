"""Семантическая нормализация текста: лемматизация + синонимы + сокращения.

Модуль используется LinkCache для fuzzy-поиска элементов страницы, чтобы
"Войти" находило кнопку "Sign In", а "Контакты" — пункт "Связаться с нами".

Используется pymorphy3 (MIT) для быстрой лемматизации; при недоступности
библиотеки выполняется graceful degradation до базовой нормализации строк.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("FirefoxMCP_TextUtils")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SYNONYMS_PATH = BASE_DIR / "config" / "synonyms.json"

# Токенизация: слова (в т.ч. с дефисом/апострофом) и отдельные числа.
TOKEN_RE = re.compile(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*|\d+[pP]?[sS]?", re.UNICODE)


def _basic_clean(text: str) -> str:
    """Нижний регистр, ё->е, unicode-NFKC, удаление пунктуации."""
    text = (text or "").strip().casefold().replace("ё", "е")
    text = unicodedata.normalize("NFKC", text)
    return text


class SemanticNormalizer:
    """Нормализует текст в список лемм / канонических форм."""

    def __init__(
        self,
        synonyms_path: Optional[Path] = None,
        use_morphology: bool = True,
    ) -> None:
        self._morph: Any = None
        if use_morphology:
            try:
                import pymorphy3  # MIT license

                self._morph = pymorphy3.MorphAnalyzer()
            except Exception as exc:  # библиотека не установлена — деградация
                logger.warning(
                    "pymorphy3 недоступен (%s); используется упрощенная "
                    "лемматизация без морфологии", exc,
                )

        self._groups: list[tuple[str, list[str]]] = []
        self._variant_map: dict[str, str] = {}
        self._abbreviations: dict[str, str] = {}
        self._load_synonyms(synonyms_path or DEFAULT_SYNONYMS_PATH)

    # ------------------------------------------------------------------ #
    # Конфигурация синонимов
    # ------------------------------------------------------------------ #
    def _load_synonyms(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("Файл синонимов %s не найден", path)
            return
        except Exception:
            logger.exception("Не удалось прочитать словарь синонимов %s", path)
            return

        for group in data.get("groups", []):
            canonical = _basic_clean(str(group.get("canonical", "")))
            variants = [_basic_clean(str(v)) for v in group.get("variants", [])]
            if not canonical:
                continue
            self._groups.append((canonical, variants))
            for variant in [canonical, *variants]:
                if variant:
                    self._variant_map[variant] = canonical

        for key, value in data.get("abbreviations", {}).items():
            self._abbreviations[_basic_clean(key)] = _basic_clean(value)

    # ------------------------------------------------------------------ #
    # Морфология
    # ------------------------------------------------------------------ #
    def _lemmatize(self, token: str) -> tuple[str, float]:
        """Лемма токена + вес (1.0 — уверенно, <1.0 — разбор не удался)."""
        if self._morph is None:
            return token, 1.0
        if token.isdigit() or re.fullmatch(r"\d+[pPsS]", token):
            return token.rstrip("pPsS"), 1.0
        try:
            parsed = self._morph.parse(token)
            if not parsed:
                # Энциклопедическая ошибка разбора — оставляем оригинал,
                # занижаем вес совпадения согласно ТЗ (5.3).
                return token, 0.6
            return parsed[0].normal_form, 1.0
        except Exception:
            return token, 0.6

    # ------------------------------------------------------------------ #
    # Публичный API
    # ------------------------------------------------------------------ #
    def normalize(self, text: str) -> list[str]:
        """Очистка → токенизация → сокращения → синонимы → леммы.

        Возвращает список канонических форм/лемм. Многословные варианты
        синонимов ("sign in", "личный кабинет") распознаются по всей строке
        до разбиения на одиночные токены.
        """
        cleaned = _basic_clean(text)
        if not cleaned:
            return []

        # 1. Многословные синонимы ищем по всей фразе (подстрокой).
        consumed_spans: list[tuple[int, int]] = []
        multi_tokens: list[str] = []
        for variant, canonical in sorted(
            self._variant_map.items(), key=lambda kv: -len(kv[0])
        ):
            if " " not in variant:
                continue
            start = 0
            while True:
                idx = cleaned.find(variant, start)
                if idx < 0:
                    break
                end = idx + len(variant)
                if any(not (end <= s or idx >= e) for s, e in consumed_spans):
                    start = idx + 1
                    continue
                consumed_spans.append((idx, end))
                multi_tokens.append(canonical)
                start = end

        # Вырезаем потребленные фрагменты для остаточной токенизации.
        remainder = cleaned
        for s, e in sorted(consumed_spans, reverse=True):
            remainder = remainder[:s] + " " + remainder[e:]

        result: list[str] = list(multi_tokens)
        weights: dict[str, float] = {}

        for token in TOKEN_RE.findall(remainder):
            token = token.strip("'-\u2019 ")
            if not token:
                continue

            # 2. Сокращения (точное совпадение токена).
            token = self._abbreviations.get(token, token)

            # 2b. Однословные синонимы группы -> каноническая форма.
            if token in self._variant_map:
                lemma, weight = self._variant_map[token], 1.0
            else:
                # 3. Лемматизация. Для многолексемных аббревиатур
                # ("личный кабинет") лемматизируем каждую лексему.
                parts = TOKEN_RE.findall(token) or [token]
                lemmas = []
                weight = 1.0
                for part in parts:
                    lem, w = self._lemmatize(part)
                    lemmas.append(lem)
                    weight = min(weight, w)
                    # Лемма тоже может попасть в словарь синонимов.
                    if lem in self._variant_map:
                        lemmas[-1] = self._variant_map[lem]
                lemma = " ".join(lemmas)

            if lemma:
                result.append(lemma)
                weights[lemma] = min(weights.get(lemma, 1.0), weight)

        # Дедупликация с сохранением порядка.
        seen: set[str] = set()
        unique = [t for t in result if not (t in seen or seen.add(t))]
        self._last_weights = weights
        return unique

    @property
    def last_weights(self) -> dict[str, float]:
        """Веса последней нормализации (для занижения неопределенных лемм)."""
        return getattr(self, "_last_weights", {})

    def similarity(
        self,
        query_terms: list[str],
        target_terms: list[str],
        use_bigrams: bool = True,
    ) -> float:
        """Оценка похожести двух списков лемм: overlap + бонус за порядок.

        bigram-бонус учитывает порядок слов: "купить товар" и "товар купить"
        получают меньший балл, чем полное совпадение последовательности.
        """
        if not query_terms or not target_terms:
            return 0.0

        q_set, t_set = set(query_terms), set(target_terms)
        weights = self.last_weights
        weighted_overlap = sum(
            weights.get(term, 1.0) for term in q_set & t_set
        )
        total_weight = sum(weights.get(term, 1.0) for term in q_set) or 1.0
        overlap = weighted_overlap / total_weight

        score = overlap
        if use_bigrams and len(query_terms) > 1:
            q_bigrams = {
                tuple(query_terms[i:i + 2]) for i in range(len(query_terms) - 1)
            }
            t_bigrams = {
                tuple(target_terms[i:i + 2]) for i in range(len(target_terms) - 1)
            }
            if q_bigrams:
                order_score = len(q_bigrams & t_bigrams) / len(q_bigrams)
                score = 0.7 * overlap + 0.3 * order_score

        # Частичное совпадение подстрок (например "видео" vs "видеоролик").
        if score < 0.5:
            partial = 0.0
            for qt in q_set:
                for tt in t_set:
                    if qt != tt and (qt in tt or tt in qt):
                        partial = max(partial, 0.4)
            score = max(score, partial)

        return round(min(1.0, score), 4)


_default_normalizer: Optional[SemanticNormalizer] = None


def get_normalizer() -> SemanticNormalizer:
    """Ленивый синглтон (загрузка словаря и модели pymorphy — одна на процесс)."""
    global _default_normalizer
    if _default_normalizer is None:
        _default_normalizer = SemanticNormalizer()
    return _default_normalizer
