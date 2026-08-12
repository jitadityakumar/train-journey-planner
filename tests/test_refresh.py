"""Refresh-job tests: startup fetch when the dataset is missing, and that a
failed/corrupt refresh never clobbers a known-good dataset already in place.
Network access is mocked throughout — `_download` is patched to copy the
checked-in fixture feed instead of hitting TravelWhiz.
"""

from __future__ import annotations

import importlib
import shutil
import zipfile
from pathlib import Path

import pytest

FIXTURE_GTFS_DIR = Path(__file__).parent / "fixtures" / "gtfs"


@pytest.fixture
def fixture_zip(tmp_path) -> Path:
    zip_path = tmp_path / "feed.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in FIXTURE_GTFS_DIR.glob("*.txt"):
            zf.write(f, arcname=f.name)
    return zip_path


@pytest.fixture
def refresh_env(tmp_path, monkeypatch, fixture_zip):
    """Reloads app.config/app.refresh pointed at an isolated data dir, with
    the network download replaced by a copy of the fixture zip."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    from app import config

    importlib.reload(config)

    from app import refresh

    importlib.reload(refresh)

    def fake_download(url, dest):
        shutil.copy(fixture_zip, dest)

    monkeypatch.setattr(refresh, "_download", fake_download)
    return refresh, config


def test_refresh_if_missing_fetches_when_dataset_absent(refresh_env):
    refresh, config = refresh_env
    assert not config.GTFS_DB_PATH.exists()

    refresh.refresh_if_missing()

    assert config.GTFS_DB_PATH.exists()
    assert config.GTFS_DB_PATH.stat().st_size > 0


def test_refresh_if_missing_is_noop_when_dataset_present(refresh_env, monkeypatch):
    refresh, config = refresh_env
    refresh.refresh_if_missing()
    original_mtime = config.GTFS_DB_PATH.stat().st_mtime

    def fail_if_called(*args, **kwargs):
        raise AssertionError("refresh_dataset should not run when dataset already exists")

    monkeypatch.setattr(refresh, "refresh_dataset", fail_if_called)
    refresh.refresh_if_missing()

    assert config.GTFS_DB_PATH.stat().st_mtime == original_mtime


def test_failed_refresh_does_not_clobber_existing_dataset(refresh_env, monkeypatch):
    refresh, config = refresh_env
    refresh.refresh_if_missing()
    original_bytes = config.GTFS_DB_PATH.read_bytes()

    def broken_download(url, dest):
        dest.write_bytes(b"not a real zip file")

    monkeypatch.setattr(refresh, "_download", broken_download)
    refresh.refresh_dataset()

    assert config.GTFS_DB_PATH.read_bytes() == original_bytes
    assert list(config.DATA_DIR.glob(".gtfs.db.*.tmp")) == []


def test_refresh_validates_before_swapping_in(refresh_env, monkeypatch):
    refresh, config = refresh_env

    def fail_validate(db_path):
        raise refresh.FeedValidationError("simulated validation failure")

    monkeypatch.setattr(refresh, "_validate", fail_validate)
    refresh.refresh_dataset()

    assert not config.GTFS_DB_PATH.exists()
    assert list(config.DATA_DIR.glob(".gtfs.db.*.tmp")) == []


def test_successful_refresh_persists_zip_and_checksum(refresh_env, fixture_zip):
    refresh, config = refresh_env
    refresh.refresh_if_missing()

    assert config.GTFS_ZIP_PATH.exists()
    assert config.GTFS_ZIP_PATH.read_bytes() == fixture_zip.read_bytes()

    checksum = config.GTFS_ZIP_CHECKSUM_PATH.read_text().strip()
    assert checksum == refresh._sha256(config.GTFS_ZIP_PATH)
    assert list(config.DATA_DIR.glob(".gtfs.zip.*.tmp")) == []


def test_failed_refresh_does_not_persist_zip(refresh_env, monkeypatch):
    refresh, config = refresh_env

    def broken_download(url, dest):
        dest.write_bytes(b"not a real zip file")

    monkeypatch.setattr(refresh, "_download", broken_download)
    refresh.refresh_dataset()

    assert not config.GTFS_ZIP_PATH.exists()
    assert not config.GTFS_ZIP_CHECKSUM_PATH.exists()
    assert list(config.DATA_DIR.glob(".gtfs.zip.*.tmp")) == []


def test_refresh_if_missing_cleans_up_stale_tmp_files_from_a_crashed_run(refresh_env):
    """A crash mid-refresh (container kill, host reboot) can leave a
    `.gtfs.db.*.tmp`/`.gtfs.zip.*.tmp`/`.gtfs.zip.sha256.*.tmp` behind —
    these must be swept on the next startup, not accumulate indefinitely
    (Opus review, 2026-08-12)."""
    refresh, config = refresh_env
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    stale_db = config.DATA_DIR / ".gtfs.db.abc123.tmp"
    stale_zip = config.DATA_DIR / ".gtfs.zip.abc123.tmp"
    stale_checksum = config.DATA_DIR / ".gtfs.zip.sha256.abc123.tmp"
    for f in (stale_db, stale_zip, stale_checksum):
        f.write_bytes(b"leftover")

    refresh.refresh_if_missing()

    assert not stale_db.exists()
    assert not stale_zip.exists()
    assert not stale_checksum.exists()
