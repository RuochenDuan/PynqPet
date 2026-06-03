from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError
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


router = APIRouter()


@dataclass
class PetSession:
    session_id: str | None = None
    device_id: str | None = None
    config_id: str | None = None
    ready: bool = False


@router.websocket("/api/v1/pet/ws")
async def pet_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session = PetSession()

    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
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
            await websocket.send_json(
                build_event(
                    "heartbeat.ack",
                    {"ok": True},
                    session_id=session.session_id,
                    request_id=envelope.request_id,
                )
            )
        elif envelope.type == "text.input":
            await _handle_text_input(websocket, session, envelope)
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
    session.ready = True

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


async def _handle_text_input(websocket: WebSocket, session: PetSession, envelope) -> None:
    if not session.ready or session.session_id is None:
        await websocket.send_json(
            build_error(
                ErrorCode.SESSION_NOT_READY,
                "Session is not ready",
                retryable=False,
                request_id=envelope.request_id,
            )
        )
        return

    turn_id = f"turn_{uuid4().hex}"
    response_id = f"rsp_{uuid4().hex}"
    for event in (
        build_event(
            "conversation.started",
            {"turn_id": turn_id, "trigger": "text"},
            session_id=session.session_id,
            request_id=envelope.request_id,
        ),
        build_event(
            "status.update",
            {"stage": "thinking"},
            session_id=session.session_id,
            request_id=envelope.request_id,
        ),
        build_event(
            "response.text",
            {
                "response_id": response_id,
                "turn_id": turn_id,
                "text": "我收到你的消息了。",
                "voice": "normal",
            },
            session_id=session.session_id,
            request_id=envelope.request_id,
        ),
        build_event(
            "response.complete",
            {"response_id": response_id, "turn_id": turn_id},
            session_id=session.session_id,
            request_id=envelope.request_id,
        ),
    ):
        await websocket.send_json(event)
