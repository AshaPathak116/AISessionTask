"""Markdown meeting-note parser.

Pure parsing: no database access.  ``parse_file`` is the only function that reads
from disk.  Problems that make a whole note unusable raise MalformedNoteError;
problems with a single line are recorded as warnings on the note and the rest of
the note is still parsed.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .dates import (
    DateParseError,
    find_date_in_text,
    looks_like_date_start,
    parse_absolute,
    resolve_due,
)

MAX_FILE_BYTES = 20 * 1024 * 1024


class MalformedNoteError(Exception):
    """The note cannot be ingested; the message explains why."""


# --------------------------------------------------------------------------- models


@dataclass
class Attendee:
    name: str
    email: str | None = None


@dataclass
class Owner:
    # resolved   -> exactly one attendee matched (name is their full name)
    # unassigned -> no owner given, or a non-person such as "TBD" / "Team"
    # ambiguous  -> a first name matching several attendees (see candidates)
    # unverified -> a name that is not among the attendees; stored as written
    # multiple   -> several people named for one task (never merged into one)
    status: str
    raw: str | None = None
    name: str | None = None
    candidates: list[str] = field(default_factory=list)


@dataclass
class ParsedItem:
    line_no: int
    raw_line: str
    text: str
    task: str
    checked: bool
    owner: Owner
    due_date: date | None
    due_raw: str | None
    due_error: str | None
    norm_task: str
    occurrence_key: str


@dataclass
class ParsedNote:
    rel_path: str
    title: str
    meeting_date: date
    attendees: list[Attendee]
    items: list[ParsedItem]
    warnings: list[str]
    content_hash: str


# --------------------------------------------------------------------------- helpers

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^(```|~~~)")
BULLET_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+(.+)$")
CHECKBOX_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s*\[([^\]]{0,3})\]\s*(.*)$")
BARE_CHECKBOX_RE = re.compile(r"^\s*\[( ?|x|X)\]\s+(.*)$")
BROKEN_CHECKBOX_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s*\[(?![^\]]*\])")
ACTION_LINE_RE = re.compile(r"^(?:\*\*)?(?:action(?: item)?|ai|todo)(?:\*\*)?\s*:\s*(.+)$", re.I)
FIELD_RE = re.compile(
    r"^(?:[-*+]\s+)?\**\s*(?P<key>meeting date|date|attendees|participants|present|attendance)"
    r"\s*\**\s*(?::|\s[-\u2013\u2014]\s)\s*\**\s*(?P<value>.*?)\s*\**\s*$",
    re.I,
)
DATE_KEYS = {"date", "meeting date", "meeting_date"}
ATTENDEE_KEYS = {"attendees", "participants", "present", "attendance"}
ACTION_HEADING_RE = re.compile(
    r"\b(action\s*items?|actions|next\s*steps|to-?dos?|follow[\s-]*ups?|tasks)\b", re.I
)
ATTENDEE_HEADING_RE = re.compile(r"^(attendees|participants|present|attendance)\b", re.I)

NAME_TOK = r"[A-Z\u00c0-\u00d6\u00d8-\u00de][\w'\u2019\-]*\.?"
NAME = rf"{NAME_TOK}(?:\s+{NAME_TOK}){{0,2}}"
NAMES = rf"{NAME}(?:\s*(?:,|&|/|\s+and)\s+{NAME})*"
TO_RE = re.compile(
    rf"^({NAMES})\s+(?:to|will|should|must|needs? to|has to|is going to|is to|shall)\s+(.+)$"
)
COLON_RE = re.compile(rf"^({NAME})\s*(?::|\s[-\u2013\u2014])\s*(.+)$")
BRACKET_RE = re.compile(r"^\[([^\]]{1,60})\]\s*[:\-\u2013\u2014]?\s*(.+)$")
AT_LEAD_RE = re.compile(r"^@([A-Za-z][\w.\-]*[A-Za-z])\s*[:,\-\u2013\u2014]?\s*(.+)$")
AT_ANY_RE = re.compile(r"(?<![\w.@])@([A-Za-z][\w.\-]*[A-Za-z])")
OWNER_TAG_RE = re.compile(
    r"[(\[]?\s*\b(?i:owner|assignee|assigned to)\s*(?:::|:|=)?\s*@?"
    r"([^\W\d_][\w.'\u2019\-]*(?:\s+[A-Z\u00c0-\u00de][\w'\u2019\-]*\.?)?)\s*[)\]]?"
)

NON_PERSON = {
    "someone", "somebody", "anyone", "anybody", "everyone", "everybody", "all", "team",
    "the team", "tbd", "tba", "tbc", "unassigned", "unknown", "nobody", "none", "na",
    "n/a", "owner", "we", "us", "i", "they", "the", "a", "an", "group", "whoever", "?",
}
NOT_NAMES = {
    "need", "needs", "remember", "try", "want", "plan", "going", "have", "has", "decide",
    "continue", "start", "make", "ask", "remind", "follow", "check", "next", "action",
    "todo", "note", "notes", "task", "agreed", "decision", "discuss", "consider",
    "ensure", "also", "send", "review", "update", "schedule", "please", "don't", "do",
}
DUE_MARKER_RE = re.compile(
    r"(?:\b(?i:due\s*(?:date|by|on)?|deadline|by|before|no later than|until|till)\s*:?\s+"
    r"|\U0001F4C5\s*|\[(?i:due)::\s*)"
)
DUE_STOP_RE = re.compile(r"\s*(?:[,;()\[\]]|\s[-\u2013\u2014]\s|\.(?:\s|$)|$)")


def clean_line(line: str) -> str:
    return line.translate(_INVISIBLE).replace("\u00a0", " ").expandtabs(4).rstrip()


def name_key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).replace(".", " ")
    return " ".join(s.casefold().split())


def normalize_task(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).casefold()
    s = re.sub(r"[^\w\s]|_", " ", s)
    return " ".join(s.split())


def strip_inline_md(text: str) -> str:
    s = text.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(s.split()).strip("* ")


def _handle_to_name(handle: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[._\-]+", handle) if part)


def _clean_person(raw: str) -> str:
    s = strip_inline_md(raw).strip().lstrip("@").strip(" .,:;")
    if " " not in s and re.search(r"[._]", s):
        s = _handle_to_name(s)
    return " ".join(s.split())


# --------------------------------------------------------------------------- attendees


def parse_attendee(text: str) -> Attendee | None:
    t = strip_inline_md(text).strip().strip("-*\u2022").strip()
    email = None
    m = EMAIL_RE.search(t)
    if m:
        email = m.group(0).lower()
        t = t[: m.start()] + t[m.end():]
    t = re.sub(r"\((?![^)]*@)[^)]*\)", " ", t)  # drop "(PM)" style roles
    t = re.sub(r"[<>()\[\]]", " ", t)
    t = re.split(r"\s+[-\u2013\u2014|]\s+|\s*:\s*", t.strip())[0]
    t = _clean_person(t)
    if t and t == t.lower():
        t = " ".join(w.capitalize() for w in t.split())
    if not t or not re.search(r"[^\W\d_]", t) or name_key(t) in NON_PERSON:
        return None
    return Attendee(t, email)


def parse_attendee_list(value: str) -> list[Attendee]:
    value = re.sub(r"\((?![^)]*@)[^)]*\)", " ", value)
    parts = re.split(r"\s*(?:,|;|\||\s&\s|\band\b)\s*", value)
    return [a for a in (parse_attendee(p) for p in parts) if a]


def _dedupe_attendees(attendees: list[Attendee]) -> list[Attendee]:
    seen: dict[str, Attendee] = {}
    for a in attendees:
        key = name_key(a.name)
        if key in seen:
            if not seen[key].email and a.email:
                seen[key].email = a.email
        else:
            seen[key] = a
    return list(seen.values())


# --------------------------------------------------------------------------- owners


def resolve_owner(raw: str, attendees: list[Attendee]) -> Owner:
    """Match a written owner against this meeting's attendees.  Never guesses."""
    name = _clean_person(raw)
    key = name_key(name)
    if not key or key in NON_PERSON:
        return Owner("unassigned", raw=raw.strip() or None)
    by_key = {name_key(a.name): a.name for a in attendees}
    if key in by_key:
        return Owner("resolved", raw=raw.strip(), name=by_key[key])
    tokens = key.split()
    attendee_parts = [(a.name, name_key(a.name).split()) for a in attendees]
    if len(tokens) == 1:
        firsts = [n for n, parts in attendee_parts if parts and parts[0] == tokens[0]]
        if len(firsts) == 1:
            return Owner("resolved", raw=raw.strip(), name=firsts[0])
        if len(firsts) > 1:
            return Owner("ambiguous", raw=raw.strip(), candidates=sorted(firsts))
        lasts = [n for n, parts in attendee_parts if len(parts) > 1 and parts[-1] == tokens[0]]
        if len(lasts) == 1:
            return Owner("resolved", raw=raw.strip(), name=lasts[0])
        if len(lasts) > 1:
            return Owner("ambiguous", raw=raw.strip(), candidates=sorted(lasts))
    else:  # "Priya S" / "Priya S." -> first name + last initial
        matches = [
            n for n, parts in attendee_parts
            if len(parts) > 1 and parts[0] == tokens[0] and parts[-1].startswith(tokens[-1])
        ]
        if len(matches) == 1:
            return Owner("resolved", raw=raw.strip(), name=matches[0])
        if len(matches) > 1:
            return Owner("ambiguous", raw=raw.strip(), candidates=sorted(matches))
    return Owner("unverified", raw=raw.strip(), name=name)


def _resolve_many(raw: str, attendees: list[Attendee]) -> Owner:
    parts = [p for p in re.split(r"\s*(?:,|&|/|\band\b)\s*", raw) if p.strip()]
    if len(parts) == 1:
        return resolve_owner(parts[0], attendees)
    owners = [resolve_owner(p, attendees) for p in parts]
    resolved = {o.name for o in owners if o.status == "resolved"}
    if len(resolved) == 1 and all(o.status == "resolved" for o in owners):
        return owners[0]
    labels = [o.name or o.raw or "" for o in owners]
    return Owner("multiple", raw=raw.strip(), candidates=labels)


def extract_owner(text: str, attendees: list[Attendee]) -> tuple[Owner, str]:
    """Return (owner, task text with the owner phrase removed)."""
    m = OWNER_TAG_RE.search(text)
    if m:
        remainder = (text[: m.start()] + " " + text[m.end():]).strip(" -\u2013\u2014,;")
        return resolve_owner(m.group(1), attendees), " ".join(remainder.split())
    m = AT_LEAD_RE.match(text)
    if m:
        return resolve_owner(_handle_to_name(m.group(1)), attendees), m.group(2)
    for regex in (BRACKET_RE, COLON_RE):
        m = regex.match(text)
        if m:
            owner = resolve_owner(m.group(1), attendees)
            if owner.status in ("resolved", "ambiguous") or name_key(m.group(1)) in NON_PERSON:
                return owner, m.group(2)
    m = TO_RE.match(text)
    if m and m.group(1).split()[0].lower() not in NOT_NAMES:
        return _resolve_many(m.group(1), attendees), m.group(2)
    m = AT_ANY_RE.search(text)
    if m:
        remainder = " ".join((text[: m.start()] + text[m.end():]).split())
        return resolve_owner(_handle_to_name(m.group(1)), attendees), remainder
    return Owner("unassigned"), text


# --------------------------------------------------------------------------- due dates


def extract_due(
    text: str, meeting: date
) -> tuple[date | None, str | None, str | None, tuple[int, int] | None]:
    """Find a due-date phrase in ``text``.

    Returns (due_date, raw_phrase, error, span).  ``span`` covers the marker and the
    phrase so callers can remove it when normalising task text.
    """
    for marker in DUE_MARKER_RE.finditer(text):
        rest = text[marker.end():]
        stop = DUE_STOP_RE.search(rest)
        chunk = (rest[: stop.start()] if stop else rest).strip().strip("*_`")
        words = chunk.split()
        if not looks_like_date_start(words):
            continue
        for n in range(min(len(words), 7), 0, -1):
            candidate = " ".join(words[:n])
            pos = text.find(candidate, marker.end())
            span = (marker.start(), (pos + len(candidate)) if pos >= 0 else marker.end())
            try:
                resolved = resolve_due(candidate, meeting)
            except DateParseError as exc:
                if exc.definitive:
                    return None, candidate, str(exc), span
                continue
            if resolved:
                return resolved, candidate, None, span
        pos = text.find(chunk, marker.end())
        span = (marker.start(), (pos + len(chunk)) if pos >= 0 else marker.end())
        return None, chunk, f"could not interpret due date '{chunk}'", span
    return None, None, None, None


# --------------------------------------------------------------------------- front matter


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def split_front_matter(lines: list[str]) -> tuple[dict, int, list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, 0, []
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        raise MalformedNoteError("front matter opened with '---' on line 1 but never closed")
    data: dict = {}
    warnings: list[str] = []
    key = None
    for offset, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        kv = re.match(r"^([A-Za-z_][\w \-]*?)\s*:\s*(.*)$", raw)
        if kv and not raw[0].isspace() and not raw.startswith("-"):
            key = kv.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            value = kv.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                data[key] = [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
            else:
                data[key] = [] if value == "" else _unquote(value)
        elif key and re.match(r"^\s*-\s+", raw):
            if not isinstance(data.get(key), list):
                data[key] = [] if data.get(key) in (None, "") else [data[key]]
            data[key].append(_unquote(re.sub(r"^\s*-\s+", "", raw)))
        else:
            warnings.append(f"line {offset}: unrecognised front matter line ignored")
    return data, end + 1, warnings


def _fm_str(fm: dict, *keys: str) -> str | None:
    for k in keys:
        v = fm.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v:
            return ", ".join(v)
    return None


# --------------------------------------------------------------------------- main parse


def decode_bytes(data: bytes) -> tuple[str, list[str]]:
    if b"\x00" in data:
        raise MalformedNoteError("binary content (NUL bytes); not a text Markdown file")
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252"), ["file is not valid UTF-8; decoded as Windows-1252"]
        except UnicodeDecodeError as exc:
            raise MalformedNoteError(f"cannot decode text: {exc}") from None


def parse_text(text: str, rel_path: str) -> ParsedNote:
    lines = [clean_line(line) for line in text.splitlines()]
    if not any(line.strip() for line in lines):
        raise MalformedNoteError("file is empty")
    fm, body_start, warnings = split_front_matter(lines)

    title = _fm_str(fm, "title")
    date_raw = _fm_str(fm, "date", "meeting_date")
    attendees: list[Attendee] = []
    for key in ("attendees", "participants", "present"):
        value = fm.get(key)
        if isinstance(value, list):
            attendees += [a for a in (parse_attendee(v) for v in value) if a]
        elif isinstance(value, str):
            attendees += parse_attendee_list(value)

    headings: list[str] = []
    raw_items: list[tuple[int, str, str, bool, str | None]] = []
    section: str | None = None
    in_code = False

    for idx in range(body_start, len(lines)):
        line_no, line = idx + 1, lines[idx]
        stripped = line.strip()
        if FENCE_RE.match(stripped):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue

        heading = HEADING_RE.match(stripped)
        if heading:
            htext = strip_inline_md(heading.group(1))
            headings.append(htext)
            if title is None and stripped.startswith("# "):
                title = htext
            if ATTENDEE_HEADING_RE.match(htext):
                section = "attendees"
            elif ACTION_HEADING_RE.search(htext):
                section = "actions"
            else:
                section = None
            continue

        fld = FIELD_RE.match(stripped)
        if fld:
            key, value = fld.group("key").lower(), fld.group("value").strip()
            if key in DATE_KEYS:
                if date_raw is None:
                    date_raw = value
                continue
            if key in ATTENDEE_KEYS:
                if value:
                    attendees += parse_attendee_list(value)
                    section = None if section == "attendees_field" else section
                else:
                    section = "attendees_field"
                continue

        checkbox = CHECKBOX_RE.match(line) or BARE_CHECKBOX_RE.match(line)
        if section in ("attendees", "attendees_field"):
            bullet = checkbox or BULLET_RE.match(line)
            if bullet:  # attendance checklists ("- [x] Priya Sharma") are attendees
                person = parse_attendee(bullet.groups()[-1])
                if person:
                    attendees.append(person)
                continue
            if section == "attendees":
                attendees += parse_attendee_list(stripped)
                continue
            section = None  # a field-style list ends at the first non-bullet line

        if checkbox:
            marker, body = checkbox.group(1).strip(), checkbox.group(2)
            if marker.lower() in ("x", "\u2713", "\u2714"):
                raw_items.append((line_no, line, body, True, None))
            elif marker == "":
                raw_items.append((line_no, line, body, False, None))
            else:
                raw_items.append((line_no, line, body, False,
                                  f"line {line_no}: unrecognised checkbox marker '[{marker}]'; stored as open"))
        elif BROKEN_CHECKBOX_RE.match(line):
            warnings.append(f"line {line_no}: malformed checkbox (missing ']'); line skipped")
        elif section == "actions" and (bullet := BULLET_RE.match(line)):
            raw_items.append((line_no, line, bullet.group(1), False, None))
        elif action := ACTION_LINE_RE.match(stripped):
            raw_items.append((line_no, line, action.group(1), False, None))

    # ---- meeting date: explicit field > heading > filename; never today's date
    stem = Path(rel_path).stem
    if date_raw is not None:
        if not date_raw.strip():
            raise MalformedNoteError("meeting date field is empty")
        try:
            meeting_date = parse_absolute(date_raw) or find_date_in_text(date_raw)
        except DateParseError as exc:
            raise MalformedNoteError(f"unparseable meeting date '{date_raw}': {exc}") from None
        if meeting_date is None:
            raise MalformedNoteError(f"unparseable meeting date '{date_raw}'")
    else:
        meeting_date = next((d for d in map(find_date_in_text, headings) if d), None)
        meeting_date = meeting_date or find_date_in_text(stem)
        if meeting_date is None:
            raise MalformedNoteError(
                "no meeting date found (expected 'Date:' line, front matter 'date:', "
                "a date in a heading, or YYYY-MM-DD in the filename)"
            )
    filename_date = find_date_in_text(stem)
    if filename_date and filename_date != meeting_date:
        warnings.append(
            f"filename date {filename_date} differs from meeting date {meeting_date}; "
            "using the date written in the note"
        )

    attendees = _dedupe_attendees(attendees)
    if not attendees:
        warnings.append("no attendees listed; owners cannot be verified")

    items: list[ParsedItem] = []
    counter: Counter[str] = Counter()
    for line_no, raw_line, body, checked, marker_warning in raw_items:
        if marker_warning:
            warnings.append(marker_warning)
        text_ = strip_inline_md(body)
        if not text_:
            warnings.append(f"line {line_no}: empty action item skipped")
            continue
        if text_.startswith("~~") and text_.endswith("~~"):
            warnings.append(f"line {line_no}: struck-through item skipped")
            continue
        owner, remainder = extract_owner(text_, attendees)
        remainder = remainder.strip(" -\u2013\u2014:,;") or text_
        due, due_raw, due_error, span = extract_due(remainder, meeting_date)
        core = remainder if span is None else remainder[: span[0]] + " " + remainder[span[1]:]
        full_norm = normalize_task(text_)
        counter[full_norm] += 1
        items.append(ParsedItem(
            line_no=line_no,
            raw_line=raw_line.strip(),
            text=text_,
            task=remainder[0].upper() + remainder[1:],
            checked=checked,
            owner=owner,
            due_date=due,
            due_raw=due_raw,
            due_error=due_error,
            norm_task=normalize_task(core) or normalize_task(remainder),
            occurrence_key=f"{full_norm}#{counter[full_norm]}",
        ))

    warnings.sort(key=_warning_line)
    normalized = "\n".join(lines).strip()
    return ParsedNote(
        rel_path=rel_path,
        title=title or stem,
        meeting_date=meeting_date,
        attendees=attendees,
        items=items,
        warnings=warnings,
        content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def _warning_line(message: str) -> int:
    m = re.match(r"line (\d+):", message)
    return int(m.group(1)) if m else 0


def parse_file(path: Path, rel_path: str) -> ParsedNote:
    """Read and parse one note.  Raises MalformedNoteError or OSError."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise MalformedNoteError(f"file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    data = path.read_bytes()
    text, decode_warnings = decode_bytes(data)
    note = parse_text(text, rel_path)
    note.warnings = decode_warnings + note.warnings
    return note
