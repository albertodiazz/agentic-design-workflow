import asyncio
import base64
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient


OUT_DIR = Path("/tmp/penpot_debug")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def dump(value: Any) -> str:
    return json.dumps(to_plain(value), ensure_ascii=False, indent=2, default=str)


def to_plain(value: Any) -> Any:
    """
    Convierte objetos de LangChain / MCP a estructuras serializables.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump()

    if isinstance(value, list):
        return [to_plain(item) for item in value]

    if isinstance(value, tuple):
        return [to_plain(item) for item in value]

    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}

    return value


def save_json(name: str, value: Any) -> Path:
    path = OUT_DIR / f"{name}.json"
    path.write_text(dump(value), encoding="utf-8")
    print(f"Guardado: {path}")
    return path


def get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def parse_json_from_mcp(value: Any) -> Any:
    """
    MCP suele devolver listas de bloques:
    [
      {
        "type": "text",
        "text": "{...json...}"
      }
    ]

    Esta función extrae y parsea ese JSON si existe.
    """
    value = to_plain(value)

    if isinstance(value, list):
        for item in value:
            parsed = parse_json_from_mcp(item)
            if parsed is not None:
                return parsed
        return value

    if isinstance(value, dict):
        text = value.get("text")

        if isinstance(text, str):
            stripped = text.strip()

            try:
                return json.loads(stripped)
            except Exception:
                return text

        return value

    if isinstance(value, str):
        stripped = value.strip()

        try:
            return json.loads(stripped)
        except Exception:
            return value

    return value


def unwrap_execute_code_result(value: Any) -> Any:
    """
    execute_code suele devolver algo así:

    {
      "result": {
        "selectionCount": 1,
        "selection": [...]
      },
      "log": ""
    }

    El bug era que el script buscaba selection directamente arriba,
    pero realmente estaba dentro de result.
    """
    if isinstance(value, dict) and "result" in value:
        inner = value["result"]

        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except Exception:
                return inner

        return inner

    return value


def looks_base64(text: str) -> bool:
    clean = text.strip()

    if len(clean) < 50:
        return False

    return re.fullmatch(r"[A-Za-z0-9+/=\s]+", clean) is not None


def extract_export_bytes(result: Any) -> tuple[bytes | None, str | None, str]:
    """
    Intenta extraer bytes de imagen desde varias formas posibles de respuesta MCP.

    Devuelve:
      blob, mime, kind
    """
    result = to_plain(result)
    items = result if isinstance(result, list) else [result]

    for item in items:
        item_type = get_field(item, "type")
        mime = (
            get_field(item, "mimeType")
            or get_field(item, "mime_type")
            or get_field(item, "mime")
        )

        # IMPORTANTE:
        # Penpot MCP está devolviendo la imagen así:
        # {
        #   "type": "image",
        #   "base64": "iVBORw0KGgo..."
        # }
        data = (
            get_field(item, "base64")
            or get_field(item, "data")
            or get_field(item, "pngBase64")
        )

        text = get_field(item, "text")

        # Caso real de tu output:
        # {"type": "image", "base64": "..."}
        if item_type == "image" and isinstance(data, str):
            try:
                blob = base64.b64decode(data)

                if blob.startswith(b"\x89PNG"):
                    return blob, mime or "image/png", "image-content-base64-png"

                if blob.lstrip().startswith(b"<svg"):
                    return blob, mime or "image/svg+xml", "image-content-base64-svg"

                return blob, mime or "application/octet-stream", "image-content-base64"
            except Exception:
                pass

        # SVG como texto
        if isinstance(text, str) and "<svg" in text:
            return text.encode("utf-8"), "image/svg+xml", "text-svg"

        # data:image/png;base64,...
        if isinstance(text, str) and text.startswith("data:image/"):
            try:
                header, b64 = text.split(",", 1)
                detected_mime = header.split(";")[0].replace("data:", "")
                return base64.b64decode(b64), detected_mime, "text-data-url"
            except Exception:
                pass

        # Base64 crudo como texto
        if isinstance(text, str) and looks_base64(text):
            try:
                blob = base64.b64decode(text)

                if blob.startswith(b"\x89PNG"):
                    return blob, "image/png", "text-raw-base64-png"

                if blob.lstrip().startswith(b"<svg"):
                    return blob, "image/svg+xml", "text-raw-base64-svg"

                return blob, "application/octet-stream", "text-raw-base64"
            except Exception:
                pass

        # Respuesta directa como string
        if isinstance(item, str):
            if "<svg" in item:
                return item.encode("utf-8"), "image/svg+xml", "direct-svg"

            if item.startswith("data:image/"):
                try:
                    header, b64 = item.split(",", 1)
                    detected_mime = header.split(";")[0].replace("data:", "")
                    return base64.b64decode(b64), detected_mime, "direct-data-url"
                except Exception:
                    pass

            if looks_base64(item):
                try:
                    blob = base64.b64decode(item)

                    if blob.startswith(b"\x89PNG"):
                        return blob, "image/png", "direct-raw-base64-png"

                    if blob.lstrip().startswith(b"<svg"):
                        return blob, "image/svg+xml", "direct-raw-base64-svg"

                    return blob, "application/octet-stream", "direct-raw-base64"
                except Exception:
                    pass

    return None, None, "no-export-found"


async def safe_invoke(tool: Any, args: dict[str, Any], name: str) -> Any:
    print(f"\n--- {name} ---")
    print("ARGS:")
    print(dump(args))

    try:
        result = await tool.ainvoke(args)
        print("TYPE:", type(result))
        print(dump(result)[:4000])
        save_json(name, result)
        return result

    except Exception as exc:
        error_payload = {
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print("ERROR:")
        print(dump(error_payload))
        save_json(name, error_payload)
        return error_payload


def save_export_files(name: str, result: Any) -> None:
    blob, mime, kind = extract_export_bytes(result)

    print(f"EXPORT KIND: {kind}")
    print(f"MIME: {mime}")

    if not blob:
        print("No pude extraer bytes de imagen de esta respuesta.")
        return

    if mime == "image/png":
        ext = "png"
    elif mime == "image/svg+xml":
        ext = "svg"
    else:
        ext = "bin"

    image_path = OUT_DIR / f"{name}.{ext}"
    image_path.write_bytes(blob)
    print(f"Imagen guardada: {image_path}")

    b64 = base64.b64encode(blob).decode("ascii")

    b64_path = OUT_DIR / f"{name}.{ext}.base64.txt"
    b64_path.write_text(b64, encoding="utf-8")
    print(f"Base64 guardado: {b64_path}")

    data_url_path = OUT_DIR / f"{name}.{ext}.data_url.txt"
    data_url_path.write_text(f"data:{mime};base64,{b64}", encoding="utf-8")
    print(f"Data URL guardada: {data_url_path}")


async def main() -> None:
    load_dotenv()

    penpot_mcp_url = os.getenv("PENPOT_MCP_KEY")

    if not penpot_mcp_url:
        raise RuntimeError("Falta PENPOT_MCP_KEY en el entorno o en .env")

    client = MultiServerMCPClient(
        {
            "penpot": {
                "transport": "http",
                "url": penpot_mcp_url,
            }
        }
    )

    tools = await client.get_tools()
    tool_by_name = {tool.name: tool for tool in tools}

    print("TOOLS:")
    for tool_name in sorted(tool_by_name.keys()):
        print(f"- {tool_name}")

    save_json("tools", [{"name": tool.name, "args_schema": getattr(tool, "args_schema", None)} for tool in tools])

    required_tools = [
        "high_level_overview",
        "penpot_api_info",
        "export_shape",
    ]

    missing_tools = [name for name in required_tools if name not in tool_by_name]

    if missing_tools:
        raise RuntimeError(f"Faltan tools requeridas: {missing_tools}")

    high_level_overview = tool_by_name["high_level_overview"]
    penpot_api_info = tool_by_name["penpot_api_info"]
    export_shape = tool_by_name["export_shape"]
    execute_code = tool_by_name.get("execute_code")

    await safe_invoke(
        high_level_overview,
        {},
        "01_high_level_overview",
    )

    await safe_invoke(
        penpot_api_info,
        {"type": "Penpot"},
        "02_penpot_api_info_penpot",
    )

    await safe_invoke(
        penpot_api_info,
        {"type": "ShapeBase"},
        "03_penpot_api_info_shapebase",
    )

    await safe_invoke(
        penpot_api_info,
        {"type": "Export"},
        "04_penpot_api_info_export",
    )

    shape_id: str | None = None
    shape_name: str | None = None
    shape_type: str | None = None

    if execute_code:
        inspect_selection_code = """
const selection = penpot.selection || [];

const selected = selection.map(shape => ({
  id: shape.id,
  name: shape.name,
  type: shape.type,
  x: shape.x,
  y: shape.y,
  width: shape.width,
  height: shape.height,
  visible: shape.visible,
  hidden: shape.hidden,
  childrenCount: shape.children ? shape.children.length : 0,
  hasExport: typeof shape.export === "function"
}));

let boards = [];

try {
  boards = penpotUtils.findShapes(
    shape => shape.type === "board",
    penpot.root
  ).map(shape => ({
    id: shape.id,
    name: shape.name,
    type: shape.type,
    x: shape.x,
    y: shape.y,
    width: shape.width,
    height: shape.height,
    visible: shape.visible,
    hidden: shape.hidden,
    childrenCount: shape.children ? shape.children.length : 0,
    hasExport: typeof shape.export === "function"
  }));
} catch (error) {
  boards = [];
}

return {
  penpotVersion: penpot.version,
  currentPage: penpot.currentPage
    ? {
        id: penpot.currentPage.id,
        name: penpot.currentPage.name
      }
    : null,
  selectionCount: selection.length,
  selection: selected,
  boards
};
""".strip()

        selection_result = await safe_invoke(
            execute_code,
            {"code": inspect_selection_code},
            "05_inspect_selection",
        )

        selection_data = unwrap_execute_code_result(parse_json_from_mcp(selection_result))

        print("\nSELECTION DATA PARSED:")
        print(dump(selection_data))

        if isinstance(selection_data, dict):
            selection = selection_data.get("selection") or []

            if isinstance(selection, list) and selection:
                first_selection = selection[0]

                if isinstance(first_selection, dict):
                    shape_id = first_selection.get("id")
                    shape_name = first_selection.get("name")
                    shape_type = first_selection.get("type")

        if shape_id:
            print("\nShape seleccionado real:")
            print(f"- id: {shape_id}")
            print(f"- name: {shape_name}")
            print(f"- type: {shape_type}")
        else:
            print("\nNo encontré shape seleccionado en selection_data.")
            print("Esto no bloquea el test: voy a probar exportar page igualmente.")

    else:
        print("\nNo existe execute_code. Voy a probar export_shape directamente.")

    # Test 1: exportar la página completa.
    # No depende de selección.
    page_png_result = await safe_invoke(
        export_shape,
        {
            "shapeId": "page",
            "format": "png",
            "mode": "shape",
        },
        "06_export_page_png",
    )
    save_export_files("06_export_page_png", page_png_result)

    page_svg_result = await safe_invoke(
        export_shape,
        {
            "shapeId": "page",
            "format": "svg",
            "mode": "shape",
        },
        "07_export_page_svg",
    )
    save_export_files("07_export_page_svg", page_svg_result)

    # Test 2: exportar la selección usando el alias "selection".
    # Esto requiere que tengas algo seleccionado en Penpot.
    selection_png_result = await safe_invoke(
        export_shape,
        {
            "shapeId": "selection",
            "format": "png",
            "mode": "shape",
        },
        "08_export_selection_png",
    )
    save_export_files("08_export_selection_png", selection_png_result)

    selection_svg_result = await safe_invoke(
        export_shape,
        {
            "shapeId": "selection",
            "format": "svg",
            "mode": "shape",
        },
        "09_export_selection_svg",
    )
    save_export_files("09_export_selection_svg", selection_svg_result)

    # Test 3: exportar por ID real detectado desde penpot.selection.
    # Este es el test más importante para tu caso.
    if shape_id:
        real_id_png_result = await safe_invoke(
            export_shape,
            {
                "shapeId": shape_id,
                "format": "png",
                "mode": "shape",
            },
            "10_export_real_id_png",
        )
        save_export_files("10_export_real_id_png", real_id_png_result)

        real_id_svg_result = await safe_invoke(
            export_shape,
            {
                "shapeId": shape_id,
                "format": "svg",
                "mode": "shape",
            },
            "11_export_real_id_svg",
        )
        save_export_files("11_export_real_id_svg", real_id_svg_result)

    print("\nListo.")
    print(f"Revisa los archivos generados en: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
