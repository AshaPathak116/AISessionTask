"""Command-line interface: ingest, list, done (followup arrives in Phase 3)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from pathlib import Path

from .dates import DateParseError, parse_iso_date, today as get_today
from .ingest import IngestError, IngestReport, ingest_folder
from .queries import QueryError, list_items, mark_done, resolve_owner_query
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


def _iso_date_arg(text: str):
    try:
        return parse_iso_date(text)
    except DateParseError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _open_existing_db(path: str):
    """list/done must not silently create an empty database."""
    if path != ":memory:" and not Path(path).expanduser().exists():
        print(f"error: database not found: {path}\n"
              "Run `actiontrack ingest <folder>` first, or pass --db / set ACTIONTRACK_DB.",
              file=sys.stderr)
        return None
    try:
        return connect(path)
    except Exception as exc:
        print(f"error: cannot open database {path}: {exc}", file=sys.stderr)
        return None


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def render_table(items, wide: bool) -> None:
    task_w = 10_000 if wide else 48
    rows = [(str(i.id), i.owner, _shorten(i.task, task_w), i.due_date or "-", i.status,
             i.source_file + (f" (+{i.sources - 1})" if i.sources > 1 else ""))
            for i in items]
    headers = ("ID", "OWNER", "TASK", "DUE", "STATUS", "SOURCE")
    widths = [max(len(h), *(len(r[n]) for r in rows)) for n, h in enumerate(headers)]
    line = "  ".join("{:<%d}" % w for w in widths)
    print(line.format(*headers).rstrip())
    print(line.format(*("-" * w for w in widths)).rstrip())
    for r in rows:
        print(line.format(*r).rstrip())


def cmd_list(args: argparse.Namespace) -> int:
    try:
        now = get_today(args.today)
    except DateParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    conn = _open_existing_db(args.db)
    if conn is None:
        return EXIT_ERROR
    try:
        owner = None
        if args.owner is not None or args.owner_id is not None:
            owner = resolve_owner_query(conn, args.owner, args.owner_id)
        result = list_items(conn, today=now, owner=owner, status=args.status,
                            due_before=args.due_before)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"today": now.isoformat(),
                          "items": [i.__dict__ for i in result.items],
                          "excluded_no_due_date": result.excluded_no_due,
                          "possibly_related_unassigned": result.related_unassigned},
                         indent=2))
        return EXIT_OK
    if result.database_empty:
        print("The database has no action items yet. Run `actiontrack ingest <folder>` first.")
        return EXIT_OK
    if not result.items:
        print("No action items match these filters.")
    else:
        render_table(result.items, args.wide)
        overdue = sum(i.status == "overdue" for i in result.items)
        done = sum(i.status == "done" for i in result.items)
        print(f"\n{len(result.items)} item(s): {len(result.items) - done} open"
              f" ({overdue} overdue), {done} done  (today = {now.isoformat()})")
    if result.excluded_no_due:
        print(f"Note: {result.excluded_no_due} otherwise-matching item(s) have no known due date"
              " and were excluded by --due-before.")
    if result.related_unassigned and owner is not None:
        ids = ", ".join(f"#{i}" for i in result.related_unassigned)
        print(f"Note: open item(s) {ids} have an ambiguous or shared owner that may include"
              f" {owner.label}; they are not assigned to anyone and not listed here.")
    return EXIT_OK


def cmd_done(args: argparse.Namespace) -> int:
    try:
        now = get_today(args.today)
    except DateParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    conn = _open_existing_db(args.db)
    if conn is None:
        return EXIT_ERROR
    exit_code = EXIT_OK
    try:
        for ref in args.ids:
            try:
                r = mark_done(conn, ref, today=now)
            except QueryError as exc:
                print(f"error: {exc}", file=sys.stderr)
                exit_code = EXIT_ERROR
                continue
            if r.already_done:
                print(f"#{r.id} is already done (completed {r.completed_on} via {r.completed_by});"
                      f" nothing changed: {r.task}")
            else:
                print(f"Marked #{r.id} done: {r.task}")
    finally:
        conn.close()
    return exit_code


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

    clock = argparse.ArgumentParser(add_help=False)
    clock.add_argument("--today", metavar="YYYY-MM-DD",
                       help="treat this as today's date (env ACTIONTRACK_TODAY)")

    lst = sub.add_parser("list", parents=[common, clock], help="list action items")
    lst.add_argument("--owner", help="full name, or a first/last name that is unique")
    lst.add_argument("--owner-id", type=int, help="owner ID (shown when a name is ambiguous)")
    lst.add_argument("--status", choices=("open", "done", "overdue"),
                     help="open includes overdue items")
    lst.add_argument("--due-before", type=_iso_date_arg, metavar="YYYY-MM-DD",
                     help="due strictly before this date (items with no due date excluded)")
    lst.add_argument("--wide", action="store_true", help="do not shorten task text")
    lst.add_argument("--json", action="store_true", help="print JSON")
    lst.set_defaults(func=cmd_list)

    done = sub.add_parser("done", parents=[common, clock], help="mark item(s) done")
    done.add_argument("ids", nargs="+", metavar="ID", help="item ID (e.g. 12) or stable uid")
    done.set_defaults(func=cmd_done)

    stub = sub.add_parser("followup", parents=[common], help="(Phase 3, not yet available)")
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
