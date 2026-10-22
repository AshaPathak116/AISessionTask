from datetime import date

import pytest

from actiontrack.dates import (DateParseError, find_date_in_text, looks_like_date_start,
                               parse_absolute, resolve_due)

MON = date(2025, 9, 22)   # Monday
FRI = date(2025, 9, 26)   # Friday


@pytest.mark.parametrize("phrase,expected", [
    ("Friday", date(2025, 9, 26)),
    ("fri", date(2025, 9, 26)),
    ("Monday", date(2025, 9, 29)),          # strictly after the meeting
    ("this Monday", date(2025, 9, 22)),     # 'this' includes the meeting day
    ("next Friday", date(2025, 10, 3)),     # same-week Friday pushed a week
    ("next Monday", date(2025, 9, 29)),     # already next week, not pushed
    ("today", MON), ("EOD", MON), ("EOD Friday", date(2025, 9, 26)),
    ("Friday EOD", date(2025, 9, 26)),
    ("tomorrow", date(2025, 9, 23)),
    ("end of week", date(2025, 9, 26)), ("EOW", date(2025, 9, 26)),
    ("end of month", date(2025, 9, 30)),
    ("2 Oct", date(2025, 10, 2)), ("Oct 2", date(2025, 10, 2)),
    ("2nd October", date(2025, 10, 2)), ("October 2nd", date(2025, 10, 2)),
    ("Thursday 2 Oct", date(2025, 10, 2)),
    ("2025-10-10", date(2025, 10, 10)), ("10 October 2025", date(2025, 10, 10)),
    ("25/09", date(2025, 9, 25)),            # unambiguous day-first
    ("the 30th", date(2025, 9, 30)),
])
def test_resolve_relative_to_meeting(phrase, expected):
    assert resolve_due(phrase, MON) == expected


def test_friday_said_on_friday_means_next_friday():
    assert resolve_due("Friday", FRI) == date(2025, 10, 3)


def test_resolution_uses_meeting_date_not_today():
    # A meeting long in the past still resolves relative to itself.
    assert resolve_due("Friday", date(2019, 1, 7)) == date(2019, 1, 11)
    assert resolve_due("tomorrow", date(2019, 1, 7)) == date(2019, 1, 8)


@pytest.mark.parametrize("meeting,phrase,expected", [
    (date(2025, 12, 29), "5 Jan", date(2026, 1, 5)),        # forward across year end
    (date(2025, 12, 29), "Friday", date(2026, 1, 2)),
    (date(2026, 1, 2), "28 Dec", date(2025, 12, 28)),        # recent past, previous year
    (date(2025, 10, 5), "2 Oct", date(2025, 10, 2)),         # recent past kept (overdue)
    (date(2025, 6, 30), "1 Jan", date(2026, 1, 1)),          # far past -> next year
    (date(2025, 12, 15), "end of month", date(2025, 12, 31)),
])
def test_year_boundaries(meeting, phrase, expected):
    assert resolve_due(phrase, meeting) == expected


@pytest.mark.parametrize("phrase", ["31 Feb", "2025-02-30", "10/02", "next week", "ASAP"])
def test_unresolvable_raise(phrase):
    with pytest.raises(DateParseError):
        resolve_due(phrase, MON)


def test_not_a_date_returns_none_or_nondefinitive():
    with pytest.raises(DateParseError) as exc:
        resolve_due("end of the sprint", MON)
    assert exc.value.definitive is False


def test_parse_absolute_variants():
    assert parse_absolute("Monday, 29 December 2025") == date(2025, 12, 29)
    assert parse_absolute("Sep 22, 2025") == date(2025, 9, 22)
    assert parse_absolute("22/09/2025") == date(2025, 9, 22)
    assert parse_absolute("Friday") is None
    with pytest.raises(DateParseError):
        parse_absolute("2025-13-45")
    with pytest.raises(DateParseError):
        parse_absolute("05/06/2025")   # ambiguous


def test_find_date_in_text():
    assert find_date_in_text("2025-09-22-sprint-planning") == date(2025, 9, 22)
    assert find_date_in_text("standup_20251006") == date(2025, 10, 6)
    assert find_date_in_text("Design Sync - 1 October 2025") == date(2025, 10, 1)
    assert find_date_in_text("2025-13-45-bad") is None
    assert find_date_in_text("no date here") is None


@pytest.mark.parametrize("words,expected", [
    (["Friday"], True), (["2", "Oct"], True), (["the", "15th"], True),
    (["email"], False), (["10%"], False), (["3", "people"], False),
    (["this", "approach"], False), (["next", "week"], True), (["end", "of", "day"], True),
])
def test_looks_like_date_start(words, expected):
    assert looks_like_date_start(words) is expected
