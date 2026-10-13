"""Unit-тесты семантической нормализации и ранжирования LinkCache (ТЗ 5.1)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from modules.link_cache import LinkCache
from modules.text_utils import SemanticNormalizer


@pytest.fixture()
def normalizer():
    return SemanticNormalizer()


class TestSemanticNormalizer:
    def test_lemmatization(self, normalizer):
        terms = normalizer.normalize("Перейти на страницу")
        assert "перейти" in terms
        assert "страница" in terms

    def test_novosti_to_news_canonical(self, normalizer):
        assert normalizer.normalize("новости") == ["news"]

    def test_register_canonical(self, normalizer):
        terms = normalizer.normalize("Регистрация нового пользователя")
        assert "register" in terms
        assert "новый" in terms
        assert "пользователь" in terms

    def test_login_synonym_group(self, normalizer):
        assert normalizer.normalize("Войти") == ["login"]
        assert normalizer.normalize("Sign In") == ["login"]
        assert normalizer.similarity(["login"], ["login"]) == 1.0

    def test_contacts_synonym(self, normalizer):
        q = normalizer.normalize("Контакты")
        t = normalizer.normalize("Связаться с нами")
        assert q == t == ["contacts"]

    def test_abbreviation(self, normalizer):
        terms = normalizer.normalize("рег.")
        assert "register" in terms

    def test_empty_and_punctuation(self, normalizer):
        assert normalizer.normalize("") == []
        assert normalizer.normalize("!!! ???") == []

    def test_yo_replaced(self, normalizer):
        assert normalizer.normalize("Настройки") == ["settings"]

    def test_unknown_word_kept(self, normalizer):
        terms = normalizer.normalize("щзхъфыва")
        assert len(terms) >= 1

    def test_order_bigram_penalty(self, normalizer):
        a = normalizer.normalize("купить товар")
        b = normalizer.normalize("товар купить")
        same = normalizer.normalize("купить товар")
        assert normalizer.similarity(a, same) > normalizer.similarity(a, b)


class TestLinkCacheRanking:
    def test_find_signin_by_voyti(self):
        cache = LinkCache()
        cache.links = [
            {"text": "Sign In", "href": "/login", "type": "button"},
            {"text": "Корзина", "href": "/cart", "type": "link"},
        ]
        results = cache.search("Войти")
        assert results and results[0]["text"] == "Sign In"

    def test_exact_match_first(self):
        cache = LinkCache()
        cache.links = [
            {"text": "О нас", "href": "/about", "type": "link"},
            {"text": "About", "href": "/about", "type": "link"},
        ]
        results = cache.search("About")
        assert results[0]["text"] == "About"

    def test_no_garbage_matches(self):
        cache = LinkCache()
        cache.links = [{"text": "Аббревиатура", "href": "", "type": "link"}]
        assert cache.search("квантовая хромодинамика") == []

    def test_scan_method_exists(self):
        assert hasattr(LinkCache, "scan_interactive_elements")
