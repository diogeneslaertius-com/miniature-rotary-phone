from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter()

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "postamats.sample.geojson"


@router.get("/postamats")
async def list_postamats(
    min_lat: float | None = Query(None),
    min_lon: float | None = Query(None),
    max_lat: float | None = Query(None),
    max_lon: float | None = Query(None),
):
    """
    MVP: отдаёт статический курируемый датасет (см. data/postamats.sample.geojson).

    В проде: заменить на синхронизацию из API/выгрузок провайдеров по
    расписанию или вебхуком, хранить в БД с геоиндексом (PostGIS/пр.).
    Для хакатона статика надёжнее — меньше точек отказа на сцене.
    """
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    features = data["features"]

    if None not in (min_lat, min_lon, max_lat, max_lon):

        def in_bbox(f: dict) -> bool:
            lon, lat = f["geometry"]["coordinates"]
            return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat

        features = [f for f in features if in_bbox(f)]

    return {"type": "FeatureCollection", "features": features}
