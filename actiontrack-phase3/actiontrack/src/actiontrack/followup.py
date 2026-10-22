"""`actiontrack followup`: compose a follow-up email and save it as a Gmail draft.

Never sends.  When Gmail is unavailable, the recipient is unknown, or anything
fails, a local preview (.eml) is written instead and the outcome says plainly
that no Gmail draft was created.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Callable

from .dates import find_date_in_text
from .gmail_mcp import (ENV_COMMAND, ENV_URL, GmailConfig, GmailError, GmailUnavailable,
                        default_gmail_factory, is_plain_email)
from .queries import OwnerMatch
from .storage import now_iso

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DUE_PREFIX = r"(?:due(?:\s*(?:date|by|on))?|deadline|by|before|no later than|until|till)"

SETUP_HINT = (
    f"To create Gmail drafts, set {ENV_URL}=https://gmailmcp.googleapis.com/mcp/v1 plus an "
    f"OAuth token (ACTIONTRACK_GMAIL_TOKEN or ACTIONTRACK_GMAIL_TOKEN_CMD), or set "
    f"{ENV_COMMAND} to a local Gmail MCP server. See README > Gmail setup."
)


def fmt_date(iso: str, with_weekday: bool = True) -> str:
    d = date.fromisoformat(iso)
    text = f"{d.day} {_MONTHS[d.month - 1]} {d.year}"
    return f"{_DAYS[d.weekday()]} {text}" if with_weekday else text


@dataclass
class FollowupItem:
    id: int
    task: str
    due_date: str | None
    overdue: bool
    meeting_title: str
    meeting_date: str
    source_file: str
    later_mentions: list[str] = field(default_factory=list)


@dataclass
class EmailDraft:
    owner_name: str
    to: list[str]
    subject: str
    body: str
    item_ids: list[int]


@dataclass
class FollowupOutcome:
    mode: str                         # "draft" | "preview" | "nothing"
    exit_code: int
    messages: list[str]
    draft: EmailDraft | None = None
    draft_id: str | None = None
    preview_path: Path | None = None


def clean_task(task: str, due_raw: str | None, due_known: bool) -> str:
    """Drop a resolved due-date phrase ('by Friday') since the email shows the real date."""
    if not due_raw or not due_known:
        return task
    pattern = (r"\s*[,(\[\-]?\s*(?:" + _DUE_PREFIX + r")?\s*:?\s*" + re.escape(due_raw)
               + r"\s*[)\]]?\s*")
    cleaned = re.sub(pattern, " ", task, count=1, flags=re.I).strip(" ,;-")
    return " ".join(cleaned.split()) or task


def collect_items(conn: sqlite3.Connection, person_id: int, today: date) -> list[FollowupItem]:
    """Open (including overdue) items whose owner is resolved to exactly this person."""
    rows = conn.execute(
        "SELECT * FROM action_items WHERE owner_status = 'resolved' AND owner_id = ?"
        " AND status = 'open'", (person_id,)).fetchall()
    items = []
    for r in rows:
        sources = conn.execute(
            "SELECT m.title, m.meeting_date, m.source_file FROM item_sources s"
            " JOIN meetings m ON m.id = s.meeting_id WHERE s.item_id = ?"
            " ORDER BY m.meeting_date, m.source_file", (r["id"],)).fetchall()
        first = sources[0] if sources else None
        due = r["due_date"]
        items.append(FollowupItem(
            id=r["id"],
            task=clean_task(r["task"], r["due_raw"], due is not None),
            due_date=due,
            overdue=due is not None and due < today.isoformat(),
            meeting_title=first["title"] if first else r["source_file"],
            meeting_date=first["meeting_date"] if first else r["meeting_date"],
            source_file=first["source_file"] if first else r["source_file"],
            later_mentions=sorted({s["meeting_date"] for s in sources[1:]}
                                  - {first["meeting_date"] if first else ""}),
        ))
    items.sort(key=lambda i: (not i.overdue, i.due_date is None, i.due_date or "", i.id))
    return items


def compose_email(owner_name: str, items: list[FollowupItem], *, to: list[str],
                  sender_name: str | None = None) -> EmailDraft:
    first_name = owner_name.split()[0]
    overdue = [i for i in items if i.overdue]
    pending = [i for i in items if not i.overdue]
    n = len(items)
    subject = f"Follow-up: {n} open action item{'s' if n != 1 else ''}"
    if overdue:
        subject += f" ({len(overdue)} overdue)"

    lines = [f"Hi {first_name},", "",
             "I'm following up on the action items assigned to you in recent meetings. "
             "Could you reply with a quick status update on each, and flag any dates "
             "that need to change?", ""]
    number = 0

    def add_section(title: str, section: list[FollowupItem]) -> None:
        nonlocal number
        if not section:
            return
        lines.append(f"{title}:")
        for item in section:
            number += 1
            if item.due_date:
                due = f"due {fmt_date(item.due_date)}" + (" (overdue)" if item.overdue else "")
            else:
                due = "no due date set"
            lines.append(f"{number}. {item.task} - {due}")
            ref = f"   Raised in: {item.meeting_title}"
            if find_date_in_text(item.meeting_title) != date.fromisoformat(item.meeting_date):
                ref += f" ({fmt_date(item.meeting_date, False)})"
            if item.later_mentions:
                ref += "; also discussed " + ", ".join(fmt_date(d, False)
                                                      for d in item.later_mentions)
            lines.append(ref)
        lines.append("")

    add_section("Overdue", overdue)
    add_section("Open" if overdue else "Open items", pending)
    lines.append("Thanks,")
    if sender_name:
        lines.append(sender_name.strip())
    return EmailDraft(owner_name=owner_name, to=list(to), subject=subject,
                      body="\n".join(lines) + "\n", item_ids=[i.id for i in items])


def to_eml(draft: EmailDraft) -> bytes:
    """An unsent message file.  X-Unsent makes Outlook open it as an editable draft."""
    msg = EmailMessage(policy=SMTP)
    if draft.to:
        msg["To"] = ", ".join(draft.to)
    msg["Subject"] = draft.subject
    msg["X-Unsent"] = "1"
    msg["X-Actiontrack"] = "local preview - not sent, not a Gmail draft"
    msg.set_content(draft.body)
    return msg.as_bytes()


def save_preview(draft: EmailDraft, folder: Path, today: date) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", draft.owner_name.lower()).strip("-") or "owner"
    path = folder / f"followup-{slug}-{today.isoformat()}.eml"
    n = 2
    while path.exists():
        path = folder / f"followup-{slug}-{today.isoformat()}-{n}.eml"
        n += 1
    path.write_bytes(to_eml(draft))
    return path


def _previous_draft(conn, person_id: int, item_ids: list[int]):
    return conn.execute(
        "SELECT created_at, draft_id FROM followups WHERE person_id = ? AND item_ids = ?"
        " AND mode = 'draft' ORDER BY id DESC LIMIT 1",
        (person_id, json.dumps(sorted(item_ids)))).fetchone()


def _record(conn, person_id: int, outcome: FollowupOutcome) -> None:
    with conn:
        conn.execute(
            "INSERT INTO followups(person_id, item_ids, mode, draft_id, preview_path,"
            " recipient, created_at) VALUES (?,?,?,?,?,?,?)",
            (person_id, json.dumps(sorted(outcome.draft.item_ids)), outcome.mode,
             outcome.draft_id, str(outcome.preview_path) if outcome.preview_path else None,
             ", ".join(outcome.draft.to) or None, now_iso()))


def run_followup(
    conn: sqlite3.Connection,
    owner: OwnerMatch,
    *,
    today: date,
    preview_dir: Path,
    gmail_config: GmailConfig | None = None,
    gmail_factory: Callable = default_gmail_factory,
    to_override: str | None = None,
    ask_email: Callable[[str], str | None] | None = None,
    remember_email: bool = False,
    preview_only: bool = False,
    sender_name: str | None = None,
    force: bool = False,
) -> FollowupOutcome:
    if owner.person_id is None:
        return FollowupOutcome("nothing", 2, [
            f"{owner.label.replace(' (unverified)', '')} is not a verified attendee of any meeting, so their tasks cannot "
            "be safely attributed to one person. Add their full name to the notes' "
            "Attendees line and re-ingest."])

    person = conn.execute("SELECT * FROM people WHERE id = ?", (owner.person_id,)).fetchone()
    items = collect_items(conn, owner.person_id, today)
    if not items:
        return FollowupOutcome("nothing", 0, [
            f"{person['full_name']} has no open action items. No draft or preview created."])

    gmail_config = gmail_config or GmailConfig.from_env()
    messages: list[str] = []

    # ---- recipient: explicit > recorded in notes > asked; never invented
    address = None
    if to_override is not None:
        if not is_plain_email(to_override.strip()):
            return FollowupOutcome("nothing", 2, [
                f"'{to_override}' is not a plain email address (use name@example.com, "
                "without a display name)."])
        address = to_override.strip()
    elif person["email"]:
        address = person["email"]
    elif ask_email is not None and gmail_config.configured and not preview_only:
        address = ask_email(person["full_name"])
        if address is not None and not is_plain_email(address):
            address = None
    if address and remember_email and address != person["email"]:
        with conn:
            conn.execute("UPDATE people SET email = ? WHERE id = ?", (address, person["id"]))
        messages.append(f"Saved {address} as the email address for {person['full_name']}.")

    draft = compose_email(person["full_name"], items, to=[address] if address else [],
                          sender_name=sender_name)

    def preview(reason: str, code: int) -> FollowupOutcome:
        path = save_preview(draft, preview_dir, today)
        out = FollowupOutcome("preview", code, messages + [
            f"No Gmail draft was created: {reason}", f"Local preview saved to {path}"],
            draft=draft, preview_path=path)
        _record(conn, person["id"], out)
        return out

    if preview_only:
        return preview("--preview was requested.", 0)
    if not gmail_config.configured:
        return preview("Gmail MCP is not configured. " + SETUP_HINT, 0)
    if not address:
        return preview(f"no email address is known for {person['full_name']}. Re-run with "
                       "--to name@example.com (add --remember-email to keep it).", 0)
    if not force:
        previous = _previous_draft(conn, person["id"], draft.item_ids)
        if previous:
            return FollowupOutcome("nothing", 0, messages + [
                f"A Gmail draft for exactly these items was already created at "
                f"{previous['created_at']} (draft ID {previous['draft_id'] or 'unknown'}). "
                "Use --force to create another."], draft=draft)

    try:
        with gmail_factory(gmail_config) as gmail:
            draft_id = gmail.create_draft(draft.to, draft.subject, draft.body)
    except GmailUnavailable:
        return preview("Gmail MCP is not configured. " + SETUP_HINT, 0)
    except GmailError as exc:
        return preview(f"{exc}", 1)
    except Exception as exc:  # never lose the email because of an unexpected failure
        return preview(f"unexpected error talking to Gmail MCP: {type(exc).__name__}: {exc}", 1)

    outcome = FollowupOutcome("draft", 0, messages + [
        f"Gmail draft created for {person['full_name']} <{address}>"
        + (f" (draft ID {draft_id})." if draft_id else ".")
        + " It has NOT been sent; review and send it from Gmail."],
        draft=draft, draft_id=draft_id)
    _record(conn, person["id"], outcome)
    return outcome
