"""Phase 2: `list` filters, overdue logic, `done`, persistence, re-ingest safety."""

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from actiontrack.cli import main
from actiontrack.dates import today as get_today
from actiontrack.ingest import ingest_folder
from actiontrack.queries import QueryError, list_items, mark_done, resolve_owner_query
from actiontrack.storage import connect
from conftest import write

SRC = Path(__file__).resolve().parents[1] / "src"
TODAY = date(2025, 10, 6)

# Meeting on Wed 2025-10-01; explicit due dates make the expectations easy to read.
NOTE = """# Planning
Date: 2025-10-01
Attendees: Priya Sharma, Priya Nair, Rahul Verma

## Action Items
- [ ] Rahul to alpha by 2025-10-05
- [ ] Rahul to bravo by 2025-10-10
- [x] Rahul to charlie by 2025-10-03
- [ ] Rahul to delta
- [ ] Priya Sharma to echo by 2025-10-04
- [ ] Priya Nair to foxtrot by 2025-10-20
- [ ] Priya to golf by 2025-10-02
- [ ] Hotel the release notes by 2025-10-01
- [ ] Vikram to india by 2025-10-06
"""


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "notes"
    write(f, "2025-10-01-planning.md", NOTE)
    return f


@pytest.fixture
def db(folder, conn):
    ingest_folder(conn, folder)
    return conn


def words(result):
    return sorted(i.task.split()[0].lower() for i in result.items)


def owner(conn, name=None, owner_id=None):
    return resolve_owner_query(conn, name, owner_id)


def run(args, db_path, capsys):
    code = main(args + ["--db", str(db_path)])
    out = capsys.readouterr()
    return code, out.out, out.err


def item_id(conn, word):
    return conn.execute("SELECT id FROM action_items WHERE lower(task) LIKE ?",
                        (word + "%",)).fetchone()[0]


# ---- combined filters ------------------------------------------------------------

@pytest.mark.parametrize("who,status,before,expected", [
    (None, None, None, ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
                        "hotel", "india"]),
    ("Rahul", None, None, ["alpha", "bravo", "charlie", "delta"]),
    ("Rahul", "open", None, ["alpha", "bravo", "delta"]),
    ("Rahul", "overdue", None, ["alpha"]),
    ("Rahul", "done", None, ["charlie"]),
    ("Rahul", "open", date(2025, 10, 10), ["alpha"]),         # strictly before; no-due out
    ("Rahul", None, date(2025, 10, 11), ["alpha", "bravo", "charlie"]),
    (None, "overdue", None, ["alpha", "echo", "golf", "hotel"]),
    (None, "overdue", date(2025, 10, 3), ["golf", "hotel"]),
    ("Priya Sharma", "overdue", None, ["echo"]),
    ("Priya Nair", "overdue", None, []),
    ("Priya Nair", "open", date(2025, 10, 21), ["foxtrot"]),
    (None, "done", date(2025, 10, 1), []),
    ("Vikram", "open", None, ["india"]),
])
def test_combined_filters(db, who, status, before, expected):
    o = owner(db, who) if who else None
    assert words(list_items(db, today=TODAY, owner=o, status=status, due_before=before)) \
        == expected


def test_due_before_reports_excluded_unknown_dates(db):
    r = list_items(db, today=TODAY, owner=owner(db, "Rahul"), due_before=date(2025, 12, 1))
    assert r.excluded_no_due == 1          # delta has no due date
    assert "delta" not in words(r)


def test_results_sorted_by_due_date_then_unknown_last(db):
    r = list_items(db, today=TODAY, owner=owner(db, "Rahul"))
    assert [i.due_date for i in r.items] == ["2025-10-03", "2025-10-05", "2025-10-10", None]


# ---- overdue -------------------------------------------------------------------

@pytest.mark.parametrize("today,expected", [
    (date(2025, 10, 4), []),              # due 10-05: not yet
    (date(2025, 10, 5), []),              # due today is NOT overdue
    (date(2025, 10, 6), ["alpha"]),       # one day late
    (date(2026, 1, 1), ["alpha", "bravo"]),
])
def test_overdue_boundaries(db, today, expected):
    r = list_items(db, today=today, owner=owner(db, "Rahul"), status="overdue")
    assert words(r) == expected


def test_done_and_undated_items_are_never_overdue(db):
    far_future = date(2030, 1, 1)
    r = list_items(db, today=far_future, owner=owner(db, "Rahul"), status="overdue")
    assert "charlie" not in words(r) and "delta" not in words(r)
    statuses = {i.task.split()[0].lower(): i.status
                for i in list_items(db, today=far_future, owner=owner(db, "Rahul")).items}
    assert statuses == {"alpha": "overdue", "bravo": "overdue", "charlie": "done",
                        "delta": "open"}


def test_open_filter_includes_overdue_items(db):
    r = list_items(db, today=TODAY, status="open")
    assert {i.status for i in r.items} == {"open", "overdue"}


def test_overdue_uses_injected_today_not_meeting_date(db, monkeypatch):
    monkeypatch.setenv("ACTIONTRACK_TODAY", "2025-10-03")
    assert get_today() == date(2025, 10, 3)
    assert get_today("2025-10-09") == date(2025, 10, 9)   # explicit beats env
    monkeypatch.delenv("ACTIONTRACK_TODAY")
    assert get_today() == date.today()


def test_cli_today_flag_and_env(folder, db_path, capsys, monkeypatch):
    main(["ingest", str(folder), "--db", str(db_path)])
    capsys.readouterr()
    _, out, _ = run(["list", "--status", "overdue", "--today", "2025-10-03"], db_path, capsys)
    assert "Golf" in out and "Alpha" not in out
    monkeypatch.setenv("ACTIONTRACK_TODAY", "2025-10-06")
    _, out, _ = run(["list", "--status", "overdue"], db_path, capsys)
    assert "Alpha" in out and "today = 2025-10-06" in out


# ---- owners ----------------------------------------------------------------------

def test_ambiguous_first_name_refused_with_choices(db, db_path, capsys):
    with pytest.raises(QueryError) as exc:
        owner(db, "priya")
    assert "Priya Nair (owner ID" in str(exc.value) and "Priya Sharma (owner ID" in str(exc.value)
    code, out, err = run(["list", "--owner", "Priya"], db_path, capsys)
    assert code == 2 and out == "" and "matches more than one person" in err


def test_owner_id_and_name_variants(db):
    sharma = owner(db, "Priya Sharma")
    assert owner(db, owner_id=sharma.person_id).label == "Priya Sharma"
    assert owner(db, "PRIYA  sharma").person_id == sharma.person_id
    assert owner(db, "Sharma").person_id == sharma.person_id
    assert owner(db, "Priya S.").person_id == sharma.person_id
    assert owner(db, "rahul").label == "Rahul Verma"


def test_unknown_owner_and_owner_id(db):
    with pytest.raises(QueryError, match="no owner matching"):
        owner(db, "Zed")
    with pytest.raises(QueryError, match="no person with owner ID"):
        owner(db, owner_id=999)


def test_ambiguous_task_never_listed_under_a_candidate_but_flagged(db):
    r = list_items(db, today=TODAY, owner=owner(db, "Priya Sharma"))
    assert "golf" not in words(r)
    assert r.related_unassigned == [item_id(db, "golf")]


def test_unassigned_and_unknown_owner_display(db):
    labels = {i.task.split()[0].lower(): i.owner for i in list_items(db, today=TODAY).items}
    assert labels["hotel"] == "(unassigned)"
    assert labels["golf"] == "Priya? (ambiguous)"
    assert labels["india"] == "Vikram (unverified)"


# ---- done ------------------------------------------------------------------------

def test_done_marks_and_is_idempotent(db):
    iid = item_id(db, "alpha")
    first = mark_done(db, str(iid), today=TODAY)
    assert not first.already_done and first.completed_by == "cli"
    second = mark_done(db, str(iid), today=date(2025, 10, 9))
    assert second.already_done and second.completed_on == "2025-10-06"   # not overwritten
    row = db.execute("SELECT status, completed_on, completed_by FROM action_items WHERE id=?",
                     (iid,)).fetchone()
    assert tuple(row) == ("done", "2025-10-06", "cli")


def test_done_on_item_completed_in_notes(db):
    r = mark_done(db, str(item_id(db, "charlie")), today=TODAY)
    assert r.already_done and r.completed_by == "notes"


def test_done_accepts_hash_prefix_and_uid(db):
    iid = item_id(db, "bravo")
    uid = db.execute("SELECT uid FROM action_items WHERE id=?", (iid,)).fetchone()[0]
    assert mark_done(db, f"#{iid}", today=TODAY).id == iid
    assert mark_done(db, uid.upper(), today=TODAY).already_done


@pytest.mark.parametrize("ref", ["999", "0", "abc", "#", "", "  ", "1.5", "12abc"])
def test_done_invalid_ids(db, ref):
    with pytest.raises(QueryError, match="no action item with ID"):
        mark_done(db, ref, today=TODAY)


def test_cli_done_messages_and_exit_codes(folder, db_path, capsys):
    main(["ingest", str(folder), "--db", str(db_path)])
    capsys.readouterr()
    code, out, _ = run(["done", "1", "--today", "2025-10-06"], db_path, capsys)
    assert code == 0 and out.startswith("Marked #1 done:")
    code, out, _ = run(["done", "1"], db_path, capsys)
    assert code == 0 and "already done (completed 2025-10-06 via cli)" in out
    code, out, err = run(["done", "999"], db_path, capsys)
    assert code == 1 and "no action item with ID '999'" in err and out == ""


def test_cli_done_mixed_valid_and_invalid(folder, db_path, capsys):
    main(["ingest", str(folder), "--db", str(db_path)])
    capsys.readouterr()
    code, out, err = run(["done", "2", "nope", "4"], db_path, capsys)
    assert code == 1                      # one bad ID -> non-zero exit ...
    assert "Marked #2" in out and "Marked #4" in out and "'nope'" in err  # ... others applied


def test_cli_negative_id_reports_not_found(folder, db_path, capsys):
    main(["ingest", str(folder), "--db", str(db_path)])
    capsys.readouterr()
    code, _, err = run(["done", "-1"], db_path, capsys)
    assert code == 1 and "no action item with ID '-1'" in err


# ---- empty / missing database --------------------------------------------------

def test_missing_database_not_created(tmp_path, capsys):
    db_path = tmp_path / "absent.db"
    for cmd in (["list"], ["done", "1"]):
        code, _, err = run(cmd, db_path, capsys)
        assert code == 1 and "database not found" in err
    assert not db_path.exists()


def test_empty_database(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    db_path = tmp_path / "e.db"
    main(["ingest", str(tmp_path / "empty"), "--db", str(db_path)])
    capsys.readouterr()
    code, out, _ = run(["list"], db_path, capsys)
    assert code == 0 and "no action items yet" in out
    code, _, err = run(["done", "1"], db_path, capsys)
    assert code == 1 and "no action item" in err


def test_no_matches_message(folder, db_path, capsys):
    main(["ingest", str(folder), "--db", str(db_path)])
    capsys.readouterr()
    _, out, _ = run(["list", "--owner", "Priya Nair", "--status", "done"], db_path, capsys)
    assert "No action items match" in out


def test_cli_table_and_json(folder, db_path, capsys):
    main(["ingest", str(folder), "--db", str(db_path)])
    capsys.readouterr()
    _, out, _ = run(["list", "--today", "2025-10-06"], db_path, capsys)
    header = out.splitlines()[0].split()
    assert header == ["ID", "OWNER", "TASK", "DUE", "STATUS", "SOURCE"]
    assert "2025-10-01-planning.md" in out and "9 item(s): 8 open (4 overdue), 1 done" in out
    _, out, _ = run(["list", "--json", "--status", "overdue", "--today", "2025-10-06"],
                    db_path, capsys)
    data = json.loads(out)
    assert {i["status"] for i in data["items"]} == {"overdue"} and len(data["items"]) == 4


def test_long_task_shortened_unless_wide(tmp_path, capsys):
    folder = tmp_path / "n"
    write(folder, "a.md", "Date: 2025-10-01\nAttendees: Rahul Verma\n- [ ] Rahul to "
          + "x" * 120 + "\n")
    db_path = tmp_path / "d.db"
    main(["ingest", str(folder), "--db", str(db_path)])
    capsys.readouterr()
    assert "..." in run(["list"], db_path, capsys)[1]
    assert "X" + "x" * 119 in run(["list", "--wide"], db_path, capsys)[1]


# ---- persistence and re-ingest safety -------------------------------------------

def _cli_process(*args, env_extra=None):
    """Run the real CLI in a separate Python process (true persistence check)."""
    env = {**os.environ, "PYTHONPATH": str(SRC), **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "actiontrack", *args],
                          capture_output=True, text=True, env=env)


def test_done_persists_across_processes(folder, db_path):
    assert _cli_process("ingest", str(folder), "--db", str(db_path)).returncode == 0
    done = _cli_process("done", "1", "--db", str(db_path), "--today", "2025-10-06")
    assert done.returncode == 0 and "Marked #1 done" in done.stdout
    listed = _cli_process("list", "--status", "done", "--db", str(db_path), "--json")
    ids = [i["id"] for i in json.loads(listed.stdout)["items"]]
    assert 1 in ids
    fresh = connect(db_path)
    assert fresh.execute("SELECT status, completed_by FROM action_items WHERE id = 1"
                         ).fetchone()[:] == ("done", "cli")
    fresh.close()


def test_reingest_same_notes_keeps_cli_done(folder, db_path, capsys):
    main(["ingest", str(folder), "--db", str(db_path)])
    run(["done", "1", "2", "--today", "2025-10-06"], db_path, capsys)
    for _ in range(3):
        assert main(["ingest", str(folder), "--db", str(db_path)]) == 0
    capsys.readouterr()
    c = connect(db_path)
    rows = c.execute("SELECT id, status, completed_by, completed_on FROM action_items"
                     " WHERE id IN (1, 2) ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "done", "cli", "2025-10-06"),
                                        (2, "done", "cli", "2025-10-06")]
    assert c.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 9
    c.close()


def test_reingest_after_note_edited_keeps_done(folder, db):
    iid = item_id(db, "alpha")
    mark_done(db, str(iid), today=TODAY)
    path = folder / "2025-10-01-planning.md"
    path.write_text(path.read_text(encoding="utf-8") + "- [ ] Rahul to juliet\n",
                    encoding="utf-8")   # note edited; alpha still unticked in the file
    r = ingest_folder(db, folder)
    assert r.changed_files == ["2025-10-01-planning.md"] and r.items_new == 1
    assert db.execute("SELECT status FROM action_items WHERE id=?", (iid,)).fetchone()[0] \
        == "done"


def test_older_note_mentioning_task_unticked_does_not_reopen(folder, db):
    iid = item_id(db, "bravo")
    mark_done(db, str(iid), today=date(2025, 10, 8))
    # A note from *before* completion that still lists the task unticked.
    write(folder, "2025-10-07-standup.md", "Date: 2025-10-07\nAttendees: Rahul Verma\n"
                                          "- [ ] Rahul to bravo by 2025-10-10\n")
    r = ingest_folder(db, folder)
    assert r.items_new == 0 and len(r.merged) == 1
    rows = db.execute("SELECT id, status FROM action_items WHERE lower(task) LIKE 'bravo%'"
                      ).fetchall()
    assert [tuple(x) for x in rows] == [(iid, "done")]


def test_later_note_with_task_ticked_keeps_single_done_item(folder, db):
    iid = item_id(db, "bravo")
    mark_done(db, str(iid), today=TODAY)
    write(folder, "2025-10-13-review.md", "Date: 2025-10-13\nAttendees: Rahul Verma\n"
                                         "- [x] Rahul to bravo by 2025-10-10\n")
    ingest_folder(db, folder)
    rows = db.execute("SELECT id, status, completed_by FROM action_items"
                      " WHERE lower(task) LIKE 'bravo%'").fetchall()
    assert [tuple(x) for x in rows] == [(iid, "done", "cli")]   # completion source kept


def test_mark_everything_done_then_reingest_fixtures(notes_dir, conn):
    ingest_folder(conn, notes_dir)
    for (iid,) in conn.execute("SELECT id FROM action_items WHERE status='open'").fetchall():
        mark_done(conn, str(iid), today=TODAY)
    ingest_folder(conn, notes_dir)
    ingest_folder(conn, notes_dir)
    assert list_items(conn, today=TODAY, status="open").items == []
