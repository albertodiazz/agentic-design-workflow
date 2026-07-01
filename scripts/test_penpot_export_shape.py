import os
import json
import requests

ENDPOINT = os.getenv("PENPOT_MCP_KEY", "http://localhost:4401/mcp")

CODE = """
return {
  ok: true,
  test: "return_only",
  message: "execute_code can return values",
  timestamp: new Date().toISOString(),
  penpotVersion: penpot.version,
  hasCurrentFile: !!penpot.currentFile,
  hasCurrentPage: !!penpot.currentPage,
  currentPageName: penpot.currentPage ? penpot.currentPage.name : null,
  selectionCount: penpot.selection ? penpot.selection.length : 0
};
""".strip()


def post(payload, session_id=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    if session_id:
        headers["Mcp-Session-Id"] = session_id

    response = requests.post(
        ENDPOINT,
        headers=headers,
        data=json.dumps(payload),
        timeout=45,
    )

    print(f"\nHTTP {response.status_code}")
    print(response.text[:4000])

    response.raise_for_status()
    return response


# 1. initialize
init_payload = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {
            "name": "dvcp-direct-return-test",
            "version": "1.0.0",
        },
    },
}

init_response = post(init_payload)
session_id = init_response.headers.get("Mcp-Session-Id")

print("SESSION:", session_id)

# 2. initialized notification
post(
    {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    },
    session_id=session_id,
)

# 3. execute_code
call_payload = {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 2,
    "params": {
        "name": "execute_code",
        "arguments": {
            "code": CODE,
        },
    },
}

post(call_payload, session_id=session_id)
