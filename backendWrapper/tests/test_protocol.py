import json
import re

import pytest

from pynq_pet_gateway.protocol import (
    DEFAULT_CONFIG_ID,
    ErrorCode,
    ProtocolError,
    build_error,
    build_event,
    parse_envelope,
    utc_now_iso,
)


def test_utc_now_iso_returns_utc_timestamp_with_z_suffix() -> None:
    timestamp = utc_now_iso()

    assert timestamp.endswith("Z")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3,6}Z",
        timestamp,
    )


def test_build_event_uses_v1_envelope_and_optional_identifiers() -> None:
    event = build_event(
        "session.ready",
        {"config_id": DEFAULT_CONFIG_ID},
        session_id="ses_001",
        request_id="req_001",
    )

    assert event["type"] == "session.ready"
    assert event["version"] == "v1"
    assert event["session_id"] == "ses_001"
    assert event["request_id"] == "req_001"
    assert event["payload"] == {"config_id": DEFAULT_CONFIG_ID}
    assert event["timestamp"].endswith("Z")


def test_build_event_omits_absent_optional_identifiers() -> None:
    event = build_event("heartbeat.ack", {"ok": True})

    assert "session_id" not in event
    assert "request_id" not in event


def test_build_error_matches_protocol_error_payload_shape() -> None:
    error = build_error(
        ErrorCode.INVALID_CONFIG_ID,
        "Selected config does not exist",
        retryable=False,
        request_id="req_001",
        session_id="ses_001",
        turn_id="turn_001",
        field="config_id",
    )

    assert error["type"] == "error"
    assert error["version"] == "v1"
    assert error["session_id"] == "ses_001"
    assert error["request_id"] == "req_001"
    assert error["payload"] == {
        "code": "INVALID_CONFIG_ID",
        "message": "Selected config does not exist",
        "retryable": False,
        "request_id": "req_001",
        "turn_id": "turn_001",
        "field": "config_id",
    }


def test_parse_envelope_accepts_valid_client_message_and_ignores_unknown_top_level() -> None:
    envelope = parse_envelope(
        json.dumps(
            {
                "type": "text.input",
                "version": "v1",
                "request_id": "req_001",
                "session_id": "ses_001",
                "timestamp": "2026-05-26T08:15:30.123Z",
                "payload": {"text": "hello"},
                "extra": "ignored",
            }
        )
    )

    assert envelope.type == "text.input"
    assert envelope.version == "v1"
    assert envelope.request_id == "req_001"
    assert envelope.session_id == "ses_001"
    assert envelope.payload == {"text": "hello"}


def test_parse_envelope_rejects_invalid_json() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_envelope("{")

    assert exc_info.value.code == ErrorCode.INVALID_MESSAGE
    assert exc_info.value.field is None


def test_parse_envelope_requires_json_object() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_envelope("[]")

    assert exc_info.value.code == ErrorCode.INVALID_MESSAGE
    assert exc_info.value.field is None


def test_parse_envelope_requires_top_level_fields() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_envelope(json.dumps({"type": "heartbeat", "version": "v1"}))

    assert exc_info.value.code == ErrorCode.INVALID_MESSAGE
    assert exc_info.value.field == "payload"


def test_parse_envelope_rejects_unsupported_version_with_request_context() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_envelope(
            json.dumps(
                {
                    "type": "heartbeat",
                    "version": "v2",
                    "request_id": "req_001",
                    "session_id": "ses_001",
                    "payload": {},
                }
            )
        )

    error = exc_info.value
    assert error.code == ErrorCode.UNSUPPORTED_VERSION
    assert error.request_id == "req_001"
    assert error.session_id == "ses_001"
    assert error.field == "version"


def test_parse_envelope_requires_payload_object() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_envelope(
            json.dumps(
                {
                    "type": "heartbeat",
                    "version": "v1",
                    "payload": [],
                    "request_id": "req_001",
                }
            )
        )

    assert exc_info.value.code == ErrorCode.INVALID_MESSAGE
    assert exc_info.value.request_id == "req_001"
    assert exc_info.value.field == "payload"
