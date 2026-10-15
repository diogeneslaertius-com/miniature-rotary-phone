from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from routers import geocode, hardware, orders, postamats, voice

app = FastAPI(title="Jana Navigator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(geocode.router, prefix="/api", tags=["geocode"])
app.include_router(postamats.router, prefix="/api", tags=["postamats"])
app.include_router(voice.router, prefix="/api", tags=["voice"])
app.include_router(hardware.router, prefix="/api", tags=["hardware"])
app.include_router(orders.router, prefix="/api", tags=["orders"])


@app.get("/api/config")
async def public_config():
    """
    Публичные (не секретные) параметры для инициализации карты на фронте.
    DGIS_MAP_KEY — домен-ограниченный клиентский ключ, его можно отдавать
    сюда. DGIS_API_KEY (серверный, для Geocoder/Directions) сюда НЕ
    попадает и остаётся только в backend/.env.
    """
    return {
        "mapKey": settings.dgis_map_key,
        "styleId": settings.map_style_id,
        "demoMode": settings.demo_mode,
        "defaultCity": settings.default_city,
        "defaultCenter": [settings.default_lon, settings.default_lat],
    }


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/proofs", StaticFiles(directory=str(orders.PROOFS_DIR)), name="proofs")
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
