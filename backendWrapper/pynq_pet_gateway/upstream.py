from __future__ import annotations

import asyncio
import json
import os
import struct
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


OPEN_LLM_WS_MODE = "open_llm_ws"
UPSTREAM_MODE_ENV = "PYNQ_PET_UPSTREAM_MODE"
OPEN_LLM_WS_URL_ENV = "PYNQ_PET_OPEN_LLM_WS_URL"
DEFAULT_OPEN_LLM_WS_URL = "ws://127.0.0.1:12393/client-ws"
IGNORED_FULL_TEXTS = {"Connection established", "Thinking..."}


@dataclass(frozen=True)
class TextSegment:
    text: str
    voice: str = "normal"
    actions: dict[str, Any] | None = None


@dataclass(frozen=True)
class TextTurnResult:
    segments: list[TextSegment]


@dataclass(frozen=True)
class AudioChunkResult:
    interrupted: bool = False
    turn: TextTurnResult | None = None


class UpstreamBridgeError(Exception):
    pass


class UpstreamAdapter(Protocol):
    async def start_text_turn(self, text: str) -> TextTurnResult:
        """Start a text turn through the upstream agent boundary."""

    async def start_audio_turn(self, audio_pcm: bytes) -> TextTurnResult:
        """Start an audio turn through the upstream agent boundary."""

    async def stream_audio_chunk(self, audio_pcm: bytes) -> AudioChunkResult:
        """Stream an audio chunk to upstream VAD without client-side cutoff."""

    async def interrupt_turn(self, turn_id: str, reason: str) -> None:
        """Interrupt the currently active upstream turn."""

    async def close(self) -> None:
        """Release upstream resources for this pet session."""


class PlaceholderUpstreamAdapter:
    async def start_text_turn(self, text: str) -> TextTurnResult:
        return TextTurnResult(segments=[TextSegment(text="我收到你的消息了。")])

    async def start_audio_turn(self, audio_pcm: bytes) -> TextTurnResult:
        return TextTurnResult(segments=[TextSegment(text="我听到了。")])

    async def stream_audio_chunk(self, audio_pcm: bytes) -> AudioChunkResult:
        return AudioChunkResult()

    async def interrupt_turn(self, turn_id: str, reason: str) -> None:
        return None

    async def close(self) -> None:
        return None


ConnectFunc = Callable[[str], Awaitable[Any]]


class OpenLlmWebSocketAdapter:
    def __init__(
        self,
        ws_url: str = DEFAULT_OPEN_LLM_WS_URL,
        *,
        connect: ConnectFunc | None = None,
        receive_timeout_s: float = 60.0,
        initial_drain_timeout_s: float = 0.2,
        audio_control_timeout_s: float = 0.05,
    ) -> None:
        self.ws_url = ws_url
        self._connect = connect or _default_connect
        self._receive_timeout_s = receive_timeout_s
        self._initial_drain_timeout_s = initial_drain_timeout_s
        self._audio_control_timeout_s = audio_control_timeout_s
        self.uses_upstream_vad = True
        self._websocket: Any | None = None
        self._pending_messages: list[dict[str, Any]] = []

    async def start_text_turn(self, text: str) -> TextTurnResult:
        websocket = await self._ensure_websocket()
        await websocket.send(json.dumps({"type": "text-input", "text": text, "images": []}))
        return await self._collect_text_turn(websocket)

    async def start_audio_turn(self, audio_pcm: bytes) -> TextTurnResult:
        websocket = await self._ensure_websocket()
        await websocket.send(
            json.dumps(
                {
                    "type": "mic-audio-data",
                    "audio": _pcm_s16le_to_float_list(audio_pcm),
                }
            )
        )
        await websocket.send(json.dumps({"type": "mic-audio-end"}))
        return await self._collect_text_turn(websocket)

    async def stream_audio_chunk(self, audio_pcm: bytes) -> AudioChunkResult:
        websocket = await self._ensure_websocket()
        await websocket.send(
            json.dumps(
                {
                    "type": "raw-audio-data",
                    "audio": _pcm_s16le_to_float_list(audio_pcm),
                }
            )
        )
        try:
            message = await self._read_next_message(
                websocket,
                timeout=self._audio_control_timeout_s,
            )
        except TimeoutError:
            return AudioChunkResult()

        if message.get("type") == "control":
            control_text = message.get("text")
            if control_text == "interrupt":
                return AudioChunkResult(interrupted=True)
            if control_text == "mic-audio-end":
                await websocket.send(json.dumps({"type": "mic-audio-end"}))
                return AudioChunkResult(turn=await self._collect_text_turn(websocket))
            return AudioChunkResult()
        if message.get("type") == "error":
            raise UpstreamBridgeError(str(message.get("message") or "Open-LLM error"))
        if _is_turn_message(message):
            self._pending_messages.append(message)
            return AudioChunkResult(turn=await self._collect_text_turn(websocket))
        return AudioChunkResult()

    async def interrupt_turn(self, turn_id: str, reason: str) -> None:
        websocket = await self._ensure_websocket()
        await websocket.send(json.dumps({"type": "interrupt-signal", "text": ""}))

    async def close(self) -> None:
        websocket = self._websocket
        self._websocket = None
        self._pending_messages = []
        if websocket is not None:
            await _close_websocket(websocket)

    async def _ensure_websocket(self) -> Any:
        if self._websocket is not None:
            return self._websocket
        try:
            websocket = await self._connect(self.ws_url)
        except Exception as exc:
            raise UpstreamBridgeError(f"Failed to connect Open-LLM-VTuber: {exc}") from exc
        self._websocket = websocket
        self._pending_messages = await self._drain_initial_messages(websocket)
        return websocket

    async def _drain_initial_messages(self, websocket: Any) -> list[dict[str, Any]]:
        pending_messages: list[dict[str, Any]] = []
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=self._initial_drain_timeout_s,
                )
            except TimeoutError:
                return pending_messages
            message = _decode_message(raw)
            if _is_actionable_message(message):
                pending_messages.append(message)
                return pending_messages

    async def _read_next_message(self, websocket: Any, *, timeout: float) -> dict[str, Any]:
        if self._pending_messages:
            return self._pending_messages.pop(0)
        raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        return _decode_message(raw)

    async def _collect_text_turn(
        self,
        websocket: Any,
    ) -> TextTurnResult:
        segments: list[TextSegment] = []
        while True:
            if self._pending_messages:
                message = self._pending_messages.pop(0)
            else:
                try:
                    message = await self._read_next_message(
                        websocket,
                        timeout=self._receive_timeout_s,
                    )
                except TimeoutError as exc:
                    raise UpstreamBridgeError("Timed out waiting for Open-LLM response") from exc
            message_type = message.get("type")
            if message_type == "backend-synth-complete":
                await websocket.send(json.dumps({"type": "frontend-playback-complete"}))
                return TextTurnResult(segments=segments)
            if message_type == "error":
                raise UpstreamBridgeError(str(message.get("message") or "Open-LLM error"))

            segment = _normalize_text_segment(message)
            if segment is not None:
                segments.append(segment)


async def _default_connect(ws_url: str) -> Any:
    import websockets

    return await websockets.connect(ws_url)


def create_upstream_adapter() -> UpstreamAdapter:
    mode = os.getenv(UPSTREAM_MODE_ENV, "").strip().lower()
    if mode == OPEN_LLM_WS_MODE:
        return OpenLlmWebSocketAdapter(
            os.getenv(OPEN_LLM_WS_URL_ENV, DEFAULT_OPEN_LLM_WS_URL)
        )
    return PlaceholderUpstreamAdapter()


def _decode_message(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpstreamBridgeError("Open-LLM returned invalid JSON") from exc
    if not isinstance(message, dict):
        raise UpstreamBridgeError("Open-LLM returned a non-object message")
    return message


def _is_turn_message(message: dict[str, Any]) -> bool:
    message_type = message.get("type")
    if message_type in {"backend-synth-complete", "error"}:
        return True
    return _normalize_text_segment(message) is not None


def _is_actionable_message(message: dict[str, Any]) -> bool:
    return message.get("type") == "control" or _is_turn_message(message)


def _normalize_text_segment(message: dict[str, Any]) -> TextSegment | None:
    message_type = message.get("type")
    if message_type == "full-text":
        text = message.get("text")
        if isinstance(text, str) and text and text not in IGNORED_FULL_TEXTS:
            return TextSegment(text=text)
        return None
    if message_type == "audio":
        display_text = message.get("display_text")
        if isinstance(display_text, dict):
            text = display_text.get("text")
            if isinstance(text, str) and text:
                actions = message.get("actions")
                return TextSegment(
                    text=text,
                    actions=actions if isinstance(actions, dict) else None,
                )
    return None


async def _close_websocket(websocket: Any) -> None:
    close = getattr(websocket, "close", None)
    if close is None:
        return
    result = close()
    if hasattr(result, "__await__"):
        await result


def _pcm_s16le_to_float_list(audio_pcm: bytes) -> list[float]:
    if len(audio_pcm) % 2 != 0:
        raise UpstreamBridgeError("PCM s16le audio length must be even")
    return [
        sample / 32768.0
        for (sample,) in struct.iter_unpack("<h", audio_pcm)
    ]
