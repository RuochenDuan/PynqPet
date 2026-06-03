from copy import deepcopy
from typing import Any

from pynq_pet_gateway.protocol import DEFAULT_CONFIG_ID


DEFAULT_CONFIG_CREATED_AT = "2026-05-26T08:15:30.123Z"

_DEFAULT_CONFIG: dict[str, Any] = {
    "config_id": DEFAULT_CONFIG_ID,
    "name": "pet-default-cn",
    "version": 1,
    "status": "ready",
    "is_default": True,
    "created_at": DEFAULT_CONFIG_CREATED_AT,
    "source_type": "yaml",
    "validation": {"ok": True, "warnings": []},
}

_CONFIG_LIST_FIELDS = (
    "config_id",
    "name",
    "version",
    "status",
    "is_default",
    "created_at",
)


def list_configs() -> list[dict[str, Any]]:
    return [{field: deepcopy(_DEFAULT_CONFIG[field]) for field in _CONFIG_LIST_FIELDS}]


def get_config(config_id: str) -> dict[str, Any] | None:
    if config_id != DEFAULT_CONFIG_ID:
        return None
    return deepcopy(_DEFAULT_CONFIG)
