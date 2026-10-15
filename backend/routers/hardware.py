from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

router = APIRouter()

Status = Literal["idle", "listening", "processing", "success", "error"]

# Простое состояние в памяти — достаточно для одного демо-устройства
# на хакатоне. В проде это будет per-device state, а не глобальная переменная.
_state: dict[str, Status] = {"status": "idle"}
_ws_clients: set[WebSocket] = set()


class StatusUpdate(BaseModel):
    status: Status


@router.post("/hardware/trigger")
async def hardware_trigger():
    """
    Вызывается прошивкой ESP32-S3 при нажатии физической кнопки курьера.

    В базовом наборе нет микрофона — ESP32-S3 не пишет звук сам, он лишь
    физический пульт: переводит статус в "listening" и толкает фронтенду
    по WebSocket команду начать запись — телефон остаётся "ушами".
    """
    _state["status"] = "listening"
    await _broadcast({"event": "start_recording"})
    return {"ok": True}


@router.get("/hardware/status")
async def hardware_status():
    """ESP32-S3 опрашивает это каждые ~300мс (HTTP-поллинг проще и
    надёжнее на хакатоне, чем WebSocket-клиент в прошивке), чтобы
    понять, каким цветом/звуком отреагировать прямо сейчас."""
    return {"status": _state["status"]}


@router.post("/hardware/status")
async def set_hardware_status(payload: StatusUpdate):
    """Фронтенд репортит сюда реальный прогресс: 'processing' пока идёт
    транскрибация/геокодинг, 'success' или 'error' по завершении."""
    _state["status"] = payload.status
    return {"ok": True}


@router.websocket("/ws/hardware")
async def hardware_ws(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # держим соединение живым, входящее не используем
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


async def _broadcast(message: dict) -> None:
    dead = []
    for client in _ws_clients:
        try:
            await client.send_json(message)
        except Exception:
            dead.append(client)
    for d in dead:
        _ws_clients.discard(d)
