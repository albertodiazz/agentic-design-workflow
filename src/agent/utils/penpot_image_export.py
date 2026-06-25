"""Helpers to extract image payloads returned by Penpot MCP export_shape."""

from __future__ import annotations

import base64
from typing import Any


def to_plain(value: Any) -> Any:
    """Convert LangChain/MCP objects into plain Python data."""
    if hasattr(value, "model_dump"):
        return value.model_dump()

    if isinstance(value, list):
        return [to_plain(item) for item in value]

    if isinstance(value, tuple):
        return [to_plain(item) for item in value]

    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}

    return value


def extract_png_base64_from_export_shape(result: Any) -> str | None:
    """
    Extract PNG base64 from Penpot MCP `export_shape`.

    The observed successful response is:
    [
      {
        "type": "image",
        "id": "...",
        "base64": "iVBORw0KGgo..."
      }
    ]
    """
    result = to_plain(result)
    items = result if isinstance(result, list) else [result]

    for item in items:
        if not isinstance(item, dict):
            continue

        if item.get("type") != "image":
            continue

        image_b64 = item.get("base64") or item.get("data") or item.get("pngBase64")

        if isinstance(image_b64, str) and image_b64.strip():
            return image_b64.strip()

    return None


def png_base64_to_data_url(image_b64: str) -> str:
    """Convert raw PNG base64 into a Mistral-compatible data URL."""
    if image_b64.startswith("data:image/"):
        return image_b64

    return f"data:image/png;base64,{image_b64}"


def assert_valid_png_base64(image_b64: str) -> None:
    """Fail early if the exported data is not actually a PNG."""
    blob = base64.b64decode(image_b64)

    if not blob.startswith(b"\x89PNG"):
        raise ValueError("La imagen exportada por Penpot no parece ser un PNG válido.")
