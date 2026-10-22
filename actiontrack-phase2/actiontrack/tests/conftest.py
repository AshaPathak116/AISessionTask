import hashlib
import shutil
from pathlib import Path

import pytest

from actiontrack.storage import connect

FIXTURES = Path(__file__).parent / "fixtures" / "notes"


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def notes_dir(tmp_path: Path) -> Path:
    """A private copy of the fixture notes, so tests never touch the originals."""
    dest = tmp_path / "notes"
    shutil.copytree(FIXTURES, dest)
    return dest


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path: Path):
    c = connect(db_path)
    yield c
    c.close()


def write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
