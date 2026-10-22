"""Folder ingestion.

Guarantees
----------
* One bad file never stops the run: it is skipped and reported with a reason.
* Re-ingesting the same notes changes nothing (each line occurrence is keyed by
  file + normalised text + occurrence number).
* Ingest only ever moves an item open -> done, never done -> open.
* A task is merged with an earlier one only when the *resolved* owner and the
  normalised task text (minus the due-date phrase) are identical.  Tasks with
  unknown / ambiguous / unverified owners are never merged across files.
  Merely similar tasks are recorded in ``possible_duplicates``, not merged.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .parser import MalformedNoteError, ParsedItem, ParsedNote, name_key, parse_file
from .storage import now_iso, record_run, upsert_person

SIMILARITY_THRESHOLD = 0.8
# "notes (1).md", "notes - Copy.md", "Copy of notes.md", "notes_copy2.md"
COPY_NAME_RE = re.compile(r"\s\(\d+\)$|(?:^|[\s_\-])copy(?:\s*\(?\d+\)?)?$|^copy of\b", re.I)


def is_copy_name(rel_path: str) -> bool:
    return bool(COPY_NAME_RE.search(Path(rel_path).stem.strip()))
IGNORED_DIRS = {"__MACOSX", ".git", ".obsidian", ".trash", "node_modules"}


class IngestError(Exception):
    """Fatal problem with the ingest request itself (e.g. folder missing)."""


@dataclass
class FileIssue:
    file: str
    reason: str


@dataclass
class IngestReport:
    folder: str
    md_files_found: int = 0
    ignored: list[FileIssue] = field(default_factory=list)
    skipped: list[FileIssue] = field(default_factory=list)
    duplicate_files: list[FileIssue] = field(default_factory=list)
    parsed_files: int = 0
    new_files: int = 0
    unchanged_files: int = 0
    changed_files: list[str] = field(default_factory=list)
    files_without_items: list[str] = field(default_factory=list)
    files_without_attendees: list[str] = field(default_factory=list)
    warnings: list[FileIssue] = field(default_factory=list)
    items_seen: int = 0
    items_new: int = 0
    items_already_known: int = 0
    merged: list[dict] = field(default_factory=list)
    marked_done_from_notes: list[int] = field(default_factory=list)
    stale_sources: list[FileIssue] = field(default_factory=list)
    missing_due: list[dict] = field(default_factory=list)
    unparseable_due: list[dict] = field(default_factory=list)
    unassigned: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)
    multiple_owners: list[dict] = field(default_factory=list)
    possible_duplicates: list[dict] = field(default_factory=list)
    db_totals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def counts(self) -> dict:
        return {
            "md_files_found": self.md_files_found,
            "ignored": len(self.ignored),
            "skipped_malformed": len(self.skipped),
            "duplicate_files": len(self.duplicate_files),
            "parsed_files": self.parsed_files,
            "items_seen": self.items_seen,
            "items_new": self.items_new,
            "items_already_known": self.items_already_known,
            "merged_duplicates": len(self.merged),
            "marked_done_from_notes": len(self.marked_done_from_notes),
            "missing_due": len(self.missing_due),
            "unparseable_due": len(self.unparseable_due),
            "unassigned": len(self.unassigned),
            "ambiguous_owner": len(self.ambiguous),
            "unverified_owner": len(self.unverified),
            "multiple_owners": len(self.multiple_owners),
            "possible_duplicates": len(self.possible_duplicates),
        }


# --------------------------------------------------------------------------- discovery


def discover(folder: Path, recursive: bool = True) -> tuple[list[Path], list[FileIssue]]:
    pattern = folder.rglob("*") if recursive else folder.glob("*")
    found, ignored = [], []
    for path in sorted(pattern):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        rel_parts = path.relative_to(folder).parts
        rel = path.relative_to(folder).as_posix()
        if any(part in IGNORED_DIRS for part in rel_parts[:-1]) or path.name.startswith("._"):
            ignored.append(FileIssue(rel, "archive/tool metadata (e.g. __MACOSX, ._ file)"))
            continue
        found.append(path)
    return found, ignored


# --------------------------------------------------------------------------- ingest


def ingest_folder(conn: sqlite3.Connection, folder: str | Path, *,
                  recursive: bool = True, record: bool = True) -> IngestReport:
    root = Path(folder).expanduser()
    if not root.exists():
        raise IngestError(f"folder not found: {root}")
    if not root.is_dir():
        raise IngestError(f"not a folder: {root}")
    root = root.resolve()

    report = IngestReport(folder=str(root))
    files, report.ignored = discover(root, recursive)
    report.md_files_found = len(files) + len(report.ignored)

    parsed: list[tuple[ParsedNote, Path]] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            parsed.append((parse_file(path, rel), path))
        except MalformedNoteError as exc:
            report.skipped.append(FileIssue(rel, str(exc)))
        except OSError as exc:
            report.skipped.append(FileIssue(rel, f"unreadable: {exc.strerror or exc}"))
        except Exception as exc:  # never let one file crash the run
            report.skipped.append(FileIssue(rel, f"unexpected parser error: "
                                                 f"{type(exc).__name__}: {exc}"))

    # Oldest meeting first so "latest mention wins" and completion order are stable.
    # Within a date, originals come before copies ("x (1).md"), so when two files
    # are identical the copy is the one reported and skipped.
    parsed.sort(key=lambda pair: (pair[0].meeting_date, is_copy_name(pair[0].rel_path),
                                  pair[0].rel_path))
    touched: set[int] = set()
    for note, path in parsed:
        try:
            with conn:
                _store_note(conn, note, path, report, touched)
        except Exception as exc:
            report.skipped.append(FileIssue(note.rel_path, f"database error, file rolled "
                                                           f"back: {type(exc).__name__}: {exc}"))

    with conn:
        report.possible_duplicates = _find_possible_duplicates(conn, touched)
    _summarise(conn, report, touched)
    if record:
        record_run(conn, str(root), report.counts())
    return report


def _store_note(conn: sqlite3.Connection, note: ParsedNote, path: Path,
                report: IngestReport, touched: set[int]) -> None:
    now = now_iso()
    attendees_json = json.dumps([{"name": a.name, "email": a.email} for a in note.attendees])
    existing = conn.execute("SELECT * FROM meetings WHERE source_file = ?",
                            (note.rel_path,)).fetchone()
    if existing is None:
        twin = conn.execute("SELECT source_file FROM meetings WHERE content_hash = ?",
                            (note.content_hash,)).fetchone()
        if twin:
            report.duplicate_files.append(
                FileIssue(note.rel_path, f"identical content to {twin['source_file']}; skipped"))
            return
        meeting_id = conn.execute(
            "INSERT INTO meetings(source_file, source_path, title, meeting_date, attendees,"
            " content_hash, first_ingested_at, last_ingested_at) VALUES (?,?,?,?,?,?,?,?)",
            (note.rel_path, str(path), note.title, note.meeting_date.isoformat(),
             attendees_json, note.content_hash, now, now),
        ).lastrowid
        report.new_files += 1
        changed = False
    else:
        meeting_id = existing["id"]
        changed = existing["content_hash"] != note.content_hash
        conn.execute(
            "UPDATE meetings SET source_path=?, title=?, meeting_date=?, attendees=?,"
            " content_hash=?, last_ingested_at=? WHERE id=?",
            (str(path), note.title, note.meeting_date.isoformat(), attendees_json,
             note.content_hash, now, meeting_id),
        )
        if changed:
            report.changed_files.append(note.rel_path)
        else:
            report.unchanged_files += 1

    report.parsed_files += 1
    for w in note.warnings:
        report.warnings.append(FileIssue(note.rel_path, w))
    if not note.items:
        report.files_without_items.append(note.rel_path)
    if not note.attendees:
        report.files_without_attendees.append(note.rel_path)

    for attendee in note.attendees:
        _, warning = upsert_person(conn, attendee.name, name_key(attendee.name), attendee.email)
        if warning:
            report.warnings.append(FileIssue(note.rel_path, warning))

    seen_keys = set()
    for item in note.items:
        report.items_seen += 1
        seen_keys.add(item.occurrence_key)
        src = conn.execute(
            "SELECT s.id, s.item_id, s.checked, i.status FROM item_sources s"
            " JOIN action_items i ON i.id = s.item_id"
            " WHERE s.meeting_id = ? AND s.occurrence_key = ?",
            (meeting_id, item.occurrence_key),
        ).fetchone()
        if src:  # already ingested -> idempotent; only allow open -> done
            report.items_already_known += 1
            touched.add(src["item_id"])
            if item.checked and not src["checked"]:
                conn.execute("UPDATE item_sources SET checked = 1 WHERE id = ?", (src["id"],))
            if item.checked and src["status"] == "open":
                _mark_done_from_notes(conn, src["item_id"], note, now)
                report.marked_done_from_notes.append(src["item_id"])
            continue

        item_id = _find_or_create_item(conn, note, item, report, now)
        touched.add(item_id)
        conn.execute(
            "INSERT INTO item_sources(item_id, meeting_id, occurrence_key, line_no, raw_line,"
            " checked, due_date, due_raw, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, meeting_id, item.occurrence_key, item.line_no, item.raw_line,
             int(item.checked), item.due_date.isoformat() if item.due_date else None,
             item.due_raw, now),
        )

    if changed:
        rows = conn.execute("SELECT occurrence_key, line_no FROM item_sources WHERE meeting_id=?",
                            (meeting_id,)).fetchall()
        gone = [r for r in rows if r["occurrence_key"] not in seen_keys]
        if gone:
            report.stale_sources.append(FileIssue(
                note.rel_path, f"{len(gone)} previously ingested item line(s) no longer in "
                               "the file; existing records kept"))


def _mark_done_from_notes(conn, item_id: int, note: ParsedNote, now: str) -> None:
    conn.execute(
        "UPDATE action_items SET status='done', completed_on=?, completed_by='notes',"
        " updated_at=? WHERE id=? AND status='open'",
        (note.meeting_date.isoformat(), now, item_id),
    )


def _find_or_create_item(conn, note: ParsedNote, item: ParsedItem,
                         report: IngestReport, now: str) -> int:
    meeting_date = note.meeting_date.isoformat()
    owner = item.owner
    owner_id = None
    if owner.status == "resolved":
        owner_id, _ = upsert_person(conn, owner.name, name_key(owner.name))
        dedupe_key = f"p:{owner_id}|{item.norm_task}"
    else:
        dedupe_key = f"u:{note.rel_path}|{item.occurrence_key}"

    reappeared_from = None
    if owner.status == "resolved":
        rows = conn.execute(
            "SELECT * FROM action_items WHERE dedupe_key = ?"
            " ORDER BY last_meeting_date DESC, id DESC", (dedupe_key,)).fetchall()
        target = next((r for r in rows if r["status"] == "open"), None)
        if target is None and rows:
            latest_done = rows[0]
            # Same task seen again: merge if it is also ticked, or if this meeting
            # happened on/before the completion.  An *unticked* mention in a later
            # meeting is evidence of a new (e.g. recurring) instance.
            if item.checked or (latest_done["completed_on"]
                                and meeting_date <= latest_done["completed_on"]):
                target = latest_done
            else:
                reappeared_from = latest_done["id"]
        if target is not None:
            if _merge_into(conn, target, note, item, now):
                report.marked_done_from_notes.append(target["id"])
            report.merged.append({
                "item_id": target["id"], "file": note.rel_path, "line": item.line_no,
                "first_seen_in": target["source_file"], "task": target["task"],
            })
            return target["id"]

    uid = hashlib.sha1(f"{note.rel_path}|{item.occurrence_key}".encode()).hexdigest()[:10]
    item_id = conn.execute(
        "INSERT INTO action_items(uid, dedupe_key, owner_id, owner_raw, owner_status,"
        " owner_candidates, task, norm_task, due_date, due_raw, due_error, status,"
        " completed_on, completed_by, source_file, meeting_date, last_meeting_date,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, dedupe_key, owner_id, owner.raw, owner.status, json.dumps(owner.candidates),
         item.task, item.norm_task, item.due_date.isoformat() if item.due_date else None,
         item.due_raw, item.due_error, "done" if item.checked else "open",
         meeting_date if item.checked else None, "notes" if item.checked else None,
         note.rel_path, meeting_date, meeting_date, now, now),
    ).lastrowid
    report.items_new += 1
    if reappeared_from is not None:
        a, b = sorted((reappeared_from, item_id))
        conn.execute("INSERT OR IGNORE INTO possible_duplicates(item_a, item_b, reason)"
                     " VALUES (?,?,?)",
                     (a, b, "same owner and task reappears unticked after completion; "
                            "stored as a new item"))
    return item_id


def _merge_into(conn, target: sqlite3.Row, note: ParsedNote, item: ParsedItem, now: str) -> bool:
    """Attach a repeated mention to ``target``.  Returns True if it marked it done."""
    meeting_date = note.meeting_date.isoformat()
    updates = {"updated_at": now}
    if meeting_date >= target["last_meeting_date"]:
        updates["last_meeting_date"] = meeting_date
        if item.due_date:  # latest mention's known deadline wins; unknown never erases
            updates.update(due_date=item.due_date.isoformat(), due_raw=item.due_raw,
                           due_error=None)
    if item.checked and target["status"] == "open":
        updates.update(status="done", completed_on=meeting_date, completed_by="notes")
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE action_items SET {assignments} WHERE id = ?",
                 (*updates.values(), target["id"]))
    return updates.get("status") == "done"


def _find_possible_duplicates(conn, touched: set[int]) -> list[dict]:
    if not touched:
        return []
    rows = conn.execute("SELECT i.id, i.owner_id, i.owner_status, i.owner_candidates,"
                        " i.norm_task, i.source_file, i.task, p.full_name FROM action_items i"
                        " LEFT JOIN people p ON p.id = i.owner_id").fetchall()
    by_id = {r["id"]: r for r in rows}
    for item_id in touched:
        a = by_id.get(item_id)
        if a is None:
            continue
        for b in rows:
            if b["id"] == a["id"]:
                continue
            reason = None
            if a["owner_status"] == "resolved" and b["owner_id"] == a["owner_id"]:
                if a["norm_task"] != b["norm_task"]:
                    ratio = difflib.SequenceMatcher(None, a["norm_task"], b["norm_task"]).ratio()
                    if ratio >= SIMILARITY_THRESHOLD:
                        reason = f"similar task text for the same owner ({ratio:.0%}); not merged"
            elif (a["owner_status"] != "resolved" and b["owner_status"] != "resolved"
                  and a["norm_task"] == b["norm_task"] and a["source_file"] != b["source_file"]):
                reason = "same task text but owner unknown/ambiguous; not merged"
            elif {a["owner_status"], b["owner_status"]} & {"ambiguous", "multiple"} and \
                    "resolved" in (a["owner_status"], b["owner_status"]):
                vague, named = (a, b) if a["owner_status"] != "resolved" else (b, a)
                if named["full_name"] in json.loads(vague["owner_candidates"] or "[]"):
                    ratio = difflib.SequenceMatcher(None, a["norm_task"], b["norm_task"]).ratio()
                    if ratio >= SIMILARITY_THRESHOLD:
                        reason = (f"looks like the same task; owner is ambiguous in one note and "
                                  f"{named['full_name']} in the other; not merged")
            if reason:
                lo, hi = sorted((a["id"], b["id"]))
                conn.execute("INSERT OR IGNORE INTO possible_duplicates(item_a, item_b, reason)"
                             " VALUES (?,?,?)", (lo, hi, reason))
    placeholders = ",".join("?" * len(touched))
    pairs = conn.execute(
        f"SELECT p.item_a, p.item_b, p.reason, a.task AS task_a, b.task AS task_b,"
        f" a.source_file AS file_a, b.source_file AS file_b FROM possible_duplicates p"
        f" JOIN action_items a ON a.id = p.item_a JOIN action_items b ON b.id = p.item_b"
        f" WHERE p.item_a IN ({placeholders}) OR p.item_b IN ({placeholders})"
        f" ORDER BY p.item_a, p.item_b", (*touched, *touched)).fetchall()
    return [dict(r) for r in pairs]


def _summarise(conn, report: IngestReport, touched: set[int]) -> None:
    if touched:
        placeholders = ",".join("?" * len(touched))
        rows = conn.execute(
            f"SELECT i.*, p.full_name FROM action_items i LEFT JOIN people p ON p.id = i.owner_id"
            f" WHERE i.id IN ({placeholders}) ORDER BY i.id", tuple(touched)).fetchall()
        for r in rows:
            info = {"item_id": r["id"], "task": r["task"], "file": r["source_file"],
                    "owner_raw": r["owner_raw"]}
            if r["due_date"] is None:
                if r["due_error"]:
                    report.unparseable_due.append({**info, "due_raw": r["due_raw"],
                                                   "error": r["due_error"]})
                else:
                    report.missing_due.append(info)
            status = r["owner_status"]
            if status == "unassigned":
                report.unassigned.append(info)
            elif status == "ambiguous":
                report.ambiguous.append({**info, "candidates": json.loads(r["owner_candidates"])})
            elif status == "unverified":
                report.unverified.append(info)
            elif status == "multiple":
                report.multiple_owners.append({**info,
                                               "candidates": json.loads(r["owner_candidates"])})
    totals = conn.execute(
        "SELECT COUNT(*) AS items, SUM(status='open') AS open, SUM(status='done') AS done"
        " FROM action_items").fetchone()
    report.db_totals = {
        "meetings": conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0],
        "items": totals["items"] or 0,
        "open": totals["open"] or 0,
        "done": totals["done"] or 0,
        "people": conn.execute("SELECT COUNT(*) FROM people").fetchone()[0],
    }
