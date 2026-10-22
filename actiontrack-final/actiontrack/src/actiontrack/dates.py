"""Date parsing and due-date resolution.

Every relative expression ("Friday", "tomorrow", "2 Oct") is resolved against the
*meeting date* supplied by the caller -- never against today's date.

Rules (also documented in README.md):

* ``Friday`` / ``this Friday``  -> the next Friday strictly after the meeting date
  (``this Friday`` said on a Friday means the same day).
* ``next Friday``                -> as above, but pushed one week further if the
  result would still fall in the meeting's own calendar week.
* ``today`` / ``EOD`` / ``COB``  -> the meeting date.  ``tomorrow`` -> +1 day.
* ``end of week`` / ``EOW``      -> the Friday on or after the meeting date.
* ``end of month`` / ``EOM``     -> last day of the meeting's month.
* ``2 Oct`` / ``Oct 2`` (no year) -> the earliest of (year-1, year, year+1) that is
  not more than LOOKBACK_DAYS before the meeting.  This handles year boundaries
  (meeting 29 Dec, "by 5 Jan" -> next January).
* Numeric dates like ``02/10`` are accepted only when unambiguous (one part > 12).
* Vague phrases (``next week``, ``ASAP``, ``soon``) are *not* converted to dates.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

LOOKBACK_DAYS = 31

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTHS: dict[str, int] = {}
for _i, _name in enumerate(_MONTH_NAMES, start=1):
    MONTHS[_name] = _i
    MONTHS[_name[:3]] = _i
MONTHS["sept"] = 9

VAGUE = {
    "next week", "next month", "next year", "next sprint", "asap", "soon", "later",
    "end of next week", "end of next month", "early next week", "this month",
    "the coming week", "coming week", "tbd", "tba",
}


class DateParseError(ValueError):
    """Raised when text looks like a date but cannot be turned into one.

    ``definitive`` errors (invalid calendar date, ambiguous, vague) stop the
    caller from trying shorter phrases; non-definitive ones do not.
    """

    def __init__(self, message: str, *, definitive: bool = True) -> None:
        super().__init__(message)
        self.definitive = definitive


_ORD = r"(\d{1,2})(?:st|nd|rd|th)?"
_MON = r"([a-z]{3,9})\.?"
ISO_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
DMY_RE = re.compile(rf"{_ORD}\s+(?:of\s+)?{_MON},?\s+(\d{{4}})")
MDY_RE = re.compile(rf"{_MON}\s+{_ORD},?\s+(\d{{4}})")
DM_RE = re.compile(rf"{_ORD}\s+(?:of\s+)?{_MON}")
MD_RE = re.compile(rf"{_MON}\s+{_ORD}")
NUM_RE = re.compile(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?")
ORDINAL_DAY_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)")
WEEKDAY_PREFIX = re.compile(
    r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+(?=\S)"
)
TIME_QUALIFIERS = re.compile(
    r"\b(?:eod|cob|eob|end of (?:the )?day|close of (?:the )?business|end of business|"
    r"noon|midday|morning|afternoon|evening|night)\b"
)


def _norm(text: str) -> str:
    s = text.replace("\u00a0", " ")
    s = re.sub(r"[*_`]", "", s)
    return " ".join(s.split()).strip(" .,;:!?()[]").lower()


def _make(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        raise DateParseError(f"{y:04d}-{m:02d}-{d:02d} is not a valid calendar date") from None


def _day_month(a: int, b: int) -> tuple[int, int]:
    """Interpret a numeric a/b pair, returning (day, month)."""
    if a > 12 and b <= 12:
        return a, b
    if b > 12 and a <= 12:
        return b, a
    if a == b:
        return a, b
    if a > 12 and b > 12:
        raise DateParseError(f"{a}/{b} is not a valid date")
    raise DateParseError(
        f"ambiguous numeric date {a}/{b} (day/month or month/day?); use YYYY-MM-DD"
    )


def parse_absolute(text: str) -> date | None:
    """Parse a date that includes a year.

    Returns ``None`` when the text is not a full date; raises DateParseError when it
    is shaped like one but is invalid or ambiguous.
    """
    s = WEEKDAY_PREFIX.sub("", _norm(text))
    m = ISO_RE.fullmatch(s)
    if m:
        return _make(int(m[1]), int(m[2]), int(m[3]))
    m = DMY_RE.fullmatch(s)
    if m and m[2] in MONTHS:
        return _make(int(m[3]), MONTHS[m[2]], int(m[1]))
    m = MDY_RE.fullmatch(s)
    if m and m[1] in MONTHS:
        return _make(int(m[3]), MONTHS[m[1]], int(m[2]))
    m = NUM_RE.fullmatch(s)
    if m and m[3]:
        year = int(m[3])
        if year < 100:
            year += 2000
        day, month = _day_month(int(m[1]), int(m[2]))
        return _make(year, month, day)
    return None


def find_date_in_text(text: str) -> date | None:
    """Find the first valid full date embedded in free text (heading, filename).

    Never raises; invalid candidates are ignored.
    """
    s = text.lower().replace("\u00a0", " ")
    for m in re.finditer(r"(?<!\d)(\d{4})[-_./](\d{1,2})[-_./](\d{1,2})(?!\d)", s):
        try:
            return _make(int(m[1]), int(m[2]), int(m[3]))
        except DateParseError:
            continue
    for m in re.finditer(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", s):
        try:
            return _make(int(m[1]), int(m[2]), int(m[3]))
        except DateParseError:
            continue
    for m in re.finditer(rf"\b{_ORD}\s+(?:of\s+)?{_MON},?\s+(\d{{4}})\b", s):
        if m[2] in MONTHS:
            try:
                return _make(int(m[3]), MONTHS[m[2]], int(m[1]))
            except DateParseError:
                continue
    for m in re.finditer(rf"\b{_MON}\s+{_ORD},?\s+(\d{{4}})\b", s):
        if m[1] in MONTHS:
            try:
                return _make(int(m[3]), MONTHS[m[1]], int(m[2]))
            except DateParseError:
                continue
    return None


def _after(base: date, weekday: int, *, inclusive: bool) -> date:
    delta = (weekday - base.weekday()) % 7
    if delta == 0 and not inclusive:
        delta = 7
    return base + timedelta(days=delta)


def _nearest_year(month: int, day: int, meeting: date) -> date:
    candidates = []
    for year in (meeting.year - 1, meeting.year, meeting.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            pass
    if not candidates:
        raise DateParseError(f"day {day} does not exist in month {month}")
    threshold = meeting - timedelta(days=LOOKBACK_DAYS)
    for candidate in candidates:
        if candidate >= threshold:
            return candidate
    return candidates[-1]


def _last_day_of_month(d: date) -> date:
    first_next = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def resolve_due(phrase: str, meeting: date) -> date | None:
    """Resolve a due-date phrase relative to ``meeting``.

    Returns ``None`` if the phrase is not a date at all; raises DateParseError if
    it is date-like but cannot be resolved safely.
    """
    s = _norm(phrase)
    if not s:
        return None
    absolute = parse_absolute(s)
    if absolute:
        return absolute

    s = re.sub(r"^the\s+", "", s)
    stripped = " ".join(TIME_QUALIFIERS.sub(" ", s).split()).strip(", ")
    had_qualifier = stripped != s
    s = stripped

    if s in ("", "today", "tonight"):
        return meeting if (s or had_qualifier) else None
    if s == "tomorrow":
        return meeting + timedelta(days=1)
    if s == "day after tomorrow":
        return meeting + timedelta(days=2)
    if s in VAGUE:
        raise DateParseError(f"vague deadline '{phrase.strip()}' not converted to a date")
    if re.fullmatch(r"(?:end of (?:the |this )?week|eow|this week)", s):
        return _after(meeting, 4, inclusive=True)
    if re.fullmatch(r"(?:end of (?:the |this )?month|eom)", s):
        return _last_day_of_month(meeting)
    if re.fullmatch(r"(?:end of (?:the |this )?year|eoy)", s):
        return date(meeting.year, 12, 31)
    if re.fullmatch(r"(?:end of (?:the |this )?quarter|eoq)", s):
        q_end_month = ((meeting.month - 1) // 3 + 1) * 3
        return _last_day_of_month(date(meeting.year, q_end_month, 1))

    m = re.fullmatch(r"(?:(this|next|coming)\s+)?([a-z]+)", s)
    if m and m[2] in WEEKDAYS:
        weekday = WEEKDAYS[m[2]]
        if m[1] == "this":
            return _after(meeting, weekday, inclusive=True)
        result = _after(meeting, weekday, inclusive=False)
        if m[1] == "next" and result.isocalendar()[:2] == meeting.isocalendar()[:2]:
            result += timedelta(days=7)
        return result

    tokens = s.split()
    if len(tokens) > 1 and tokens[0].rstrip(",.") in WEEKDAYS:
        s = " ".join(tokens[1:])  # "Friday 3 Oct" -> "3 Oct"

    m = DM_RE.fullmatch(s)
    if m:
        if m[2] not in MONTHS:
            raise DateParseError(f"unknown month '{m[2]}'", definitive=False)
        return _nearest_year(MONTHS[m[2]], int(m[1]), meeting)
    m = MD_RE.fullmatch(s)
    if m:
        if m[1] not in MONTHS:
            raise DateParseError(f"unknown month '{m[1]}'", definitive=False)
        return _nearest_year(MONTHS[m[1]], int(m[2]), meeting)
    m = NUM_RE.fullmatch(s)
    if m and not m[3]:
        day, month = _day_month(int(m[1]), int(m[2]))
        return _nearest_year(month, day, meeting)
    m = ORDINAL_DAY_RE.fullmatch(s)
    if m:
        day = int(m[1])
        probe = date(meeting.year, meeting.month, 1)
        for _ in range(3):
            try:
                candidate = probe.replace(day=day)
                if candidate >= meeting:
                    return candidate
            except ValueError:
                pass
            probe = _last_day_of_month(probe) + timedelta(days=1)
        raise DateParseError(f"cannot resolve day-of-month '{phrase.strip()}'")

    raise DateParseError(f"unrecognised date expression '{phrase.strip()}'", definitive=False)


_DATE_START_WORDS = {
    "today", "tomorrow", "tonight", "eod", "cob", "eob", "eow", "eom", "eoy", "eoq",
    "asap", "soon", "noon", "midday", "close", "day",
}


def looks_like_date_start(words: list[str]) -> bool:
    """Heuristic: does a phrase that follows 'by'/'due' start like a date?"""
    if not words:
        return False
    w0 = words[0].lower().strip(".,*_`")
    nxt = words[1].lower().strip(".,*_`") if len(words) > 1 else ""
    if w0 in WEEKDAYS or w0 in MONTHS or w0 in _DATE_START_WORDS:
        return True
    if w0 in ("this", "next", "coming"):
        return nxt in WEEKDAYS or nxt in ("week", "month", "year", "sprint", "quarter")
    if w0 == "end":
        return nxt == "of"
    if w0 == "the":
        return looks_like_date_start(words[1:])
    if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", w0):
        return True
    if re.fullmatch(r"\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?", w0):
        return True
    if re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)", w0):
        return True
    if re.fullmatch(r"\d{1,2}", w0):
        return nxt in MONTHS or nxt == "of"
    return False


TODAY_ENV = "ACTIONTRACK_TODAY"


def parse_iso_date(text: str) -> date:
    """Strict YYYY-MM-DD parser for command-line options."""
    m = re.fullmatch(r"\s*(\d{4})-(\d{2})-(\d{2})\s*", text or "")
    if not m:
        raise DateParseError(f"'{text}' is not a date in YYYY-MM-DD format")
    return _make(int(m[1]), int(m[2]), int(m[3]))


def today(override: date | str | None = None) -> date:
    """The current date used for overdue checks and completion stamps.

    Precedence: explicit ``override`` > ACTIONTRACK_TODAY env var > system clock.
    This is only ever used for "today" questions, never for resolving due dates.
    """
    import os

    if isinstance(override, date):
        return override
    if override:
        return parse_iso_date(override)
    env = os.environ.get(TODAY_ENV)
    if env:
        return parse_iso_date(env)
    return date.today()
