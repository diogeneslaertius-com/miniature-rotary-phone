from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from services import fuzzy

router = APIRouter()
logger = logging.getLogger("jana.geocode")

GEOCODE_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
SUGGEST_URL = "https://catalog.api.2gis.com/3.0/suggests"


class GeocodeRequest(BaseModel):
    query: str
    city_hint: str | None = None  # None -> берём settings.default_city


class GeocodeCandidate(BaseModel):
    full_name: str
    lat: float
    lon: float
    confidence: float
    source: str  # "2gis" | "2gis+fuzzy" | "2gis-suggest" | "llm"


class GeocodeResponse(BaseModel):
    query: str
    candidates: list[GeocodeCandidate]


def _location_param() -> str:
    """lon,lat центра города по умолчанию — мягкая геопривязка:
    ранжирует ближайшие результаты выше, но не исключает остальные.
    В отличие от region_id это не жёсткий фильтр."""
    return f"{settings.default_lon},{settings.default_lat}"


async def _geocode_2gis(q: str) -> list[GeocodeCandidate]:
    params = {
        "q": q,
        "fields": "items.point,items.full_name,items.address_name",
        "key": settings.dgis_api_key,
        "locale": "ru_RU",
        "location": _location_param(),
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(GEOCODE_URL, params=params)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"2GIS geocoder недоступен: {exc}") from exc

    items = r.json().get("result", {}).get("items", [])
    out: list[GeocodeCandidate] = []
    for item in items:
        point = item.get("point")
        if not point:
            continue
        out.append(
            GeocodeCandidate(
                full_name=item.get("full_name") or item.get("address_name") or q,
                lat=point["lat"],
                lon=point["lon"],
                confidence=0.9 if item.get("full_name") else 0.6,
                source="2gis",
            )
        )
    return out


async def _suggest_2gis(q: str) -> list[GeocodeCandidate]:
    """
    Tier 1b: официальный Suggest API 2ГИС.

    Геоограничение — фикс против результатов из других регионов/стран:
    - region_id — ЖЁСТКОЕ ограничение конкретным регионом 2ГИС.
      Получите один раз вручную (см. .env.example) и впишите в
      DGIS_REGION_ID. Без значения параметр просто не передаётся, и
      Suggest продолжит вести себя как раньше.
    - location — мягкая геопривязка к центру города (влияет на
      ранжирование, не исключает результаты).
    - type=building,street — исключает кафе/туалеты/прочий "branch"
      из выдачи, оставляет только топонимы/здания.

    Запрос НЕ дополняется текстом города вручную (в отличие от
    _geocode_2gis) — когда регион уже зафиксирован параметром,
    добавление названия города в саму строку скорее мешает
    релевантности автодополнения, чем помогает.
    """
    params = {
        "q": q,
        "type": "building,street",
        "fields": "items.point,items.full_name,items.address_name",
        "key": settings.dgis_api_key,
        "locale": "ru_RU",
        "location": _location_param(),
    }
    if settings.dgis_region_id:
        params["region_id"] = settings.dgis_region_id

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(SUGGEST_URL, params=params)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            # Suggest — уже "план Б", его сбой не должен ронять запрос,
            # но раньше причина тонула молча. Теперь она видна в логах
            # uvicorn — смотрите туда, если Suggest "вдруг перестал работать".
            logger.warning("2GIS Suggest failed for q=%r: %s", q, exc)
            return []

    items = r.json().get("result", {}).get("items", [])
    out: list[GeocodeCandidate] = []
    for item in items:
        point = item.get("point")
        if not point:
            continue
        out.append(
            GeocodeCandidate(
                full_name=item.get("full_name") or item.get("address_name") or q,
                lat=point["lat"],
                lon=point["lon"],
                confidence=0.7,
                source="2gis-suggest",
            )
        )
    return out


@router.post("/geocode", response_model=GeocodeResponse)
async def geocode(payload: GeocodeRequest) -> GeocodeResponse:
    """
    Tier 0 -> Tier 1a -> Tier 1b, с точкой расширения под Tier 2 (LLM).

    Tier 0: прямой запрос в 2ГИС Geocoder API.
    Tier 1a (RapidFuzz): не нашёл → пробуем исправить опечатку в
    названии улицы/микрорайона по канонической базе города и повторяем
    запрос в 2ГИС уже с исправленным текстом.
    Tier 1b (2ГИС Suggest API): не помогло и это → официальный Suggest,
    геоограниченный регионом, рассчитанный на неточный/неполный ввод.

    TODO Tier 2 (LLM): резолюция "человеческих" описаний с
    ориентирами ("возле Магнума на Абая"), когда не помогло ничего
    из вышеперечисленного.
    """
    city = payload.city_hint or settings.default_city
    query_text = payload.query
    full_query = f"{city}, {query_text}"

    candidates = await _geocode_2gis(full_query)
    if candidates:
        return GeocodeResponse(query=query_text, candidates=candidates)

    correction = fuzzy.correct_query(query_text, city)
    if correction:
        corrected_text, score = correction
        retry = await _geocode_2gis(f"{city}, {corrected_text}")
        if retry:
            for c in retry:
                c.confidence = round(min(c.confidence, score / 100), 2)
                c.source = "2gis+fuzzy"
            return GeocodeResponse(query=query_text, candidates=retry)

    suggested = await _suggest_2gis(query_text)
    if suggested:
        return GeocodeResponse(query=query_text, candidates=suggested)

    return GeocodeResponse(query=query_text, candidates=[])
