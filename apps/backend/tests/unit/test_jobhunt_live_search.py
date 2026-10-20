"""Profile query construction and safe live-job normalization."""

from app.services.jobhunt_live_search import (
    DEFAULT_SEARCH_PROFILE,
    _within_similarity_filters,
    build_query_groups,
    build_skill_query_groups,
    normalize_job,
)


def test_profile_search_expands_titles_into_supported_bounded_groups() -> None:
    groups = build_query_groups(DEFAULT_SEARCH_PROFILE)
    assert len(groups) > 1
    assert all(group.count('title:"') <= 5 for group in groups)
    assert any(" OR " in group for group in groups)
    assert any("Natural Language Processing Engineer" in group for group in groups)


def test_profile_skill_queries_skip_generic_terms_and_stay_bounded() -> None:
    groups = build_skill_query_groups(DEFAULT_SEARCH_PROFILE)
    assert 1 <= len(groups) <= 2
    assert all(group.count('"') <= 10 for group in groups)
    assert any('"rag"' in group or '"llm"' in group for group in groups)
    assert all('"python"' not in group and '"sql"' not in group for group in groups)


def test_semantic_results_must_match_age_and_location_filters() -> None:
    from datetime import date

    today = date(2026, 10, 20)
    recent_uae = {
        "posted_at": "2026-10-12T00:00:00Z",
        "countries": ["ae"],
        "work_mode": None,
    }
    old_uae = {**recent_uae, "posted_at": "2026-09-01T00:00:00Z"}
    recent_us = {**recent_uae, "countries": ["us"]}
    remote_us = {**recent_us, "work_mode": "fully_remote"}

    assert _within_similarity_filters(
        recent_uae,
        country="AE",
        jobage=14,
        include_remote=False,
        today=today,
    )
    assert not _within_similarity_filters(
        old_uae,
        country="AE",
        jobage=14,
        include_remote=True,
        today=today,
    )
    assert not _within_similarity_filters(
        recent_us,
        country="AE",
        jobage=14,
        include_remote=False,
        today=today,
    )
    assert _within_similarity_filters(
        remote_us,
        country="AE",
        jobage=14,
        include_remote=True,
        today=today,
    )


def test_normalize_job_keeps_source_provenance_and_scores_locally() -> None:
    description = "A role description with requirements and responsibilities. " * 5
    job = normalize_job(
        {
            "public_slug": "ai-engineer-example-123",
            "title": "AI Engineer",
            "company": "Example",
            "url": "https://jobs.example.test/roles/123",
            "source": "greenhouse",
            "location": "Dubai, UAE",
            "countries": ["AE"],
            "cities": ["Dubai"],
            "work_mode": "onsite",
            "posted_at": "2026-10-10T00:00:00Z",
            "description": description,
            "skills": ["python", "pytorch"],
            "enrichment": {
                "employment_type": "full_time",
                "salary_min": 100000,
                "salary_currency": "AED",
            },
        },
        DEFAULT_SEARCH_PROFILE,
        now="2026-10-20T00:00:00+00:00",
    )
    assert job is not None
    assert job["job_key"] == "freehire:ai-engineer-example-123"
    assert job["source_url"] == "https://jobs.example.test/roles/123"
    assert job["description_status"] == "complete"
    assert job["fit_score"] == 96
    assert job["metadata"]["employment_type"] == "full_time"
    assert job["salary"]["salary_currency"] == "AED"


def test_normalize_job_rejects_unsafe_or_unidentified_listings() -> None:
    profile = DEFAULT_SEARCH_PROFILE
    assert (
        normalize_job(
            {"public_slug": "x", "title": "Bad", "url": "javascript:alert(1)"}, profile
        )
        is None
    )
    assert (
        normalize_job(
            {"title": "No stable source ID", "url": "https://jobs.example.test/1"},
            profile,
        )
        is None
    )
