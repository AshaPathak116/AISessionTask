"""Command-line interface.  Phase 1 implements ``ingest``."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .ingest import IngestError, IngestReport, ingest_folder
from .storage import connect

DEFAULT_DB = "actiontrack.db"
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


def _print_section(title: str, rows: list[str], limit: int | None) -> None:
    if not rows:
        return
    print(f"\n{title} ({len(rows)})")
    shown = rows if limit is None else rows[:limit]
    for row in shown:
        print(f"  - {row}")
    if limit is not None and len(rows) > limit:
        print(f"  ... {len(rows) - limit} more (use --verbose to see all)")


def render_report(report: IngestReport, verbose: bool, dry_run: bool) -> None:
    limit = None if verbose else 10
    c = report.counts()
    print(f"Ingest {'(dry run - nothing saved) ' if dry_run else ''}of {report.folder}")
    print(f"  Markdown files found : {c['md_files_found']}")
    print(f"  Parsed               : {c['parsed_files']}  "
          f"(new {report.new_files}, unchanged {report.unchanged_files}, "
          f"changed {len(report.changed_files)})")
    print(f"  Skipped (malformed)  : {c['skipped_malformed']}")
    print(f"  Duplicate files      : {c['duplicate_files']}")
    print(f"  Ignored metadata     : {c['ignored']}")
    print(f"  Action items seen    : {c['items_seen']}  "
          f"(new {c['items_new']}, already known {c['items_already_known']}, "
          f"merged as duplicates {c['merged_duplicates']})")
    print(f"  Marked done by notes : {c['marked_done_from_notes']}")

    _print_section("Skipped malformed files",
                   [f"{f.file}: {f.reason}" for f in report.skipped], None)
    _print_section("Duplicate files", [f"{f.file}: {f.reason}" for f in report.duplicate_files],
                   None)
    _print_section("Ignored files", [f"{f.file}: {f.reason}" for f in report.ignored], limit)
    _print_section("Merged duplicate mentions",
                   [f"#{m['item_id']} {m['task']!r} - {m['file']}:{m['line']} "
                    f"(first seen in {m['first_seen_in']})" for m in report.merged], limit)
    _print_section("Possible duplicates (NOT merged - review)",
                   [f"#{p['item_a']} {p['task_a']!r} [{p['file_a']}] vs #{p['item_b']} "
                    f"{p['task_b']!r} [{p['file_b']}]: {p['reason']}"
                    for p in report.possible_duplicates], limit)
    _print_section("Ambiguous owners (not assigned)",
                   [f"#{i['item_id']} {i['owner_raw']!r} could be {', '.join(i['candidates'])}"
                    f" - {i['task']!r} [{i['file']}]" for i in report.ambiguous], None)
    _print_section("Multiple owners on one task (not assigned)",
                   [f"#{i['item_id']} {i['owner_raw']!r} - {i['task']!r} [{i['file']}]"
                    for i in report.multiple_owners], None)
    _print_section("Owners not found among attendees (stored as written)",
                   [f"#{i['item_id']} {i['owner_raw']!r} - {i['task']!r} [{i['file']}]"
                    for i in report.unverified], limit)
    _print_section("Unassigned items",
                   [f"#{i['item_id']} {i['task']!r} [{i['file']}]" for i in report.unassigned],
                   limit)
    _print_section("Missing due dates (stored as NULL)",
                   [f"#{i['item_id']} {i['task']!r} [{i['file']}]" for i in report.missing_due],
                   limit)
    _print_section("Unparseable due dates (stored as NULL, raw text kept)",
                   [f"#{i['item_id']} {i['due_raw']!r}: {i['error']} [{i['file']}]"
                    for i in report.unparseable_due], limit)
    _print_section("Notes with no action items", report.files_without_items, limit)
    _print_section("Edited notes with removed lines",
                   [f"{f.file}: {f.reason}" for f in report.stale_sources], limit)
    _print_section("Warnings", [f"{w.file}: {w.reason}" for w in report.warnings], limit)

    t = report.db_totals
    print(f"\nDatabase now holds {t.get('items', 0)} items ({t.get('open', 0)} open, "
          f"{t.get('done', 0)} done) from {t.get('meetings', 0)} meetings, "
          f"{t.get('people', 0)} people.")


def cmd_ingest(args: argparse.Namespace) -> int:
    db = ":memory:" if args.dry_run else args.db
    try:
        conn = connect(db)
    except Exception as exc:
        print(f"error: cannot open database {db}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        report = ingest_folder(conn, args.folder, recursive=not args.no_recursive,
                               record=not args.dry_run)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        if report.md_files_found == 0:
            print(f"No Markdown (.md) files found in {report.folder}.")
            return EXIT_OK
        render_report(report, args.verbose, args.dry_run)
    return EXIT_OK


def cmd_not_yet(args: argparse.Namespace) -> int:
    print(f"'{args.command}' is planned for a later phase and is not implemented yet.",
          file=sys.stderr)
    return EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=os.environ.get("ACTIONTRACK_DB", DEFAULT_DB),
                        help="SQLite database path (env ACTIONTRACK_DB; default %(default)s)")

    parser = argparse.ArgumentParser(
        prog="actiontrack", description="Track action items from Markdown meeting notes.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    ingest = sub.add_parser("ingest", parents=[common], help="read notes from a folder")
    ingest.add_argument("folder", help="folder containing .md meeting notes")
    ingest.add_argument("--no-recursive", action="store_true",
                        help="only read files directly in the folder")
    ingest.add_argument("--dry-run", action="store_true",
                        help="parse and report without writing to the database")
    ingest.add_argument("-v", "--verbose", action="store_true", help="show every row")
    ingest.add_argument("--json", action="store_true", help="print the report as JSON")
    ingest.set_defaults(func=cmd_ingest)

    for name, phase in (("list", 2), ("done", 2), ("followup", 3)):
        stub = sub.add_parser(name, parents=[common], help=f"(Phase {phase}, not yet available)")
        stub.add_argument("args", nargs="*")
        stub.set_defaults(func=cmd_not_yet)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # avoid crashes on legacy Windows consoles
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_ERROR
    except BrokenPipeError:  # e.g. output piped into `head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
