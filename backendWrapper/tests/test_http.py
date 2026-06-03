from datetime import datetime
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pynq_pet_gateway.app import app


client = TestClient(app)


def test_health_reports_gateway_status_and_utc_time() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "pet-gateway"
    assert body["version"] == "v1"
    assert body["open_llm_vtuber"] == {"reachable": False}
    assert body["time"].endswith("Z")
    datetime.fromisoformat(body["time"].replace("Z", "+00:00"))


def test_capabilities_describe_v1_features_and_limits() -> None:
    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": "v1",
        "features": {
            "streaming_audio_upload": True,
            "image_upload": True,
            "sensor_upload": True,
            "bidirectional_behavior_events": True,
            "local_tts_expected": True,
        },
        "limits": {
            "max_image_bytes": 262144,
            "max_ws_message_bytes": 1048576,
            "audio_chunk_ms_recommended": 96,
        },
    }


def test_configs_lists_default_config_metadata() -> None:
    response = client.get("/api/v1/configs")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "config_id": "cfg_pet_default_cn",
                "name": "pet-default-cn",
                "version": 1,
                "status": "ready",
                "is_default": True,
                "created_at": "2026-05-26T08:15:30.123Z",
            }
        ]
    }


def test_config_detail_returns_default_config() -> None:
    response = client.get("/api/v1/configs/cfg_pet_default_cn")

    assert response.status_code == 200
    assert response.json() == {
        "config_id": "cfg_pet_default_cn",
        "name": "pet-default-cn",
        "version": 1,
        "status": "ready",
        "is_default": True,
        "created_at": "2026-05-26T08:15:30.123Z",
        "source_type": "yaml",
        "validation": {"ok": True, "warnings": []},
    }


def test_config_detail_returns_404_for_unknown_config() -> None:
    response = client.get("/api/v1/configs/unknown")

    assert response.status_code == 404
