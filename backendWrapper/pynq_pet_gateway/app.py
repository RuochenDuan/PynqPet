from typing import Any

from fastapi import FastAPI, HTTPException

from pynq_pet_gateway.configs import get_config, list_configs
from pynq_pet_gateway.protocol import PROTOCOL_VERSION, SERVICE_NAME, utc_now_iso
from pynq_pet_gateway.websocket import router as websocket_router


MAX_IMAGE_BYTES = 262144
MAX_WS_MESSAGE_BYTES = 1048576
AUDIO_CHUNK_MS_RECOMMENDED = 96

app = FastAPI(title="Pynq Pet Gateway")
app.include_router(websocket_router)


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": PROTOCOL_VERSION,
        "open_llm_vtuber": {"reachable": False},
        "time": utc_now_iso(),
    }


@app.get("/api/v1/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "features": {
            "streaming_audio_upload": True,
            "image_upload": True,
            "sensor_upload": True,
            "bidirectional_behavior_events": True,
            "local_tts_expected": True,
        },
        "limits": {
            "max_image_bytes": MAX_IMAGE_BYTES,
            "max_ws_message_bytes": MAX_WS_MESSAGE_BYTES,
            "audio_chunk_ms_recommended": AUDIO_CHUNK_MS_RECOMMENDED,
        },
    }


@app.get("/api/v1/configs")
def configs() -> dict[str, list[dict[str, Any]]]:
    return {"items": list_configs()}


@app.get("/api/v1/configs/{config_id}")
def config_detail(config_id: str) -> dict[str, Any]:
    config = get_config(config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Config not found")
    return config
