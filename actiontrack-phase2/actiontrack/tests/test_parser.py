from datetime import date

import pytest

from actiontrack.parser import (Attendee, MalformedNoteError, decode_bytes, extract_due,
                                extract_owner, parse_attendee, parse_text, resolve_owner)

PEOPLE = [Attendee("Priya Sharma"), Attendee("Priya Nair"), Attendee("Rahul Verma"),
          Attendee("Karan Mehta")]
MEETING = date(2025, 9, 22)


def note(body: str, header: str = "# M\nDate: 2025-09-22\n"
                                  "Attendees: Priya Sharma, Rahul Verma, Ananya Iyer\n"):
    return parse_text(header + body, "x.md")


# ---- owners ---------------------------------------------------------------------

@pytest.mark.parametrize("text,status,name", [
    ("Rahul to fix CI", "resolved", "Rahul Verma"),
    ("Rahul Verma will fix CI", "resolved", "Rahul Verma"),
    ("Verma to fix CI", "resolved", "Rahul Verma"),
    ("Priya Nair to export icons", "resolved", "Priya Nair"),
    ("Priya S. to send quote", "resolved", "Priya Sharma"),
    ("@karan.mehta prepare guide", "resolved", "Karan Mehta"),
    ("Karan: prepare guide", "resolved", "Karan Mehta"),
    ("[Rahul] fix CI", "resolved", "Rahul Verma"),
    ("Send minutes (owner: Rahul)", "resolved", "Rahul Verma"),
    ("Send minutes, assigned to Karan", "resolved", "Karan Mehta"),
    ("Priya to review wireframes", "ambiguous", None),
    ("Vikram to rotate keys", "unverified", "Vikram"),
    ("Priya Sharma & Karan to align", "multiple", None),
    ("Someone to check backups", "unassigned", None),
    ("TBD to write template", "unassigned", None),
    ("Team to review runbook", "unassigned", None),
    ("Need to check backups", "unassigned", None),     # verb, not a name
    ("Note: ask legal", "unassigned", None),           # colon form needs an attendee
    ("Draft the release notes", "unassigned", None),
])
def test_extract_owner(text, status, name):
    owner, _ = extract_owner(text, PEOPLE)
    assert owner.status == status
    assert owner.name == name


def test_ambiguous_lists_candidates_and_never_picks_one():
    owner = resolve_owner("Priya", PEOPLE)
    assert owner.status == "ambiguous"
    assert owner.candidates == ["Priya Nair", "Priya Sharma"]
    assert owner.name is None


def test_owner_without_attendees_is_unverified_not_guessed():
    owner = resolve_owner("Priya", [])
    assert owner.status == "unverified" and owner.name == "Priya"


def test_owner_phrase_removed_from_task():
    _, rest = extract_owner("Rahul to fix the flaky CI job by Monday", PEOPLE)
    assert rest == "fix the flaky CI job by Monday"


# ---- due dates ------------------------------------------------------------------

def test_extract_due_skips_non_date_by():
    due, raw, err, _ = extract_due("send it by email by Friday", MEETING)
    assert due == date(2025, 9, 26) and raw == "Friday" and err is None


@pytest.mark.parametrize("text", ["increase coverage by 10%", "reviewed by the team",
                                  "write acceptance tests"])
def test_no_due_date(text):
    assert extract_due(text, MEETING) == (None, None, None, None)


def test_due_metadata_forms():
    assert extract_due("guide (due: 2025-10-10)", MEETING)[0] == date(2025, 10, 10)
    assert extract_due("guide \U0001F4C5 2025-10-11", MEETING)[0] == date(2025, 10, 11)
    assert extract_due("guide [due:: 2025-10-12]", MEETING)[0] == date(2025, 10, 12)
    assert extract_due("guide, deadline: 3 Oct", MEETING)[0] == date(2025, 10, 3)


def test_unparseable_due_kept_raw_not_invented():
    due, raw, err, _ = extract_due("renew cert by 31 Feb", MEETING)
    assert due is None and raw == "31 Feb" and "does not exist" in err


# ---- whole notes ----------------------------------------------------------------

def test_checkbox_spacing_variants():
    n = note("- [ ] Rahul to a\n-[ ]Rahul to b\n*   [X]   Rahul to c\n1. [ ] Rahul to d\n"
             "- [  ] Rahul to e\n+ [x] Rahul to f\n[ ] Rahul to g\n\t- [ ] Rahul to h\n")
    assert [i.task for i in n.items] == list("ABCDEFGH")
    assert [i.checked for i in n.items] == [False, False, True, False, False, True, False, False]


def test_relative_due_uses_meeting_date():
    n = note("- [ ] Rahul to send quote by Friday\n")
    assert n.items[0].due_date == date(2025, 9, 26)


def test_missing_due_is_none():
    n = note("- [ ] Rahul to write tests\n")
    assert n.items[0].due_date is None and n.items[0].due_raw is None


def test_line_level_problems_are_warnings_not_failures():
    n = note("- [ ]\n- [ x broken\n- [?] Rahul to look\n- [ ] ~~Rahul to old~~\n"
             "- [ ] Rahul to real task\n")
    assert [i.task for i in n.items] == ["Look", "Real task"]
    joined = " | ".join(n.warnings)
    for fragment in ("empty action item", "malformed checkbox", "unrecognised checkbox",
                     "struck-through"):
        assert fragment in joined


def test_code_blocks_ignored():
    n = note("```\n- [ ] Rahul to not a task\n```\n- [ ] Rahul to real\n")
    assert [i.task for i in n.items] == ["Real"]


def test_plain_bullets_only_count_in_action_sections():
    n = note("## Notes\n- Rahul to just discussed\n## Next steps\n- Rahul to do it\n"
             "ACTION: Priya Sharma to follow up\n")
    assert [i.task for i in n.items] == ["Do it", "Follow up"]


def test_attendee_formats():
    fm = parse_text("---\ndate: 2025-09-22\nattendees: [Priya Sharma, 'Rahul Verma']\n---\n",
                    "a.md")
    assert [a.name for a in fm.attendees] == ["Priya Sharma", "Rahul Verma"]
    head = parse_text("# M 2025-09-22\n## Attendees\n- Priya Sharma <PRIYA@x.com>\n"
                      "- [x] Rahul Verma (Backend)\n- Dana White - Acme PM\n", "b.md")
    assert [(a.name, a.email) for a in head.attendees] == [
        ("Priya Sharma", "priya@x.com"), ("Rahul Verma", None), ("Dana White", None)]
    assert head.items == []   # attendance checklist is not an action item
    fld = parse_text("Date: 2025-09-22\nAttendees:\n- Priya Sharma\n- Rahul Verma\n\nText\n",
                     "c.md")
    assert len(fld.attendees) == 2
    assert parse_attendee("@priya.sharma").name == "Priya Sharma"


def test_meeting_date_sources():
    assert parse_text("---\ndate: 2025-09-22\n---\n", "a.md").meeting_date == date(2025, 9, 22)
    assert parse_text("**Date:** 22 Sep 2025\n", "a.md").meeting_date == date(2025, 9, 22)
    assert parse_text("# Sync - 1 October 2025\n", "a.md").meeting_date == date(2025, 10, 1)
    assert parse_text("# Sync\n", "2025-10-06-x.md").meeting_date == date(2025, 10, 6)


def test_filename_date_conflict_warns_and_uses_note_date():
    n = parse_text("Date: 2025-09-23\n", "2025-09-22-x.md")
    assert n.meeting_date == date(2025, 9, 23)
    assert any("differs" in w for w in n.warnings)


@pytest.mark.parametrize("text,reason", [
    ("", "empty"),
    ("   \n\t\n", "empty"),
    ("# Notes\n- [ ] Rahul to x\n", "no meeting date"),
    ("Date: 2025-13-45\n", "unparseable meeting date"),
    ("Date: someday\n", "unparseable meeting date"),
    ("Date: 05/06/2025\n", "ambiguous"),
    ("---\ndate: 2025-09-22\n- [ ] x\n", "never closed"),
])
def test_malformed_notes(text, reason):
    with pytest.raises(MalformedNoteError, match=reason):
        parse_text(text, "notes.md")


def test_decode_fallbacks():
    assert decode_bytes("José".encode("utf-8")) == ("José", [])
    text, warnings = decode_bytes("José".encode("cp1252"))
    assert text == "José" and warnings
    with pytest.raises(MalformedNoteError, match="binary"):
        decode_bytes(b"\x00\x05\x16\x07")


def test_long_note_and_unusual_whitespace():
    body = "\n".join(f"- [ ]   Rahul\u00a0to  task\u200b number {i}   by  Friday  "
                     for i in range(2000))
    n = note(body + "\n")
    assert len(n.items) == 2000
    assert all(i.owner.name == "Rahul Verma" and i.due_date == date(2025, 9, 26)
               for i in n.items)
