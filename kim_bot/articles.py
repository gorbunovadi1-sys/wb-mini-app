"""Resolves a marketplace's own article code to the canonical article her
stock file uses. Most items match 1:1 (she confirmed codes are "usually the
same, might differ") — ArticleMap only needs a row for real exceptions
(WB's own article routinely differs, per fulfillment_sheet.sync_article_map).

Everything also goes through a case/homoglyph-insensitive fallback match
against the canonical article list — verified live 2026-09-15: a real Ozon
offer_id came back as "4856с" (Cyrillic с) against a canonical "4856C"
(Latin C), which would otherwise silently create a second, disconnected
"article" bucket in every report."""
from .db import SessionLocal
from .models import ArticleMap

_CYRILLIC_LOOKALIKES = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "а": "A", "в": "B", "е": "E", "к": "K", "м": "M", "н": "H", "о": "O", "р": "P", "с": "C", "т": "T", "у": "Y", "х": "X",
})

_override_cache = None  # {(marketplace, mp_article): canonical_article} — explicit ArticleMap rows
_canonical_by_normalized = None  # {normalized(canonical): canonical} — fallback fuzzy match


def _normalize(s: str) -> str:
    return s.translate(_CYRILLIC_LOOKALIKES).upper().strip()


def _load_cache():
    global _override_cache, _canonical_by_normalized
    _override_cache = {}
    _canonical_by_normalized = {}
    with SessionLocal() as db:
        for m in db.query(ArticleMap).all():
            if m.wb_article:
                _override_cache[("wb", m.wb_article)] = m.canonical_article
            if m.ozon_article:
                _override_cache[("ozon", m.ozon_article)] = m.canonical_article
            _canonical_by_normalized[_normalize(m.canonical_article)] = m.canonical_article


def invalidate_cache():
    global _override_cache, _canonical_by_normalized
    _override_cache = None
    _canonical_by_normalized = None


def canonical_article(marketplace: str, mp_article: str) -> str:
    """Explicit override first, then a case/homoglyph-insensitive match
    against known canonical articles, else the marketplace's own code
    unchanged (matches her confirmation that codes usually already agree)."""
    if _override_cache is None:
        _load_cache()
    if (marketplace, mp_article) in _override_cache:
        return _override_cache[(marketplace, mp_article)]
    return _canonical_by_normalized.get(_normalize(mp_article), mp_article)
