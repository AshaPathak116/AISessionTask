import json

from actiontrack.cli import main
from conftest import write


def test_ingest_command_text_report(notes_dir, db_path, capsys):
    assert main(["ingest", str(notes_dir), "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "Markdown files found : 17" in out
    assert "Skipped malformed files (4)" in out and "empty.md: file is empty" in out
    assert "Ambiguous owners" in out and "Priya Nair, Priya Sharma" in out
    assert db_path.exists()


def test_ingest_twice_reports_no_new_items(notes_dir, db_path, capsys):
    main(["ingest", str(notes_dir), "--db", str(db_path)])
    capsys.readouterr()
    main(["ingest", str(notes_dir), "--db", str(db_path)])
    out = capsys.readouterr().out
    assert "(new 0, already known 38, merged as duplicates 0)" in out


def test_dry_run_writes_nothing(notes_dir, tmp_path, capsys):
    db = tmp_path / "never.db"
    assert main(["ingest", str(notes_dir), "--db", str(db), "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert not db.exists()


def test_json_output(notes_dir, db_path, capsys):
    main(["ingest", str(notes_dir), "--db", str(db_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["md_files_found"] == 17 and len(data["skipped"]) == 4


def test_env_db(notes_dir, tmp_path, monkeypatch, capsys):
    db = tmp_path / "env.db"
    monkeypatch.setenv("ACTIONTRACK_DB", str(db))
    assert main(["ingest", str(notes_dir)]) == 0
    assert db.exists()


def test_missing_folder_exit_code(tmp_path, capsys):
    assert main(["ingest", str(tmp_path / "nope"), "--db", str(tmp_path / "d.db")]) == 2
    assert "folder not found" in capsys.readouterr().err


def test_empty_folder_message(tmp_path, capsys):
    (tmp_path / "e").mkdir()
    assert main(["ingest", str(tmp_path / "e"), "--db", str(tmp_path / "d.db")]) == 0
    assert "No Markdown" in capsys.readouterr().out


def test_folder_with_spaces_and_parentheses(tmp_path, capsys):
    folder = tmp_path / "notes (2)"
    write(folder, "a.md", "Date: 2025-09-22\nAttendees: Rahul Verma\n- [ ] Rahul to x\n")
    assert main(["ingest", str(folder), "--db", str(tmp_path / "d.db")]) == 0
    assert "Action items seen    : 1" in capsys.readouterr().out
