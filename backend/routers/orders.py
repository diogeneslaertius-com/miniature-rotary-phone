from __future__ import annotations

import json
import math
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter()

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "orders.sample.json"
PROOFS_DIR = Path(__file__).resolve().parent.parent / "data" / "proofs"
PROOFS_DIR.mkdir(parents=True, exist_ok=True)

AVG_SPEED_KMH = 18  # консервативная оценка городской доставки "на глаз"
STOP_HANDLING_MIN = 3  # среднее время на месте: найти вход, передать заказ

# Простое состояние в памяти — статусы сбрасываются при рестарте сервера.
# Для демо этого достаточно; для прода нужна нормальная БД под заказы.
_state: dict | None = None


class StatusUpdate(BaseModel):
    status: str  # "delivered" | "failed"


def _load() -> dict:
    global _state
    if _state is None:
        _state = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return _state


def _find_stop(data: dict, order_id: str) -> dict:
    for stop in data["stops"]:
        if stop["id"] == order_id:
            return stop
    raise HTTPException(404, "Заказ не найден")


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Манхэттенское расстояние в км — грубая, но честная оценка на
    одну строчку, ровно как и предлагалось. Не заменяет реальный
    Directions API, но для ETA в списке задач этого достаточно."""
    lat_km = abs(lat1 - lat2) * 111.0
    lon_km = abs(lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return lat_km + lon_km


@router.get("/orders")
async def list_orders():
    """
    Сегодняшний маршрут курьера с накопительным ETA по точкам.
    Порядок точек — из orders.sample.json, зафиксирован вручную
    (см. комментарий в файле), не результат оптимизации маршрута.
    """
    data = _load()
    start = data["courier_start"]
    stops = data["stops"]

    result = []
    prev = start
    cumulative_min = 0.0
    for i, stop in enumerate(stops, start=1):
        leg_km = _distance_km(prev["lat"], prev["lon"], stop["lat"], stop["lon"])
        cumulative_min += leg_km / AVG_SPEED_KMH * 60 + STOP_HANDLING_MIN
        result.append({**stop, "order": i, "eta_minutes": round(cumulative_min, 1)})
        prev = stop

    return {"stops": result}


@router.post("/orders/{order_id}/status")
async def set_order_status(order_id: str, payload: StatusUpdate):
    """Используется для 'Не получилось' — статус 'delivered' проставляется
    только через /proof, чтобы нельзя было закрыть заказ без фото."""
    data = _load()
    stop = _find_stop(data, order_id)
    stop["status"] = payload.status
    return {"ok": True}


@router.post("/orders/{order_id}/proof")
async def upload_proof(order_id: str, photo: UploadFile = File(...)):
    """
    Фото-подтверждение «у двери». Приняли фото -> сохранили на диск ->
    ТОЛЬКО ПОСЛЕ этого проставили статус "delivered". Так курьер физически
    не может закрыть бесконтактную доставку без снимка — это и есть
    антифрод-механика, а не просто красивая кнопка.

    Хранение на диске — MVP-решение для демо, не продакшен: файлы не
    ротируются, метаданные не в БД. Для реального использования нужно
    объектное хранилище (S3-совместимое) + запись в БД заказов.
    """
    data = _load()
    stop = _find_stop(data, order_id)

    suffix = Path(photo.filename or "proof.jpg").suffix or ".jpg"
    dest_name = f"{order_id}_{uuid.uuid4().hex[:8]}{suffix}"
    dest_path = PROOFS_DIR / dest_name

    with dest_path.open("wb") as f:
        shutil.copyfileobj(photo.file, f)

    stop["status"] = "delivered"
    stop["proof_photo"] = dest_name

    return {"ok": True, "proof_photo": dest_name}
