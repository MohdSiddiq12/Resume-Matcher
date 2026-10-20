"""Rehearsal tests for the read-only JobHunt application import."""

import sqlite3

import pytest

from app.services.jobhunt_import import import_jobhunt_applications


@pytest.fixture
def jobhunt_source(tmp_path):
    source = tmp_path / "jobhunt.sqlite"
    connection = sqlite3.connect(source)
    connection.executescript(
        "CREATE TABLE jobs (key TEXT PRIMARY KEY, title TEXT, company TEXT, "
        "url TEXT, location TEXT, posted_date TEXT);"
        "CREATE TABLE applications (job_key TEXT PRIMARY KEY, status TEXT, "
        "notes TEXT, follow_up TEXT, updated_at TEXT);"
    )
    connection.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
        (
            "candidate-1",
            "ML Engineer",
            "Example Co",
            "https://example.test/jobs/1",
            "Dubai",
            "2026-10-18",
        ),
    )
    connection.execute(
        "INSERT INTO applications VALUES (?, ?, ?, ?, ?)",
        (
            "candidate-1",
            "investigating",
            "Recruiter noted",
            "2026-10-25",
            "2026-10-19T10:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    return source


async def test_import_is_idempotent_and_preserves_source_status(
    jobhunt_source, isolated_db
):
    first = await import_jobhunt_applications(isolated_db, jobhunt_source)
    second = await import_jobhunt_applications(isolated_db, jobhunt_source)

    assert first == {"imported": 1, "already_present": 0}
    assert second == {"imported": 0, "already_present": 1}
    cards = await isolated_db.list_applications()
    assert len(cards) == 1
    card = cards[0]
    assert card["status"] == "saved"
    assert card["legacy_status"] == "investigating"
    assert card["legacy_application_id"] == "candidate-1"
    assert card["follow_up_date"] == "2026-10-25"
    assert "Recruiter noted" in card["notes"]
    assert await isolated_db.list_jobhunt_discovery_states(["candidate-1"]) == {
        "candidate-1": "saved"
    }
    job = await isolated_db.get_job(card["job_id"])
    assert job["source_url"] == "https://example.test/jobs/1"
    assert job["source_system"] == "jobhunt"


async def test_import_maps_confirmed_historical_interview_and_keeps_original_status(
    jobhunt_source, isolated_db
):
    connection = sqlite3.connect(jobhunt_source)
    connection.execute(
        "UPDATE applications SET status='interview' WHERE job_key='candidate-1'"
    )
    connection.commit()
    connection.close()

    result = await import_jobhunt_applications(isolated_db, jobhunt_source)
    card = (await isolated_db.list_applications("interview"))[0]
    events = await isolated_db.list_application_events(card["application_id"])
    assert result["imported"] == 1
    assert card["legacy_status"] == "interview"
    assert card["applied_at"] == "2026-10-19T10:00:00Z"
    assert events[0]["confirmed"] is True
