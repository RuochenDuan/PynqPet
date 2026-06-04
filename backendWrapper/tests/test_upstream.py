import json

import pytest

from pynq_pet_gateway.upstream import (
    OpenLlmWebSocketAdapter,
    PlaceholderUpstreamAdapter,
    UpstreamBridgeError,
    create_upstream_adapter,
)


class FakeOpenLlmWebSocket:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self.messages:
            await _sleep_forever()
        return self.messages.pop(0)

    async def close(self) -> None:
        self.closed = True


async def _sleep_forever() -> None:
    import asyncio

    await asyncio.Event().wait()


def make_connect(fake: FakeOpenLlmWebSocket):
    async def connect(url: str):
        fake.url = url
        return fake

    return connect


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_sends_text_input_and_normalizes_full_text() -> None:
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "full-text", "text": "Connection established"},
            {"type": "set-model-and-conf", "client_uid": "client_001"},
            {"type": "full-text", "text": "Thinking..."},
            {"type": "full-text", "text": "你好，我在。"},
            {"type": "backend-synth-complete"},
        ]
    )
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    result = await adapter.start_text_turn("你好")

    assert fake.url == "ws://example.test/client-ws"
    assert fake.sent[0] == {"type": "text-input", "text": "你好", "images": []}
    assert fake.sent[1] == {"type": "frontend-playback-complete"}
    assert [segment.text for segment in result.segments] == ["你好，我在。"]
    assert fake.closed is True


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_maps_audio_display_text_without_audio_payload() -> None:
    fake = FakeOpenLlmWebSocket(
        [
            {
                "type": "audio",
                "audio": "SHOULD_NOT_LEAK",
                "volumes": [1.0],
                "display_text": {"text": "我看到了。"},
                "actions": {"expressions": ["happy"]},
            },
            {"type": "backend-synth-complete"},
        ]
    )
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    result = await adapter.start_text_turn("看一下")

    assert result.segments[0].text == "我看到了。"
    assert result.segments[0].actions == {"expressions": ["happy"]}
    assert "audio" not in result.segments[0].__dict__


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_sends_interrupt_signal() -> None:
    fake = FakeOpenLlmWebSocket([])
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    await adapter.interrupt_turn("turn_001", "user_speaking_again")

    assert fake.sent == [{"type": "interrupt-signal", "text": ""}]
    assert fake.closed is True


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_sends_audio_data_and_audio_end() -> None:
    pcm_silence = b"\x00\x00\x00\x40"
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "full-text", "text": "我听到了。"},
            {"type": "backend-synth-complete"},
        ]
    )
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    result = await adapter.start_audio_turn(pcm_silence)

    assert fake.sent[0]["type"] == "mic-audio-data"
    assert fake.sent[0]["audio"] == [0.0, 0.5]
    assert fake.sent[1] == {"type": "mic-audio-end"}
    assert fake.sent[2] == {"type": "frontend-playback-complete"}
    assert [segment.text for segment in result.segments] == ["我听到了。"]


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_raises_bridge_error_for_upstream_error() -> None:
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "error", "message": "agent failed"},
        ]
    )
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    with pytest.raises(UpstreamBridgeError) as exc_info:
        await adapter.start_text_turn("你好")

    assert "agent failed" in str(exc_info.value)


def test_create_upstream_adapter_uses_placeholder_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PYNQ_PET_UPSTREAM_MODE", raising=False)

    adapter = create_upstream_adapter()

    assert isinstance(adapter, PlaceholderUpstreamAdapter)


def test_create_upstream_adapter_uses_open_llm_ws_from_env(monkeypatch) -> None:
    monkeypatch.setenv("PYNQ_PET_UPSTREAM_MODE", "open_llm_ws")
    monkeypatch.setenv("PYNQ_PET_OPEN_LLM_WS_URL", "ws://127.0.0.1:9999/client-ws")

    adapter = create_upstream_adapter()

    assert isinstance(adapter, OpenLlmWebSocketAdapter)
    assert adapter.ws_url == "ws://127.0.0.1:9999/client-ws"
