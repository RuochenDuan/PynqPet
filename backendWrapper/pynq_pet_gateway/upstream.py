from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TextTurnResult:
    text: str
    voice: str = "normal"


class UpstreamAdapter(Protocol):
    async def start_text_turn(self, text: str) -> TextTurnResult:
        """Start a text turn through the upstream agent boundary."""

    async def interrupt_turn(self, turn_id: str, reason: str) -> None:
        """Interrupt the currently active upstream turn."""


class PlaceholderUpstreamAdapter:
    async def start_text_turn(self, text: str) -> TextTurnResult:
        return TextTurnResult(text="我收到你的消息了。")

    async def interrupt_turn(self, turn_id: str, reason: str) -> None:
        return None
