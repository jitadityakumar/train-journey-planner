"""Downloads, validates, and atomically installs the TravelWhiz GTFS feed.

Decisions (see PLAN.md/RESEARCH.md, 2026-08-01):
- Daily refresh at 04:00 (comfortable margin after TravelWhiz's nightly
  22:00-00:00 regeneration window), not weekly — short-notice
  engineering-work exceptions are exactly what a week-stale feed would miss.
- Runs as an in-process APScheduler job rather than host cron, so the whole
  stack is self-contained inside `docker compose up`.
- On startup, if no dataset is present yet (fresh volume / first run), fetch
  immediately rather than waiting for the next scheduled slot.
- Downloads and rebuilds into a temp path first, validates structure, then
  atomically swaps it into place — a bad or partial download never disturbs
  a known-good dataset already being served.

Note: `start_scheduler` runs one BackgroundScheduler per process. This is
fine under a single uvicorn worker (the only configuration this app runs
today — see Dockerfile), but running multiple workers would start multiple
independent schedulers racing to refresh the same files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config
from app.db import build_database

# TravelWhiz regenerates on UK time; REFRESH_HOUR=4 is meant as 04:00
# Europe/London regardless of the server/container's own timezone (the
# Docker image sets none, so it runs UTC — an unqualified cron trigger would
# silently drift to 05:00 local during BST).
LONDON_TZ = ZoneInfo("Europe/London")

logger = logging.getLogger("train_journey_planner.refresh")

# `agency` deliberately excluded: an empty/near-empty agency.txt only
# degrades DirectTrip.agency_name to None (LEFT JOIN, not an inner join),
# not a crash risk — and the checked-in fixture's agency.txt has just 1 row
# (real feeds have ~34), so a min-rows check here would need a fixture-only
# carve-out for no real safety benefit.
REQUIRED_TABLES_MIN_ROWS = {
    "stops": 100,
    "routes": 10,
    "trips": 100,
    "stop_times": 1000,
    "calendar": 10,
}


class FeedValidationError(RuntimeError):
    pass


def refresh_if_missing() -> None:
    _cleanup_stale_tmp_files()
    if config.GTFS_DB_PATH.exists():
        logger.info("GTFS dataset already present at %s — skipping startup fetch", config.GTFS_DB_PATH)
        return
    logger.warning("No GTFS dataset found at %s — fetching immediately instead of waiting for the next scheduled refresh", config.GTFS_DB_PATH)
    refresh_dataset()


def _cleanup_stale_tmp_files() -> None:
    """Removes any `.gtfs.db.*.tmp`/`.gtfs.zip.*.tmp`/`.gtfs.zip.sha256.*.tmp`
    left behind by a refresh that was interrupted mid-write (container kill,
    host reboot) before its final os.replace — these are otherwise never
    cleaned up, and accumulate indefinitely across repeated crash-restarts
    (found in Opus review, 2026-08-12; `gtfs.zip`'s ~83MB per leftover file
    is a meaningfully bigger leak than the pre-issue-#26 `gtfs.db` case
    alone). Only called once, at process startup — a refresh in progress
    concurrently with this call isn't a real scenario this app has (see this
    module's own docstring: one BackgroundScheduler per process, one
    refresh_if_missing() call at lifespan startup)."""
    if not config.DATA_DIR.exists():
        return
    for pattern in (".gtfs.db.*.tmp", ".gtfs.zip.*.tmp", ".gtfs.zip.sha256.*.tmp"):
        for stale in config.DATA_DIR.glob(pattern):
            logger.warning("Removing stale temp file from an interrupted refresh: %s", stale)
            stale.unlink(missing_ok=True)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        refresh_dataset,
        trigger=CronTrigger(hour=config.REFRESH_HOUR, minute=config.REFRESH_MINUTE, timezone=LONDON_TZ),
        id="daily_gtfs_refresh",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler


def refresh_dataset() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # The final DB is built directly inside DATA_DIR (not the system tempdir)
    # so the swap below is a same-filesystem os.replace — a true atomic
    # rename, not a copy-then-delete that could leave a half-written file if
    # interrupted. A unique name (not a fixed .gtfs.db.tmp) avoids two
    # refreshes racing on the same temp file — e.g. a slow cold-start fetch
    # from refresh_if_missing() still running when the 04:00 cron job fires.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=config.DATA_DIR, prefix=".gtfs.db.", suffix=".tmp")
    os.close(tmp_fd)
    tmp_db_path = Path(tmp_name)

    # Downloaded into DATA_DIR (not the system tempdir) so it's on the same
    # filesystem as GTFS_ZIP_PATH, keeping the final persist step below a
    # true atomic os.replace rather than a cross-filesystem copy.
    zip_fd, zip_name = tempfile.mkstemp(dir=config.DATA_DIR, prefix=".gtfs.zip.", suffix=".tmp")
    os.close(zip_fd)
    zip_path = Path(zip_name)

    with tempfile.TemporaryDirectory(prefix="gtfs-refresh-") as tmp:
        tmp_path = Path(tmp)
        extract_dir = tmp_path / "extracted"

        try:
            _download(config.GTFS_DOWNLOAD_URL, zip_path)
            _extract(zip_path, extract_dir)
            build_database(extract_dir, tmp_db_path)
            _validate(tmp_db_path)
        except Exception:
            logger.exception("GTFS refresh failed — keeping existing dataset (if any) unchanged")
            tmp_db_path.unlink(missing_ok=True)
            zip_path.unlink(missing_ok=True)
            return

        # The DB swap is the primary, already-validated deliverable — it must
        # go ahead regardless of what happens to the zip/checksum below.
        os.replace(tmp_db_path, config.GTFS_DB_PATH)
        logger.info("GTFS dataset refreshed successfully at %s", config.GTFS_DB_PATH)

        # Persisting the zip (for the OTP sidecar's pull-and-poll refresh,
        # GitHub issue #26) is secondary to the DB swap above — a failure
        # here must not look like the whole refresh failed, since the served
        # dataset already updated successfully. Still atomic within itself
        # (checksum written via the same mkstemp + os.replace pattern as the
        # zip/DB, not Path.write_text directly, so a mid-write interruption
        # can never leave a checksum file that doesn't match what's on disk)
        # and never leaves the checksum pointing at a zip that isn't there.
        try:
            checksum = _sha256(zip_path)
            # mkstemp creates files 0600 — unlike gtfs.db (which happens to
            # end up 0644 because build_database() unlinks and lets sqlite3
            # recreate it under the normal umask), the zip/checksum are
            # written directly into their mkstemp'd file and would otherwise
            # keep that restrictive mode forever, making them unreadable by
            # the non-root SSH user the OTP sidecar's poller connects as.
            os.chmod(zip_path, 0o644)
            os.replace(zip_path, config.GTFS_ZIP_PATH)
            checksum_fd, checksum_name = tempfile.mkstemp(
                dir=config.DATA_DIR, prefix=".gtfs.zip.sha256.", suffix=".tmp"
            )
            with os.fdopen(checksum_fd, "w") as f:
                f.write(checksum)
            os.chmod(checksum_name, 0o644)
            os.replace(checksum_name, config.GTFS_ZIP_CHECKSUM_PATH)
        except Exception:
            logger.exception(
                "Persisting gtfs.zip/checksum failed after a successful DB refresh — "
                "the OTP sidecar will keep serving its last successfully-pulled feed "
                "until this succeeds on a future refresh"
            )
            zip_path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)


def _extract(zip_path: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    resolved_extract_dir = extract_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            member_path = (extract_dir / member).resolve()
            if not member_path.is_relative_to(resolved_extract_dir):
                raise FeedValidationError(f"zip entry {member!r} escapes the extraction directory")
        zf.extractall(extract_dir)


def _validate(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for table, min_rows in REQUIRED_TABLES_MIN_ROWS.items():
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count < min_rows:
                raise FeedValidationError(
                    f"table {table!r} has only {count} rows, expected at least {min_rows}"
                )

        known_crs = conn.execute(
            "SELECT COUNT(*) FROM stops WHERE stop_code IN ('WAT','BNS','CLJ')"
        ).fetchone()[0]
        if known_crs < 3:
            raise FeedValidationError(
                "expected CRS codes (WAT/BNS/CLJ) not found in stops table — feed structure may have changed"
            )
    finally:
        conn.close()
