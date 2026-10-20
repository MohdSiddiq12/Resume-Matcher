"""Read and import JobHunt application history without modifying its database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.database import Database

_STATUS_MAP = {
    "investigating": "saved",
    "shortlisted": "saved",
    "drafting": "saved",
    "ready_for_review": "saved",
    "applied": "applied",
    "assessment": "applied",
    "interview": "interview",
    "offer": "accepted",
    "hired": "accepted",
    "rejected": "rejected",
    "withdrawn": "rejected",
    "no_response": "no_response",
    "offer_declined": "rejected",
}


class JobHuntImportError(RuntimeError):
    """The read-only JobHunt source is incompatible or incomplete."""


def read_jobhunt_applications(path: str | Path) -> list[dict[str, Any]]:
    """Read application records and matching job metadata through SQLite read-only mode."""
    source = Path(path).expanduser().resolve(strict=True)
    try:
        connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro&immutable=1", uri=True, timeout=2
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(applications)")
        }
        if not {"job_key", "status", "notes", "follow_up", "updated_at"}.issubset(
            columns
        ):
            raise JobHuntImportError("JobHunt application schema is incompatible")
        rows = connection.execute(
            "SELECT a.job_key, a.status, a.notes, a.follow_up, a.updated_at, "
            "j.title, j.company, j.url, j.location, j.posted_date "
            "FROM applications a JOIN jobs j ON j.key = a.job_key "
            "ORDER BY a.job_key"
        ).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError, ValueError) as exc:
        if isinstance(exc, JobHuntImportError):
            raise
        raise JobHuntImportError("Could not read JobHunt application history") from exc
    finally:
        if "connection" in locals():
            connection.close()


async def import_jobhunt_applications(
    database: Database, source_path: str | Path
) -> dict[str, int]:
    """Idempotently add source applications as Resume Matcher tracker cards.

    Original JobHunt stages are retained in ``legacy_status`` and notes; statuses
    outside Matcher's seven columns use the nearest display column while preserving
    the canonical stage. Imported submitted statuses are marked confirmed because
    JobHunt's own API required confirmation before writing them.
    """
    imported = 0
    existing = 0
    for record in read_jobhunt_applications(source_path):
        key = str(record["job_key"])
        legacy_status = str(record["status"])
        status = _STATUS_MAP.get(legacy_status)
        if status is None:
            raise JobHuntImportError(f"Unsupported JobHunt status: {legacy_status}")
        metadata = {
            "source_system": "jobhunt",
            "source_job_key": key,
            "source_url": record["url"],
            "location": record["location"],
            "posted_date": record["posted_date"],
            "legacy_application_status": legacy_status,
        }
        is_external_status = status in {"applied", "interview", "accepted"}
        note_parts = [
            f"Imported from JobHunt; original status: {legacy_status}.",
            str(record["notes"] or "").strip(),
        ]
        follow_up = str(record["follow_up"] or "").strip() or None
        _, created = await database.import_jobhunt_application(
            job_key=key,
            content=None,
            metadata=metadata,
            status=status,
            company=record["company"],
            role=record["title"],
            applied_at=record["updated_at"] if is_external_status else None,
            notes="\n".join(part for part in note_parts if part),
            legacy_status=legacy_status,
            follow_up_date=follow_up,
            confirmed=is_external_status,
        )
        imported += int(created)
        existing += int(not created)
    return {"imported": imported, "already_present": existing}
