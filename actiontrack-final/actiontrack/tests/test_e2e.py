"""Runs scripts/verify_e2e.py (the real CLI, in subprocesses) on both note folders."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["sample_notes", "tests/fixtures/notes"])
def test_end_to_end_verification(folder):
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_e2e.py"),
                           str(ROOT / folder)], capture_output=True, text=True,
                          encoding="utf-8", timeout=600)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "0 failed" in done.stdout


def test_sample_folder_has_at_least_30_notes():
    notes = [p for p in (ROOT / "sample_notes").rglob("*.md") if "__MACOSX" not in p.parts]
    assert len(notes) >= 30
