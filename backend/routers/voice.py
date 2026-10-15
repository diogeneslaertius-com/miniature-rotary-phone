from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, File, UploadFile

from config import settings

router = APIRouter()

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Порог "вероятность, что это не речь" — сегменты выше порога
# отбрасываются. 0.6 совпадает с внутренним дефолтом самого Whisper
# (no_speech_threshold), это не случайная цифра, но всё равно
# откалибруйте на реальных тестах с вашим микрофоном.
NO_SPEECH_THRESHOLD = 0.6

# Известные фразы-галлюцинации Whisper на тишине/шуме — задокументированный
# артефакт модели (перекос обучающих данных: много роликов на YouTube
# заканчивается титрами вроде "Thank you for watching"). Список не
# исчерпывающий — дополняйте по мере встречи новых на тестах.
HALLUCINATION_PHRASES = {
    "thank you",
    "thank you.",
    "thanks for watching",
    "thanks for watching!",
    "subscribe",
    "bye",
    "bye.",
    "bye bye.",
    "you",
    "you.",
}

_local_model = None


def get_local_model():
    """Ленивая загрузка локальной модели — см. README про прогрев
    перед демо, тот же принцип, что и раньше."""
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel

        _local_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _local_model


def _filter_hallucinations(parts: list[tuple[str, float | None]]) -> str:
    """parts — список (текст_сегмента, no_speech_prob). Отбрасывает
    сегменты с высокой вероятностью "не речь" и короткие фразы из
    списка известных Whisper-галлюцинаций. Общая для Groq и локального
    пути логика, чтобы не чинить баг дважды в двух местах."""
    out = []
    for text, no_speech_prob in parts:
        text = text.strip()
        if not text:
            continue
        if no_speech_prob is not None and no_speech_prob > NO_SPEECH_THRESHOLD:
            continue
        if text.lower().strip(".! ") in HALLUCINATION_PHRASES and len(text) < 20:
            continue
        out.append(text)
    return " ".join(out).strip()


async def _transcribe_groq(audio_bytes: bytes, filename: str) -> dict | None:
    """Быстрый облачный путь. Возвращает None при любой проблеме —
    вызывающий код в этом случае просто идёт на локальный фоллбэк,
    без паники и без падения запроса целиком."""
    if not settings.groq_api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            files = {"file": (filename, audio_bytes, "audio/webm")}
            data = {
                "model": "whisper-large-v3-turbo",
                # verbose_json — чтобы получить no_speech_prob по сегментам
                # и применить тот же фильтр галлюцинаций, что и локально.
                "response_format": "verbose_json",
            }
            headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
            r = await client.post(GROQ_URL, headers=headers, data=data, files=files)
            r.raise_for_status()
            payload = r.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    segments = payload.get("segments") or []
    if segments:
        parts = [(s.get("text", ""), s.get("no_speech_prob")) for s in segments]
        text = _filter_hallucinations(parts)
    else:
        # На случай, если ответ пришёл без сегментов (неожиданный формат) —
        # деградация до обычного текста без фильтрации, не падение.
        text = (payload.get("text") or "").strip()

    return {
        "text": text,
        "language": payload.get("language"),
        "language_probability": None,
        "engine": "groq",
    }


def _transcribe_local(audio_bytes: bytes, filename: str) -> dict:
    suffix = Path(filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    model = get_local_model()
    segments, info = model.transcribe(tmp_path, language=None, vad_filter=True)
    parts = [(seg.text, getattr(seg, "no_speech_prob", None)) for seg in segments]
    text = _filter_hallucinations(parts)

    Path(tmp_path).unlink(missing_ok=True)

    return {
        "text": text,
        "language": info.language,
        "language_probability": round(info.language_probability, 2),
        "engine": "local",
    }


@router.post("/voice")
async def transcribe(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    filename = file.filename or "audio.webm"

    # Groq первым — если задан ключ и уложился в 4 сек. Иначе (нет
    # ключа / таймаут / сбой сети) — локальный faster-whisper, который
    # не зависит от интернета площадки вообще.
    result = await _transcribe_groq(audio_bytes, filename)
    if result is not None:
        return result

    return _transcribe_local(audio_bytes, filename)
