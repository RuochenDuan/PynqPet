import json
import asyncio

import pytest

from pynq_pet_gateway.upstream import (
    AudioChunkResult,
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


class SendFailingOpenLlmWebSocket(FakeOpenLlmWebSocket):
    async def send(self, raw: str) -> None:
        raise RuntimeError("upstream connection closed")


async def _sleep_forever() -> None:
    import asyncio

    await asyncio.Event().wait()


def make_connect(fake: FakeOpenLlmWebSocket):
    async def connect(url: str):
        fake.url = url
        return fake

    return connect


async def wait_until(predicate, *, timeout_s: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Timed out waiting for condition")


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_sends_text_input_and_normalizes_full_text() -> None:
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "full-text", "text": "Connection established"},
            {"type": "set-model-and-conf", "client_uid": "client_001"},
            {"type": "full-text", "text": "Thinking..."},
            {"type": "full-text", "text": "你好，我在。"},
            {"type": "backend-synth-complete"},
            {"type": "force-new-message"},
            {"type": "control", "text": "conversation-chain-end"},
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
    assert fake.closed is False

    await adapter.close()

    assert fake.closed is True


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_sends_text_input_with_images() -> None:
    image = {
        "source": "camera",
        "data": "data:image/jpeg;base64,AAECAw==",
        "mime_type": "image/jpeg",
    }
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "full-text", "text": "我看到了图片。"},
            {"type": "backend-synth-complete"},
            {"type": "control", "text": "conversation-chain-end"},
        ]
    )
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    result = await adapter.start_text_turn("请看图", images=[image])

    assert fake.sent[0] == {
        "type": "text-input",
        "text": "请看图",
        "images": [image],
    }
    assert [segment.text for segment in result.segments] == ["我看到了图片。"]


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_returns_on_conversation_chain_end_without_tts() -> None:
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "control", "text": "conversation-chain-start"},
            {"type": "full-text", "text": "Thinking..."},
            {"type": "full-text", "text": "纯文本回复。"},
            {"type": "control", "text": "conversation-chain-end"},
        ]
    )
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    result = await adapter.start_text_turn("你好")

    assert fake.sent == [{"type": "text-input", "text": "你好", "images": []}]
    assert [segment.text for segment in result.segments] == ["纯文本回复。"]


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_wraps_send_failures() -> None:
    fake = SendFailingOpenLlmWebSocket([])
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    with pytest.raises(UpstreamBridgeError) as exc_info:
        await adapter.start_text_turn(
            "请看图",
            images=[
                {
                    "source": "camera",
                    "data": "data:image/jpeg;base64,AAECAw==",
                    "mime_type": "image/jpeg",
                }
            ],
        )

    assert "Open-LLM WebSocket send failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_reconnects_once_before_text_turn() -> None:
    stale_fake = SendFailingOpenLlmWebSocket([])
    fresh_fake = FakeOpenLlmWebSocket(
        [
            {"type": "full-text", "text": "我看到了图片。"},
            {"type": "backend-synth-complete"},
            {"type": "control", "text": "conversation-chain-end"},
        ]
    )
    connected = []

    async def connect(url: str):
        fake = stale_fake if not connected else fresh_fake
        connected.append(fake)
        return fake

    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=connect,
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
    )

    result = await adapter.start_text_turn(
        "请看图",
        images=[
            {
                "source": "camera",
                "data": "data:image/jpeg;base64,AAECAw==",
                "mime_type": "image/jpeg",
            }
        ],
    )

    assert connected == [stale_fake, fresh_fake]
    assert fresh_fake.sent[0]["type"] == "text-input"
    assert fresh_fake.sent[0]["images"][0]["source"] == "camera"
    assert [segment.text for segment in result.segments] == ["我看到了图片。"]


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
            {"type": "control", "text": "conversation-chain-end"},
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
    assert fake.closed is False

    await adapter.close()

    assert fake.closed is True


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_interrupt_uses_active_connection() -> None:
    active_fake = FakeOpenLlmWebSocket([])
    extra_fake = FakeOpenLlmWebSocket([])
    connected: list[FakeOpenLlmWebSocket] = []

    async def connect(url: str):
        fake = active_fake if not connected else extra_fake
        connected.append(fake)
        return fake

    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=connect,
        receive_timeout_s=0.05,
        initial_drain_timeout_s=0.01,
    )
    turn_task = asyncio.create_task(adapter.start_text_turn("你好"))
    await wait_until(
        lambda: active_fake.sent == [{"type": "text-input", "text": "你好", "images": []}]
    )

    await adapter.interrupt_turn("turn_001", "user_speaking_again")

    assert connected == [active_fake]
    assert active_fake.sent == [
        {"type": "text-input", "text": "你好", "images": []},
        {"type": "interrupt-signal", "text": ""},
    ]
    assert extra_fake.sent == []
    with pytest.raises(UpstreamBridgeError):
        await turn_task


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_sends_audio_data_and_audio_end() -> None:
    pcm_silence = b"\x00\x00\x00\x40"
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "full-text", "text": "我听到了。"},
            {"type": "backend-synth-complete"},
            {"type": "control", "text": "conversation-chain-end"},
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
async def test_open_llm_ws_adapter_streams_raw_audio_chunk_without_client_cutoff() -> None:
    pcm_silence = b"\x00\x00\x00\x40"
    fake = FakeOpenLlmWebSocket([])
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
        audio_control_timeout_s=0.01,
    )

    result = await adapter.stream_audio_chunk(pcm_silence)

    assert fake.sent == [{"type": "raw-audio-data", "audio": [0.0, 0.5]}]
    assert result == AudioChunkResult()


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_uses_olv_vad_control_to_trigger_audio_turn() -> None:
    pcm_silence = b"\x00\x00\x00\x40"
    fake = FakeOpenLlmWebSocket(
        [
            {"type": "control", "text": "mic-audio-end"},
            {"type": "full-text", "text": "我听到了。"},
            {"type": "backend-synth-complete"},
            {"type": "control", "text": "conversation-chain-end"},
        ]
    )
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
        audio_control_timeout_s=0.01,
    )

    result = await adapter.stream_audio_chunk(pcm_silence)

    assert fake.sent[0] == {"type": "raw-audio-data", "audio": [0.0, 0.5]}
    assert fake.sent[1] == {"type": "mic-audio-end"}
    assert fake.sent[2] == {"type": "frontend-playback-complete"}
    assert result.turn is not None
    assert [segment.text for segment in result.turn.segments] == ["我听到了。"]


@pytest.mark.asyncio
async def test_open_llm_ws_adapter_maps_olv_vad_interrupt_control() -> None:
    fake = FakeOpenLlmWebSocket([{"type": "control", "text": "interrupt"}])
    adapter = OpenLlmWebSocketAdapter(
        "ws://example.test/client-ws",
        connect=make_connect(fake),
        receive_timeout_s=0.01,
        initial_drain_timeout_s=0.01,
        audio_control_timeout_s=0.01,
    )

    result = await adapter.stream_audio_chunk(b"\x00\x00")

    assert fake.sent == [{"type": "raw-audio-data", "audio": [0.0]}]
    assert result.interrupted is True
    assert result.turn is None


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
