"""Live discovery uses the public adapter and persists results locally."""

from datetime import datetime, timedelta, timezone

import respx
from httpx import ASGITransport, AsyncClient, Response

from app.database import Database
from app.main import app
from app.models import JobHuntSavedSearch
from app.routers.resumes import _auto_create_tracker_application
from app.services.jobhunt_live_search import (
    DEFAULT_SEARCH_PROFILE,
    FREEHIRE_SEARCH_URL,
    FREEHIRE_SIMILAR_URL,
)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _freehire_job(description: str) -> dict:
    return {
        "public_slug": "ai-engineer-example-123",
        "source": "greenhouse",
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "title": "AI Engineer",
        "company": "Example AI",
        "location": "Dubai, UAE",
        "countries": ["AE"],
        "cities": ["Dubai"],
        "work_mode": "onsite",
        "skills": ["python", "pytorch"],
        "posted_at": "2026-10-10T00:00:00Z",
        "description": description,
        "enrichment": {
            "employment_type": "full_time",
            "salary_min": 100000,
            "salary_currency": "AED",
        },
    }


def _payload(description: str) -> dict:
    return {"data": [_freehire_job(description)], "meta": {"total": 1}}


async def test_profile_live_search_persists_then_hands_full_jd_to_tailor(
    isolated_backend_state: Database,
) -> None:
    description = (
        "We are hiring an AI Engineer in Dubai. Responsibilities include building "
        "machine learning systems, deploying Python services, reviewing model "
        "quality, and partnering with product teams. Requirements include Python, "
        "PyTorch, SQL, and experience delivering production software. "
    ) * 3
    with respx.mock(assert_all_called=True) as router:
        route = router.get(FREEHIRE_SEARCH_URL).mock(
            return_value=Response(200, json=_payload(description))
        )
        async with _client() as client:
            response = await client.post(
                "/api/v1/discovery/jobs/live-search",
                json={
                    "query": "AI Engineer",
                    "include_remote": False,
                    "limit_per_query": 3,
                },
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "complete"
            assert body["found"] == 1
            assert body["new"] == 1
            assert route.call_count == 1
            assert route.calls[0].request.url.params["countries"] == "AE"
            assert route.calls[0].request.url.params["q"] == 'title:"AI Engineer"'
            assert route.calls[0].request.url.params["description_format"] == "text"

            job_key = "freehire:ai-engineer-example-123"
            detail = await client.get(f"/api/v1/discovery/jobs/live-jobs/{job_key}")
            assert detail.status_code == 200
            assert detail.json()["description_text"] == description.strip()
            assert (
                detail.json()["source_url"]
                == "https://boards.greenhouse.io/example/jobs/123"
            )
            assert detail.json()["description_status"] == "complete"

            saved = await client.post(
                f"/api/v1/discovery/jobs/live-jobs/{job_key}/save"
            )
            saved_again = await client.post(
                f"/api/v1/discovery/jobs/live-jobs/{job_key}/save"
            )
            assert saved.status_code == saved_again.status_code == 200
            assert (
                saved.json()["application_id"] == saved_again.json()["application_id"]
            )
            card = await isolated_backend_state.get_application_by_legacy_id(job_key)
            assert card is not None
            assert card["status"] == "saved"
            assert card["applied_at"] is None

            prepared = await client.post(
                f"/api/v1/discovery/jobs/live-jobs/{job_key}/tailor-job", json={}
            )
            assert prepared.status_code == 200, prepared.text
            assert prepared.json()["description_status"] == "complete"
            prepared_job = await isolated_backend_state.get_job(
                prepared.json()["job_id"]
            )
            assert prepared_job is not None
            assert prepared_job["content"] == description.strip()
            assert (
                prepared_job["source_url"]
                == "https://boards.greenhouse.io/example/jobs/123"
            )

            tailored_resume = await isolated_backend_state.create_resume(
                content="# Tailored AI Engineer resume"
            )
            await _auto_create_tracker_application(
                job_id=prepared_job["job_id"],
                tailored_resume_id=tailored_resume["resume_id"],
                master_resume_id=tailored_resume["resume_id"],
                job=prepared_job,
                title="AI Engineer",
            )
            linked_card = await isolated_backend_state.get_application_by_legacy_id(
                job_key
            )
            assert linked_card is not None
            assert linked_card["application_id"] == card["application_id"]
            assert linked_card["resume_id"] == tailored_resume["resume_id"]
            assert linked_card["status"] == "saved"
            assert linked_card["applied_at"] is None

            local_page = await client.get("/api/v1/discovery/jobs/live-jobs")
            assert local_page.status_code == 200
            assert local_page.json()["total"] == 1
            assert local_page.json()["items"][0]["pipeline_state"] == "saved"


async def test_repeat_search_updates_one_row_and_preserves_complete_description(
    isolated_backend_state: Database,
) -> None:
    complete = "A complete job description. " * 20
    short = "Short excerpt."
    with respx.mock(assert_all_called=True) as router:
        route = router.get(FREEHIRE_SEARCH_URL)
        route.side_effect = [
            Response(200, json=_payload(complete)),
            Response(200, json=_payload(short)),
        ]
        async with _client() as client:
            first = await client.post(
                "/api/v1/discovery/jobs/live-search",
                json={"query": "AI Engineer", "include_remote": False},
            )
            second = await client.post(
                "/api/v1/discovery/jobs/live-search",
                json={"query": "AI Engineer", "include_remote": False},
            )
    assert first.status_code == second.status_code == 200
    assert first.json()["new"] == 1
    assert second.json()["new"] == 0
    stored = await isolated_backend_state.get_jobhunt_live_job(
        "freehire:ai-engineer-example-123"
    )
    assert stored is not None
    assert stored["description_status"] == "complete"
    assert stored["description_text"] == complete.strip()


async def test_live_search_uses_saved_profile_filters_when_request_omits_them(
    isolated_backend_state: Database,
) -> None:
    profile = {
        **DEFAULT_SEARCH_PROFILE,
        "country": "GB",
        "city": "London",
        "jobage": 7,
        "include_remote": False,
    }
    await isolated_backend_state.save_jobhunt_search_profile(profile)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(FREEHIRE_SEARCH_URL).mock(
            return_value=Response(200, json=_payload("A full posting. " * 20))
        )
        async with _client() as client:
            response = await client.post(
                "/api/v1/discovery/jobs/live-search",
                json={"query": "AI Engineer", "limit_per_query": 1},
            )

    assert response.status_code == 200, response.text
    params = route.calls[0].request.url.params
    assert params["countries"] == "GB"
    assert params["posted_within_days"] == "7"
    assert "work_mode" not in params


async def test_profile_search_uses_skill_and_semantic_strategies_with_local_filters(
    isolated_backend_state: Database,
) -> None:
    profile = {
        **DEFAULT_SEARCH_PROFILE,
        "target_titles": ["AI Engineer"],
        "skills": ["rag", "llm", "embeddings", "python", "sql"],
    }
    await isolated_backend_state.save_jobhunt_search_profile(profile)
    description = "A full AI engineering job description. " * 12
    recent = datetime.now(timezone.utc).date().isoformat()
    similar_jobs = {
        "data": [
            {
                "public_slug": "similar-llm-engineer-123",
                "source": "greenhouse",
                "url": "https://boards.greenhouse.io/example/jobs/456",
                "title": "LLM Engineer",
                "company": "Example AI",
                "location": "Abu Dhabi, UAE",
                "countries": ["ae"],
                "cities": ["Abu Dhabi"],
                "work_mode": "onsite",
                "skills": ["rag", "llm"],
                "posted_at": f"{recent}T00:00:00Z",
                "description": description,
            },
            {
                "public_slug": "old-us-engineer-456",
                "source": "workday",
                "url": "https://jobs.example.test/old",
                "title": "AI Engineer",
                "company": "Old Example",
                "location": "United States",
                "countries": ["us"],
                "work_mode": "onsite",
                "posted_at": "2026-01-01T00:00:00Z",
                "description": description,
            },
        ]
    }
    with respx.mock(assert_all_called=True) as router:
        search_route = router.get(FREEHIRE_SEARCH_URL).mock(
            return_value=Response(200, json=_payload(description))
        )
        similar_route = router.get(
            f"{FREEHIRE_SIMILAR_URL}/ai-engineer-example-123/similar"
        ).mock(return_value=Response(200, json=similar_jobs))
        async with _client() as client:
            response = await client.post(
                "/api/v1/discovery/jobs/live-search",
                json={"include_remote": False, "limit_per_query": 1},
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["found"] == 2
    assert len(body["query_groups"]) == 2
    recorded_runs = await isolated_backend_state.list_jobhunt_search_runs()
    assert len(recorded_runs[0]["queries"]) == 2
    assert search_route.call_count == 2
    skill_request = next(
        call.request
        for call in search_route.calls
        if call.request.url.params.get("q_fields") == "title,description"
    )
    assert '"rag"' in skill_request.url.params["q"]
    assert similar_route.call_count == 1
    assert {item["job_key"] for item in body["items"]} == {
        "freehire:ai-engineer-example-123",
        "freehire:similar-llm-engineer-123",
    }
    similar = await isolated_backend_state.get_jobhunt_live_job(
        "freehire:similar-llm-engineer-123"
    )
    assert similar is not None
    assert "semantic_similarity" in similar["metadata"]["search_methods"]
    assert (
        await isolated_backend_state.get_jobhunt_live_job(
            "freehire:old-us-engineer-456"
        )
        is None
    )


async def test_saved_search_runs_locally_and_scheduler_claims_due_rows_once(
    isolated_backend_state: Database,
) -> None:
    async with _client() as client:
        created = await client.post(
            "/api/v1/discovery/jobs/saved-searches",
            json={
                "name": "AI roles",
                "query": "AI Engineer",
                "include_remote": False,
                "limit_per_query": 1,
                "interval_minutes": 60,
            },
        )
        assert created.status_code == 201, created.text
        saved = created.json()
        assert saved["enabled"] is False
        assert saved["next_run_at"] is None
        assert (await client.get("/api/v1/discovery/jobs/saved-searches")).json()[
            "total"
        ] == 1

        with respx.mock(assert_all_called=True) as router:
            router.get(FREEHIRE_SEARCH_URL).mock(
                return_value=Response(200, json=_payload("Full description. " * 20))
            )
            result = await client.post(
                f"/api/v1/discovery/jobs/saved-searches/{saved['search_id']}/run"
            )
        assert result.status_code == 200, result.text

        enabled = await client.patch(
            f"/api/v1/discovery/jobs/saved-searches/{saved['search_id']}",
            json={"enabled": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["next_run_at"] is not None

        async with isolated_backend_state._session() as session:
            row = await session.get(JobHuntSavedSearch, saved["search_id"])
            assert row is not None
            assert row.last_run_status == "complete"
            assert row.last_run_id == result.json()["run_id"]
            row.next_run_at = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat()
            await session.commit()

        claimed = await isolated_backend_state.claim_due_jobhunt_saved_searches()
        claimed_again = await isolated_backend_state.claim_due_jobhunt_saved_searches()
        assert [item["search_id"] for item in claimed] == [saved["search_id"]]
        assert claimed_again == []

        paused = await client.patch(
            f"/api/v1/discovery/jobs/saved-searches/{saved['search_id']}",
            json={"enabled": False},
        )
        assert paused.status_code == 200
        assert paused.json()["next_run_at"] is None
        deleted = await client.delete(
            f"/api/v1/discovery/jobs/saved-searches/{saved['search_id']}"
        )
        assert deleted.status_code == 200
