from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "street_dictionary.json"


@lru_cache
def _load_dictionary() -> dict[str, list[str]]:
    return json.loads(DICT_PATH.read_text(encoding="utf-8"))


def correct_query(query: str, city: str, score_cutoff: int = 80) -> tuple[str, float] | None:
    """
    Ищет ближайшее по написанию название улицы/микрорайона в канонической
    базе города. Если совпадение достаточно уверенное (score_cutoff) —
    возвращает (исправленный_текст, score) для повторного запроса
    к геокодеру. Иначе — None, идём дальше по каскаду (Tier 1b).

    ВАЖНО: street_dictionary.json сейчас — маленький иллюстративный
    список на пару десятков названий. Разница между "почти не работает"
    и "реально ловит опечатки" здесь чисто в объёме словаря — расширьте
    его реальным списком улиц/микрорайонов города перед демо.
    """
    candidates = _load_dictionary().get(city, [])
    if not candidates:
        return None

    match = process.extractOne(query, candidates, scorer=fuzz.WRatio, score_cutoff=score_cutoff)
    if match is None:
        return None

    matched_text, score, _ = match
    return matched_text, score
