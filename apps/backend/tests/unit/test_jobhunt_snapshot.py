"""The JobHunt snapshot adapter must stay read-only and tolerate bad rows."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.services.jobhunt_snapshot import (
    JobHuntSnapshotCatalog,
    JobHuntSnapshotUnavailable,
)


def _create_snapshot(
    path: Path, rows: list[tuple], descriptions: list[tuple] = ()
) -> None:
    connection = sqlite3.connect(path)
    try:
        # Deliberately omit unique constraints so duplicate-corruption behavior is testable.
        connection.executescript(
            """
            CREATE TABLE jobs (
                key TEXT, title TEXT, company TEXT, url TEXT, location TEXT,
                posted_date TEXT, first_seen TEXT, source TEXT
            );
            CREATE TABLE job_description_snapshots (
                id INTEGER PRIMARY KEY, job_key TEXT, status TEXT
            );
            """
        )
        connection.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.executemany(
            "INSERT INTO job_description_snapshots VALUES (?, ?, ?)", descriptions
        )
        connection.commit()
    finally:
        connection.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reads_bounded_job_summaries_and_preserves_source(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_snapshot(
        snapshot,
        [
            (
                "key-1",
                "ML Engineer",
                "Example",
                "https://jobs.test/1",
                "Dubai",
                "2026-10-01",
                "2026-10-04",
                "portal",
            ),
            (
                "key-2",
                "Python Developer",
                None,
                "https://jobs.test/2",
                None,
                None,
                "2026-10-03",
                None,
            ),
        ],
        [(1, "key-1", "manual")],
    )
    before = _digest(snapshot)

    result = JobHuntSnapshotCatalog(snapshot).list_jobs(page=1, page_size=1)

    assert result.total == 2
    assert result.pages == 2
    assert len(result.items) == 1
    assert result.items[0].job_key == "key-1"
    assert result.items[0].description_available is True
    assert result.items[0].description_status == "manual"
    assert not hasattr(result.items[0], "application")
    assert _digest(snapshot) == before


def test_missing_optional_values_and_description_are_not_invented(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_snapshot(
        snapshot,
        [
            (
                "key-1",
                "Python Developer",
                None,
                "https://jobs.test/1",
                None,
                "bad-date",
                None,
                None,
            )
        ],
    )

    item = JobHuntSnapshotCatalog(snapshot).list_jobs().items[0]

    assert item.company is None
    assert item.location is None
    assert item.posted_date is None
    assert item.first_seen is None
    assert item.description_status == "missing"
    assert item.description_available is False


def test_duplicate_keys_and_urls_are_skipped_without_crashing(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_snapshot(
        snapshot,
        [
            ("dup-key", "One", None, "https://jobs.test/1", None, None, None, None),
            ("dup-key", "Two", None, "https://jobs.test/2", None, None, None, None),
            (
                "key-3",
                "Three",
                None,
                "https://jobs.test/shared",
                None,
                None,
                None,
                None,
            ),
            ("key-4", "Four", None, "https://jobs.test/shared", None, None, None, None),
            ("key-5", "Valid", None, "https://jobs.test/5", None, None, None, None),
        ],
    )

    result = JobHuntSnapshotCatalog(snapshot).list_jobs()

    assert [job.job_key for job in result.items] == ["key-5"]
    assert result.total == 1
    assert JobHuntSnapshotCatalog(snapshot).list_jobs(query="One").total == 0
    assert JobHuntSnapshotCatalog(snapshot).list_jobs(query="Three").total == 0


def test_malformed_required_fields_are_skipped_and_optional_dates_are_null(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_snapshot(
        snapshot,
        [
            ("bad-title", "  ", None, "https://jobs.test/1", None, None, None, None),
            ("bad-url", "Title", None, "javascript:alert(1)", None, None, None, None),
            (
                "good",
                "Title",
                None,
                "https://jobs.test/good",
                None,
                "not-a-date",
                None,
                None,
            ),
        ],
    )

    result = JobHuntSnapshotCatalog(snapshot).list_jobs()

    assert result.total == 1
    assert result.items[0].job_key == "good"
    assert result.items[0].posted_date is None


def test_filters_are_parameterized_and_pagination_is_bounded(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_snapshot(
        snapshot,
        [
            (
                "key-1",
                "AI Engineer",
                "A",
                "https://jobs.test/1",
                "Dubai",
                None,
                None,
                None,
            ),
            (
                "key-2",
                "Python Developer",
                "B",
                "https://jobs.test/2",
                "Abu Dhabi",
                None,
                None,
                None,
            ),
            (
                "key-3",
                "AI Engineer",
                "C",
                "https://jobs.test/3",
                "Dubai",
                None,
                None,
                None,
            ),
        ],
    )

    page = JobHuntSnapshotCatalog(snapshot).list_jobs(
        query="AI", location="Dubai", page=2, page_size=1
    )
    assert page.total == 2
    assert [job.job_key for job in page.items] == ["key-3"]
    literal_wildcard = JobHuntSnapshotCatalog(snapshot).list_jobs(query="AI%")
    assert literal_wildcard.total == 0
    with pytest.raises(ValueError):
        JobHuntSnapshotCatalog(snapshot).list_jobs(page=0)
    with pytest.raises(ValueError):
        JobHuntSnapshotCatalog(snapshot).list_jobs(page_size=21)


def test_snapshot_connection_rejects_writes(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_snapshot(
        snapshot,
        [("key-1", "Title", None, "https://jobs.test/1", None, None, None, None)],
    )
    catalog = JobHuntSnapshotCatalog(snapshot)

    with catalog._connection() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE jobs SET title='changed'")


def test_search_filters_sorting_offsets_and_injection_are_safe(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_snapshot(
        snapshot,
        [
            (
                "a",
                "Zebra Engineer",
                "North",
                "https://jobs.test/a",
                "Dubai",
                "2026-10-01",
                "2026-10-04",
                "portal",
            ),
            (
                "b",
                "AI Engineer",
                "South",
                "https://jobs.test/b",
                "Abu Dhabi",
                "2026-10-03",
                "2026-10-03",
                "board",
            ),
            (
                "c",
                "AI Analyst",
                "West",
                "https://jobs.test/c",
                "Dubai",
                None,
                "2026-10-02",
                "portal",
            ),
        ],
        [(1, "a", "complete")],
    )
    catalog = JobHuntSnapshotCatalog(snapshot)
    assert catalog.list_jobs(query="dUbAi").total == 2
    assert catalog.list_jobs(source="PORTAL").total == 2
    assert catalog.list_jobs(posted_after="2026-10-02").total == 1
    assert catalog.list_jobs(posted_before="2026-10-02").total == 1
    assert catalog.list_jobs(description_available=True).total == 1
    assert catalog.list_jobs(description_available=False).total == 2
    ordered = catalog.list_jobs(sort="title", direction="asc", offset=1, page_size=1)
    assert ordered.items[0].title == "AI Engineer"
    assert ordered.offset == 1
    assert catalog.list_jobs(query="%' OR 1=1 --").total == 0
    with pytest.raises(ValueError):
        catalog.list_jobs(sort="title; DROP TABLE jobs")
    with pytest.raises(ValueError):
        catalog.list_jobs(posted_after="2026-99-99")


def test_missing_or_incompatible_snapshot_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(JobHuntSnapshotUnavailable):
        JobHuntSnapshotCatalog(tmp_path / "missing.sqlite").list_jobs()

    incompatible = tmp_path / "incompatible.sqlite"
    connection = sqlite3.connect(incompatible)
    connection.execute("CREATE TABLE jobs (id INTEGER)")
    connection.close()
    with pytest.raises(JobHuntSnapshotUnavailable):
        JobHuntSnapshotCatalog(incompatible).list_jobs()
