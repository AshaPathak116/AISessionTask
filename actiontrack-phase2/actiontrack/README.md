# actiontrack

Track action items from Markdown meeting notes in a local SQLite database.

**Status: Phase 2 of 3.** Implemented: `ingest`, `list` and `done`.
`followup` (Phase 3, Gmail drafts) is a placeholder that exits with code 2.
No Gmail or network code exists yet.

## Install (Windows PowerShell)

Requires Python 3.11 or newer. No runtime dependencies beyond the standard library.

```powershell
cd path\to\actiontrack
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
actiontrack --version
```

On macOS/Linux use `python3 -m venv .venv` and `source .venv/bin/activate`.

## Inspect your notes first (writes nothing)

Quote the path because the folder name contains spaces and parentheses:

```powershell
actiontrack ingest "C:\Users\asha.pathak_coditas\Desktop\Session\notes (2)" --dry-run --verbose
```

The report lists the number of `.md` files found, malformed files with reasons,
duplicate files, merged duplicate tasks, possible duplicates that were *not*
merged, ambiguous owners with their candidates, owners not found among attendees,
unassigned items, missing and unparseable due dates, and line-level warnings.
Add `--json` for machine-readable output. Your note files are only ever read.

## Ingest

```powershell
actiontrack ingest "C:\path\to\notes"                 # recursive by default
actiontrack ingest notes --db C:\data\actions.db      # choose the database
$env:ACTIONTRACK_DB = "C:\data\actions.db"            # or set it once
actiontrack ingest notes --no-recursive
```

Default database: `actiontrack.db` in the current folder. Exit codes: `0` success
(even when some files were skipped, since they are reported), `1` database error
or unknown item ID, `2` bad folder, bad option, ambiguous owner, or unimplemented
command.

## List items

```powershell
actiontrack list
actiontrack list --owner "Priya Sharma"
actiontrack list --owner Rahul --status overdue
actiontrack list --status open --due-before 2025-10-31
actiontrack list --owner-id 4 --json
```

Columns: ID, owner, task, due date, status, source file (`(+1)` means the task was
also mentioned in one more note). Sorted by due date; items with no due date last.

- `--status done`: completed items.
- `--status overdue`: open items whose **known** due date is earlier than today.
  Due today is not overdue. Items with no due date are never overdue.
- `--status open`: every item not yet done, **including** overdue ones (the status
  column still shows `overdue` for those).
- `--due-before YYYY-MM-DD`: due strictly before that date. Items with no due date
  are excluded and the output says how many were left out.
- `--owner`: a full name (case-insensitive), or a first name, last name or
  `First L.` that matches exactly one person. If it matches several people
  (e.g. two Priyas), nothing is listed and the error shows each person's owner
  ID so you can use `--owner-id`. Names that were not among a note's attendees
  can be listed by the exact name written (e.g. `--owner Vikram`).
- Tasks with an ambiguous or shared owner are never shown under a person, but
  `--owner` prints a note naming any open ones that might involve them.
- All filters combine with AND. Use `--wide` for full task text, `--json` for scripts.

## Mark items done

```powershell
actiontrack done 12
actiontrack done 12 15 18       # several at once
actiontrack done 3fa2c19b04     # stable uid from `list --json`
```

Repeating `done` on a completed item changes nothing and says when and how it
was completed. An unknown ID prints an error and exits with code 1; with several
IDs, the valid ones are still applied. `list` and `done` never create a database:
if none exists they tell you to run `ingest` first.

## Today's date

Overdue checks and completion dates use the system date. Override it with
`--today YYYY-MM-DD` or the `ACTIONTRACK_TODAY` environment variable (the flag
wins). This never affects how due dates in notes are resolved; those always use
the meeting date.

## Completed items stay completed

Re-ingesting never reopens a task. A task marked done with `actiontrack done`
stays done when the same notes are ingested again, when a note is edited, when an
earlier note still lists it unticked, and when a later note ticks it. The one
case that creates a *new* open item is the task appearing unticked in a meeting
held **after** it was completed, which is treated as recurring work and linked as
a possible duplicate.

## What the parser understands

**Meeting date** (required; a note without one is skipped as malformed). Checked
in order: front matter `date:`, a `Date:` / `**Date:**` / `Meeting date:` line, a
date inside a heading (`# Retro — 2025-10-13`, `# Sync - 1 October 2025`), then a
date in the filename (`2025-10-06-standup.md`, `standup_20251006.md`). If the note
and filename disagree, the note wins and a warning is shown. An explicit but
unparseable date (`Date: 2025-13-45`, `Date: TBD`) makes the note malformed rather
than falling back to a guess.

**Attendees**: `Attendees:` / `Participants:` / `Present:` lines (comma, semicolon,
`and`, `&` separated), the same keys in front matter, or an `## Attendees` section
of bullets. Roles in parentheses and after ` - ` are dropped. Emails are recorded
only when written (`Priya Sharma <priya@x.com>`). Attendance checklists under an
Attendees heading are treated as attendees, not tasks.

**Action items**: checkboxes anywhere (`- [ ]`, `-[ ]`, `* [X]`, `1. [ ]`, `[ ]`,
irregular spacing, tabs, non-breaking spaces), plain bullets inside an
*Action items / Next steps / To-do / Follow-ups / Tasks* section, and lines
starting `ACTION:` / `TODO:`. Content inside code fences is ignored.

**Owners**: `Priya to…`, `Priya Sharma will…`, `Priya: …`, `[Priya] …`,
`@priya.sharma …`, `(owner: Priya)`, `assigned to Priya`. Each written name is
matched against that note's attendees:

| Written | Result |
|---|---|
| Full name, or a first/last name matching exactly one attendee, or `Priya S.` | **resolved** to that person |
| First name matching several attendees | **ambiguous**: owner left empty, candidates stored |
| Name not among attendees (or no attendee list) | **unverified**: stored exactly as written |
| `Priya & Karan to…` | **multiple**: not assigned to either person |
| No owner, or `TBD`, `Someone`, `Team`, `We` | **unassigned** |

Sentences such as `Need to check…` or `Note: …` are not mistaken for owners.

## Due-date rules

Every relative date is resolved against the **meeting date**, never today's date.

| Phrase | Meaning |
|---|---|
| `Friday` | next Friday strictly after the meeting (said on a Friday, means a week later) |
| `this Friday` | as above, but the meeting day itself counts |
| `next Friday` | pushed a further week if the result is still in the meeting's week |
| `today`, `EOD`, `COB` / `tomorrow` | meeting date / +1 day |
| `end of week`, `EOW` / `end of month`, `EOM` | Friday on or after meeting / last day of month |
| `2 Oct`, `Oct 2`, `2nd October` | earliest year not more than 31 days before the meeting (29 Dec + "5 Jan" gives next January) |
| `2025-10-10`, `10 October 2025` | taken as written |
| `25/09` | accepted only when unambiguous; `10/02` is rejected as ambiguous |
| `next week`, `ASAP`, `end of the sprint`, `31 Feb` | **not converted**: due date stays NULL, the raw text and reason are kept |

Markers recognised: `by`, `due`, `due by/on`, `deadline:`, `before`, `until`,
`no later than`, `(due: …)`, `[due:: …]`, and the 📅 emoji. `by email` or
`by 10%` are not treated as dates.

## Duplicates and idempotency

- Every occurrence is keyed by file + normalised line text + occurrence number, so
  re-ingesting the same folder creates nothing new.
- A task in a later note is **merged** into an existing open item only when the
  owner is *resolved* to the same person and the task text matches after
  normalisation (case, punctuation, whitespace, bold markers and the due-date
  phrase removed). Every mention is kept in `item_sources` with file, line, raw
  text, checkbox state and that mention's due date. The item takes the due date
  from its most recent mention that has one.
- Tasks with unassigned, ambiguous, unverified or multiple owners are **never**
  merged across files. Same-text ones are listed as possible duplicates.
- Similar but non-identical tasks for the same owner (≥80% text similarity, e.g.
  "quote" vs "quotation") are listed as possible duplicates, **not** merged.
- A task that reappears **unticked** in a meeting after it was completed is
  stored as a new item (recurring work) and linked as a possible duplicate.
- Byte-identical copies of a note (e.g. `copy-of-x.md`) are skipped as duplicate
  files. `__MACOSX/` folders and `._*` files from ZIP extraction are ignored.
- Ingest only ever changes an item from open to done (when a box is ticked),
  never from done back to open.

## Malformed files

Skipped and reported with a reason, and the rest of the folder still ingests:
empty files, binary content, unclosed front matter, missing or unparseable
meeting dates, unreadable files, files over 20 MB. Non-UTF-8 files are decoded as
Windows-1252 with a warning. Problems with single lines (empty checkbox, missing
`]`, unknown marker like `[?]`, struck-through item) are warnings; the rest of
the note is kept.

## Database schema

`people`, `meetings` (relative `source_file`, absolute path, title, date,
attendees, content hash), `action_items` (owner, owner status and candidates,
task, due date / raw / error, status, completion source, first source file and
meeting), `item_sources` (full provenance per mention), `possible_duplicates`,
`ingest_runs`. See `src/actiontrack/storage.py`. Inspect with any SQLite viewer,
for example `sqlite3 actiontrack.db "select id, task, due_date, status from action_items"`.

## Tests

```powershell
python -m pytest -q
```

Tests use temporary SQLite databases and temporary copies of
`tests/fixtures/notes/` (17 `.md` files covering the edge cases above); one test
asserts the fixture files are byte-for-byte unchanged after ingest.

## Troubleshooting

- **"folder not found"**: put the path in quotes; check it with `dir "path"`.
- **A note was skipped**: the report gives the reason; most often the note has no
  date. Add `Date: YYYY-MM-DD` near the top.
- **Owner shows as ambiguous/unverified**: add the person's full name to the
  note's `Attendees:` line or write the full name in the task.
- **"matches more than one person"**: use the full name or the `--owner-id`
  shown in the message.
- **Overdue looks wrong**: check `today = …` at the bottom of `list`; a leftover
  `ACTIONTRACK_TODAY` variable overrides the real date (`Remove-Item Env:ACTIONTRACK_TODAY`).
- **`actiontrack` not recognised**: activate the virtual environment, or run
  `python -m actiontrack ingest …`.
- **PowerShell blocks `Activate.ps1`**: run
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Known limitations

- People are identified by full name; two different people with the same full
  name would be treated as one.
- If a note is edited so a task line disappears, the earlier record is kept and
  the report says so; nothing is deleted.
- Ingest the same root folder each time. A different root changes relative paths;
  unchanged files are still recognised by content, but edited ones would be
  treated as new meetings.
- There is no command to reopen an item marked done by mistake yet; for now use
  `sqlite3 actiontrack.db "update action_items set status='open', completed_on=null, completed_by=null where id=12"`.
- English month and weekday names only. Times of day (`3pm`) are ignored.
- Owner detection is pattern-based; unusual phrasings fall back to unassigned
  rather than guessing.
