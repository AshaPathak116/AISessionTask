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


# --------------------------------------------------------------------------- safety nets

import socket as _socket

_LOCAL = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Tests may only talk to localhost fakes -- never to Gmail or anything else."""
    real_create, real_connect = _socket.create_connection, _socket.socket.connect

    def guarded_create(address, *a, **kw):
        if address[0] not in _LOCAL:
            raise AssertionError(f"test tried to open a real network connection to {address}")
        return real_create(address, *a, **kw)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and address[0] not in _LOCAL:
            raise AssertionError(f"test tried to open a real network connection to {address}")
        return real_connect(self, address)

    monkeypatch.setattr(_socket, "create_connection", guarded_create)
    monkeypatch.setattr(_socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _clean_gmail_env(monkeypatch):
    for name in ("ACTIONTRACK_GMAIL_MCP_URL", "ACTIONTRACK_GMAIL_TOKEN",
                 "ACTIONTRACK_GMAIL_TOKEN_CMD", "ACTIONTRACK_GMAIL_MCP_COMMAND",
                 "ACTIONTRACK_GMAIL_DRAFT_TOOL", "ACTIONTRACK_SENDER_NAME", "ACTIONTRACK_TODAY",
                 "ACTIONTRACK_DB"):
        monkeypatch.delenv(name, raising=False)
