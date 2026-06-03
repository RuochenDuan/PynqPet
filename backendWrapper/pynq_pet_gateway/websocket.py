from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from enum import StrEnum
from json import JSONDecodeError
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pynq_pet_gateway.configs import get_config
from pynq_pet_gateway.protocol import (
    DEFAULT_CONFIG_ID,
    ErrorCode,
    ProtocolError,
    build_error,
    build_event,
    parse_envelope,
)
from pynq_pet_gateway.upstream import PlaceholderUpstreamAdapter, UpstreamAdapter


router = APIRouter()

MAX_IMAGE_BYTES = 262144
MAX_AUDIO_CHUNK_BYTES = 1048576
MAX_IMAGE_BASE64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_AUDIO_CHUNK_BASE64_CHARS = 4 * ((MAX_AUDIO_CHUNK_BYTES + 2) // 3)
MAX_IMAGE_WIDTH = 320
MAX_IMAGE_HEIGHT = 240
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}


class SessionState(StrEnum):
    CONNECTED = "connected"
    READY = "ready"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"
    WAITING_CLIENT_PLAYBACK = "waiting_client_playback"
    IDLE = "idle"
    CLOSING = "closing"


@dataclass
class PetSession:
    state: SessionState = SessionState.CONNECTED
    session_id: str | None = None
    device_id: str | None = None
    config_id: str | None = None
    last_audio_seq: int | None = None
    latest_image: dict[str, Any] | None = None
    latest_sensor: dict[str, Any] | None = None
    behavior_events: list[dict[str, Any]] | None = None
    behavior_event_count: int = 0
    active_turn_id: str | None = None
    active_response_id: str | None = None

    def is_initialized(self) -> bool:
        return self.session_id is not None and self.state != SessionState.CONNECTED

    def transition_to(self, state: SessionState) -> None:
        self.state = state

    def should_interrupt_on_audio(self) -> bool:
        return self.active_turn_id is not None and self.state in {
            SessionState.RESPONDING,
            SessionState.WAITING_CLIENT_PLAYBACK,
        }


@router.websocket("/api/v1/pet/ws")
async def pet_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session = PetSession()
    upstream = PlaceholderUpstreamAdapter()

    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            session.transition_to(SessionState.CLOSING)
            return

        try:
            envelope = parse_envelope(raw)
        except ProtocolError as exc:
            if exc.__cause__ is not None and isinstance(exc.__cause__, JSONDecodeError):
                await websocket.close()
                return
            await websocket.send_json(
                build_error(
                    exc.code,
                    exc.message,
                    retryable=False,
                    request_id=exc.request_id,
                    session_id=exc.session_id or session.session_id,
                    turn_id=exc.turn_id,
                    field=exc.field,
                )
            )
            continue

        if envelope.type == "session.init":
            await _handle_session_init(websocket, session, envelope)
        elif envelope.type == "heartbeat":
            if await _ensure_ready_session(websocket, session, envelope):
                await websocket.send_json(
                    build_event(
                        "heartbeat.ack",
                        {"ok": True},
                        session_id=session.session_id,
                        request_id=envelope.request_id,
                    )
                )
        elif envelope.type == "text.input":
            await _handle_text_input(websocket, session, envelope, upstream)
        elif envelope.type == "audio.chunk":
            if await _ensure_ready_session(websocket, session, envelope):
                await _handle_audio_chunk(websocket, session, envelope, upstream)
        elif envelope.type == "image.upload":
            if await _ensure_ready_session(websocket, session, envelope):
                await _handle_image_upload(websocket, session, envelope)
        elif envelope.type == "sensor.report":
            if await _ensure_ready_session(websocket, session, envelope):
                session.latest_sensor = dict(envelope.payload)
                await _send_status(
                    websocket,
                    session,
                    envelope.request_id,
                    "sensor_received",
                )
        elif envelope.type == "behavior.event":
            if await _ensure_ready_session(websocket, session, envelope):
                if session.behavior_events is None:
                    session.behavior_events = []
                session.behavior_events.append(dict(envelope.payload))
                session.behavior_event_count += 1
                await _send_status(
                    websocket,
                    session,
                    envelope.request_id,
                    "behavior_event_received",
                )
        elif envelope.type == "client.command":
            if await _ensure_ready_session(websocket, session, envelope):
                await websocket.send_json(
                    build_error(
                        ErrorCode.INVALID_CLIENT_COMMAND,
                        "Client command is not supported",
                        retryable=False,
                        request_id=envelope.request_id,
                        session_id=session.session_id,
                        field="command",
                    )
                )
        elif envelope.type == "conversation.interrupt":
            if await _ensure_ready_session(websocket, session, envelope):
                await _handle_interrupt(websocket, session, envelope, upstream)
        elif envelope.type == "client.tts.started":
            if await _ensure_ready_session(websocket, session, envelope):
                if await _ensure_current_response(websocket, session, envelope):
                    session.transition_to(SessionState.WAITING_CLIENT_PLAYBACK)
                    await _send_status(
                        websocket,
                        session,
                        envelope.request_id,
                        "client_tts_started",
                    )
        elif envelope.type == "client.tts.finished":
            if await _ensure_ready_session(websocket, session, envelope):
                if await _ensure_current_response(websocket, session, envelope):
                    session.active_turn_id = None
                    session.active_response_id = None
                    session.transition_to(SessionState.IDLE)
                    await _send_status(
                        websocket,
                        session,
                        envelope.request_id,
                        "client_tts_finished",
                    )
        elif envelope.type == "session.close":
            if await _ensure_close_allowed(websocket, session, envelope):
                session.transition_to(SessionState.CLOSING)
                await websocket.send_json(
                    build_event(
                        "session.closed",
                        {"reason": envelope.payload.get("reason", "client_shutdown")},
                        session_id=session.session_id,
                        request_id=envelope.request_id,
                    )
                )
                await websocket.close()
                return
        else:
            await websocket.send_json(
                build_error(
                    ErrorCode.INVALID_MESSAGE,
                    "Unknown event type",
                    retryable=False,
                    request_id=envelope.request_id,
                    session_id=session.session_id or envelope.session_id,
                    field="type",
                )
            )


async def _handle_session_init(
    websocket: WebSocket,
    session: PetSession,
    envelope,
) -> None:
    if session.state != SessionState.CONNECTED:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Session is already initialized",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id,
                field="type",
            )
        )
        return

    config_id = envelope.payload.get("config_id") or DEFAULT_CONFIG_ID
    if get_config(config_id) is None:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_CONFIG_ID,
                "Selected config does not exist",
                retryable=False,
                request_id=envelope.request_id,
                field="config_id",
            )
        )
        return

    session.session_id = f"ses_{uuid4().hex}"
    session.device_id = envelope.payload.get("device_id")
    session.config_id = config_id
    session.transition_to(SessionState.READY)

    await websocket.send_json(
        build_event(
            "session.ready",
            {
                "session_id": session.session_id,
                "device_id": session.device_id,
                "config_id": config_id,
                "server_capabilities": {
                    "vad": False,
                    "asr": False,
                    "vision_context": False,
                    "behavior_planning": False,
                },
            },
            session_id=session.session_id,
            request_id=envelope.request_id,
        )
    )


async def _handle_text_input(
    websocket: WebSocket,
    session: PetSession,
    envelope,
    upstream: UpstreamAdapter,
) -> None:
    if not await _ensure_ready_session(websocket, session, envelope):
        return
    if session.active_turn_id is not None:
        await websocket.send_json(
            build_error(
                ErrorCode.TURN_ALREADY_ACTIVE,
                "A turn is already active",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id,
                turn_id=session.active_turn_id,
            )
        )
        return

    turn_id = f"turn_{uuid4().hex}"
    response_id = f"rsp_{uuid4().hex}"
    session.active_turn_id = turn_id
    session.active_response_id = response_id
    session.transition_to(SessionState.PROCESSING)
    await websocket.send_json(
        build_event(
            "conversation.started",
            {"turn_id": turn_id, "trigger": "text"},
            session_id=session.session_id,
            request_id=envelope.request_id,
        )
    )
    await _send_status(websocket, session, envelope.request_id, "thinking")

    result = await upstream.start_text_turn(envelope.payload.get("text", ""))
    session.transition_to(SessionState.RESPONDING)
    await websocket.send_json(
        build_event(
            "response.text",
            {
                "response_id": response_id,
                "turn_id": turn_id,
                "text": result.text,
                "voice": result.voice,
            },
            session_id=session.session_id,
            request_id=envelope.request_id,
        )
    )
    session.transition_to(SessionState.WAITING_CLIENT_PLAYBACK)
    await websocket.send_json(
        build_event(
            "response.complete",
            {"response_id": response_id, "turn_id": turn_id},
            session_id=session.session_id,
            request_id=envelope.request_id,
        )
    )
    await _send_status(
        websocket,
        session,
        envelope.request_id,
        "waiting_client_playback",
    )


async def _handle_audio_chunk(
    websocket: WebSocket,
    session: PetSession,
    envelope,
    upstream: UpstreamAdapter,
) -> None:
    payload = envelope.payload
    if (
        payload.get("codec") != "pcm_s16le"
        or payload.get("sample_rate") != 16000
        or payload.get("channels") != 1
    ):
        await websocket.send_json(
            build_error(
                ErrorCode.AUDIO_FORMAT_UNSUPPORTED,
                "Audio format is not supported",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
            )
        )
        return

    audio_base64 = payload.get("audio_base64")
    if not isinstance(audio_base64, str) or not audio_base64:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Audio data must be provided as base64",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="audio_base64",
            )
        )
        return
    if len(audio_base64) > MAX_AUDIO_CHUNK_BASE64_CHARS:
        await websocket.send_json(
            build_error(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "Audio chunk exceeds configured limits",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="audio_base64",
            )
        )
        return

    try:
        audio_bytes = base64.b64decode(audio_base64, validate=True)
    except binascii.Error:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Audio data must be valid base64",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="audio_base64",
            )
        )
        return
    if len(audio_bytes) > MAX_AUDIO_CHUNK_BYTES:
        await websocket.send_json(
            build_error(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "Audio chunk exceeds configured limits",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="audio_base64",
            )
        )
        return

    seq = payload.get("seq")
    if type(seq) is not int or seq < 0:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Audio seq must be an integer",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="seq",
            )
        )
        return
    if session.last_audio_seq is not None and seq <= session.last_audio_seq:
        await websocket.send_json(
            build_error(
                ErrorCode.AUDIO_CHUNK_OUT_OF_ORDER,
                "Audio chunk seq must increase",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="seq",
            )
        )
        return

    if session.should_interrupt_on_audio():
        await _send_interrupted_ack(
            websocket,
            session,
            envelope,
            upstream,
            reason="user_speaking_again",
        )

    session.last_audio_seq = seq
    session.transition_to(SessionState.LISTENING)
    await _send_status(websocket, session, envelope.request_id, "audio_received")


async def _handle_image_upload(websocket: WebSocket, session: PetSession, envelope) -> None:
    payload = envelope.payload
    mime_type = payload.get("mime_type")
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Image MIME type is not supported",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="mime_type",
            )
        )
        return

    data_base64 = payload.get("data_base64")
    if not isinstance(data_base64, str) or not data_base64:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Image data must be provided as base64",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="data_base64",
            )
        )
        return
    if len(data_base64) > MAX_IMAGE_BASE64_CHARS:
        await websocket.send_json(
            build_error(
                ErrorCode.IMAGE_TOO_LARGE,
                "Image exceeds configured limits",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="image",
            )
        )
        return

    try:
        image_bytes = base64.b64decode(data_base64, validate=True)
    except binascii.Error:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Image data must be valid base64",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="data_base64",
            )
        )
        return

    width = payload.get("width")
    height = payload.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Image dimensions must be positive integers",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="image",
            )
        )
        return
    if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT or len(image_bytes) > MAX_IMAGE_BYTES:
        await websocket.send_json(
            build_error(
                ErrorCode.IMAGE_TOO_LARGE,
                "Image exceeds configured limits",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="image",
            )
        )
        return

    session.latest_image = {
        "image_id": payload.get("image_id"),
        "source": payload.get("source"),
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "sampled_at": payload.get("sampled_at"),
    }
    await _send_status(websocket, session, envelope.request_id, "image_received")


async def _handle_interrupt(
    websocket: WebSocket,
    session: PetSession,
    envelope,
    upstream: UpstreamAdapter,
) -> None:
    if session.active_turn_id is None:
        await websocket.send_json(
            build_error(
                ErrorCode.INTERRUPT_WITHOUT_ACTIVE_TURN,
                "No active turn to interrupt",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
            )
        )
        return

    await _send_interrupted_ack(
        websocket,
        session,
        envelope,
        upstream,
        reason=envelope.payload.get("reason", "user_speaking_again"),
    )


async def _send_interrupted_ack(
    websocket: WebSocket,
    session: PetSession,
    envelope,
    upstream: UpstreamAdapter,
    *,
    reason: str,
) -> None:
    turn_id = session.active_turn_id
    if turn_id is None:
        return

    session.active_turn_id = None
    session.active_response_id = None
    await upstream.interrupt_turn(turn_id, reason)
    session.transition_to(SessionState.IDLE)
    await websocket.send_json(
        build_event(
            "conversation.interrupted_ack",
            {
                "turn_id": turn_id,
                "reason": reason,
            },
            session_id=session.session_id,
            request_id=envelope.request_id,
        )
    )


async def _ensure_ready_session(
    websocket: WebSocket,
    session: PetSession,
    envelope,
) -> bool:
    if not session.is_initialized():
        await websocket.send_json(
            build_error(
                ErrorCode.SESSION_NOT_READY,
                "Session is not ready",
                retryable=False,
                request_id=envelope.request_id,
                session_id=envelope.session_id,
            )
        )
        return False
    if envelope.session_id != session.session_id:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Session id does not match active session",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id,
                field="session_id",
            )
        )
        return False
    return True


async def _ensure_current_response(
    websocket: WebSocket,
    session: PetSession,
    envelope,
) -> bool:
    response_id = envelope.payload.get("response_id")
    if (
        not isinstance(response_id, str)
        or response_id != session.active_response_id
    ):
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Response id does not match active response",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id,
                turn_id=session.active_turn_id,
                field="response_id",
            )
        )
        return False
    return True


async def _ensure_close_allowed(
    websocket: WebSocket,
    session: PetSession,
    envelope,
) -> bool:
    if session.session_id is None:
        return True
    if envelope.session_id != session.session_id:
        await websocket.send_json(
            build_error(
                ErrorCode.INVALID_MESSAGE,
                "Session id does not match active session",
                retryable=False,
                request_id=envelope.request_id,
                session_id=session.session_id,
                field="session_id",
            )
        )
        return False
    return True


async def _send_status(
    websocket: WebSocket,
    session: PetSession,
    request_id: str | None,
    stage: str,
) -> None:
    await websocket.send_json(
        build_event(
            "status.update",
            {"stage": stage, "state": session.state.value},
            session_id=session.session_id,
            request_id=request_id,
        )
    )
