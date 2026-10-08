import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("SKILLOPS_MODE", "demo")
    monkeypatch.setenv("SKILLOPS_DEMO_DELAY", "0.001")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "test.db"))) as test_client:
        yield test_client
