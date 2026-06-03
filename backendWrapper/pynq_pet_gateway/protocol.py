from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


PROTOCOL_VERSION = "v1"
SERVICE_NAME = "pet-gateway"
DEFAULT_CONFIG_ID = "cfg_pet_default_cn"


class ErrorCode(StrEnum):
    INVALID_MESSAGE = "INVALID_MESSAGE"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    INVALID_CONFIG_ID = "INVALID_CONFIG_ID"
    SESSION_NOT_READY = "SESSION_NOT_READY"
    TURN_ALREADY_ACTIVE = "TURN_ALREADY_ACTIVE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ProtocolError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.field = field


class ClientEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    version: str
    payload: dict[str, Any]
    request_id: str | None = None
    session_id: str | None = None
    timestamp: str | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def payload_must_be_object(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("payload must be an object")
        return value


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_event(
    event_type: str,
    payload: dict,
    session_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "version": PROTOCOL_VERSION,
        "timestamp": utc_now_iso(),
        "payload": payload,
    }
    if session_id is not None:
        event["session_id"] = session_id
    if request_id is not None:
        event["request_id"] = request_id
    return event


def build_error(
    code: str,
    message: str,
    retryable: bool,
    request_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": str(code),
        "message": message,
        "retryable": retryable,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    if turn_id is not None:
        payload["turn_id"] = turn_id
    if field is not None:
        payload["field"] = field

    return build_event(
        "error",
        payload,
        session_id=session_id,
        request_id=request_id,
    )


def parse_envelope(raw: str) -> ClientEnvelope:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            ErrorCode.INVALID_MESSAGE,
            "Message must be valid JSON",
        ) from exc

    if not isinstance(data, dict):
        raise ProtocolError(
            ErrorCode.INVALID_MESSAGE,
            "Message must be a JSON object",
        )

    request_id = _optional_string(data.get("request_id"))
    session_id = _optional_string(data.get("session_id"))

    for field in ("type", "version", "payload"):
        if field not in data:
            raise ProtocolError(
                ErrorCode.INVALID_MESSAGE,
                f"Missing required field: {field}",
                request_id=request_id,
                session_id=session_id,
                field=field,
            )

    if data.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED_VERSION,
            "Unsupported protocol version",
            request_id=request_id,
            session_id=session_id,
            field="version",
        )

    try:
        return ClientEnvelope.model_validate(data)
    except ValidationError as exc:
        field = _first_validation_field(exc)
        raise ProtocolError(
            ErrorCode.INVALID_MESSAGE,
            "Invalid message envelope",
            request_id=request_id,
            session_id=session_id,
            field=field,
        ) from exc


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _first_validation_field(exc: ValidationError) -> str | None:
    errors = exc.errors()
    if not errors:
        return None
    location = errors[0].get("loc", ())
    if not location:
        return None
    return str(location[0])
