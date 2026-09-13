import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["COMPLIANCE_AUTOSEED"] = "0"

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["COMPLIANCE_DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed  # noqa: E402
from app.database import SessionLocal  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _setup():
    init_db()
    db = SessionLocal()
    seed(db)
    db.close()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def login(client, username, password="compliance123"):
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code in (302, 303), r.text[:200]
    return client
