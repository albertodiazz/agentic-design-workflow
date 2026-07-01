"""Google Stitch MCP client for DVCP import-only HTML flows.

This module imports an existing Stitch screen. It does not generate, edit, or
create Stitch designs. After `get_screen` succeeds, it downloads the screen's
`htmlCode.downloadUrl` so the Penpot importer can build an editable UI from the
actual HTML instead of from Stitch metadata.

Required selection can come from graph input or environment variables:
- stitch_project_id / STITCH_PROJECT_ID
- stitch_screen_id / STITCH_SCREEN_ID

Important API normalization discovered from Stitch MCP schemas:
- list_screens uses {"projectId": "143..."} without the `projects/` prefix.
- get_screen works reliably with {"name": "projects/{project}/screens/{screen}"}.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

DEFAULT_STITCH_ENDPOINT = "https://stitch.googleapis.com/mcp"
DEFAULT_MCP_PROTOCOL_VERSION = "2025-06-18"


@dataclass
class StitchRpcResult:
    ok: bool
    status: Optional[int]
    elapsed_ms: int
    session_id: Optional[str]
    payload: Optional[Dict[str, Any]]
    raw_preview: str
    error: Optional[str] = None


@dataclass
class DownloadResult:
    ok: bool
    status: Optional[int]
    elapsed_ms: int
    url: str
    text: str = ""
    content_type: str = ""
    bytes_read: int = 0
    error: Optional[str] = None


@dataclass
class BinaryDownloadResult:
    ok: bool
    status: Optional[int]
    elapsed_ms: int
    url: str
    content_type: str = ""
    bytes_read: int = 0
    base64_data: str = ""
    error: Optional[str] = None


def redact_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def parse_sse_or_json(raw: bytes, content_type: str) -> Optional[Dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None

    if "application/json" in content_type or text.startswith("{") or text.startswith("["):
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}

    data_chunks: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            value = line[len("data:") :].strip()
            if value and value != "[DONE]":
                data_chunks.append(value)

    for candidate in reversed(data_chunks):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
            return {"result": parsed}
        except json.JSONDecodeError:
            continue

    raise ValueError("Could not parse MCP response as JSON/SSE")


def rpc_post(
    endpoint: str,
    api_key: str,
    payload: Dict[str, Any],
    *,
    timeout: int,
    session_id: Optional[str] = None,
    protocol_version: str = DEFAULT_MCP_PROTOCOL_VERSION,
) -> StitchRpcResult:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol_version,
        "X-Goog-Api-Key": api_key,
        "User-Agent": "dvcp-stitch-html-import/0.3",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    started = time.perf_counter()
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            content_type = resp.headers.get("Content-Type", "")
            next_session = resp.headers.get("Mcp-Session-Id") or session_id
            parsed = parse_sse_or_json(raw, content_type)
            return StitchRpcResult(
                ok=True,
                status=resp.status,
                elapsed_ms=elapsed_ms,
                session_id=next_session,
                payload=parsed,
                raw_preview=raw.decode("utf-8", errors="replace")[:1200],
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return StitchRpcResult(
            ok=False,
            status=exc.code,
            elapsed_ms=elapsed_ms,
            session_id=session_id,
            payload=None,
            raw_preview=raw.decode("utf-8", errors="replace")[:2000],
            error=f"HTTP {exc.code}: {exc.reason}",
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return StitchRpcResult(
            ok=False,
            status=None,
            elapsed_ms=elapsed_ms,
            session_id=session_id,
            payload=None,
            raw_preview="",
            error=f"{type(exc).__name__}: {exc}",
        )


def download_text_url(url: str, *, timeout: int, max_bytes: int) -> DownloadResult:
    started = time.perf_counter()
    if not url:
        return DownloadResult(ok=False, status=None, elapsed_ms=0, url=url, error="missing_url")

    headers = {
        "Accept": "text/html, text/plain, application/xhtml+xml, */*",
        "User-Agent": "dvcp-stitch-html-import/0.3",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            truncated = len(raw) > max_bytes
            if truncated:
                raw = raw[:max_bytes]
            content_type = resp.headers.get("Content-Type", "")
            # Stitch HTML exports are UTF-8 in practice; fall back safely.
            text = raw.decode("utf-8", errors="replace")
            if truncated:
                text += "\n<!-- DVCP_HTML_TRUNCATED -->"
            return DownloadResult(
                ok=True,
                status=getattr(resp, "status", None),
                elapsed_ms=elapsed_ms,
                url=url,
                text=text,
                content_type=content_type,
                bytes_read=len(raw),
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return DownloadResult(
            ok=False,
            status=exc.code,
            elapsed_ms=elapsed_ms,
            url=url,
            text=raw.decode("utf-8", errors="replace")[:2000],
            error=f"HTTP {exc.code}: {exc.reason}",
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return DownloadResult(ok=False, status=None, elapsed_ms=elapsed_ms, url=url, error=f"{type(exc).__name__}: {exc}")


def download_binary_url(url: str, *, timeout: int, max_bytes: int) -> BinaryDownloadResult:
    """Download a binary asset, used mainly for Stitch screenshots sent to vision LLMs."""
    started = time.perf_counter()
    if not url:
        return BinaryDownloadResult(ok=False, status=None, elapsed_ms=0, url=url, error="missing_url")

    headers = {
        "Accept": "image/png,image/jpeg,image/webp,image/*,*/*",
        "User-Agent": "dvcp-stitch-html-import/0.4",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if len(raw) > max_bytes:
                return BinaryDownloadResult(
                    ok=False,
                    status=getattr(resp, "status", None),
                    elapsed_ms=elapsed_ms,
                    url=url,
                    content_type=resp.headers.get("Content-Type", ""),
                    bytes_read=len(raw),
                    error=f"binary_download_too_large>{max_bytes}",
                )
            content_type = resp.headers.get("Content-Type", "") or "image/png"
            return BinaryDownloadResult(
                ok=True,
                status=getattr(resp, "status", None),
                elapsed_ms=elapsed_ms,
                url=url,
                content_type=content_type.split(";", 1)[0].strip() or "image/png",
                bytes_read=len(raw),
                base64_data=base64.b64encode(raw).decode("ascii"),
            )
    except urllib.error.HTTPError as exc:
        _ = exc.read() or b""
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return BinaryDownloadResult(ok=False, status=exc.code, elapsed_ms=elapsed_ms, url=url, error=f"HTTP {exc.code}: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return BinaryDownloadResult(ok=False, status=None, elapsed_ms=elapsed_ms, url=url, error=f"{type(exc).__name__}: {exc}")


def build_binary_download_record(download: BinaryDownloadResult) -> dict[str, Any]:
    record = {
        "ok": download.ok,
        "status": download.status,
        "elapsed_ms": download.elapsed_ms,
        "url": download.url,
        "content_type": download.content_type,
        "bytes_read": download.bytes_read,
        "error": download.error,
    }
    if download.ok and download.base64_data:
        record["base64"] = download.base64_data
        record["data_url"] = f"{download.content_type or 'image/png'};base64,{download.base64_data}"
    return record


class StitchMCPError(RuntimeError):
    pass


class StitchMCPClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: Optional[int] = None,
        protocol_version: Optional[str] = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("STITCH_API_KEY", "")).strip()
        self.endpoint = endpoint or os.getenv("STITCH_MCP_ENDPOINT", DEFAULT_STITCH_ENDPOINT)
        self.timeout = int(timeout or os.getenv("STITCH_MCP_TIMEOUT", "60"))
        self.protocol_version = protocol_version or os.getenv("MCP_PROTOCOL_VERSION", DEFAULT_MCP_PROTOCOL_VERSION)
        self.session_id: Optional[str] = None
        self._next_id = 1

        if not self.api_key:
            raise StitchMCPError("Missing STITCH_API_KEY environment variable")

    def _next_rpc_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    async def post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await asyncio.to_thread(
            rpc_post,
            self.endpoint,
            self.api_key,
            payload,
            timeout=self.timeout,
            session_id=self.session_id,
            protocol_version=self.protocol_version,
        )
        if result.session_id:
            self.session_id = result.session_id
        if not result.ok:
            raise StitchMCPError(
                json.dumps(
                    {"error": result.error, "status": result.status, "raw_preview": result.raw_preview},
                    ensure_ascii=False,
                )
            )
        payload_obj = result.payload or {}
        if "error" in payload_obj:
            raise StitchMCPError(json.dumps(payload_obj["error"], ensure_ascii=False))
        return payload_obj

    async def initialize(self) -> Dict[str, Any]:
        data = await self.post(
            {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "dvcp-stitch-html-import", "version": "0.3.0"},
                },
            }
        )
        try:
            await self.post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            pass
        return data

    async def list_tools(self) -> list[dict[str, Any]]:
        data = await self.post({"jsonrpc": "2.0", "id": self._next_rpc_id(), "method": "tools/list", "params": {}})
        result = data.get("result", {}) if isinstance(data, dict) else {}
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await self.post(
            {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )


def normalize_tool_response(data: Any) -> Any:
    """Unwrap common MCP tool result formats into plain JSON/text."""
    if not isinstance(data, dict):
        return data

    result = data.get("result", data)

    # Prefer structuredContent when present; this avoids treating JSON text as UI.
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]

    if isinstance(result, dict) and isinstance(result.get("content"), list):
        texts: list[str] = []
        decoded: list[Any] = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
                try:
                    decoded.append(json.loads(text))
                except Exception:
                    pass
        if decoded:
            return decoded[0] if len(decoded) == 1 else decoded
        if texts:
            return "\n".join(texts)

    return result


def tool_result_is_error(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, ""
    result = data.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        normalized = normalize_tool_response(data)
        return True, str(normalized)[:500]
    if isinstance(data.get("error"), dict):
        return True, json.dumps(data.get("error"), ensure_ascii=False)[:500]
    if data.get("error"):
        return True, str(data.get("error"))[:500]
    return False, ""


async def call_first_successful_tool(
    client: StitchMCPClient,
    candidates: Iterable[tuple[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    errors: list[dict[str, Any]] = []
    for name, args in candidates:
        try:
            data = await client.call_tool(name, args)
            is_error, message = tool_result_is_error(data)
            if is_error:
                errors.append({"tool": name, "arguments": args, "error": message or "MCP tool returned isError=true"})
                continue
            return {
                "tool": name,
                "arguments": args,
                "data": data,
                "normalized": normalize_tool_response(data),
                "errors_before_success": errors,
            }
        except Exception as exc:  # noqa: BLE001
            errors.append({"tool": name, "arguments": args, "error": repr(exc)})
    raise StitchMCPError(json.dumps({"error": "all_tool_candidates_failed", "details": errors}, ensure_ascii=False))


def canonical_project_name(project_id: str) -> str:
    project_id = (project_id or "").strip().strip("/")
    if not project_id:
        return ""
    if project_id.startswith("projects/"):
        return project_id
    return f"projects/{project_id}"


def compact_project_id(project_id: str) -> str:
    project_id = (project_id or "").strip().strip("/")
    if project_id.startswith("projects/"):
        return project_id.split("/", 1)[1]
    return project_id


def compact_screen_id(screen_id: str) -> str:
    screen_id = (screen_id or "").strip().strip("/")
    if "/screens/" in screen_id:
        return screen_id.rsplit("/screens/", 1)[1]
    return screen_id


def canonical_screen_name(project_id: str, screen_id: str) -> str:
    screen_id = (screen_id or "").strip().strip("/")
    if not screen_id:
        return ""
    if screen_id.startswith("projects/") and "/screens/" in screen_id:
        return screen_id
    project_name = canonical_project_name(project_id)
    if project_name:
        return f"{project_name}/screens/{compact_screen_id(screen_id)}"
    return screen_id


def walk_values(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for item in value:
            yield from walk_values(item)


def first_string(value: Any, keys: tuple[str, ...]) -> Optional[str]:
    wanted = {k.lower() for k in keys}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in wanted and isinstance(child, (str, int)) and str(child).strip():
                return str(child).strip()
        for child in value.values():
            found = first_string(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = first_string(item, keys)
            if found:
                return found
    return None


def collect_named_items(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for candidate in walk_values(value):
        if not isinstance(candidate, dict):
            continue
        ident = first_string(candidate, ("id", "project_id", "projectId", "screen_id", "screenId"))
        name = first_string(candidate, ("name", "title", "displayName", "screen_name", "screenName"))
        if ident or name:
            obj_id = id(candidate)
            if obj_id not in seen:
                seen.add(obj_id)
                items.append(candidate)
    return items


def item_id(item: dict[str, Any], kind: str) -> Optional[str]:
    if kind == "project":
        # Stitch projects identify themselves with name=projects/{project}.
        return first_string(item, ("project_id", "projectId", "id", "name"))
    if kind == "screen":
        # list_projects may expose screenInstances.id and sourceScreen.
        return first_string(item, ("screen_id", "screenId", "id", "name", "sourceScreen"))
    return first_string(item, ("id", "name"))


def item_name(item: dict[str, Any]) -> Optional[str]:
    return first_string(item, ("title", "displayName", "screen_name", "screenName", "name"))


def preview_items(items: list[dict[str, Any]], kind: str, limit: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items[:limit]:
        out.append({"id": item_id(item, kind), "name": item_name(item)})
    return out


def select_item(items: list[dict[str, Any]], *, wanted_id: str = "", wanted_name: str = "", kind: str) -> Optional[dict[str, Any]]:
    wanted_id = (wanted_id or "").strip()
    wanted_id_compact = compact_project_id(wanted_id) if kind == "project" else compact_screen_id(wanted_id)
    wanted_name_norm = (wanted_name or "").strip().lower()
    if wanted_id:
        for item in items:
            current = str(item_id(item, kind) or "")
            current_compact = compact_project_id(current) if kind == "project" else compact_screen_id(current)
            if current == wanted_id or current_compact == wanted_id_compact:
                return item
    if wanted_name_norm:
        exact = [item for item in items if (item_name(item) or "").strip().lower() == wanted_name_norm]
        if len(exact) == 1:
            return exact[0]
        contains = [item for item in items if wanted_name_norm in (item_name(item) or "").strip().lower()]
        if len(contains) == 1:
            return contains[0]
    if not wanted_id and not wanted_name_norm and len(items) == 1:
        return items[0]
    return None


def require_tool(tool_names: set[str], name: str) -> None:
    if name not in tool_names:
        raise StitchMCPError(f"Stitch MCP tool not available: {name}")


def screen_urls(screen: Any) -> tuple[str, str]:
    if not isinstance(screen, dict):
        return "", ""
    html_url = ""
    screenshot_url = ""
    html_code = screen.get("htmlCode") or screen.get("html_code") or {}
    screenshot = screen.get("screenshot") or {}
    if isinstance(html_code, dict):
        html_url = str(html_code.get("downloadUrl") or html_code.get("download_url") or "")
    if isinstance(screenshot, dict):
        screenshot_url = str(screenshot.get("downloadUrl") or screenshot.get("download_url") or "")
    return html_url, screenshot_url


def build_download_record(download: DownloadResult) -> dict[str, Any]:
    return {
        "ok": download.ok,
        "status": download.status,
        "elapsed_ms": download.elapsed_ms,
        "url": download.url,
        "content_type": download.content_type,
        "bytes_read": download.bytes_read,
        "error": download.error,
        "text": download.text if download.ok else "",
        "text_preview": (download.text or "")[:1000],
    }


async def fetch_existing_stitch_screen(
    *,
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
    screen_id: Optional[str] = None,
    screen_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch an existing Stitch screen and download its HTML export.

    This function never calls create_project, generate_screen_from_text,
    edit_screens, or generate_variants.
    """
    client = StitchMCPClient()
    await client.initialize()
    tools = await client.list_tools()
    tool_names = {str(tool.get("name")) for tool in tools}

    require_tool(tool_names, "get_screen")

    project_id = (project_id or os.getenv("STITCH_PROJECT_ID", "")).strip()
    project_name = (project_name or os.getenv("STITCH_PROJECT_NAME", "")).strip()
    screen_id = (screen_id or os.getenv("STITCH_SCREEN_ID", "")).strip()
    screen_name = (screen_name or os.getenv("STITCH_SCREEN_NAME", "")).strip()

    selected_project: Optional[dict[str, Any]] = None
    listed_projects: Optional[dict[str, Any]] = None
    listed_screens: Optional[dict[str, Any]] = None

    if not project_id and (project_name or not screen_id):
        require_tool(tool_names, "list_projects")
        listed_projects = await call_first_successful_tool(client, [("list_projects", {})])
        projects = collect_named_items(listed_projects.get("normalized"))
        selected_project = select_item(projects, wanted_name=project_name, kind="project")
        if selected_project:
            project_id = item_id(selected_project, "project") or ""
        elif not screen_id:
            raise StitchMCPError(
                json.dumps(
                    {
                        "error": "project_not_selected",
                        "message": "Set STITCH_PROJECT_ID or STITCH_PROJECT_NAME. Automatic selection is allowed only when exactly one project is discoverable.",
                        "available_projects": preview_items(projects, "project"),
                    },
                    ensure_ascii=False,
                )
            )

    if not screen_id:
        require_tool(tool_names, "list_screens")
        short_project_id = compact_project_id(project_id)
        candidates: list[tuple[str, Dict[str, Any]]] = []
        if short_project_id:
            candidates.extend(
                [
                    ("list_screens", {"projectId": short_project_id}),
                    ("list_screens", {"project_id": short_project_id}),
                    ("list_screens", {"project": short_project_id}),
                ]
            )
        listed_screens = await call_first_successful_tool(client, candidates)
        screens = collect_named_items(listed_screens.get("normalized"))
        selected_screen = select_item(screens, wanted_name=screen_name, kind="screen")
        if selected_screen:
            screen_id = item_id(selected_screen, "screen") or ""
            if not screen_name:
                screen_name = item_name(selected_screen) or ""
        else:
            raise StitchMCPError(
                json.dumps(
                    {
                        "error": "screen_not_selected",
                        "message": "Set STITCH_SCREEN_ID or STITCH_SCREEN_NAME. If several screens exist, automatic selection is intentionally disabled.",
                        "project_id": project_id or None,
                        "available_screens": preview_items(screens, "screen"),
                    },
                    ensure_ascii=False,
                )
            )

    full_screen_name = canonical_screen_name(project_id, screen_id)
    short_project_id = compact_project_id(project_id)
    short_screen_id = compact_screen_id(screen_id)

    get_candidates: list[tuple[str, Dict[str, Any]]] = []
    if full_screen_name:
        get_candidates.append(("get_screen", {"name": full_screen_name}))

    # Deprecated forms retained only as fallback.
    if short_project_id and short_screen_id:
        get_candidates.extend(
            [
                ("get_screen", {"projectId": short_project_id, "screenId": short_screen_id}),
                ("get_screen", {"project_id": short_project_id, "screen_id": short_screen_id}),
            ]
        )

    screen_result = await call_first_successful_tool(client, get_candidates)
    screen = screen_result.get("normalized")
    if isinstance(screen, str):
        try:
            parsed = json.loads(screen)
            if isinstance(parsed, dict):
                screen = parsed
        except Exception:
            pass

    if not isinstance(screen, dict):
        raise StitchMCPError(json.dumps({"error": "screen_payload_not_structured", "normalized": str(screen)[:1000]}, ensure_ascii=False))

    html_url, screenshot_url = screen_urls(screen)
    downloads: dict[str, Any] = {
        "html": {"ok": False, "url": html_url, "error": "not_downloaded"},
        "screenshot": {"ok": False, "url": screenshot_url, "error": "not_downloaded"},
    }

    if html_url:
        timeout = int(os.getenv("STITCH_DOWNLOAD_TIMEOUT", "45"))
        max_bytes = int(os.getenv("STITCH_HTML_MAX_BYTES", "2000000"))
        html_download = await asyncio.to_thread(download_text_url, html_url, timeout=timeout, max_bytes=max_bytes)
        downloads["html"] = build_download_record(html_download)
    else:
        downloads["html"] = {"ok": False, "url": "", "error": "htmlCode.downloadUrl_missing"}

    # Download the official Stitch screenshot for optional multimodal LLM planning.
    # Keep it out of compact outputs later, but preserve metadata and base64 internally.
    if screenshot_url and os.getenv("STITCH_DOWNLOAD_SCREENSHOT", "1").strip().lower() in {"1", "true", "yes", "on"}:
        timeout = int(os.getenv("STITCH_SCREENSHOT_DOWNLOAD_TIMEOUT", os.getenv("STITCH_DOWNLOAD_TIMEOUT", "45")))
        max_bytes = int(os.getenv("STITCH_SCREENSHOT_MAX_BYTES", "5000000"))
        screenshot_download = await asyncio.to_thread(download_binary_url, screenshot_url, timeout=timeout, max_bytes=max_bytes)
        downloads["screenshot"] = build_binary_download_record(screenshot_download)
    elif screenshot_url:
        downloads["screenshot"] = {"ok": False, "url": screenshot_url, "error": "disabled_by_STITCH_DOWNLOAD_SCREENSHOT"}
    else:
        downloads["screenshot"] = {"ok": False, "url": "", "error": "screenshot.downloadUrl_missing"}

    return {
        "mode": "get_existing_screen",
        "endpoint": client.endpoint,
        "api_key_redacted": redact_secret(client.api_key),
        "tools_detected": sorted(tool_names),
        "project_id": canonical_project_name(project_id) or None,
        "project_name": project_name or (item_name(selected_project) if selected_project else None),
        "screen_id": compact_screen_id(screen_id) or None,
        "screen_name": screen_name or screen.get("title") or None,
        "listed_projects": listed_projects,
        "listed_screens": listed_screens,
        "result": screen_result,
        "screen": screen,
        "downloads": downloads,
    }


# Backwards-compatible name used by older graph.py versions.
# It is intentionally import-only now: no create_project and no generation.
async def fetch_or_generate_stitch_screen(prompt: str = "") -> Dict[str, Any]:
    return await fetch_existing_stitch_screen(screen_name=os.getenv("STITCH_SCREEN_NAME", "").strip())
