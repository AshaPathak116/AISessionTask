"""Phase 3: followup emails, Gmail draft creation (mocked), and proof nothing is sent."""

import ast
import email
import json
import sys
from datetime import date
from pathlib import Path

import pytest

import actiontrack
import actiontrack.cli as cli
from actiontrack.cli import main
from actiontrack.followup import clean_task, collect_items, compose_email, run_followup
from actiontrack.gmail_mcp import (DraftOnlyGmail, GmailConfig, GmailError, McpClient,
                                   UnsafeToolError, _unwrap, classify_tool)
from actiontrack.ingest import ingest_folder
from actiontrack.queries import mark_done, resolve_owner_query
from actiontrack.storage import connect
from conftest import FIXTURES
from fake_gmail_server import handle, start_http

TODAY = date(2025, 10, 9)
FAKE_SERVER = Path(__file__).parent / "fake_gmail_server.py"
CONFIGURED = GmailConfig(url="https://gmailmcp.googleapis.com/mcp/v1", token="unused")
ALLOWED_METHODS = {"initialize", "notifications/initialized", "tools/list", "tools/call"}


class RecordingTransport:
    """In-memory MCP transport backed by the fake server; records every message."""

    def __init__(self, mode="normal"):
        self.mode, self.calls, self.closed = mode, [], False

    def request(self, method, params=None):
        msg = {"jsonrpc": "2.0", "id": len(self.calls) + 1, "method": method,
               "params": params or {}}
        self.calls.append(msg)
        return _unwrap(handle(msg, self.mode))

    def notify(self, method, params=None):
        self.calls.append({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        self.closed = True

    def tool_calls(self):
        return [c["params"]["name"] for c in self.calls if c["method"] == "tools/call"]


def factory_for(transport):
    def factory(config):
        client = McpClient(transport, allowed_tools={config.draft_tool})
        client.initialize()
        return DraftOnlyGmail(client, config.draft_tool)
    return factory


def never_called(config):
    raise AssertionError("Gmail must not be contacted in this scenario")


def assert_never_sent(transport):
    assert {c["method"] for c in transport.calls} <= ALLOWED_METHODS
    assert set(transport.tool_calls()) <= {"create_draft"}


@pytest.fixture
def db(conn, notes_dir):
    ingest_folder(conn, notes_dir)
    return conn


def follow(db, name, tmp_path, **kw):
    kw.setdefault("gmail_config", CONFIGURED)
    kw.setdefault("preview_dir", tmp_path / "previews")
    return run_followup(db, resolve_owner_query(db, name), today=TODAY, **kw)


# ---- email content ----------------------------------------------------------------

def test_email_content(db):
    pid = resolve_owner_query(db, "Priya Sharma").person_id
    d = compose_email("Priya Sharma", collect_items(db, pid, TODAY), to=["p@x.com"])
    assert d.subject == "Follow-up: 4 open action items (1 overdue)"
    body = d.body
    assert body.startswith("Hi Priya,\n")
    assert body.index("Overdue:") < body.index("Open:")
    assert "1. Send the revised quote - due Fri 3 Oct 2025 (overdue)" in body
    assert "Raised in: Sprint Planning (22 Sep 2025); also discussed 29 Sep 2025" in body
    assert "Send the revised quotation - due Wed 15 Oct 2025" in body
    assert "Share the pricing sheet with sales - no due date set" in body
    assert "Raised in: Retro — 2025-10-13\n" in body          # no repeated date
    assert "by Friday" not in body and "by Wednesday" not in body  # relative phrases resolved
    assert body.rstrip().endswith("Thanks,")                   # no invented signature


def test_email_never_mixes_people(db):
    pid = resolve_owner_query(db, "Priya Sharma").person_id
    body = compose_email("Priya Sharma", collect_items(db, pid, TODAY), to=[]).body
    assert "icon set" not in body           # Priya Nair's task
    assert "wireframes" not in body         # ambiguous 'Priya' task
    assert "colour tokens" not in body      # 'Priya & Karan' shared task


def test_done_items_excluded_and_sender_name(db):
    pid = resolve_owner_query(db, "Rahul Verma").person_id
    items = collect_items(db, pid, TODAY)
    tasks = " ".join(i.task for i in items)
    assert "acceptance tests" not in tasks and "staging cluster" not in tasks
    body = compose_email("Rahul Verma", items, to=[], sender_name="Asha Pathak").body
    assert body.rstrip().endswith("Thanks,\nAsha Pathak")


@pytest.mark.parametrize("task,raw,known,expected", [
    ("Send the revised quote by Friday", "Friday", True, "Send the revised quote"),
    ("Prepare the style guide (due: 2025-10-10)", "2025-10-10", True, "Prepare the style guide"),
    ("Close the Q4 budget by 5 Jan", "5 Jan", True, "Close the Q4 budget"),
    ("Plan the DR drill by next week", "next week", False, "Plan the DR drill by next week"),
    ("Write acceptance tests", None, False, "Write acceptance tests"),
])
def test_clean_task(task, raw, known, expected):
    assert clean_task(task, raw, known) == expected


# ---- Gmail draft creation ---------------------------------------------------------

def test_draft_created_with_exact_arguments(db, tmp_path):
    t = RecordingTransport()
    out = follow(db, "Priya Sharma", tmp_path, gmail_factory=factory_for(t))
    assert out.mode == "draft" and out.exit_code == 0 and out.draft_id == "r-draft-123"
    assert [c["method"] for c in t.calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"]
    call = t.calls[-1]["params"]
    assert call["name"] == "create_draft"
    assert call["arguments"] == {"to": ["priya.sharma@example.com"],
                                 "subject": out.draft.subject, "body": out.draft.body}
    assert "NOT been sent" in " ".join(out.messages)
    assert t.closed
    assert not (tmp_path / "previews").exists()
    row = db.execute("SELECT mode, draft_id, recipient FROM followups").fetchone()
    assert tuple(row) == ("draft", "r-draft-123", "priya.sharma@example.com")


@pytest.mark.parametrize("mode", ["normal", "is_error", "bad_schema", "no_draft_tool",
                                  "rpc_error"])
def test_no_send_tool_invoked_in_any_server_state(db, tmp_path, mode):
    t = RecordingTransport(mode)
    follow(db, "Priya Sharma", tmp_path, gmail_factory=factory_for(t), force=True)
    assert_never_sent(t)
    assert "send_message" not in t.tool_calls() and "forward" not in t.tool_calls()


@pytest.mark.parametrize("name", ["send_message", "send_draft", "forward", "reply_all",
                                  "create_draft_and_send", "schedule_send", "search_threads"])
def test_client_refuses_every_tool_but_the_draft_tool(name):
    t = RecordingTransport()
    client = McpClient(t, allowed_tools={"create_draft"})
    with pytest.raises(UnsafeToolError):
        client.call_tool(name, {"to": ["a@b.com"]})
    assert t.tool_calls() == []


def test_send_like_draft_tool_name_rejected_before_connecting(db, tmp_path):
    bad = GmailConfig(command="this-would-fail-if-started", draft_tool="send_message")
    with pytest.raises(UnsafeToolError):
        DraftOnlyGmail.connect(bad)
    out = follow(db, "Priya Sharma", tmp_path, gmail_config=bad)   # real default factory
    assert out.mode == "preview" and out.exit_code == 1
    assert "refusing" in " ".join(out.messages)


def test_draft_only_wrapper_requires_restricted_client():
    with pytest.raises(UnsafeToolError):
        DraftOnlyGmail(McpClient(RecordingTransport(), {"create_draft", "send_message"}))


def test_static_proof_no_send_path_in_package():
    """Scan the Gmail-capable modules: one tool-call site, the draft tool; no SMTP anywhere."""
    pkg = Path(actiontrack.__file__).parent
    call_sites, imports = [], set()
    for path in (pkg / "gmail_mcp.py", pkg / "followup.py", pkg / "cli.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "call_tool":
                call_sites.append((path.name, ast.unparse(node.args[0])))
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports |= {a.name.split(".")[0] for a in node.names}
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"send_message", "send_draft", "send", "forward"}
    assert call_sites == [("gmail_mcp.py", "self.draft_tool")]
    for path in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports |= {a.name.split(".")[0] for a in node.names}
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
    assert not imports & {"smtplib", "imaplib", "poplib"}


@pytest.mark.parametrize("mode,fragment", [
    ("no_draft_tool", "has no 'create_draft' tool"),
    ("bad_schema", "no array field 'to'"),
    ("is_error", "quota exceeded"),
    ("rpc_error", "boom"),
])
def test_server_problems_fall_back_to_preview(db, tmp_path, mode, fragment):
    out = follow(db, "Priya Sharma", tmp_path, gmail_factory=factory_for(RecordingTransport(mode)))
    assert out.mode == "preview" and out.exit_code == 1
    text = " ".join(out.messages)
    assert "No Gmail draft was created" in text and fragment in text
    assert out.preview_path.exists()


def test_connection_failure_and_unexpected_errors_fall_back(db, tmp_path):
    def unauthorized(config):
        raise GmailError("Gmail MCP rejected the credentials (HTTP 401).")

    def crash(config):
        raise RuntimeError("kaput")

    for factory, fragment in ((unauthorized, "HTTP 401"), (crash, "kaput")):
        out = follow(db, "Priya Sharma", tmp_path, gmail_factory=factory, force=True)
        assert out.mode == "preview" and out.exit_code == 1 and fragment in " ".join(out.messages)


# ---- recipients -------------------------------------------------------------------

def test_missing_address_gives_preview_and_never_contacts_gmail(db, tmp_path):
    out = follow(db, "Ananya Iyer", tmp_path, gmail_factory=never_called)
    assert out.mode == "preview" and out.exit_code == 0
    assert "no email address is known for Ananya Iyer" in " ".join(out.messages)
    assert out.draft.to == []
    msg = email.message_from_bytes(out.preview_path.read_bytes())
    assert msg["To"] is None                                   # nothing invented
    assert db.execute("SELECT email FROM people WHERE full_name='Ananya Iyer'"
                      ).fetchone()[0] is None


def test_asked_address_used_and_only_remembered_on_request(db, tmp_path):
    t = RecordingTransport()
    out = follow(db, "Ananya Iyer", tmp_path, gmail_factory=factory_for(t),
                 ask_email=lambda name: "ananya@example.com")
    assert out.mode == "draft" and t.calls[-1]["params"]["arguments"]["to"] == ["ananya@example.com"]
    assert db.execute("SELECT email FROM people WHERE full_name='Ananya Iyer'"
                      ).fetchone()[0] is None
    follow(db, "Ananya Iyer", tmp_path, gmail_factory=factory_for(RecordingTransport()),
           to_override="ananya@example.com", remember_email=True, force=True)
    assert db.execute("SELECT email FROM people WHERE full_name='Ananya Iyer'"
                      ).fetchone()[0] == "ananya@example.com"


def test_invalid_asked_address_gives_preview(db, tmp_path):
    out = follow(db, "Ananya Iyer", tmp_path, gmail_factory=never_called,
                 ask_email=lambda name: "not-an-address")
    assert out.mode == "preview"


@pytest.mark.parametrize("bad", ["Priya <p@x.com>", "p@x", "", "two@x.com, three@x.com"])
def test_to_override_must_be_plain_address(db, tmp_path, bad):
    out = follow(db, "Priya Sharma", tmp_path, gmail_factory=never_called, to_override=bad)
    assert out.mode == "nothing" and out.exit_code == 2
    assert not (tmp_path / "previews").exists()


def test_to_override_beats_recorded_address(db, tmp_path):
    t = RecordingTransport()
    follow(db, "Priya Sharma", tmp_path, gmail_factory=factory_for(t), to_override="ps@corp.com")
    assert t.calls[-1]["params"]["arguments"]["to"] == ["ps@corp.com"]


# ---- owners and other guards -------------------------------------------------------

def test_unverified_owner_refused(db, tmp_path):
    out = follow(db, "Vikram", tmp_path, gmail_factory=never_called)
    assert out.mode == "nothing" and out.exit_code == 2
    assert out.messages[0].startswith("Vikram is not a verified attendee")


def test_no_open_tasks_no_draft(db, tmp_path):
    mark_done(db, str(db.execute("SELECT id FROM action_items WHERE task LIKE 'Confirm the go-live%'"
                                 ).fetchone()[0]), today=TODAY)
    out = follow(db, "Dana White", tmp_path, gmail_factory=never_called)
    assert out.mode == "nothing" and "no open action items" in out.messages[0]
    assert not (tmp_path / "previews").exists()


def test_not_configured_preview_explains_setup(db, tmp_path):
    out = follow(db, "Priya Sharma", tmp_path, gmail_config=GmailConfig(),
                 gmail_factory=never_called)
    text = " ".join(out.messages)
    assert out.mode == "preview" and out.exit_code == 0
    assert "No Gmail draft was created: Gmail MCP is not configured" in text
    assert "ACTIONTRACK_GMAIL_MCP_URL" in text and "README" in text


def test_preview_flag_skips_gmail(db, tmp_path):
    out = follow(db, "Priya Sharma", tmp_path, gmail_factory=never_called, preview_only=True)
    assert out.mode == "preview" and out.exit_code == 0


def test_duplicate_draft_guard(db, tmp_path):
    first = follow(db, "Priya Sharma", tmp_path, gmail_factory=factory_for(RecordingTransport()))
    assert first.mode == "draft"
    again = follow(db, "Priya Sharma", tmp_path, gmail_factory=never_called)
    assert again.mode == "nothing" and "already created" in again.messages[0]
    forced = follow(db, "Priya Sharma", tmp_path, force=True,
                    gmail_factory=factory_for(RecordingTransport()))
    assert forced.mode == "draft"
    mark_done(db, str(first.draft.item_ids[0]), today=TODAY)       # items changed
    changed = follow(db, "Priya Sharma", tmp_path,
                     gmail_factory=factory_for(RecordingTransport()))
    assert changed.mode == "draft" and len(changed.draft.item_ids) == 3


def test_eml_preview_file(db, tmp_path):
    out = follow(db, "Priya Sharma", tmp_path, preview_only=True)
    msg = email.message_from_bytes(out.preview_path.read_bytes())
    assert msg["X-Unsent"] == "1" and msg["To"] == "priya.sharma@example.com"
    assert msg["Subject"] == out.draft.subject
    assert msg.get_payload(decode=True).decode("utf-8").replace("\r\n", "\n") == out.draft.body
    second = follow(db, "Priya Sharma", tmp_path, preview_only=True)
    assert second.preview_path != out.preview_path                 # never overwritten


def test_classify_tool():
    assert classify_tool("create_draft", "create_draft").startswith("used")
    assert classify_tool("send_message", "create_draft").startswith("BLOCKED")
    assert classify_tool("forward", "create_draft").startswith("BLOCKED")
    assert classify_tool("search_threads", "create_draft") == "not used"


# ---- CLI -----------------------------------------------------------------------------

@pytest.fixture
def cli_db(tmp_path):
    db_path = tmp_path / "cli.db"
    c = connect(db_path)
    ingest_folder(c, FIXTURES)
    c.close()
    return db_path


def run_cli(args, db_path, capsys, tmp_path):
    code = main(args + ["--db", str(db_path), "--today", "2025-10-09",
                        "--preview-dir", str(tmp_path / "previews")])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_ambiguous_owner_non_interactive(cli_db, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "gmail_factory", never_called)
    code, out, err = run_cli(["followup", "Priya", "--no-input"], cli_db, capsys, tmp_path)
    assert code == 2 and out == ""
    assert "Priya Nair (owner ID" in err and "Priya Sharma (owner ID" in err
    assert "never combined" in err
    assert not (tmp_path / "previews").exists()


def test_cli_ambiguous_owner_interactive_choice(cli_db, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    answers = iter(["1"])
    monkeypatch.setattr(cli, "_prompt", lambda text: next(answers))
    code, out, _ = run_cli(["followup", "Priya", "--preview"], cli_db, capsys, tmp_path)
    assert code == 0
    assert "1) Priya Nair" in out and "2) Priya Sharma" in out
    assert "Export the icon set" in out and "revised quote" not in out


def test_cli_ambiguous_owner_interactive_cancel(cli_db, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda text: "")
    code, out, _ = run_cli(["followup", "Priya"], cli_db, capsys, tmp_path)
    assert code == 2 and "Cancelled" in out


def test_cli_prompts_for_missing_address(cli_db, capsys, tmp_path, monkeypatch):
    t = RecordingTransport()
    monkeypatch.setattr(cli, "gmail_factory", factory_for(t))
    monkeypatch.setenv("ACTIONTRACK_GMAIL_MCP_URL", "https://gmailmcp.googleapis.com/mcp/v1")
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    answers = iter(["oops", "ananya@example.com"])
    monkeypatch.setattr(cli, "_prompt", lambda text: next(answers))
    code, out, _ = run_cli(["followup", "Ananya Iyer"], cli_db, capsys, tmp_path)
    assert code == 0 and "not a plain email address" in out
    assert "Gmail draft created for Ananya Iyer <ananya@example.com>" in out
    assert_never_sent(t)


def test_cli_owner_id_and_output(cli_db, capsys, tmp_path):
    c = connect(cli_db)
    pid = c.execute("SELECT id FROM people WHERE full_name='Priya Nair'").fetchone()[0]
    c.close()
    code, out, _ = run_cli(["followup", "--owner-id", str(pid)], cli_db, capsys, tmp_path)
    assert code == 0 and "Hi Priya," in out and "No Gmail draft was created" in out
    assert "Subject: Follow-up: 1 open action item (1 overdue)" in out


def test_cli_requires_owner(cli_db, capsys, tmp_path):
    code, _, err = run_cli(["followup"], cli_db, capsys, tmp_path)
    assert code == 2 and "owner" in err


# ---- real transports against the fake server ----------------------------------------

def read_log(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def logged_tool_calls(log):
    return [m["params"]["name"] for m in log if m.get("method") == "tools/call"]


def test_stdio_end_to_end(cli_db, capsys, tmp_path, monkeypatch):
    log = tmp_path / "mcp.log"
    monkeypatch.setenv("FAKE_MCP_LOG", str(log))
    monkeypatch.setenv("ACTIONTRACK_GMAIL_MCP_COMMAND", f'"{sys.executable}" "{FAKE_SERVER}"')
    code, out, err = run_cli(["followup", "Priya Sharma"], cli_db, capsys, tmp_path)
    assert code == 0, err
    assert "Gmail draft created for Priya Sharma <priya.sharma@example.com>" in out
    assert "draft ID r-draft-123" in out and "NOT been sent" in out
    entries = read_log(log)
    assert [m["method"] for m in entries] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert logged_tool_calls(entries) == ["create_draft"]


def test_gmail_tools_inspection_calls_no_tool(capsys, tmp_path, monkeypatch):
    log = tmp_path / "mcp.log"
    monkeypatch.setenv("FAKE_MCP_LOG", str(log))
    monkeypatch.setenv("ACTIONTRACK_GMAIL_MCP_COMMAND", f'"{sys.executable}" "{FAKE_SERVER}"')
    assert main(["gmail-tools"]) == 0
    out = capsys.readouterr().out
    assert "5 tool(s) advertised" in out
    for line in ("send_message", "send_draft", "forward"):
        assert any(l.split()[0] == line and "BLOCKED" in l for l in out.splitlines()
                   if l.strip().startswith(line))
    assert '"to"' in out and "No tool was called" in out
    assert logged_tool_calls(read_log(log)) == []


@pytest.mark.parametrize("sse", [True, False])
def test_http_end_to_end(cli_db, capsys, tmp_path, monkeypatch, sse):
    log = tmp_path / "mcp.log"
    monkeypatch.setenv("FAKE_MCP_LOG", str(log))
    server, url, seen = start_http(token="good-token", sse=sse)
    try:
        monkeypatch.setenv("ACTIONTRACK_GMAIL_MCP_URL", url)
        monkeypatch.setenv("ACTIONTRACK_GMAIL_TOKEN", "good-token")
        code, out, err = run_cli(["followup", "Priya Sharma"], cli_db, capsys, tmp_path)
    finally:
        server.shutdown()
    assert code == 0, err
    assert "draft ID r-draft-123" in out
    assert logged_tool_calls(read_log(log)) == ["create_draft"]
    assert all(h["authorization"] == "Bearer good-token" for h in seen)
    assert "mcp-protocol-version" not in seen[0]                 # not sent before initialize
    assert seen[-1]["mcp-protocol-version"] == "2025-06-18"
    assert seen[-1]["mcp-session-id"] == "sess-42"


def test_http_bad_token_falls_back_without_leaking_token(cli_db, capsys, tmp_path, monkeypatch):
    server, url, _ = start_http(token="good-token")
    try:
        monkeypatch.setenv("ACTIONTRACK_GMAIL_MCP_URL", url)
        monkeypatch.setenv("ACTIONTRACK_GMAIL_TOKEN", "WRONG-SECRET-TOKEN")
        code, out, err = run_cli(["followup", "Priya Sharma"], cli_db, capsys, tmp_path)
    finally:
        server.shutdown()
    assert code == 1
    assert "rejected the credentials (HTTP 401)" in err and "No Gmail draft was created" in err
    assert "WRONG-SECRET-TOKEN" not in out + err
    assert list((tmp_path / "previews").glob("*.eml"))


def test_http_token_command(cli_db, capsys, tmp_path, monkeypatch):
    server, url, seen = start_http(token="from-command")
    try:
        monkeypatch.setenv("ACTIONTRACK_GMAIL_MCP_URL", url)
        monkeypatch.setenv("ACTIONTRACK_GMAIL_TOKEN_CMD",
                           f'"{sys.executable}" -c "print(\'from-command\')"')
        code, out, err = run_cli(["followup", "Priya Sharma"], cli_db, capsys, tmp_path)
    finally:
        server.shutdown()
    assert code == 0, err and seen


def test_network_guard_blocks_real_hosts():
    import urllib.request
    with pytest.raises(AssertionError, match="real network connection"):
        urllib.request.urlopen("https://gmailmcp.googleapis.com/mcp/v1", timeout=1)
