"""Helpers to extract Penpot export_shape images for visual validation."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any


DEFAULT_DEBUG_DIR = Path(os.getenv("PENPOT_DEBUG_OUT", "/tmp/penpot_debug"))


def to_plain(value: Any) -> Any:
    """Convert MCP/LangChain objects into regular Python containers."""
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
    Extract PNG base64 from Penpot MCP export_shape output.

    The working Penpot MCP output observed in debugging is:

    [
      {
        "type": "image",
        "base64": "iVBORw0KGgo..."
      }
    ]
    """
    result = to_plain(result)
    items = result if isinstance(result, list) else [result]

    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")

        if item_type == "image":
            image_b64 = (
                item.get("base64")
                or item.get("data")
                or item.get("pngBase64")
            )

            if isinstance(image_b64, str) and image_b64.strip():
                return image_b64.strip()

    return None


def png_base64_to_data_url(image_b64: str) -> str:
    """Convert raw PNG base64 into a data URL accepted by Mistral Vision."""
    if image_b64.startswith("data:image/"):
        return image_b64

    return f"data:image/png;base64,{image_b64}"


def assert_valid_png_base64(image_b64: str) -> None:
    """Fail fast if the exported image is not a valid PNG."""
    try:
        blob = base64.b64decode(image_b64)
    except Exception as exc:
        raise ValueError("La imagen exportada no es base64 válido.") from exc

    if not blob.startswith(b"\x89PNG"):
        raise ValueError("La imagen exportada no parece ser un PNG válido.")


def save_debug_png_export(
    image_b64: str,
    *,
    output_dir: str | Path | None = None,
    stem: str = "validator_export",
) -> dict[str, str]:
    """
    Save PNG, raw base64 and data URL for debugging when enabled.

    This does not affect the workflow. It is only useful to inspect what the
    validator sent to the vision model.
    """
    target_dir = Path(output_dir) if output_dir else DEFAULT_DEBUG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    blob = base64.b64decode(image_b64)
    data_url = png_base64_to_data_url(image_b64)

    png_path = target_dir / f"{stem}.png"
    b64_path = target_dir / f"{stem}.png.base64.txt"
    data_url_path = target_dir / f"{stem}.png.data_url.txt"

    png_path.write_bytes(blob)
    b64_path.write_text(image_b64, encoding="utf-8")
    data_url_path.write_text(data_url, encoding="utf-8")

    return {
        "png": str(png_path),
        "base64": str(b64_path),
        "data_url": str(data_url_path),
    }
