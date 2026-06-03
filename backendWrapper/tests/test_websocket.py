import base64
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pynq_pet_gateway.app import app
from pynq_pet_gateway.protocol import DEFAULT_CONFIG_ID


client = TestClient(app)


def envelope(
    event_type: str,
    payload: dict,
    *,
    request_id: str = "req_001",
    session_id: str | None = None,
    version: str = "v1",
) -> str:
    message = {
        "type": event_type,
        "version": version,
        "request_id": request_id,
        "payload": payload,
    }
    if session_id is not None:
        message["session_id"] = session_id
    return json.dumps(message)


def init_session(websocket) -> dict:
    websocket.send_text(
        envelope(
            "session.init",
            {"device_id": "pynq-pet-001"},
            request_id="req_init",
        )
    )
    return websocket.receive_json()


def audio_chunk(seq: int, **overrides) -> dict:
    payload = {
        "seq": seq,
        "audio_base64": "AAECAw==",
        "codec": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
        "frame_ms": 96,
    }
    payload.update(overrides)
    return payload


def image_upload(**overrides) -> dict:
    payload = {
        "image_id": "img_001",
        "source": "camera",
        "mime_type": "image/jpeg",
        "data_base64": "AAECAw==",
        "width": 320,
        "height": 240,
        "sampled_at": "2026-05-26T08:15:30.123Z",
    }
    payload.update(overrides)
    return payload


def assert_error_code(event: dict, code: str, request_id: str) -> None:
    assert event["type"] == "error"
    assert event["request_id"] == request_id
    assert event["payload"]["code"] == code
    assert event["payload"]["request_id"] == request_id
    assert event["payload"]["retryable"] is False


def assert_status_update(
    event: dict,
    *,
    stage: str,
    state: str,
    request_id: str,
    session_id: str,
) -> None:
    assert event["type"] == "status.update"
    assert event["request_id"] == request_id
    assert event["session_id"] == session_id
    assert event["payload"] == {"stage": stage, "state": state}


def receive_until_type(websocket, event_type: str, *, limit: int = 6) -> dict:
    for _ in range(limit):
        event = websocket.receive_json()
        if event["type"] == event_type:
            return event
    raise AssertionError(f"Did not receive {event_type!r} within {limit} events")


def receive_until_request_id(websocket, request_id: str, *, limit: int = 6) -> dict:
    for _ in range(limit):
        event = websocket.receive_json()
        if event.get("request_id") == request_id:
            return event
    raise AssertionError(f"Did not receive request_id {request_id!r} within {limit} events")


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("audio.chunk", audio_chunk(1)),
        ("image.upload", image_upload()),
        (
            "sensor.report",
            {
                "temperature_c": 24.5,
                "humidity_pct": 52.0,
                "sampled_at": "2026-05-26T08:15:30.123Z",
            },
        ),
        (
            "behavior.event",
            {
                "related_command_id": "cmd_001",
                "event": "behavior.finished",
                "status": "success",
            },
        ),
        ("client.tts.started", {"response_id": "rsp_001"}),
        ("client.tts.finished", {"response_id": "rsp_001"}),
        ("heartbeat", {"uptime_ms": 100}),
    ],
)
def test_post_init_events_before_ready_return_session_not_ready(
    event_type: str,
    payload: dict,
) -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        websocket.send_text(
            envelope(event_type, payload, request_id=f"req_{event_type}")
        )
        event = websocket.receive_json()

    assert_error_code(event, "SESSION_NOT_READY", f"req_{event_type}")


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("audio.chunk", audio_chunk(1)),
        ("image.upload", image_upload()),
        (
            "sensor.report",
            {
                "temperature_c": 24.5,
                "humidity_pct": 52.0,
                "sampled_at": "2026-05-26T08:15:30.123Z",
            },
        ),
        (
            "behavior.event",
            {
                "related_command_id": "cmd_001",
                "event": "behavior.finished",
                "status": "success",
            },
        ),
        ("client.tts.started", {"response_id": "rsp_001"}),
        ("client.tts.finished", {"response_id": "rsp_001"}),
        ("heartbeat", {"uptime_ms": 100}),
        ("client.command", {"command": "unsupported.command"}),
        ("conversation.interrupt", {"reason": "user_speaking_again"}),
    ],
)
def test_post_init_events_with_stale_session_id_return_invalid_message(
    event_type: str,
    payload: dict,
) -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        init_session(websocket)

        websocket.send_text(
            envelope(
                event_type,
                payload,
                request_id=f"req_stale_{event_type}",
                session_id="ses_stale",
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", f"req_stale_{event_type}")
    assert event["payload"]["field"] == "session_id"


def test_ready_events_missing_session_id_return_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        init_session(websocket)

        websocket.send_text(
            envelope(
                "heartbeat",
                {"uptime_ms": 100},
                request_id="req_missing_session",
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_missing_session")
    assert event["payload"]["field"] == "session_id"


def test_session_init_after_ready_returns_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "session.init",
                {"device_id": "pynq-pet-001"},
                request_id="req_reinit",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_reinit")
    assert event["payload"]["field"] == "type"


def test_session_init_uses_default_config_and_reports_ready_capabilities() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        event = init_session(websocket)

    assert event["type"] == "session.ready"
    assert event["version"] == "v1"
    assert event["request_id"] == "req_init"
    assert event["session_id"].startswith("ses_")
    assert event["payload"] == {
        "session_id": event["session_id"],
        "device_id": "pynq-pet-001",
        "config_id": DEFAULT_CONFIG_ID,
        "server_capabilities": {
            "vad": False,
            "asr": False,
            "vision_context": False,
            "behavior_planning": False,
        },
    }


def test_heartbeat_before_ready_returns_session_not_ready() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        websocket.send_text(
            envelope("heartbeat", {"uptime_ms": 100}, request_id="req_heartbeat")
        )
        event = websocket.receive_json()

    assert_error_code(event, "SESSION_NOT_READY", "req_heartbeat")


def test_heartbeat_ack_includes_ready_session_id() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "heartbeat",
                {"uptime_ms": 100},
                request_id="req_heartbeat",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert event["type"] == "heartbeat.ack"
    assert event["request_id"] == "req_heartbeat"
    assert event["session_id"] == ready["session_id"]
    assert event["payload"] == {"ok": True}


def test_text_input_after_ready_sends_response_sequence() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "你好"},
                request_id="req_text",
                session_id=ready["session_id"],
            )
        )
        events = [websocket.receive_json() for _ in range(4)]
        websocket.send_text(
            envelope(
                "heartbeat",
                {"uptime_ms": 100},
                request_id="req_probe_after_text",
                session_id=ready["session_id"],
            )
        )
        events.append(websocket.receive_json())

    assert [event["type"] for event in events] == [
        "conversation.started",
        "status.update",
        "response.text",
        "response.complete",
        "status.update",
    ]
    assert all(event["session_id"] == ready["session_id"] for event in events)
    turn_id = events[0]["payload"]["turn_id"]
    response_id = events[2]["payload"]["response_id"]
    assert turn_id.startswith("turn_")
    assert response_id.startswith("rsp_")
    assert events[0]["payload"] == {"turn_id": turn_id, "trigger": "text"}
    assert events[1]["payload"] == {"stage": "thinking", "state": "processing"}
    assert events[2]["payload"] == {
        "response_id": response_id,
        "turn_id": turn_id,
        "text": "我收到你的消息了。",
        "voice": "normal",
    }
    assert events[3]["payload"] == {"response_id": response_id, "turn_id": turn_id}
    assert events[4]["payload"] == {
        "stage": "waiting_client_playback",
        "state": "waiting_client_playback",
    }


def test_text_input_before_ready_returns_session_not_ready_error() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        websocket.send_text(envelope("text.input", {"text": "你好"}, request_id="req_text"))
        event = websocket.receive_json()

    assert event["type"] == "error"
    assert event["request_id"] == "req_text"
    assert event["payload"] == {
        "code": "SESSION_NOT_READY",
        "message": "Session is not ready",
        "retryable": False,
        "request_id": "req_text",
    }


def test_audio_chunk_after_ready_returns_audio_received_status() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1),
                request_id="req_audio",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_status_update(
        event,
        stage="audio_received",
        state="listening",
        request_id="req_audio",
        session_id=ready["session_id"],
    )


def test_audio_chunk_enters_listening_without_starting_conversation() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1),
                request_id="req_audio",
                session_id=ready["session_id"],
            )
        )
        assert_status_update(
            websocket.receive_json(),
            stage="audio_received",
            state="listening",
            request_id="req_audio",
            session_id=ready["session_id"],
        )

        websocket.send_text(
            envelope(
                "heartbeat",
                {"uptime_ms": 100},
                request_id="req_after_audio",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert event["type"] == "heartbeat.ack"
    assert event["request_id"] == "req_after_audio"


def test_text_input_from_listening_starts_turn() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1),
                request_id="req_audio",
                session_id=ready["session_id"],
            )
        )
        assert_status_update(
            websocket.receive_json(),
            stage="audio_received",
            state="listening",
            request_id="req_audio",
            session_id=ready["session_id"],
        )

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "从监听进入对话"},
                request_id="req_text_from_listening",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert event["type"] == "conversation.started"
    assert event["request_id"] == "req_text_from_listening"
    assert event["payload"]["trigger"] == "text"


def test_audio_chunk_with_invalid_format_returns_audio_format_unsupported() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1, codec="opus"),
                request_id="req_bad_audio",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "AUDIO_FORMAT_UNSUPPORTED", "req_bad_audio")


def test_audio_chunk_seq_must_increase() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1),
                request_id="req_audio_1",
                session_id=ready["session_id"],
            )
        )
        assert websocket.receive_json()["payload"] == {
            "stage": "audio_received",
            "state": "listening",
        }

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1),
                request_id="req_audio_2",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "AUDIO_CHUNK_OUT_OF_ORDER", "req_audio_2")


def test_audio_chunk_seq_must_be_integer() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk("one"),
                request_id="req_audio_seq_type",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_audio_seq_type")
    assert event["payload"]["field"] == "seq"


def test_audio_chunk_seq_boolean_is_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(True),
                request_id="req_audio_seq_bool",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_audio_seq_bool")
    assert event["payload"]["field"] == "seq"


def test_audio_chunk_with_invalid_base64_returns_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1, audio_base64="not base64"),
                request_id="req_bad_audio_base64",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_bad_audio_base64")
    assert event["payload"]["field"] == "audio_base64"


def test_audio_chunk_missing_base64_returns_invalid_message() -> None:
    payload = audio_chunk(1)
    payload.pop("audio_base64")
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                payload,
                request_id="req_missing_audio_base64",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_missing_audio_base64")
    assert event["payload"]["field"] == "audio_base64"


def test_audio_chunk_too_large_returns_payload_too_large() -> None:
    oversized_audio = base64.b64encode(bytes(1_048_577)).decode("ascii")
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "audio.chunk",
                audio_chunk(1, audio_base64=oversized_audio),
                request_id="req_large_audio",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "PAYLOAD_TOO_LARGE", "req_large_audio")
    assert event["payload"]["field"] == "audio_base64"


def test_second_text_input_while_turn_active_returns_turn_already_active() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "第一句"},
                request_id="req_first_text",
                session_id=ready["session_id"],
            )
        )
        for _ in range(4):
            websocket.receive_json()

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "第二句"},
                request_id="req_second_text",
                session_id=ready["session_id"],
            )
        )
        event = receive_until_type(websocket, "error", limit=3)

    assert_error_code(event, "TURN_ALREADY_ACTIVE", "req_second_text")


def test_image_upload_after_ready_returns_image_received_status() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                image_upload(),
                request_id="req_image",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_status_update(
        event,
        stage="image_received",
        state="ready",
        request_id="req_image",
        session_id=ready["session_id"],
    )


def test_oversized_image_upload_returns_image_too_large() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                image_upload(width=321),
                request_id="req_large_image",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "IMAGE_TOO_LARGE", "req_large_image")


def test_unsupported_image_mime_returns_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                image_upload(mime_type="image/gif"),
                request_id="req_bad_image_mime",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_bad_image_mime")
    assert event["payload"]["field"] == "mime_type"


def test_image_upload_too_many_bytes_returns_image_too_large() -> None:
    oversized_image = base64.b64encode(bytes(262_145)).decode("ascii")
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                image_upload(data_base64=oversized_image),
                request_id="req_large_image_bytes",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "IMAGE_TOO_LARGE", "req_large_image_bytes")
    assert event["payload"]["field"] == "image"


def test_image_upload_zero_dimensions_return_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                image_upload(width=0),
                request_id="req_zero_image_width",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_zero_image_width")
    assert event["payload"]["field"] == "image"


def test_image_upload_boolean_dimensions_return_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                image_upload(width=True),
                request_id="req_bool_image_width",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_bool_image_width")
    assert event["payload"]["field"] == "image"


def test_invalid_image_base64_returns_invalid_message() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                image_upload(data_base64="not base64"),
                request_id="req_bad_image",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_bad_image")
    assert event["payload"]["field"] == "data_base64"


def test_image_upload_missing_base64_returns_invalid_message() -> None:
    payload = image_upload()
    payload.pop("data_base64")
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "image.upload",
                payload,
                request_id="req_missing_image",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_MESSAGE", "req_missing_image")
    assert event["payload"]["field"] == "data_base64"


def test_sensor_report_returns_sensor_received_without_starting_conversation() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "sensor.report",
                {
                    "temperature_c": 24.5,
                    "humidity_pct": 52.0,
                    "sampled_at": "2026-05-26T08:15:30.123Z",
                },
                request_id="req_sensor",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_status_update(
        event,
        stage="sensor_received",
        state="ready",
        request_id="req_sensor",
        session_id=ready["session_id"],
    )


def test_behavior_event_returns_behavior_event_received_status() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "behavior.event",
                {
                    "related_command_id": "cmd_001",
                    "event": "behavior.finished",
                    "status": "success",
                    "duration_ms": 1200,
                    "end_at": "2026-05-26T08:15:30.123Z",
                },
                request_id="req_behavior",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_status_update(
        event,
        stage="behavior_event_received",
        state="ready",
        request_id="req_behavior",
        session_id=ready["session_id"],
    )


def test_unsupported_client_command_returns_invalid_client_command() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "client.command",
                {"command": "unsupported.command"},
                request_id="req_command",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INVALID_CLIENT_COMMAND", "req_command")


def test_conversation_interrupt_before_active_turn_returns_error() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "conversation.interrupt",
                {"reason": "user_speaking_again"},
                request_id="req_interrupt",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(event, "INTERRUPT_WITHOUT_ACTIVE_TURN", "req_interrupt")


def test_conversation_interrupt_after_text_input_returns_interrupted_ack() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "你好"},
                request_id="req_text_active",
                session_id=ready["session_id"],
            )
        )
        started = websocket.receive_json()

        websocket.send_text(
            envelope(
                "conversation.interrupt",
                {"reason": "user_speaking_again"},
                request_id="req_interrupt_active",
                session_id=ready["session_id"],
            )
        )
        event = receive_until_type(websocket, "conversation.interrupted_ack")

    assert started["type"] == "conversation.started"
    assert event["request_id"] == "req_interrupt_active"
    assert event["session_id"] == ready["session_id"]
    assert event["payload"] == {
        "turn_id": started["payload"]["turn_id"],
        "reason": "user_speaking_again",
    }


def test_client_tts_lifecycle_returns_status_updates() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "client.tts.started",
                {"response_id": "rsp_001"},
                request_id="req_tts_started",
                session_id=ready["session_id"],
            )
        )
        started = websocket.receive_json()

        websocket.send_text(
            envelope(
                "client.tts.finished",
                {"response_id": "rsp_001"},
                request_id="req_tts_finished",
                session_id=ready["session_id"],
            )
        )
        finished = websocket.receive_json()

    assert_status_update(
        started,
        stage="client_tts_started",
        state="waiting_client_playback",
        request_id="req_tts_started",
        session_id=ready["session_id"],
    )
    assert_status_update(
        finished,
        stage="client_tts_finished",
        state="idle",
        request_id="req_tts_finished",
        session_id=ready["session_id"],
    )


def test_client_tts_finished_clears_active_turn() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "你好"},
                request_id="req_text_for_tts",
                session_id=ready["session_id"],
            )
        )
        for _ in range(4):
            websocket.receive_json()

        websocket.send_text(
            envelope(
                "client.tts.finished",
                {"response_id": "rsp_001"},
                request_id="req_finish_turn",
                session_id=ready["session_id"],
            )
        )
        assert receive_until_request_id(websocket, "req_finish_turn")["payload"] == {
            "stage": "client_tts_finished",
            "state": "idle",
        }

        websocket.send_text(
            envelope(
                "conversation.interrupt",
                {"reason": "user_speaking_again"},
                request_id="req_interrupt_after_finished",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

    assert_error_code(
        event,
        "INTERRUPT_WITHOUT_ACTIVE_TURN",
        "req_interrupt_after_finished",
    )


def test_client_tts_finished_allows_new_text_input_from_idle() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "第一句"},
                request_id="req_first_text",
                session_id=ready["session_id"],
            )
        )
        for _ in range(4):
            websocket.receive_json()

        websocket.send_text(
            envelope(
                "client.tts.finished",
                {"response_id": "rsp_001"},
                request_id="req_finish_turn",
                session_id=ready["session_id"],
            )
        )
        assert receive_until_request_id(websocket, "req_finish_turn")["payload"] == {
            "stage": "client_tts_finished",
            "state": "idle",
        }

        websocket.send_text(
            envelope(
                "text.input",
                {"text": "第二句"},
                request_id="req_second_text",
                session_id=ready["session_id"],
            )
        )
        event = receive_until_type(websocket, "conversation.started", limit=3)

    assert event["type"] == "conversation.started"
    assert event["request_id"] == "req_second_text"


def test_session_close_returns_session_closed_then_closes_connection() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "session.close",
                {"reason": "client_shutdown"},
                request_id="req_close",
                session_id=ready["session_id"],
            )
        )
        event = websocket.receive_json()

        assert event["type"] == "session.closed"
        assert event["request_id"] == "req_close"
        assert event["session_id"] == ready["session_id"]
        assert event["payload"] == {"reason": "client_shutdown"}
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_session_close_with_stale_session_id_returns_error_and_stays_open() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        ready = init_session(websocket)

        websocket.send_text(
            envelope(
                "session.close",
                {"reason": "client_shutdown"},
                request_id="req_close_stale",
                session_id="ses_stale",
            )
        )
        error = websocket.receive_json()

        websocket.send_text(
            envelope(
                "heartbeat",
                {"uptime_ms": 100},
                request_id="req_after_stale_close",
                session_id=ready["session_id"],
            )
        )
        ack = websocket.receive_json()

    assert_error_code(error, "INVALID_MESSAGE", "req_close_stale")
    assert error["payload"]["field"] == "session_id"
    assert ack["type"] == "heartbeat.ack"
    assert ack["request_id"] == "req_after_stale_close"


def test_unsupported_version_returns_error_and_keeps_connection_open() -> None:
    with client.websocket_connect("/api/v1/pet/ws") as websocket:
        websocket.send_text(
            envelope("heartbeat", {}, request_id="req_bad_version", version="v2")
        )
        error = websocket.receive_json()

        ready = init_session(websocket)
        websocket.send_text(
            envelope(
                "heartbeat",
                {},
                request_id="req_heartbeat",
                session_id=ready["session_id"],
            )
        )
        ack = websocket.receive_json()

    assert error["type"] == "error"
    assert error["request_id"] == "req_bad_version"
    assert error["payload"] == {
        "code": "UNSUPPORTED_VERSION",
        "message": "Unsupported protocol version",
        "retryable": False,
        "request_id": "req_bad_version",
        "field": "version",
    }
    assert ack["type"] == "heartbeat.ack"
    assert ack["payload"] == {"ok": True}


def test_non_json_message_closes_without_error_event() -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/pet/ws") as websocket:
            websocket.send_text("{")
            websocket.receive_json()
