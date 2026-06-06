from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from enum import StrEnum
import json
from json import JSONDecodeError
import re
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
from pynq_pet_gateway.upstream import (
    AudioChunkResult,
    UpstreamAdapter,
    UpstreamBridgeError,
    create_upstream_adapter,
)


router = APIRouter()

MAX_IMAGE_BYTES = 262144
MAX_AUDIO_CHUNK_BYTES = 1048576
MAX_IMAGE_BASE64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_AUDIO_CHUNK_BASE64_CHARS = 4 * ((MAX_AUDIO_CHUNK_BYTES + 2) // 3)
MAX_IMAGE_WIDTH = 320
MAX_IMAGE_HEIGHT = 240
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}
PYNQ_EXPRESSIONS = {
    "normal",
    "happy",
    "sad",
    "hungry",
    "sleepy",
    "thinking",
    "listening",
    "remind",
    "confused",
}
LEGACY_EXPRESSION_MAP = {
    "neutral": "normal",
    "joy": "happy",
    "smirk": "happy",
    "sadness": "sad",
    "anger": "confused",
    "disgust": "confused",
    "fear": "confused",
    "surprise": "confused",
}
EXPRESSION_TAG_PATTERN = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)\]")
PYNQ_COMMAND_BLOCK_PATTERN = re.compile(
    r"<PYNQ_COMMANDS>(.*?)</PYNQ_COMMANDS>",
    re.DOTALL,
)
PYNQ_COMMANDS = {
    "ui.switch_screen",
    "oled.display",
    "camera.capture",
    "time.speak_current",
    "environment.speak_current",
    "todo.manage",
    "pet.update_status",
    "tts.speak",
}
PYNQ_SCREEN_IDS = {
    "home_screen",
    "main_menu_screen",
    "pet_status_screen",
    "interaction_feedback_screen",
    "voice_interaction_screen",
    "todo_list_screen",
    "todo_detail_screen",
    "todo_confirm_screen",
    "reminder_popup_screen",
    "environment_screen",
    "camera_capture_screen",
    "settings_screen",
    "error_status_screen",
}
OLED_CONTENT_TYPES = {
    "text",
    "expression",
    "text_with_expression",
    "reminder",
    "error",
}
TODO_ACTIONS = {"create", "query", "update", "complete", "delete"}
TODO_STATUSES = {"pending", "completed", "reminded"}
PET_STATUS_DELTA_FIELDS = {
    "mood_delta",
    "satiety_delta",
    "energy_delta",
    "affinity_delta",
    "health_delta",
}


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
    audio_buffer: bytearray = field(default_factory=bytearray)

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
    upstream = create_upstream_adapter()

    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            session.transition_to(SessionState.CLOSING)
            await _close_upstream(upstream)
            return

        try:
            envelope = parse_envelope(raw)
        except ProtocolError as exc:
            if exc.__cause__ is not None and isinstance(exc.__cause__, JSONDecodeError):
                await websocket.close()
                await _close_upstream(upstream)
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
                await _close_upstream(upstream)
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

    try:
        result = await upstream.start_text_turn(envelope.payload.get("text", ""))
    except UpstreamBridgeError as exc:
        session.active_turn_id = None
        session.active_response_id = None
        session.transition_to(SessionState.IDLE)
        await websocket.send_json(
            build_error(
                ErrorCode.UPSTREAM_AGENT_ERROR,
                str(exc),
                retryable=True,
                request_id=envelope.request_id,
                session_id=session.session_id,
                field="upstream",
            )
        )
        return

    session.transition_to(SessionState.RESPONDING)
    segment_processor = ResponseSegmentProcessor(response_id)
    for segment in result.segments:
        text, behavior_payloads = segment_processor.process(segment)
        if text:
            await websocket.send_json(
                build_event(
                    "response.text",
                    {
                        "response_id": response_id,
                        "turn_id": turn_id,
                        "text": text,
                        "voice": segment.voice,
                    },
                    session_id=session.session_id,
                    request_id=envelope.request_id,
                )
            )
        for behavior_payload in behavior_payloads:
            await websocket.send_json(
                build_event(
                    "response.behavior",
                    behavior_payload,
                    session_id=session.session_id,
                    request_id=envelope.request_id,
                )
            )
    for behavior_payload in segment_processor.flush():
        await websocket.send_json(
            build_event(
                "response.behavior",
                behavior_payload,
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

    uses_upstream_vad = bool(getattr(upstream, "uses_upstream_vad", False))
    if not uses_upstream_vad and session.should_interrupt_on_audio():
        interrupted = await _send_interrupted_ack(
            websocket,
            session,
            envelope,
            upstream,
            reason="user_speaking_again",
        )
        if not interrupted:
            return

    session.last_audio_seq = seq
    session.audio_buffer.extend(audio_bytes)
    session.transition_to(SessionState.LISTENING)

    try:
        audio_result = await _stream_audio_chunk(upstream, audio_bytes)
    except UpstreamBridgeError as exc:
        await websocket.send_json(
            build_error(
                ErrorCode.UPSTREAM_AGENT_ERROR,
                str(exc),
                retryable=True,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                field="upstream",
            )
        )
        return

    if audio_result.interrupted and session.active_turn_id is not None:
        await _send_interrupted_ack(
            websocket,
            session,
            envelope,
            upstream,
            reason="user_speaking_again",
            notify_upstream=False,
        )
        return

    await _send_status(websocket, session, envelope.request_id, "audio_received")
    if audio_result.turn is not None:
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
        await _send_turn_result(
            websocket,
            session,
            envelope.request_id,
            audio_result.turn,
            trigger="voice",
        )


async def _send_turn_result(
    websocket: WebSocket,
    session: PetSession,
    request_id: str | None,
    result,
    *,
    trigger: str,
) -> None:
    turn_id = f"turn_{uuid4().hex}"
    response_id = f"rsp_{uuid4().hex}"
    session.active_turn_id = turn_id
    session.active_response_id = response_id
    session.transition_to(SessionState.PROCESSING)
    await websocket.send_json(
        build_event(
            "conversation.started",
            {"turn_id": turn_id, "trigger": trigger},
            session_id=session.session_id,
            request_id=request_id,
        )
    )
    await _send_status(websocket, session, request_id, "thinking")

    session.transition_to(SessionState.RESPONDING)
    segment_processor = ResponseSegmentProcessor(response_id)
    for segment in result.segments:
        text, behavior_payloads = segment_processor.process(segment)
        if text:
            await websocket.send_json(
                build_event(
                    "response.text",
                    {
                        "response_id": response_id,
                        "turn_id": turn_id,
                        "text": text,
                        "voice": segment.voice,
                    },
                    session_id=session.session_id,
                    request_id=request_id,
                )
            )
        for behavior_payload in behavior_payloads:
            await websocket.send_json(
                build_event(
                    "response.behavior",
                    behavior_payload,
                    session_id=session.session_id,
                    request_id=request_id,
                )
            )
    for behavior_payload in segment_processor.flush():
        await websocket.send_json(
            build_event(
                "response.behavior",
                behavior_payload,
                session_id=session.session_id,
                request_id=request_id,
            )
        )
    session.transition_to(SessionState.WAITING_CLIENT_PLAYBACK)
    await websocket.send_json(
        build_event(
            "response.complete",
            {"response_id": response_id, "turn_id": turn_id},
            session_id=session.session_id,
            request_id=request_id,
        )
    )
    await _send_status(
        websocket,
        session,
        request_id,
        "waiting_client_playback",
    )


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
    notify_upstream: bool = True,
) -> bool:
    turn_id = session.active_turn_id
    if turn_id is None:
        return False

    try:
        if notify_upstream:
            await upstream.interrupt_turn(turn_id, reason)
    except UpstreamBridgeError as exc:
        await websocket.send_json(
            build_error(
                ErrorCode.UPSTREAM_AGENT_ERROR,
                str(exc),
                retryable=True,
                request_id=envelope.request_id,
                session_id=session.session_id or envelope.session_id,
                turn_id=turn_id,
                field="upstream",
            )
        )
        return False

    session.active_turn_id = None
    session.active_response_id = None
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
    return True


async def _stream_audio_chunk(
    upstream: UpstreamAdapter,
    audio_bytes: bytes,
) -> AudioChunkResult:
    stream_audio_chunk = getattr(upstream, "stream_audio_chunk", None)
    if stream_audio_chunk is None:
        return AudioChunkResult()
    return await stream_audio_chunk(audio_bytes)


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


async def _close_upstream(upstream: UpstreamAdapter) -> None:
    close = getattr(upstream, "close", None)
    if close is not None:
        await close()


@dataclass
class ResponseSegmentProcessor:
    response_id: str
    command_buffer: str | None = None

    def process(self, segment: Any) -> tuple[str, list[dict[str, Any]]]:
        text, command_payloads = self._extract_pynq_command_blocks(segment.text)
        text, tagged_expressions = _extract_expression_tags(text)
        behavior_payloads = _behavior_payloads_from_actions(
            segment.actions,
            self.response_id,
            extra_expressions=tagged_expressions,
        )
        behavior_payloads.extend(command_payloads)
        return text, behavior_payloads

    def _extract_pynq_command_blocks(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        payloads: list[dict[str, Any]] = []
        output_parts: list[str] = []
        remaining = text
        saw_command_block = self.command_buffer is not None

        while remaining:
            if self.command_buffer is None:
                start = remaining.find("<PYNQ_COMMANDS>")
                if start < 0:
                    output_parts.append(remaining)
                    break
                saw_command_block = True
                output_parts.append(remaining[:start])
                remaining = remaining[start + len("<PYNQ_COMMANDS>") :]
                self.command_buffer = ""

            end = remaining.find("</PYNQ_COMMANDS>")
            if end < 0:
                self.command_buffer += remaining
                remaining = ""
                break

            self.command_buffer += remaining[:end]
            payloads.extend(
                _behavior_payloads_from_command_block(
                    self.command_buffer,
                    self.response_id,
                )
            )
            self.command_buffer = None
            remaining = remaining[end + len("</PYNQ_COMMANDS>") :]

        cleaned = "".join(output_parts)
        if saw_command_block:
            cleaned = " ".join(cleaned.split())
        return cleaned, payloads

    def flush(self) -> list[dict[str, Any]]:
        if self.command_buffer is None:
            return []
        payloads = _behavior_payloads_from_command_block(
            self.command_buffer,
            self.response_id,
        )
        self.command_buffer = None
        return payloads


def _behavior_payloads_from_command_block(
    block: str,
    response_id: str,
) -> list[dict[str, Any]]:
    try:
        commands = json.loads(block.strip())
    except JSONDecodeError:
        return []
    if not isinstance(commands, list):
        return []
    payloads: list[dict[str, Any]] = []
    for command in commands:
        payload = _behavior_payload_from_command(command, response_id)
        if payload is not None:
            payloads.append(payload)
    return payloads


def _extract_expression_tags(text: str) -> tuple[str, list[str]]:
    if EXPRESSION_TAG_PATTERN.search(text) is None:
        return text, []

    expressions: list[str] = []

    def replace_tag(match: re.Match[str]) -> str:
        expression = _canonical_pynq_expression(match.group(1))
        if expression is not None and expression not in expressions:
            expressions.append(expression)
        return ""

    cleaned = EXPRESSION_TAG_PATTERN.sub(replace_tag, text)
    cleaned = " ".join(cleaned.split())
    return cleaned, expressions


def _behavior_payloads_from_actions(
    actions: dict[str, Any] | None,
    response_id: str,
    *,
    extra_expressions: list[str] | None = None,
) -> list[dict[str, Any]]:
    seen_expressions: set[str] = set()
    payloads: list[dict[str, Any]] = []

    if not isinstance(actions, dict):
        actions = {}

    commands = actions.get("commands")
    if isinstance(commands, list):
        for command in commands:
            payload = _behavior_payload_from_command(command, response_id)
            if payload is not None:
                payloads.append(payload)

    payload = _behavior_payload_from_command(actions, response_id)
    if payload is not None:
        payloads.append(payload)

    expressions = actions.get("expressions")
    if isinstance(expressions, list):
        for expression in expressions:
            _append_expression_payload(payloads, seen_expressions, response_id, expression)
    expression = actions.get("expression")
    _append_expression_payload(payloads, seen_expressions, response_id, expression)

    if extra_expressions is not None:
        for expression in extra_expressions:
            _append_expression_payload(payloads, seen_expressions, response_id, expression)

    return payloads


def _append_expression_payload(
    payloads: list[dict[str, Any]],
    seen_expressions: set[str],
    response_id: str,
    expression: Any,
) -> None:
    expression = _canonical_pynq_expression(expression)
    if expression is None or expression in seen_expressions:
        return
    seen_expressions.add(expression)
    payloads.append(
        _make_behavior_payload(
            response_id,
            "oled.display",
            {
                "content_type": "expression",
                "expression": expression,
            },
        )
    )


def _canonical_pynq_expression(expression: Any) -> str | None:
    if not isinstance(expression, str):
        return None
    if expression in PYNQ_EXPRESSIONS:
        return expression
    return LEGACY_EXPRESSION_MAP.get(expression)


def _behavior_payload_from_command(
    command: Any,
    response_id: str,
) -> dict[str, Any] | None:
    if not isinstance(command, dict):
        return None
    command_name = command.get("command")
    if not isinstance(command_name, str) or command_name not in PYNQ_COMMANDS:
        return None
    args = command.get("args", {})
    if not isinstance(args, dict):
        return None
    args = _validated_command_args(command_name, args)
    if args is None:
        return None
    return _make_behavior_payload(response_id, command_name, args)


def _validated_command_args(command_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    args = dict(args)
    if command_name == "ui.switch_screen":
        return _validated_ui_switch_screen_args(args)
    if command_name == "oled.display":
        return _validated_oled_display_args(args)
    if command_name == "camera.capture":
        return {} if not args else None
    if command_name in {"time.speak_current", "environment.speak_current"}:
        return _validated_show_on_oled_args(args)
    if command_name == "todo.manage":
        return _validated_todo_manage_args(args)
    if command_name == "pet.update_status":
        return _validated_pet_update_status_args(args)
    if command_name == "tts.speak":
        return args
    return None


def _validated_ui_switch_screen_args(args: dict[str, Any]) -> dict[str, Any] | None:
    screen_id = args.get("screen_id")
    if not isinstance(screen_id, str) or screen_id not in PYNQ_SCREEN_IDS:
        return None
    validated: dict[str, Any] = {"screen_id": screen_id}
    reason = args.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return None
        validated["reason"] = reason
    return validated


def _validated_oled_display_args(args: dict[str, Any]) -> dict[str, Any] | None:
    content_type = args.get("content_type")
    if not isinstance(content_type, str) or content_type not in OLED_CONTENT_TYPES:
        return None
    validated: dict[str, Any] = {"content_type": content_type}
    text = args.get("text")
    if text is not None:
        if not isinstance(text, str):
            return None
        validated["text"] = text
    expression = args.get("expression")
    if expression is not None:
        expression = _canonical_pynq_expression(expression)
        if expression is None:
            return None
        validated["expression"] = expression
    duration_ms = args.get("duration_ms")
    if duration_ms is not None:
        if not _is_int(duration_ms):
            return None
        validated["duration_ms"] = duration_ms
    return validated


def _validated_show_on_oled_args(args: dict[str, Any]) -> dict[str, Any] | None:
    show_on_oled = args.get("show_on_oled")
    if show_on_oled is None:
        return {}
    if not isinstance(show_on_oled, bool):
        return None
    return {"show_on_oled": show_on_oled}


def _validated_todo_manage_args(args: dict[str, Any]) -> dict[str, Any] | None:
    action = args.get("action")
    if not isinstance(action, str) or action not in TODO_ACTIONS:
        return None
    validated: dict[str, Any] = {"action": action}
    for arg_name in ("todo_id", "title", "remind_time"):
        value = args.get(arg_name)
        if value is not None:
            if not isinstance(value, str):
                return None
            validated[arg_name] = value
    status = args.get("status")
    if status is not None:
        if not isinstance(status, str) or status not in TODO_STATUSES:
            return None
        validated["status"] = status
    return validated


def _validated_pet_update_status_args(args: dict[str, Any]) -> dict[str, Any] | None:
    validated: dict[str, Any] = {}
    for arg_name in PET_STATUS_DELTA_FIELDS:
        value = args.get(arg_name)
        if value is not None:
            if not _is_int(value):
                return None
            validated[arg_name] = value
    status_text = args.get("status_text")
    if status_text is not None:
        if not isinstance(status_text, str):
            return None
        validated["status_text"] = status_text
    return validated


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _make_behavior_payload(
    response_id: str,
    command: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    return {
        "response_id": response_id,
        "command_id": f"cmd_{uuid4().hex}",
        "command": command,
        "args": args,
    }
