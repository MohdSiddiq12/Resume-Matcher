"""JobHunt snapshot browsing and explicit tailoring handoff."""

import hashlib
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query

from app.ai_limits import MAX_JOB_CHARACTERS, require_source_size
from app.database import DatabaseBusyError, db
from app.schemas.jobhunt_discovery import (
    JobHuntJobDetail,
    JobHuntJobPage,
    JobHuntLiveJobDetail,
    JobHuntLiveJobPage,
    JobHuntLiveJobSummary,
    JobHuntLiveSearchRequest,
    JobHuntLiveSearchResponse,
    JobHuntSavedSearchCreate,
    JobHuntSavedSearchList,
    JobHuntSavedSearchResponse,
    JobHuntSavedSearchUpdate,
    JobHuntSaveResponse,
    JobHuntSearchProfile,
    JobHuntSkipResponse,
    JobHuntTailorJobRequest,
    JobHuntTailorJobResponse,
)
from app.services.jobhunt_live_search import (
    DEFAULT_SEARCH_PROFILE,
    LiveSearchUnavailable,
    build_query_groups,
    build_skill_query_groups,
    search_profile,
)
from app.services.jobhunt_scheduler import run_saved_search
from app.services.jobhunt_snapshot import (
    JobHuntSnapshotCatalog,
    JobHuntSnapshotTooLarge,
    JobHuntSnapshotUnavailable,
)

router = APIRouter(prefix="/discovery/jobs", tags=["JobHunt Discovery"])
logger = logging.getLogger(__name__)


def _catalog() -> JobHuntSnapshotCatalog:
    return JobHuntSnapshotCatalog()


def _live_summary(row: dict, pipeline_state: str = "new") -> JobHuntLiveJobSummary:
    metadata = row.get("metadata") or {}
    return JobHuntLiveJobSummary(
        job_key=row["job_key"],
        canonical_id=row["canonical_id"],
        provider=row["provider"],
        source=row.get("source"),
        title=row["title"],
        company=row.get("company"),
        location=row.get("location"),
        source_url=row["source_url"],
        posted_date=row.get("posted_date"),
        description_status=row["description_status"],
        description_available=row["description_status"] in {"complete", "partial"},
        fit_score=row.get("fit_score"),
        fit_evidence=row.get("fit_evidence"),
        salary=row.get("salary"),
        work_mode=metadata.get("work_mode"),
        employment_type=metadata.get("employment_type"),
        retrieved_at=row["retrieved_at"],
        last_checked_at=row["last_checked_at"],
        pipeline_state=pipeline_state,
    )


@router.get("", response_model=JobHuntJobPage)
async def list_discovered_jobs(
    page: int = Query(default=1, ge=1, le=501),
    page_size: int = Query(default=20, ge=1, le=20),
    q: str | None = Query(default=None, max_length=100),
    location: str | None = Query(default=None, max_length=100),
    source: str | None = Query(default=None, max_length=100),
    description_available: bool | None = None,
    posted_after: str | None = Query(default=None, max_length=10),
    posted_before: str | None = Query(default=None, max_length=10),
    sort: str = Query(
        default="first_seen",
        pattern="^(first_seen|posted_date|title|company|location|source)$",
    ),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
    offset: int | None = Query(default=None, ge=0, le=10_000),
) -> JobHuntJobPage:
    try:
        page_result = _catalog().list_jobs(
            page=page,
            page_size=page_size,
            query=q.strip() if q else None,
            location=location.strip() if location else None,
            source=source.strip() if source else None,
            description_available=description_available,
            posted_after=posted_after,
            posted_before=posted_before,
            sort=sort,
            direction=direction,
            offset=offset,
        )
        states = await db.list_jobhunt_discovery_states(
            [item.job_key for item in page_result.items]
        )
        return page_result.model_copy(
            update={
                "items": [
                    item.model_copy(
                        update={"pipeline_state": states.get(item.job_key, "new")}
                    )
                    for item in page_result.items
                ]
            }
        )
    except JobHuntSnapshotTooLarge:
        raise HTTPException(
            status_code=503, detail="Job discovery is temporarily unavailable."
        ) from None
    except JobHuntSnapshotUnavailable:
        raise HTTPException(
            status_code=503, detail="Job discovery is temporarily unavailable."
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Invalid job discovery filters."
        ) from None


@router.get("/search-profile", response_model=JobHuntSearchProfile)
async def get_jobhunt_search_profile() -> JobHuntSearchProfile:
    profile = await db.get_jobhunt_search_profile()
    if profile is None:
        profile = DEFAULT_SEARCH_PROFILE
        await db.save_jobhunt_search_profile(profile)
    return JobHuntSearchProfile(**profile)


@router.put("/search-profile", response_model=JobHuntSearchProfile)
async def update_jobhunt_search_profile(
    profile: JobHuntSearchProfile,
) -> JobHuntSearchProfile:
    saved = await db.save_jobhunt_search_profile(profile.model_dump())
    return JobHuntSearchProfile(**saved)


@router.get("/saved-searches", response_model=JobHuntSavedSearchList)
async def list_saved_searches() -> JobHuntSavedSearchList:
    items = await db.list_jobhunt_saved_searches()
    return JobHuntSavedSearchList(items=items, total=len(items))


@router.post(
    "/saved-searches",
    response_model=JobHuntSavedSearchResponse,
    status_code=201,
)
async def create_saved_search(
    request: JobHuntSavedSearchCreate,
) -> JobHuntSavedSearchResponse:
    data = request.model_dump()
    filter_keys = {
        "country",
        "city",
        "jobage",
        "include_remote",
        "limit_per_query",
    }
    filters = {key: data[key] for key in filter_keys if data.get(key) is not None}
    try:
        saved = await db.create_jobhunt_saved_search(
            name=request.name,
            query=request.query,
            filters=filters,
            enabled=request.enabled,
            interval_minutes=request.interval_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobHuntSavedSearchResponse(**saved)


@router.get("/saved-searches/{search_id}", response_model=JobHuntSavedSearchResponse)
async def get_saved_search(search_id: str) -> JobHuntSavedSearchResponse:
    saved = await db.get_jobhunt_saved_search(search_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved search not found.")
    return JobHuntSavedSearchResponse(**saved)


@router.patch("/saved-searches/{search_id}", response_model=JobHuntSavedSearchResponse)
async def update_saved_search(
    search_id: str, request: JobHuntSavedSearchUpdate
) -> JobHuntSavedSearchResponse:
    current = await db.get_jobhunt_saved_search(search_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Saved search not found.")
    changes = request.model_dump(exclude_unset=True)
    for key in ("name", "enabled", "interval_minutes"):
        if changes.get(key) is None:
            changes.pop(key, None)
    filter_keys = {
        "country",
        "city",
        "jobage",
        "include_remote",
        "limit_per_query",
    }
    if any(key in changes for key in filter_keys):
        filters = dict(current["filters"])
        for key in filter_keys & changes.keys():
            value = changes.pop(key)
            if value is None:
                filters.pop(key, None)
            else:
                filters[key] = value
        changes["filters"] = filters
    saved = await db.update_jobhunt_saved_search(search_id, changes)
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved search not found.")
    return JobHuntSavedSearchResponse(**saved)


@router.delete("/saved-searches/{search_id}")
async def delete_saved_search(search_id: str) -> dict[str, bool]:
    if not await db.delete_jobhunt_saved_search(search_id):
        raise HTTPException(status_code=404, detail="Saved search not found.")
    return {"deleted": True}


@router.post(
    "/saved-searches/{search_id}/run",
    response_model=JobHuntLiveSearchResponse,
)
async def run_saved_search_now(search_id: str) -> JobHuntLiveSearchResponse:
    try:
        return await run_saved_search(search_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Saved search not found.") from exc


@router.post("/live-search", response_model=JobHuntLiveSearchResponse)
async def search_live_jobs(
    request: JobHuntLiveSearchRequest,
) -> JobHuntLiveSearchResponse:
    profile = await db.get_jobhunt_search_profile()
    if profile is None:
        profile = DEFAULT_SEARCH_PROFILE
        await db.save_jobhunt_search_profile(profile)
    country = (request.country or str(profile.get("country") or "AE")).upper()
    city = (request.city or str(profile.get("city") or "dubai")).strip()
    jobage = (
        request.jobage if request.jobage is not None else int(profile.get("jobage", 14))
    )
    include_remote = (
        request.include_remote
        if request.include_remote is not None
        else bool(profile.get("include_remote", True))
    )
    profile = {**profile, "city": city}
    query_groups = build_query_groups(profile, request.query)
    if request.query is None:
        query_groups.extend(build_skill_query_groups(profile))
    run_id = str(uuid4())
    filters = {
        "country": country,
        "city": city,
        "jobage": jobage,
        "include_remote": include_remote,
        "limit_per_query": request.limit_per_query,
    }
    try:
        await db.create_jobhunt_search_run(
            run_id=run_id, queries=query_groups, filters=filters
        )
        result = await search_profile(
            profile=profile,
            query=request.query,
            country=country,
            jobage=jobage,
            include_remote=include_remote,
            limit_per_query=request.limit_per_query,
        )
        saved, new_count = await db.save_jobhunt_live_jobs(result["jobs"])
        states = await db.list_jobhunt_discovery_states(
            [item["job_key"] for item in saved]
        )
        status = "partial" if result["errors"] else "complete"
        await db.finish_jobhunt_search_run(
            run_id=run_id,
            status=status,
            found_count=result["found"],
            new_count=new_count,
            errors=result["errors"],
        )
        return JobHuntLiveSearchResponse(
            run_id=run_id,
            status=status,
            query_groups=result["query_groups"],
            found=result["found"],
            new=new_count,
            items=[
                _live_summary(item, states.get(item["job_key"], "new"))
                for item in saved
            ],
            errors=result["errors"],
        )
    except LiveSearchUnavailable as exc:
        await db.finish_jobhunt_search_run(
            run_id=run_id,
            status="failed",
            found_count=0,
            new_count=0,
            errors=[str(exc)],
        )
        raise HTTPException(
            status_code=502,
            detail="The external job source is temporarily unavailable. Stored results are unchanged.",
        ) from exc
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Live JobHunt search failed")
        raise HTTPException(
            status_code=500,
            detail="Live search failed. Previously stored jobs are unchanged.",
        ) from exc


@router.get("/live-jobs", response_model=JobHuntLiveJobPage)
async def list_live_jobs(
    page: int = Query(default=1, ge=1, le=500),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=100),
    location: str | None = Query(default=None, max_length=100),
    min_score: int | None = Query(default=None, ge=0, le=100),
) -> JobHuntLiveJobPage:
    items, total = await db.list_jobhunt_live_jobs(
        query=q,
        location=location,
        min_score=min_score,
        page=page,
        page_size=page_size,
    )
    states = await db.list_jobhunt_discovery_states([item["job_key"] for item in items])
    return JobHuntLiveJobPage(
        items=[
            _live_summary(item, states.get(item["job_key"], "new")) for item in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


@router.get("/live-jobs/{job_key}", response_model=JobHuntLiveJobDetail)
async def get_live_job(job_key: str) -> JobHuntLiveJobDetail:
    if not job_key.startswith("freehire:") or len(job_key) > 256:
        raise HTTPException(status_code=404, detail="Job not found.")
    row = await db.get_jobhunt_live_job(job_key)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    state = (await db.list_jobhunt_discovery_states([job_key])).get(job_key, "new")
    summary = _live_summary(row, state)
    description_text = row.get("description_text")
    description_status = row["description_status"]
    description_method = row["description_method"]
    description_sha256 = row.get("description_sha256")
    if description_status != "complete":
        prepared = await db.get_jobhunt_prepared_description(job_key)
        if prepared and prepared.get("description_status") in {"complete", "manual"}:
            description_text = prepared.get("description_text")
            description_status = prepared["description_status"]
            description_method = prepared.get("description_method") or "user_pasted"
            description_sha256 = hashlib.sha256(
                (description_text or "").encode("utf-8")
            ).hexdigest()
            summary = summary.model_copy(
                update={
                    "description_status": description_status,
                    "description_available": bool(description_text),
                }
            )
    return JobHuntLiveJobDetail(
        **summary.model_dump(),
        description_text=description_text,
        description_method=description_method,
        description_sha256=description_sha256,
    )


@router.get("/search-runs")
async def list_live_search_runs() -> dict[str, list[dict]]:
    return {"items": await db.list_jobhunt_search_runs()}


@router.post("/live-jobs/{job_key}/save", response_model=JobHuntSaveResponse)
async def save_live_job(job_key: str) -> JobHuntSaveResponse:
    listing = await get_live_job(job_key)
    try:
        application = await db.track_discovery_job(
            job_key=listing.job_key,
            title=listing.title,
            company=listing.company,
            source_url=listing.source_url,
            location=listing.location,
            posted_date=listing.posted_date,
            description=listing.description_text
            if listing.description_status == "complete"
            else None,
        )
    except Exception as exc:
        logger.exception("Could not save live job to tracker")
        raise HTTPException(
            status_code=500, detail="Could not save this job. Please try again."
        ) from exc
    return JobHuntSaveResponse(
        job_key=job_key, application_id=application["application_id"]
    )


@router.post("/live-jobs/{job_key}/skip", response_model=JobHuntSkipResponse)
async def skip_live_job(job_key: str) -> JobHuntSkipResponse:
    await get_live_job(job_key)
    try:
        skipped = await db.skip_jobhunt_discovery_job(job_key)
    except Exception as exc:
        logger.exception("Could not skip live job")
        raise HTTPException(
            status_code=500, detail="Could not update this job. Please try again."
        ) from exc
    if not skipped:
        raise HTTPException(
            status_code=409,
            detail="This job is already in the application tracker.",
        )
    return JobHuntSkipResponse(job_key=job_key)


@router.post("/live-jobs/{job_key}/tailor-job", response_model=JobHuntTailorJobResponse)
async def prepare_live_job_for_tailoring(
    job_key: str, request: JobHuntTailorJobRequest
) -> JobHuntTailorJobResponse:
    listing = await get_live_job(job_key)
    source_description = (
        listing.description_text
        if listing.description_status in {"complete", "manual"}
        else None
    )
    content = (source_description or request.job_description or "").strip()
    if len(content) < 50:
        raise HTTPException(
            status_code=422,
            detail="Paste the full job description (at least 50 characters).",
        )
    require_source_size(content, MAX_JOB_CHARACTERS)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    metadata = {
        "source_system": "jobhunt",
        "source_provider": listing.provider,
        "source_job_key": listing.job_key,
        "canonical_source_id": listing.canonical_id,
        "title": listing.title,
        "role": listing.title,
        "company": listing.company,
        "location": listing.location,
        "source_url": listing.source_url,
        "source": listing.source,
        "posted_date": listing.posted_date,
        "description_status": listing.description_status
        if source_description
        else "manual",
        "description_provenance": listing.description_method
        if source_description
        else "user_pasted",
        "description_sha256": digest,
    }
    try:
        job, created = await db.create_or_get_jobhunt_job(
            job_key=listing.job_key,
            content=content,
            content_sha256=digest,
            metadata=metadata,
        )
    except Exception as exc:
        logger.exception("Could not prepare live job for tailoring")
        raise HTTPException(
            status_code=500, detail="Could not prepare this job for tailoring."
        ) from exc
    return JobHuntTailorJobResponse(
        job_key=listing.job_key,
        job_id=job["job_id"],
        created=created,
        description_status=metadata["description_status"],
    )


@router.get("/{job_key}", response_model=JobHuntJobDetail)
async def get_discovered_job(job_key: str) -> JobHuntJobDetail:
    if not job_key or len(job_key) > 256:
        raise HTTPException(status_code=404, detail="Job not found.")
    try:
        job = _catalog().get_job(job_key)
    except (JobHuntSnapshotTooLarge, JobHuntSnapshotUnavailable):
        raise HTTPException(
            status_code=503, detail="Job discovery is temporarily unavailable."
        ) from None
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    states = await db.list_jobhunt_discovery_states([job_key])
    description = _catalog().get_job_description_record(job_key)
    if description is None:
        description = await db.get_jobhunt_prepared_description(job_key)
    status = (
        description.get("description_status")
        if description
        and description.get("description_status")
        in {
            "missing",
            "complete",
            "partial",
            "manual",
            "failed",
            "unknown",
        }
        else job.description_status
    )
    detail = job.model_dump()
    detail.update(
        pipeline_state=states.get(job_key, "new"),
        description_status=status,
        description_available=status in {"complete", "partial", "manual"},
        description_text=description.get("description_text") if description else None,
        description_method=description.get("description_method")
        if description
        else None,
        description_retrieved_at=description.get("description_retrieved_at")
        if description
        else None,
    )
    return JobHuntJobDetail(**detail)


@router.post("/{job_key}/save", response_model=JobHuntSaveResponse)
async def save_discovered_job(job_key: str) -> JobHuntSaveResponse:
    """Create one saved tracker card for a discovered listing."""
    listing = await get_discovered_job(job_key)
    try:
        application = await db.track_discovery_job(
            job_key=listing.job_key,
            title=listing.title,
            company=listing.company,
            source_url=listing.source_url,
            location=listing.location,
            posted_date=listing.posted_date,
        )
    except Exception as exc:
        logger.exception("Could not save JobHunt listing")
        raise HTTPException(
            status_code=500, detail="Could not save this job. Please try again."
        ) from exc
    return JobHuntSaveResponse(
        job_key=job_key,
        application_id=application["application_id"],
    )


@router.post("/{job_key}/skip", response_model=JobHuntSkipResponse)
async def skip_discovered_job(job_key: str) -> JobHuntSkipResponse:
    """Hide an untracked listing from the discovery workflow."""
    await get_discovered_job(job_key)
    try:
        skipped = await db.skip_jobhunt_discovery_job(job_key)
    except Exception as exc:
        logger.exception("Could not skip JobHunt listing")
        raise HTTPException(
            status_code=500, detail="Could not update this job. Please try again."
        ) from exc
    if not skipped:
        raise HTTPException(
            status_code=409,
            detail="This job is already in the application tracker.",
        )
    return JobHuntSkipResponse(job_key=job_key)


@router.post("/{job_key}/tailor-job", response_model=JobHuntTailorJobResponse)
async def prepare_job_for_tailoring(
    job_key: str, request: JobHuntTailorJobRequest
) -> JobHuntTailorJobResponse:
    """Persist a user-pasted full description and return its idempotent Matcher job."""
    if not job_key or len(job_key) > 256:
        raise HTTPException(status_code=404, detail="Job not found.")
    try:
        listing = _catalog().get_job(job_key)
    except (JobHuntSnapshotTooLarge, JobHuntSnapshotUnavailable):
        raise HTTPException(
            status_code=503, detail="Job discovery is temporarily unavailable."
        ) from None
    if listing is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    source_description = None
    if listing.description_status in {"complete", "manual"}:
        source_description = _catalog().get_job_description(job_key)
    content = (source_description or request.job_description or "").strip()
    if len(content) < 50:
        raise HTTPException(
            status_code=422,
            detail="Paste the full job description (at least 50 characters).",
        )
    require_source_size(content, MAX_JOB_CHARACTERS)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    metadata = {
        "source_system": "jobhunt",
        "source_job_key": listing.job_key,
        "title": listing.title,
        "role": listing.title,
        "company": listing.company,
        "location": listing.location,
        "source_url": listing.source_url,
        "source": listing.source,
        "posted_date": listing.posted_date,
        "description_status": listing.description_status
        if source_description
        else "manual",
        "description_provenance": "jobhunt_snapshot"
        if source_description
        else "user_pasted",
        "description_sha256": digest,
    }
    try:
        job, created = await db.create_or_get_jobhunt_job(
            job_key=listing.job_key,
            content=content,
            content_sha256=digest,
            metadata=metadata,
        )
    except Exception as exc:
        logger.exception("Could not prepare JobHunt job for tailoring")
        raise HTTPException(
            status_code=500, detail="Could not prepare this job for tailoring."
        ) from exc
    return JobHuntTailorJobResponse(
        job_key=listing.job_key,
        job_id=job["job_id"],
        created=created,
        description_status=metadata["description_status"],
    )
