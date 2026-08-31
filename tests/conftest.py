"""Shared fixtures: fresh DB + TestClient for each test."""
import os
import tempfile

import pytest

_tmpdir = tempfile.mkdtemp(prefix="fl_test_")
os.environ["DATA_DIR"] = _tmpdir

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_db():
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def alice(client):
    r = client.post("/api/auth/register", json={
        "email": "alice@test.com", "name": "Alice", "password": "pass123",
    })
    assert r.status_code == 200
    return r.json()


@pytest.fixture()
def bob(client):
    r = client.post("/api/auth/register", json={
        "email": "bob@test.com", "name": "Bob", "password": "pass456",
    })
    assert r.status_code == 200
    return r.json()


@pytest.fixture()
def project(client, alice):
    # Ensure alice is the active session (bob fixture may have overwritten the cookie).
    client.post("/api/auth/login", json={"email": "alice@test.com", "password": "pass123"})
    r = client.post("/api/projects", json={"name": "Demo", "classes": ["car", "person"]})
    assert r.status_code == 200
    return r.json()
