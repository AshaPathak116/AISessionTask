"""Read/update operations used by `list` and `done`.

Status semantics
----------------
* ``done``    -> status is done.
* ``overdue`` -> status is open AND a known due date is earlier than today.
* ``open``    -> status is open (this INCLUDES overdue items; the displayed status
                 column still says "overdue" for them).
Items without a due date are never overdue.

Filters are always combined with AND.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from .parser import name_key
from .storage import now_iso

STATUSES = ("open", "done", "overdue")


class QueryError(Exception):
    """A user-facing problem (bad owner, bad ID).  ``code`` is the CLI exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- owners


@dataclass
class OwnerMatch:
    label: str                      # what to call the owner in messages
    person_id: int | None = None    # resolved person, or
    raw_key: str | None = None      # an unverified name exactly as written in notes


def resolve_owner_query(conn: sqlite3.Connection, query: str | None = None,
                        owner_id: int | None = None) -> OwnerMatch:
    """Turn ``--owner``/``--owner-id`` into exactly one owner, or raise.

    Never picks between several people with the same first name.
    """
    if owner_id is not None:
        row = conn.execute("SELECT id, full_name FROM people WHERE id = ?",
                           (owner_id,)).fetchone()
        if row is None:
            raise QueryError(f"no person with owner ID {owner_id}", code=2)
        return OwnerMatch(row["full_name"], person_id=row["id"])

    key = name_key(query or "")
    if not key:
        raise QueryError("owner name is empty", code=2)
    people = conn.execute("SELECT id, full_name, name_key FROM people ORDER BY full_name"
                          ).fetchall()

    exact = [p for p in people if p["name_key"] == key]
    if exact:
        return OwnerMatch(exact[0]["full_name"], person_id=exact[0]["id"])

    tokens = key.split()
    if len(tokens) == 1:
        matches = [p for p in people if p["name_key"].split()[0] == key]
        if not matches:
            matches = [p for p in people if p["name_key"].split()[-1] == key]
    else:  # "Priya S"
        matches = [p for p in people
                   if p["name_key"].split()[0] == tokens[0]
                   and len(p["name_key"].split()) > 1
                   and p["name_key"].split()[-1].startswith(tokens[-1])]

    if len(matches) == 1:
        return OwnerMatch(matches[0]["full_name"], person_id=matches[0]["id"])
    if len(matches) > 1:
        options = ", ".join(f"{p['full_name']} (owner ID {p['id']})" for p in matches)
        raise QueryError(
            f"'{query}' matches more than one person: {options}. "
            "Use the full name or --owner-id.", code=2)

    # Not a known person: maybe an unverified name kept exactly as written.
    unverified = conn.execute(
        "SELECT owner_raw FROM action_items WHERE owner_status = 'unverified'").fetchall()
    if any(name_key(r["owner_raw"] or "") == key for r in unverified):
        return OwnerMatch(f"{query} (unverified)", raw_key=key)
    raise QueryError(f"no owner matching '{query}'", code=2)


# --------------------------------------------------------------------------- listing


@dataclass
class ListedItem:
    id: int
    uid: str
    owner: str
    owner_status: str
    task: str
    due_date: str | None
    due_raw: str | None
    status: str           # open | done | overdue (computed)
    source_file: str
    meeting_date: str
    sources: int


@dataclass
class ListResult:
    items: list[ListedItem]
    today: date
    owner: OwnerMatch | None = None
    excluded_no_due: int = 0            # hidden by --due-before because due is unknown
    related_unassigned: list[int] = field(default_factory=list)  # ambiguous mentions
    database_empty: bool = False


def owner_label(row: sqlite3.Row) -> str:
    status = row["owner_status"]
    if status == "resolved":
        return row["full_name"]
    if status == "unverified":
        return f"{row['owner_raw']} (unverified)"
    if status == "ambiguous":
        return f"{row['owner_raw']}? (ambiguous)"
    if status == "multiple":
        return f"{row['owner_raw']} (multiple)"
    return "(unassigned)"


def computed_status(status: str, due: str | None, today: date) -> str:
    if status == "open" and due is not None and due < today.isoformat():
        return "overdue"
    return status


def list_items(conn: sqlite3.Connection, *, today: date, owner: OwnerMatch | None = None,
               status: str | None = None, due_before: date | None = None) -> ListResult:
    if status is not None and status not in STATUSES:
        raise QueryError(f"status must be one of {', '.join(STATUSES)}", code=2)

    result = ListResult(items=[], today=today, owner=owner)
    if conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 0:
        result.database_empty = True
        return result

    where, params = [], []
    if owner is not None:
        if owner.person_id is not None:
            where.append("i.owner_status = 'resolved' AND i.owner_id = ?")
            params.append(owner.person_id)
        else:
            where.append("i.owner_status = 'unverified'")
    if status == "done":
        where.append("i.status = 'done'")
    elif status == "open":
        where.append("i.status = 'open'")
    elif status == "overdue":
        where.append("i.status = 'open' AND i.due_date IS NOT NULL AND i.due_date < ?")
        params.append(today.isoformat())

    base = (" FROM action_items i LEFT JOIN people p ON p.id = i.owner_id"
            + (" WHERE " + " AND ".join(where) if where else ""))
    rows = conn.execute(
        "SELECT i.*, p.full_name, (SELECT COUNT(*) FROM item_sources s"
        " WHERE s.item_id = i.id) AS n_sources" + base
        + " ORDER BY i.due_date IS NULL, i.due_date, i.id", params).fetchall()

    if owner is not None and owner.raw_key is not None:  # exact raw-name match
        rows = [r for r in rows if name_key(r["owner_raw"] or "") == owner.raw_key]

    for r in rows:
        if due_before is not None:
            if r["due_date"] is None:
                result.excluded_no_due += 1
                continue
            if r["due_date"] >= due_before.isoformat():
                continue
        result.items.append(ListedItem(
            id=r["id"], uid=r["uid"], owner=owner_label(r), owner_status=r["owner_status"],
            task=r["task"], due_date=r["due_date"], due_raw=r["due_raw"],
            status=computed_status(r["status"], r["due_date"], today),
            source_file=r["source_file"], meeting_date=r["meeting_date"],
            sources=r["n_sources"]))

    if owner is not None and owner.person_id is not None:
        # Tasks whose owner could be this person but wasn't assigned (never shown as theirs)
        for r in conn.execute("SELECT id, owner_candidates FROM action_items"
                              " WHERE owner_status IN ('ambiguous', 'multiple')"
                              " AND status = 'open' ORDER BY id"):
            if owner.label in json.loads(r["owner_candidates"]):
                result.related_unassigned.append(r["id"])
    return result


# --------------------------------------------------------------------------- done


@dataclass
class DoneResult:
    id: int
    task: str
    already_done: bool
    completed_on: str
    completed_by: str


def find_item(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """Look an item up by numeric ID (``12`` or ``#12``) or by its stable uid."""
    text = (ref or "").strip().lstrip("#")
    row = None
    if text.isdigit():
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (int(text),)).fetchone()
    elif text:
        row = conn.execute("SELECT * FROM action_items WHERE uid = ?",
                           (text.lower(),)).fetchone()
    if row is None:
        raise QueryError(f"no action item with ID '{ref}'. Run `actiontrack list` to see IDs.")
    return row


def mark_done(conn: sqlite3.Connection, ref: str, *, today: date) -> DoneResult:
    row = find_item(conn, ref)
    if row["status"] == "done":
        return DoneResult(row["id"], row["task"], True, row["completed_on"] or "unknown date",
                          row["completed_by"] or "unknown")
    with conn:
        conn.execute(
            "UPDATE action_items SET status = 'done', completed_on = ?, completed_by = 'cli',"
            " updated_at = ? WHERE id = ? AND status = 'open'",
            (today.isoformat(), now_iso(), row["id"]))
    return DoneResult(row["id"], row["task"], False, today.isoformat(), "cli")
