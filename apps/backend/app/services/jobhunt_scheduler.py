"""Local scheduler for explicitly enabled JobHunt saved searches."""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from app.database import db
from app.schemas.jobhunt_discovery import (
    JobHuntLiveSearchRequest,
    JobHuntLiveSearchResponse,
)

logger = logging.getLogger(__name__)
SCHEDULER_POLL_SECONDS = 60


def _request_for_saved_search(search: dict) -> JobHuntLiveSearchRequest:
    filters = search.get("filters") or {}
    allowed = {
        "country",
        "city",
        "jobage",
        "include_remote",
        "limit_per_query",
    }
    return JobHuntLiveSearchRequest(
        query=search.get("query"),
        **{key: value for key, value in filters.items() if key in allowed},
    )


async def run_saved_search(search_id: str) -> JobHuntLiveSearchResponse:
    """Run one stored search now and record its run status."""
    search = await db.get_jobhunt_saved_search(search_id)
    if search is None:
        raise KeyError(search_id)

    # Imported at call time to avoid coupling the API router's import cycle to
    # the scheduler module.
    from app.routers.jobhunt_discovery import search_live_jobs

    try:
        result = await search_live_jobs(_request_for_saved_search(search))
    except Exception as exc:
        await db.finish_jobhunt_saved_search(
            search_id=search_id, run_id=None, status="failed"
        )
        if isinstance(exc, HTTPException):
            raise
        logger.exception("Saved JobHunt search failed")
        raise

    await db.finish_jobhunt_saved_search(
        search_id=search_id, run_id=result.run_id, status=result.status
    )
    return result


async def process_due_saved_searches() -> int:
    """Claim and run due searches sequentially to respect source request limits."""
    due = await db.claim_due_jobhunt_saved_searches()
    for search in due:
        try:
            await run_saved_search(search["search_id"])
        except Exception:
            logger.exception("Scheduled JobHunt refresh failed")
    return len(due)


async def scheduler_loop() -> None:
    """Poll the local database; no search runs unless enabled and due."""
    while True:
        try:
            await process_due_saved_searches()
        except Exception:
            logger.exception("Could not poll local JobHunt saved searches")
        await asyncio.sleep(SCHEDULER_POLL_SECONDS)
