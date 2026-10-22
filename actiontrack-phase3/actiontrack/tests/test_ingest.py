import json
from pathlib import Path

import pytest

from actiontrack.ingest import IngestError, ingest_folder
from actiontrack.storage import connect
from conftest import FIXTURES, tree_hashes, write

HEADER = "Date: 2025-09-22\nAttendees: Priya Sharma, Rahul Verma\n"


def items(conn):
    return conn.execute("SELECT * FROM action_items ORDER BY id").fetchall()


def by_task(conn, fragment):
    rows = [r for r in items(conn) if fragment.lower() in r["task"].lower()]
    assert rows, fragment
    return rows


# ---- fixture folder end to end ---------------------------------------------------

def test_fixture_folder_report(conn, notes_dir):
    r = ingest_folder(conn, notes_dir)
    assert r.md_files_found == 17
    assert len(r.ignored) == 1 and "__MACOSX" in r.ignored[0].file
    assert sorted(f.file for f in r.skipped) == [
        "bad-date.md", "broken-frontmatter.md", "empty.md", "malformed-no-date.md"]
    assert [f.file for f in r.duplicate_files] == ["copy-of-sprint-planning.md"]
    assert r.parsed_files == 11
    assert r.items_seen == 38 and r.items_new == 35 and len(r.merged) == 3
    assert {i["owner_raw"] for i in r.ambiguous} == {"Priya"}
    assert {i["owner_raw"] for i in r.unverified} == {"Vikram"}
    assert len(r.unassigned) == 4 and len(r.missing_due) == 10 and len(r.unparseable_due) == 4
    assert r.files_without_items == ["2025-10-15-all-hands.md"]


def test_original_fixture_files_never_modified(conn):
    before = tree_hashes(FIXTURES)
    ingest_folder(conn, FIXTURES)
    ingest_folder(conn, FIXTURES)
    assert tree_hashes(FIXTURES) == before


def test_source_provenance_preserved(conn, notes_dir):
    ingest_folder(conn, notes_dir)
    item = by_task(conn, "revised quote by Friday")[0]
    sources = conn.execute(
        "SELECT m.source_file, s.line_no, s.raw_line, s.due_date FROM item_sources s "
        "JOIN meetings m ON m.id = s.meeting_id WHERE s.item_id = ? ORDER BY m.meeting_date",
        (item["id"],)).fetchall()
    assert [(s["source_file"], s["line_no"]) for s in sources] == [
        ("2025-09-22-sprint-planning.md", 10), ("2025-09-29-sprint-review.md", 14)]
    assert [s["due_date"] for s in sources] == ["2025-09-26", "2025-10-03"]
    assert item["source_file"] == "2025-09-22-sprint-planning.md"
    assert item["meeting_date"] == "2025-09-22"
    assert item["due_date"] == "2025-10-03"   # latest mention's deadline
    assert conn.execute("SELECT source_file FROM meetings WHERE source_file LIKE 'client/%'"
                        ).fetchone()[0] == "client/2025-10-08-client-call.md"


# ---- idempotency and status ----------------------------------------------------

def test_reingest_creates_no_duplicates(conn, notes_dir):
    first = ingest_folder(conn, notes_dir)
    snapshot = [dict(r) for r in items(conn)]
    second = ingest_folder(conn, notes_dir)
    assert second.items_new == 0 and second.merged == []
    assert second.items_already_known == first.items_seen
    assert second.unchanged_files == first.parsed_files
    after = [dict(r) for r in items(conn)]
    for row in after + snapshot:
        row.pop("updated_at")
    assert after == snapshot


def test_reingest_preserves_cli_done_status(conn, notes_dir):
    ingest_folder(conn, notes_dir)
    item = by_task(conn, "book the demo room")[0]
    assert item["status"] == "open"
    with conn:  # what `actiontrack done` (Phase 2) will do
        conn.execute("UPDATE action_items SET status='done', completed_on='2025-09-24',"
                     " completed_by='cli' WHERE id=?", (item["id"],))
    ingest_folder(conn, notes_dir)
    assert conn.execute("SELECT status FROM action_items WHERE id=?",
                        (item["id"],)).fetchone()[0] == "done"


def test_ticking_box_in_edited_note_marks_done_but_unticking_never_reopens(conn, tmp_path):
    folder = tmp_path / "n"
    path = write(folder, "a.md", HEADER + "- [ ] Rahul to fix CI by Friday\n")
    ingest_folder(conn, folder)
    path.write_text(HEADER + "- [x] Rahul to fix CI by Friday\n", encoding="utf-8")
    r = ingest_folder(conn, folder)
    assert r.changed_files == ["a.md"] and len(r.marked_done_from_notes) == 1
    assert items(conn)[0]["status"] == "done"
    path.write_text(HEADER + "- [ ] Rahul to fix CI by Friday\n", encoding="utf-8")
    ingest_folder(conn, folder)
    assert len(items(conn)) == 1 and items(conn)[0]["status"] == "done"


def test_persistence_across_connections(db_path, notes_dir):
    c1 = connect(db_path)
    ingest_folder(c1, notes_dir)
    count = len(items(c1))
    c1.close()
    c2 = connect(db_path)
    assert len(items(c2)) == count
    assert c2.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 1
    c2.close()


# ---- duplicates -------------------------------------------------------------------

def test_same_owner_same_task_across_notes_merged(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "a.md", HEADER + "- [ ] Rahul to fix CI by Friday\n")
    write(folder, "b.md", "Date: 2025-09-29\nAttendees: Rahul Verma\n"
                          "- [ ] **Rahul Verma** to fix CI by Monday.\n")
    r = ingest_folder(conn, folder)
    assert len(items(conn)) == 1 and len(r.merged) == 1
    assert items(conn)[0]["due_date"] == "2025-10-06"


def test_same_task_different_owner_not_merged(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "a.md", HEADER + "- [ ] Rahul to fix CI\n- [ ] Priya to fix CI\n")
    ingest_folder(conn, folder)
    assert len(items(conn)) == 2


def test_similar_tasks_reported_not_merged(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "a.md", HEADER + "- [ ] Priya to send the revised quote\n")
    write(folder, "b.md", HEADER.replace("22", "29") + "- [ ] Priya to send the revised quotation\n")
    r = ingest_folder(conn, folder)
    assert len(items(conn)) == 2
    assert len(r.possible_duplicates) == 1 and "similar" in r.possible_duplicates[0]["reason"]


def test_unknown_owner_tasks_not_merged_across_notes(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "a.md", HEADER + "- [ ] Draft the release notes\n")
    write(folder, "b.md", HEADER.replace("22", "29") + "- [ ] Draft the release notes\n")
    r = ingest_folder(conn, folder)
    assert len(items(conn)) == 2
    assert "owner unknown" in r.possible_duplicates[0]["reason"]


def test_recurring_task_after_completion_is_new_item(conn, notes_dir):
    ingest_folder(conn, notes_dir)
    rows = by_task(conn, "weekly status report")
    assert [r["status"] for r in rows] == ["done", "open"]


def test_identical_file_copies_skipped(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "a.md", HEADER + "- [ ] Rahul to fix CI\n")
    write(folder, "sub/a copy.md", HEADER + "- [ ] Rahul to fix CI\n")
    r = ingest_folder(conn, folder)
    assert len(r.duplicate_files) == 1 and len(items(conn)) == 1


# ---- owners, names, missing values --------------------------------------------------

def test_ambiguous_first_name_not_assigned_and_not_merged(conn, tmp_path):
    folder = tmp_path / "n"
    hdr = "Attendees: Priya Sharma, Priya Nair\n"
    write(folder, "a.md", "Date: 2025-10-01\n" + hdr + "- [ ] Priya to review wireframes\n")
    write(folder, "b.md", "Date: 2025-10-02\n" + hdr + "- [ ] Priya to review wireframes\n")
    ingest_folder(conn, folder)
    rows = items(conn)
    assert len(rows) == 2
    assert all(r["owner_status"] == "ambiguous" and r["owner_id"] is None for r in rows)
    assert json.loads(rows[0]["owner_candidates"]) == ["Priya Nair", "Priya Sharma"]


def test_same_first_name_resolved_by_full_name(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "a.md", "Date: 2025-10-01\nAttendees: Priya Sharma, Priya Nair\n"
                          "- [ ] Priya Nair to export icons\n- [ ] Priya Sharma to send quote\n")
    ingest_folder(conn, folder)
    owners = conn.execute("SELECT p.full_name FROM action_items i JOIN people p"
                          " ON p.id = i.owner_id ORDER BY i.id").fetchall()
    assert [o[0] for o in owners] == ["Priya Nair", "Priya Sharma"]


def test_missing_owner_and_due_stored_as_null(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "a.md", HEADER + "- [ ] Draft the release notes\n")
    r = ingest_folder(conn, folder)
    row = items(conn)[0]
    assert row["owner_id"] is None and row["owner_status"] == "unassigned"
    assert row["due_date"] is None and row["due_error"] is None
    assert len(r.missing_due) == 1 and len(r.unassigned) == 1


def test_attendee_emails_recorded_only_when_written(conn, notes_dir):
    ingest_folder(conn, notes_dir)
    emails = dict(conn.execute("SELECT full_name, email FROM people").fetchall())
    assert emails["Priya Sharma"] == "priya.sharma@example.com"
    assert emails["Ananya Iyer"] is None


# ---- folder / file errors -------------------------------------------------------

def test_malformed_files_skipped_valid_ones_ingested(conn, tmp_path):
    folder = tmp_path / "n"
    write(folder, "good.md", HEADER + "- [ ] Rahul to fix CI\n")
    write(folder, "bad.md", "no date here\n- [ ] Rahul to x\n")
    (folder / "binary.md").write_bytes(b"\x00\x01\x02")
    r = ingest_folder(conn, folder)
    assert {f.file for f in r.skipped} == {"bad.md", "binary.md"}
    assert len(items(conn)) == 1


def test_unreadable_file_skipped(conn, tmp_path, monkeypatch):
    folder = tmp_path / "n"
    write(folder, "good.md", HEADER + "- [ ] Rahul to fix CI\n")
    locked = write(folder, "locked.md", HEADER + "- [ ] Rahul to y\n")
    original = Path.read_bytes

    def fake_read(self):
        if self.name == locked.name:
            raise PermissionError(13, "Permission denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read)
    r = ingest_folder(conn, folder)
    assert r.skipped[0].file == "locked.md" and "unreadable" in r.skipped[0].reason
    assert len(items(conn)) == 1


def test_unexpected_parser_error_does_not_crash(conn, tmp_path, monkeypatch):
    import actiontrack.ingest as ingest_mod
    folder = tmp_path / "n"
    write(folder, "a.md", HEADER + "- [ ] Rahul to fix CI\n")
    write(folder, "b.md", HEADER + "- [ ] Rahul to other\n")
    real = ingest_mod.parse_file

    def flaky(path, rel):
        if rel == "a.md":
            raise RuntimeError("boom")
        return real(path, rel)

    monkeypatch.setattr(ingest_mod, "parse_file", flaky)
    r = ingest_folder(conn, folder)
    assert "boom" in r.skipped[0].reason and len(items(conn)) == 1


def test_nonexistent_folder(conn, tmp_path):
    with pytest.raises(IngestError, match="not found"):
        ingest_folder(conn, tmp_path / "missing")


def test_file_instead_of_folder(conn, tmp_path):
    f = write(tmp_path, "a.md", HEADER)
    with pytest.raises(IngestError, match="not a folder"):
        ingest_folder(conn, f)


def test_empty_folder(conn, tmp_path):
    (tmp_path / "empty").mkdir()
    r = ingest_folder(conn, tmp_path / "empty")
    assert r.md_files_found == 0 and items(conn) == []


def test_non_recursive(conn, notes_dir):
    r = ingest_folder(conn, notes_dir, recursive=False)
    assert not any(f.startswith("client/") for f in
                   [m[0] for m in conn.execute("SELECT source_file FROM meetings")])
    assert r.md_files_found == 15
