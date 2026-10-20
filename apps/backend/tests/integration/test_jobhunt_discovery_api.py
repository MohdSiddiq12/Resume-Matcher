"""API boundary tests for the read-only JobHunt discovery snapshot."""

import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import Database
from app.main import app
from app.models import JobHuntJobLink
from app.routers.resumes import _auto_create_tracker_application


@pytest.fixture
def snapshot_path(tmp_path: Path) -> Path:
    path = tmp_path / "jobhunt.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE jobs (
            key TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT, url TEXT UNIQUE,
            location TEXT, posted_date TEXT, first_seen TEXT, source TEXT
        );
        CREATE TABLE job_description_snapshots (
            id INTEGER PRIMARY KEY, job_key TEXT, status TEXT
        );
        INSERT INTO jobs VALUES
            ('key-1', 'AI Engineer', 'Example', 'https://jobs.test/1', 'Dubai',
             '2026-10-01', '2026-10-02', 'portal');
        """
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
async def client(snapshot_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBHUNT_SNAPSHOT_PATH", str(snapshot_path))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


async def test_list_and_detail_return_only_job_owned_fields(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/discovery/jobs?page_size=1")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["job_key"] == "key-1"
    assert body["items"][0]["description_status"] == "missing"
    assert "application" not in body["items"][0]
    assert "resume" not in body["items"][0]
    assert set(body["items"][0]) == {
        "job_key",
        "title",
        "company",
        "location",
        "source_url",
        "source",
        "posted_date",
        "first_seen",
        "description_status",
        "description_available",
        "pipeline_state",
    }

    detail = await client.get("/api/v1/discovery/jobs/key-1")
    assert detail.status_code == 200
    assert detail.json()["source_url"] == "https://jobs.test/1"
    assert set(detail.json()) == set(body["items"][0]) | {
        "description_text",
        "description_method",
        "description_retrieved_at",
    }
    assert detail.json()["description_text"] is None


async def test_empty_page_and_filters_are_supported(client: AsyncClient) -> None:
    empty = await client.get("/api/v1/discovery/jobs?q=missing")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0

    filtered = await client.get("/api/v1/discovery/jobs?location=Dubai")
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1


@pytest.mark.parametrize(
    "query", ["page=0", "page_size=21", "page=100001", f"q={'x' * 101}"]
)
async def test_invalid_pagination_and_filters_return_422(
    client: AsyncClient, query: str
) -> None:
    response = await client.get(f"/api/v1/discovery/jobs?{query}")
    assert response.status_code == 422


async def test_endpoint_has_no_write_methods(client: AsyncClient) -> None:
    response = await client.post("/api/v1/discovery/jobs", json={})
    assert response.status_code == 405


async def test_save_is_idempotent_and_skip_state_is_persisted(
    client: AsyncClient,
    snapshot_path: Path,
    isolated_backend_state: Database,
) -> None:
    saved_once = await client.post("/api/v1/discovery/jobs/key-1/save")
    saved_twice = await client.post("/api/v1/discovery/jobs/key-1/save")
    assert saved_once.status_code == saved_twice.status_code == 200
    assert saved_twice.json()["application_id"] == saved_once.json()["application_id"]
    application = await isolated_backend_state.get_application_by_legacy_id("key-1")
    assert application is not None
    assert application["status"] == "saved"
    assert application["applied_at"] is None

    connection = sqlite3.connect(snapshot_path)
    connection.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "key-2",
            "Analyst",
            "Example",
            "https://jobs.test/2",
            "Dubai",
            None,
            None,
            "portal",
        ),
    )
    connection.commit()
    connection.close()
    skipped = await client.post("/api/v1/discovery/jobs/key-2/skip")
    assert skipped.status_code == 200
    detail = await client.get("/api/v1/discovery/jobs/key-2")
    assert detail.json()["pipeline_state"] == "skipped"

    conflicting_skip = await client.post("/api/v1/discovery/jobs/key-1/skip")
    assert conflicting_skip.status_code == 409


async def test_tailoring_handoff_requires_full_text_and_deduplicates_by_version(
    client: AsyncClient,
    isolated_backend_state: Database,
) -> None:
    saved = await client.post("/api/v1/discovery/jobs/key-1/save")
    assert saved.status_code == 200
    saved_application_id = saved.json()["application_id"]

    path = "/api/v1/discovery/jobs/key-1/tailor-job"
    too_short = await client.post(path, json={"job_description": "short"})
    assert too_short.status_code == 422

    description = "Full verified job description for this role. " * 3
    first = await client.post(path, json={"job_description": description})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["created"] is True
    assert first_body["description_status"] == "manual"

    second = await client.post(path, json={"job_description": description})
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["job_id"] == first_body["job_id"]

    job = await isolated_backend_state.get_job(first_body["job_id"])
    assert job is not None
    assert job["content"] == description.strip()
    assert job["source_system"] == "jobhunt"
    assert job["source_job_key"] == "key-1"
    assert job["description_status"] == "manual"
    assert len(job["description_sha256"]) == 64
    detail = await client.get("/api/v1/discovery/jobs/key-1")
    assert detail.json()["description_text"] == description.strip()
    assert detail.json()["description_status"] == "manual"
    assert detail.json()["description_method"] == "user_pasted"
    assert detail.json()["description_retrieved_at"]

    await _auto_create_tracker_application(
        job_id=job["job_id"],
        tailored_resume_id="tailored-resume-id",
        master_resume_id="master-resume-id",
        job=job,
        title="AI Engineer",
    )
    applications = await isolated_backend_state.list_applications()
    job_application = next(
        row for row in applications if row["job_id"] == job["job_id"]
    )
    assert job_application["application_id"] == saved_application_id
    assert job_application["status"] == "saved"
    assert job_application["applied_at"] is None

    changed = await client.post(
        path,
        json={"job_description": description + "Additional verified detail."},
    )
    assert changed.status_code == 200
    assert changed.json()["created"] is True
    assert changed.json()["job_id"] != first_body["job_id"]

    async with isolated_backend_state._session() as session:
        links = await session.scalars(
            select(JobHuntJobLink).where(JobHuntJobLink.job_key == "key-1")
        )
        assert len(list(links)) == 2


async def test_sort_allowlist_and_injection_filters(client: AsyncClient) -> None:
    valid = await client.get("/api/v1/discovery/jobs?sort=title&direction=asc&offset=0")
    assert valid.status_code == 200
    assert valid.json()["offset"] == 0
    invalid = await client.get(
        "/api/v1/discovery/jobs?sort=title%3BDROP%20TABLE%20jobs"
    )
    assert invalid.status_code == 422
    injection = await client.get("/api/v1/discovery/jobs?q=%25%27%20OR%201%3D1%20--")
    assert injection.status_code == 200
    assert injection.json()["total"] == 0


async def test_missing_snapshot_returns_generic_error_without_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOBHUNT_SNAPSHOT_PATH", r"Z:\private\missing.sqlite")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/discovery/jobs")
    assert response.status_code == 503
    assert r"Z:\private" not in response.text
    assert "sqlite" not in response.text.lower()
