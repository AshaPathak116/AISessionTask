"""End-to-end verification of actiontrack against any folder of meeting notes.

    python scripts/verify_e2e.py "C:\\path\\to\\notes"

Runs the real CLI in separate processes against a COPY of the folder (the
originals are only read and are hash-checked afterwards), using a temporary
database. Every result is checked against an independent computation (plain
SQL, calendar arithmetic) rather than against actiontrack's own code.

Gmail: drafts are only ever created against a LOCAL fake Gmail MCP server
(tests/fake_gmail_server.py) that also advertises send tools and logs every
request; no real Gmail account is contacted and the script refuses to run if
real Gmail settings are present in the environment.

Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = ROOT / "tests" / "fake_gmail_server.py"
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
            "saturday": 5, "sunday": 6, "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4}
GMAIL_ENV = ("ACTIONTRACK_GMAIL_MCP_URL", "ACTIONTRACK_GMAIL_TOKEN",
             "ACTIONTRACK_GMAIL_TOKEN_CMD", "ACTIONTRACK_GMAIL_MCP_COMMAND")


class Checker:
    def __init__(self) -> None:
        self.passed, self.failed = 0, []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  FAIL  {name}  {detail}")
        return ok

    def section(self, title: str) -> None:
        print(f"\n== {title}")


def hashes(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--keep", action="store_true", help="keep the temporary workspace")
    args = ap.parse_args()
    source = Path(args.folder).expanduser().resolve()
    if not source.is_dir():
        print(f"error: not a folder: {source}")
        return 2
    leaked = [v for v in GMAIL_ENV if os.environ.get(v)]
    if leaked:
        print(f"error: unset {', '.join(leaked)} first; this script must never reach real Gmail")
        return 2

    work = Path(tempfile.mkdtemp(prefix="actiontrack-verify-"))
    notes, db = work / "notes", work / "verify.db"
    shutil.copytree(source, notes)
    original = hashes(source)
    c = Checker()
    env = {k: v for k, v in os.environ.items() if k not in GMAIL_ENV and
           not k.startswith("ACTIONTRACK_")}
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"

    def cli(*argv, extra_env=None):
        done = subprocess.run([sys.executable, "-m", "actiontrack", *argv, "--db", str(db)],
                              capture_output=True, text=True, encoding="utf-8",
                              env={**env, **(extra_env or {})}, cwd=work)
        return done.returncode, done.stdout, done.stderr

    def sql(query, params=()):
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(query, params).fetchall()
        finally:
            con.close()

    print(f"Verifying actiontrack against a copy of {source}\nWorkspace: {work}")

    # ------------------------------------------------------------------ ingest
    c.section("ingest")
    code, out, err = cli("ingest", str(notes), "--json")
    c.check(code == 0, "ingest exits 0", err[-300:])
    rep = json.loads(out)
    found = rep["md_files_found"]
    accounted = (rep["parsed_files"] + len(rep["skipped"]) + len(rep["duplicate_files"])
                 + len(rep["ignored"]))
    print(f"  {found} .md files: {rep['parsed_files']} parsed, {len(rep['skipped'])} malformed,"
          f" {len(rep['duplicate_files'])} duplicate files, {len(rep['ignored'])} ignored")
    print(f"  {rep['items_seen']} action-item lines -> {rep['items_new']} items "
          f"({len(rep['merged'])} repeated mentions merged, "
          f"{len(rep['possible_duplicates'])} possible duplicates flagged)")
    c.check(found == accounted, "every .md file is parsed, skipped, duplicate or ignored",
            f"{found} found vs {accounted} accounted")
    c.check(all(s["reason"] for s in rep["skipped"]), "every malformed file has a reason")
    if found > len(rep["skipped"]) + len(rep["ignored"]):
        c.check(rep["parsed_files"] > 0, "malformed files do not stop valid ones")
    for s in rep["skipped"]:
        print(f"  skipped {s['file']}: {s['reason']}")

    snapshot = [dict(r) for r in sql("SELECT * FROM action_items ORDER BY id")]
    total = len(snapshot)

    c.section("re-ingest (idempotency)")
    code, out, _ = cli("ingest", str(notes), "--json")
    rep2 = json.loads(out)
    c.check(code == 0 and rep2["items_new"] == 0 and not rep2["merged"],
            "second ingest adds nothing", f"new={rep2['items_new']} merged={len(rep2['merged'])}")
    again = [dict(r) for r in sql("SELECT * FROM action_items ORDER BY id")]
    for row in again + snapshot:
        row.pop("updated_at")
    c.check(again == snapshot, "all item rows identical after re-ingest")

    # ------------------------------------------------------------------ dates
    c.section("due-date resolution (checked against the calendar)")
    checked = 0
    for s in sql("SELECT s.due_raw, s.due_date, m.meeting_date, m.source_file, s.line_no"
                 " FROM item_sources s JOIN meetings m ON m.id = s.meeting_id"
                 " WHERE s.due_date IS NOT NULL"):
        raw = s["due_raw"].lower().strip()
        meeting, due = date.fromisoformat(s["meeting_date"]), date.fromisoformat(s["due_date"])
        where = f"{s['source_file']}:{s['line_no']} '{s['due_raw']}' {meeting} -> {due}"
        words = raw.replace("eod", "").split()
        wd = next((WEEKDAYS[w] for w in words if w in WEEKDAYS), None)
        if raw in ("today", "eod", "cob", "tonight"):
            ok = due == meeting
        elif raw == "tomorrow":
            ok = due == meeting + timedelta(days=1)
        elif wd is not None and not re.search(r"\d", raw):
            ok = due.weekday() == wd and 0 <= (due - meeting).days <= 14
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            ok = due.isoformat() == raw
        elif re.search(r"\d", raw):
            ok = -31 <= (due - meeting).days <= 366 and str(due.day) in raw.lstrip("0") + raw
        else:
            ok = 0 <= (due - meeting).days <= 31
        checked += 1
        c.check(ok, "due date plausible for its phrase", where)
    print(f"  {checked} resolved due dates checked")
    bad_no_due = sql("SELECT COUNT(*) FROM action_items WHERE due_date IS NULL AND due_raw IS NULL"
                     " AND due_error IS NOT NULL")[0][0]
    c.check(bad_no_due == 0, "unparseable dates keep their raw text")

    # ------------------------------------------------------------------ list
    c.section("list filters (compared with independent SQL)")
    dues = sorted(r[0] for r in sql("SELECT due_date FROM action_items WHERE due_date IS NOT NULL"))
    today = dues[len(dues) // 2] if dues else date.today().isoformat()
    cutoff = dues[(len(dues) * 3) // 4] if dues else today
    print(f"  using --today {today} and --due-before {cutoff}")

    def listed(*filters):
        code, out, err = cli("list", "--json", "--today", today, *filters)
        return code, (sorted(i["id"] for i in json.loads(out)["items"]) if code == 0 else None)

    def expected(owner_id=None, status=None, before=None):
        where, params = [], []
        if owner_id is not None:
            where.append("owner_status='resolved' AND owner_id=?")
            params.append(owner_id)
        if status == "done":
            where.append("status='done'")
        elif status == "open":
            where.append("status='open'")
        elif status == "overdue":
            where.append("status='open' AND due_date IS NOT NULL AND due_date < ?")
            params.append(today)
        if before:
            where.append("due_date IS NOT NULL AND due_date < ?")
            params.append(before)
        q = "SELECT id FROM action_items" + (" WHERE " + " AND ".join(where) if where else "")
        return sorted(r[0] for r in sql(q, params))

    code, ids = listed()
    c.check(code == 0 and len(ids) == total, "list shows every item", f"{len(ids or [])}/{total}")
    combos = 0
    for status in (None, "open", "done", "overdue"):
        for before in (None, cutoff):
            f = (["--status", status] if status else []) + (["--due-before", before] if before else [])
            c.check(listed(*f)[1] == expected(None, status, before), "list " + " ".join(f))
            combos += 1
    people = sql("SELECT id, full_name FROM people ORDER BY full_name")
    first_counts = Counter(p["full_name"].split()[0].lower() for p in people)
    for p in people:
        for status in (None, "open", "overdue", "done"):
            f = ["--owner", p["full_name"]] + (["--status", status] if status else [])
            c.check(listed(*f)[1] == expected(p["id"], status), "list " + " ".join(f))
            combos += 1
        f = ["--owner-id", str(p["id"]), "--status", "open", "--due-before", cutoff]
        c.check(listed(*f)[1] == expected(p["id"], "open", cutoff), "list " + " ".join(f))
        combos += 1
    print(f"  {combos} filter combinations compared")
    overdue = set(expected(None, "overdue"))
    c.check(overdue <= set(expected(None, "open")), "every overdue item is also open")
    c.check(not overdue & set(expected(None, "done")), "no done item is overdue")
    code, _, err = cli("list", "--due-before", "31/12/2025")
    c.check(code == 2, "bad --due-before format rejected")

    c.section("owner ambiguity")
    shared = [n for n, k in first_counts.items() if k > 1]
    print(f"  first names shared by several people: {', '.join(shared) or 'none'}")
    for name in shared:
        code, out, err = cli("list", "--owner", name.capitalize())
        c.check(code == 2 and "more than one person" in err and out == "",
                f"list --owner {name} refused")
        code, out, err = cli("followup", name.capitalize(), "--no-input",
                             "--preview-dir", str(work / "amb"))
        c.check(code == 2 and not (work / "amb").exists(),
                f"followup {name} refused, nothing created")
    for r in sql("SELECT owner_candidates FROM action_items WHERE owner_status='ambiguous'"):
        c.check(len(json.loads(r[0])) >= 2, "ambiguous items list their candidates")
    c.check(sql("SELECT COUNT(*) FROM action_items WHERE owner_status<>'resolved'"
                " AND owner_id IS NOT NULL")[0][0] == 0, "unresolved owners never get a person")

    # ------------------------------------------------------------------ done
    c.section("done + persistence")
    open_ids = expected(None, "open")
    if open_ids:
        target = str(open_ids[0])
        code, out, _ = cli("done", target, "--today", today)
        c.check(code == 0 and out.startswith(f"Marked #{target} done"), "done marks an item")
        code, out, _ = cli("done", target)
        c.check(code == 0 and "already done" in out, "repeating done is safe")
        code, _, err = cli("done", "999999999")
        c.check(code == 1 and "no action item" in err, "invalid ID reported")
        code, _, err = cli("done", "not-an-id")
        c.check(code == 1, "non-numeric ID reported")
        cli("ingest", str(notes))
        cli("ingest", str(notes))
        status = sql("SELECT status, completed_by FROM action_items WHERE id=?", (target,))[0]
        c.check(tuple(status) == ("done", "cli"), "re-ingest never reopens a completed item")
        code, ids = listed("--status", "done")
        c.check(int(target) in ids, "completion visible from a new process")
        c.check(len(sql("SELECT id FROM action_items")) == total, "no items created by re-ingest")

    # ------------------------------------------------------------------ followup
    c.section("followup + Gmail safety")
    log = work / "gmail-requests.log"
    fake = {"ACTIONTRACK_GMAIL_MCP_COMMAND": f'"{sys.executable}" "{FAKE_SERVER}"',
            "FAKE_MCP_LOG": str(log)}
    drafts = previews = nothings = 0
    for p in people:
        has_open = bool(expected(p["id"], "open"))
        code, out, err = cli("followup", p["full_name"], "--today", today, "--no-input",
                             "--preview-dir", str(work / "previews"))
        if not has_open:
            c.check(code == 0 and "no open action items" in out, f"{p['full_name']}: no email")
            nothings += 1
            continue
        c.check(code == 0 and "No Gmail draft was created: Gmail MCP is not configured" in out,
                f"{p['full_name']}: preview when Gmail unconfigured", err[-200:])
        previews += 1
        address = sql("SELECT email FROM people WHERE id=?", (p["id"],))[0][0]
        extra = [] if address else ["--to", f"verify-{p['id']}@example.invalid"]
        code, out, err = cli("followup", p["full_name"], "--today", today, "--no-input",
                             "--preview-dir", str(work / "previews"), *extra, extra_env=fake)
        ok = c.check(code == 0 and "Gmail draft created" in out and "NOT been sent" in out,
                     f"{p['full_name']}: draft via fake Gmail MCP", (err or out)[-300:])
        drafts += ok
        body = out.split("-" * 60)[1] if out.count("-" * 60) >= 2 else ""
        for other in people:
            if other["id"] != p["id"] and other["full_name"] in body:
                c.check(False, "email mentions only its owner", other["full_name"])
        code, out, _ = cli("followup", p["full_name"], "--today", today, "--no-input", *extra,
                           extra_env=fake)
        c.check(code == 0 and "already created" in out, f"{p['full_name']}: no duplicate draft")
    requests = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    calls = [r["params"]["name"] for r in requests if r.get("method") == "tools/call"]
    methods = {r.get("method") for r in requests}
    print(f"  {previews} previews, {drafts} drafts, {nothings} people with nothing open")
    print(f"  fake Gmail received {len(requests)} requests; tools called: {Counter(calls)}")
    c.check(set(calls) <= {"create_draft"}, "ONLY create_draft was ever called", str(set(calls)))
    c.check(calls.count("create_draft") == drafts, "one create_draft per reported draft")
    c.check(methods <= {"initialize", "notifications/initialized", "tools/list", "tools/call"},
            "no other MCP methods used", str(methods))
    for eml in (work / "previews").glob("*.eml") if (work / "previews").exists() else []:
        text = eml.read_text(encoding="utf-8", errors="replace")
        c.check("X-Unsent: 1" in text, "preview marked unsent", eml.name)

    # ------------------------------------------------------------------ originals
    c.section("originals")
    c.check(hashes(source) == original, "supplied notes folder unchanged")

    print(f"\nRESULT: {c.passed} checks passed, {len(c.failed)} failed")
    for f in c.failed:
        print(f"  - {f}")
    if args.keep:
        print(f"Workspace kept at {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if not c.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
