from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import build_database, get_connection

FIXTURE_GTFS_DIR = Path(__file__).parent / "fixtures" / "gtfs"


@pytest.fixture(scope="session")
def fixture_db_path(tmp_path_factory) -> Path:
    """Build the fixture SQLite DB once per test session."""
    db_path = tmp_path_factory.mktemp("gtfs") / "gtfs.db"
    build_database(FIXTURE_GTFS_DIR, db_path)
    return db_path


@pytest.fixture
def conn(fixture_db_path):
    connection = get_connection(fixture_db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(fixture_db_path, tmp_path, monkeypatch):
    """A TestClient wired to a data dir pre-populated with the fixture DB.

    Data dir already has gtfs.db present, so app startup's `refresh_if_missing`
    is a no-op and no network access happens during tests.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shutil.copy(fixture_db_path, data_dir / "gtfs.db")

    monkeypatch.setenv("DATA_DIR", str(data_dir))

    # app.config computes DATA_DIR/GTFS_DB_PATH from the environment at
    # import time, so reload it after setting the env var above. Other
    # modules do `from app import config` and read config.GTFS_DB_PATH as an
    # attribute lookup at call time, so they pick up the reloaded values
    # without needing to be reloaded themselves.
    import importlib

    from app import config

    importlib.reload(config)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
