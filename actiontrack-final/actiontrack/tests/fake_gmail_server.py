"""A fake Gmail MCP server for tests (stdio or HTTP).

Mirrors the published input schema of Google's `create_draft` tool and also
advertises tools that SEND mail, so tests can prove actiontrack never calls them.
Every JSON-RPC message received is appended to $FAKE_MCP_LOG as one JSON line.
Behaviour switch: $FAKE_MCP_MODE = normal | no_draft_tool | bad_schema | is_error | rpc_error
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CREATE_DRAFT = {
    "name": "create_draft",
    "description": "Creates a new draft email in the authenticated user's Gmail account.",
    "inputSchema": {"type": "object", "properties": {
        "to": {"type": "array", "items": {"type": "string"}},
        "cc": {"type": "array", "items": {"type": "string"}},
        "bcc": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"}, "body": {"type": "string"},
        "htmlBody": {"type": "string"}, "replyToMessageId": {"type": "string"}}},
}
DANGEROUS = [
    {"name": "send_message", "inputSchema": {"type": "object", "properties": {
        "to": {"type": "array"}, "subject": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "send_draft", "inputSchema": {"type": "object", "properties": {
        "draftId": {"type": "string"}}}},
    {"name": "forward", "inputSchema": {"type": "object", "properties": {
        "messageId": {"type": "string"}, "to": {"type": "array"}}}},
]
OTHER = [{"name": "search_threads", "inputSchema": {"type": "object", "properties": {}}}]


def _log(msg):
    path = os.environ.get("FAKE_MCP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg) + "\n")


def handle(msg, mode):
    _log(msg)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None  # notification
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-gmail", "version": "1"}}
    elif method == "tools/list":
        draft = dict(CREATE_DRAFT)
        if mode == "bad_schema":
            draft = {"name": "create_draft",
                     "inputSchema": {"type": "object", "properties": {"recipient": {}}}}
        tools = ([] if mode == "no_draft_tool" else [draft]) + DANGEROUS + OTHER
        result = {"tools": tools}
    elif method == "tools/call":
        name = msg["params"]["name"]
        if mode == "rpc_error":
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "boom"}}
        if name != "create_draft":
            # If this branch is ever reached, the log proves a forbidden tool was called.
            result = {"content": [{"type": "text", "text": "SENT!"}]}
        elif mode == "is_error":
            result = {"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"id": "r-draft-123"})}],
                      "structuredContent": {"id": "r-draft-123",
                                            "toRecipients": msg["params"]["arguments"]["to"]}}
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def stdio_main():
    mode = os.environ.get("FAKE_MCP_MODE", "normal")
    print("fake gmail server starting (log noise on stdout must be tolerated)", flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), mode)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def start_http(mode="normal", token="good-token", sse=True):
    """Start a Streamable-HTTP fake on localhost; returns (server, url, seen_headers)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            msg = json.loads(body)
            reply = handle(msg, mode)
            if reply is None:
                self.send_response(202)
                self.end_headers()
                return
            if msg.get("method") == "initialize":
                session = "sess-42"
            else:
                assert self.headers.get("Mcp-Session-Id") == "sess-42"
                session = None
            payload = json.dumps(reply)
            if sse:
                data = f"event: message\ndata: {payload}\n\n".encode()
                ctype = "text/event-stream"
            else:
                data, ctype = payload.encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/mcp/v1", seen


if __name__ == "__main__":
    stdio_main()
