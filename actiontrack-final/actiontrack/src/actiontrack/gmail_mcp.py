"""Draft-only Gmail access over the Model Context Protocol (MCP).

Safety design (this module is the ONLY place actiontrack talks to Gmail):

1. Allowlist, not blocklist.  ``McpClient`` is constructed with the set of tool
   names it may call.  For Gmail that set contains exactly one tool: the draft
   tool (default ``create_draft``).  Any other name -- ``send_message``,
   ``send_draft``, ``forward``, anything -- is refused before a request is made.
2. The configured draft tool name itself is rejected if it looks like it could
   send (contains send/forward/reply/schedule/...).
3. The draft tool's input schema is checked after ``tools/list`` and before
   calling it; only the fields ``to``, ``subject`` and ``body`` are ever passed.
4. There is no SMTP code and no other network path in the package.

Reference: Google's server is https://gmailmcp.googleapis.com/mcp/v1 (Streamable
HTTP, OAuth 2.0 bearer token, ``create_draft`` needs the gmail.compose scope).
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_DRAFT_TOOL = "create_draft"
DEFAULT_TIMEOUT = 30.0
SEND_LIKE = re.compile(
    r"send|forward|reply|schedule|deliver|dispatch|submit|transmit|outbox|mail_merge", re.I)
PLAIN_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")

ENV_URL = "ACTIONTRACK_GMAIL_MCP_URL"
ENV_TOKEN = "ACTIONTRACK_GMAIL_TOKEN"
ENV_TOKEN_CMD = "ACTIONTRACK_GMAIL_TOKEN_CMD"
ENV_COMMAND = "ACTIONTRACK_GMAIL_MCP_COMMAND"
ENV_DRAFT_TOOL = "ACTIONTRACK_GMAIL_DRAFT_TOOL"


class GmailUnavailable(Exception):
    """Gmail MCP is not configured."""


class GmailError(Exception):
    """Gmail MCP is configured but the operation failed.  No draft was created."""


class UnsafeToolError(GmailError):
    """An attempt to call or configure a tool other than the draft tool."""


def is_plain_email(address: str) -> bool:
    return bool(PLAIN_EMAIL.match(address or ""))


def classify_tool(name: str, draft_tool: str) -> str:
    if name == draft_tool:
        return "used: creates drafts only"
    if SEND_LIKE.search(name):
        return "BLOCKED: can send email"
    return "not used"


# --------------------------------------------------------------------------- config


@dataclass
class GmailConfig:
    url: str | None = None
    token: str | None = field(default=None, repr=False)
    token_cmd: str | None = field(default=None, repr=False)
    command: str | None = None
    draft_tool: str = DEFAULT_DRAFT_TOOL
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GmailConfig":
        env = os.environ if env is None else env
        return cls(
            url=(env.get(ENV_URL) or "").strip() or None,
            token=(env.get(ENV_TOKEN) or "").strip() or None,
            token_cmd=(env.get(ENV_TOKEN_CMD) or "").strip() or None,
            command=(env.get(ENV_COMMAND) or "").strip() or None,
            draft_tool=(env.get(ENV_DRAFT_TOOL) or "").strip() or DEFAULT_DRAFT_TOOL,
        )

    @property
    def configured(self) -> bool:
        return bool(self.url or self.command)

    def describe(self) -> str:
        if self.command:
            return f"stdio MCP server: {self.command}"
        if self.url:
            auth = ("token from " + ENV_TOKEN_CMD if self.token_cmd
                    else "token from " + ENV_TOKEN if self.token else "no token")
            return f"HTTP MCP server: {self.url} ({auth})"
        return "not configured"

    def resolve_token(self) -> str | None:
        if self.token:
            return self.token
        if not self.token_cmd:
            return None
        try:
            done = subprocess.run(self.token_cmd, shell=True, capture_output=True, text=True,
                                  timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GmailError(f"{ENV_TOKEN_CMD} failed to run: {type(exc).__name__}") from None
        token = done.stdout.strip().splitlines()[-1].strip() if done.stdout.strip() else ""
        if done.returncode != 0 or not token:
            raise GmailError(f"{ENV_TOKEN_CMD} exited with code {done.returncode} "
                             "and printed no token")
        return token


# --------------------------------------------------------------------------- transports


class HttpTransport:
    """MCP Streamable HTTP transport (JSON or SSE responses, session header)."""

    def __init__(self, url: str, token: str | None, timeout: float = DEFAULT_TIMEOUT):
        self.url, self._token, self.timeout = url, token, timeout
        self.session_id: str | None = None
        self.negotiated = False
        self._next_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.negotiated:
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    def _post(self, payload: dict):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"),
                                     headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise GmailError(
                    f"Gmail MCP rejected the credentials (HTTP {exc.code}). The token is "
                    "missing, expired, or lacks the gmail.compose scope.") from None
            raise GmailError(f"Gmail MCP returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise GmailError(f"cannot reach Gmail MCP at {self.url}: {exc.reason}") from None
        except TimeoutError:
            raise GmailError(f"Gmail MCP at {self.url} timed out") from None
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        return resp

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                           "params": params or {}})
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read().decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            messages = []
            for event in re.split(r"\r?\n\r?\n", raw):
                data = "\n".join(line[5:].lstrip() for line in event.splitlines()
                                 if line.startswith("data:"))
                if data:
                    messages.append(json.loads(data))
        else:
            parsed = json.loads(raw) if raw.strip() else []
            messages = parsed if isinstance(parsed, list) else [parsed]
        for msg in messages:
            if isinstance(msg, dict) and msg.get("id") == rid:
                return _unwrap(msg)
        raise GmailError(f"Gmail MCP sent no response to '{method}'")

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}).read()

    def close(self) -> None:
        if self.session_id:
            try:
                req = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass


class StdioTransport:
    """MCP stdio transport: newline-delimited JSON-RPC to a local server process."""

    def __init__(self, command: str, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        args = command if os.name == "nt" else shlex.split(command)
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True,
                                         encoding="utf-8", bufsize=1)
        except OSError as exc:
            raise GmailError(f"cannot start Gmail MCP server '{command}': {exc}") from None
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self._next_id = 0

    def _read(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _write(self, payload: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise GmailError("Gmail MCP server process exited unexpectedly") from None

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            try:
                line = self._lines.get(timeout=self.timeout)
            except queue.Empty:
                raise GmailError(f"Gmail MCP server did not answer '{method}' in time") from None
            if line is None:
                raise GmailError("Gmail MCP server process exited unexpectedly")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log output on stdout
            if isinstance(msg, dict) and msg.get("id") == rid and "method" not in msg:
                return _unwrap(msg)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _unwrap(msg: dict) -> dict:
    if "error" in msg:
        err = msg["error"] or {}
        raise GmailError(f"Gmail MCP error {err.get('code')}: {err.get('message')}")
    return msg.get("result") or {}


# --------------------------------------------------------------------------- client


class McpClient:
    """Minimal MCP client that can only call tools in ``allowed_tools``."""

    def __init__(self, transport, allowed_tools: set[str]):
        self.transport = transport
        self.allowed_tools = frozenset(allowed_tools)
        self._initialized = False

    def initialize(self) -> dict:
        result = self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "actiontrack", "version": "0.3.0"},
        })
        if hasattr(self.transport, "negotiated"):
            self.transport.negotiated = True
        self.transport.notify("notifications/initialized")
        self._initialized = True
        return result

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(50):  # pagination, bounded
            result = self.transport.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in self.allowed_tools or SEND_LIKE.search(name):
            raise UnsafeToolError(f"refusing to call MCP tool '{name}': only "
                                  f"{sorted(self.allowed_tools)} may be called")
        return self.transport.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        self.transport.close()


@dataclass
class ToolReport:
    tools: list[dict]
    draft_tool: dict | None
    schema_problem: str | None


class DraftOnlyGmail:
    """The only Gmail capability actiontrack has: create one draft.  Never sends."""

    def __init__(self, client: McpClient, draft_tool: str = DEFAULT_DRAFT_TOOL):
        if SEND_LIKE.search(draft_tool):
            raise UnsafeToolError(f"configured draft tool '{draft_tool}' looks like it could "
                                  "send email; refusing to use it")
        if client.allowed_tools != {draft_tool}:
            raise UnsafeToolError("MCP client must be restricted to the draft tool only")
        self.client = client
        self.draft_tool = draft_tool
        self._report: ToolReport | None = None

    @classmethod
    def connect(cls, config: GmailConfig) -> "DraftOnlyGmail":
        if not config.configured:
            raise GmailUnavailable("Gmail MCP is not configured")
        if SEND_LIKE.search(config.draft_tool):
            raise UnsafeToolError(f"configured draft tool '{config.draft_tool}' looks like it "
                                  "could send email; refusing to use it")
        if config.command:
            transport = StdioTransport(config.command, config.timeout)
        else:
            transport = HttpTransport(config.url, config.resolve_token(), config.timeout)
        client = McpClient(transport, allowed_tools={config.draft_tool})
        try:
            client.initialize()
        except Exception:
            client.close()
            raise
        return cls(client, config.draft_tool)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.client.close()

    def inspect(self) -> ToolReport:
        tools = self.client.list_tools()
        draft = next((t for t in tools if t.get("name") == self.draft_tool), None)
        problem = None
        if draft is None:
            problem = f"the server has no '{self.draft_tool}' tool"
        else:
            schema = draft.get("inputSchema") or {}
            props = schema.get("properties") or {}
            to = props.get("to") or {}
            if to.get("type") != "array":
                problem = f"'{self.draft_tool}' has no array field 'to'"
            elif "body" not in props and "subject" not in props:
                problem = f"'{self.draft_tool}' accepts neither 'subject' nor 'body'"
            else:
                unknown_required = set(schema.get("required") or []) - {"to", "subject", "body"}
                if unknown_required:
                    problem = (f"'{self.draft_tool}' requires unsupported fields: "
                               f"{', '.join(sorted(unknown_required))}")
        self._report = ToolReport(tools, draft, problem)
        return self._report

    def create_draft(self, to: list[str], subject: str, body: str) -> str | None:
        """Create a Gmail draft.  Returns the draft ID if the server reports one."""
        if not to:
            raise GmailError("no recipient address; refusing to create a draft without one")
        bad = [a for a in to if not is_plain_email(a)]
        if bad:
            raise GmailError(f"not a plain email address: {', '.join(bad)}")
        report = self._report or self.inspect()
        if report.schema_problem:
            raise GmailError(f"cannot create draft: {report.schema_problem}")
        props = (report.draft_tool.get("inputSchema") or {}).get("properties") or {}
        args = {"to": list(to)}
        if "subject" in props:
            args["subject"] = subject
        if "body" in props:
            args["body"] = body
        result = self.client.call_tool(self.draft_tool, args)
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content") or []
                            if isinstance(c, dict))
            raise GmailError(f"Gmail MCP could not create the draft: {text.strip()[:300]}")
        return _draft_id(result)


def _draft_id(result: dict) -> str | None:
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured.get("id"):
        return str(structured["id"])
    for item in result.get("content") or []:
        text = item.get("text") if isinstance(item, dict) else None
        if not text:
            continue
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data.get("id"):
                return str(data["id"])
        except json.JSONDecodeError:
            m = re.search(r'"?id"?\s*[:=]\s*"?([A-Za-z0-9_\-]+)', text)
            if m:
                return m.group(1)
    return None


def default_gmail_factory(config: GmailConfig) -> DraftOnlyGmail:
    return DraftOnlyGmail.connect(config)

