"""Read-only adapter for a disposable JobHunt SQLite snapshot."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from app.schemas.jobhunt_discovery import JobHuntJobPage, JobHuntJobSummary

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 20
MAX_SCANNED_ROWS = 10_000
_REQUIRED_COLUMNS = {"key", "title", "url"}
_DESCRIPTION_STATUSES = {"complete", "partial", "manual", "failed"}
_SORT_COLUMNS = {
    "first_seen": "j.first_seen",
    "posted_date": "j.posted_date",
    "title": "j.title COLLATE NOCASE",
    "company": "j.company COLLATE NOCASE",
    "location": "j.location COLLATE NOCASE",
    "source": "j.source COLLATE NOCASE",
}


class JobHuntSnapshotUnavailable(Exception):
    """The configured snapshot is missing, unreadable, or incompatible."""


class JobHuntSnapshotTooLarge(Exception):
    """The snapshot exceeds this pilot adapter's deliberate scan limit."""


class JobHuntSnapshotCatalog:
    """Read summaries only; never returns applications or mutates the source."""

    def __init__(self, snapshot_path: str | Path | None = None) -> None:
        self._configured_path = snapshot_path

    def _path(self) -> Path:
        configured = self._configured_path or os.environ.get("JOBHUNT_SNAPSHOT_PATH")
        if configured is None or not str(configured).strip():
            raise JobHuntSnapshotUnavailable
        try:
            path = Path(configured).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise JobHuntSnapshotUnavailable from None
        if not path.is_file():
            raise JobHuntSnapshotUnavailable
        return path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        path = self._path()
        connection: sqlite3.Connection | None = None
        try:
            # `mode=ro` blocks writes at SQLite's VFS boundary. `immutable=1`
            # is appropriate because this configured file is a static pilot copy.
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro&immutable=1", uri=True, timeout=1
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection
        except (sqlite3.Error, OSError, ValueError):
            logger.warning("JobHunt snapshot read failed")
            raise JobHuntSnapshotUnavailable from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _clean_optional(value: object, max_length: int) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned or len(cleaned) > max_length:
            return None
        return cleaned

    @staticmethod
    def _valid_date(value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        cleaned = value.strip()
        try:
            return date.fromisoformat(cleaned[:10]).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _valid_url(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        if not candidate or len(candidate) > 2048:
            return None
        try:
            parsed = urlsplit(candidate)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                return None
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }

    @staticmethod
    def _duplicate_values(
        connection: sqlite3.Connection, column: str, values: list[object]
    ) -> set[str]:
        """Check candidate identities against the whole table, in bounded SQL chunks."""
        candidates = list(
            dict.fromkeys(value for value in values if isinstance(value, str))
        )
        duplicates: set[str] = set()
        for offset in range(0, len(candidates), 500):
            chunk = candidates[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            # `column` is selected only from the internal `key`/`url` call sites.
            rows = connection.execute(
                f"SELECT {column} FROM jobs WHERE {column} IN ({placeholders}) "
                f"GROUP BY {column} HAVING COUNT(*) > 1",
                chunk,
            )
            duplicates.update(row[0] for row in rows if isinstance(row[0], str))
        return duplicates

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        query: str | None = None,
        location: str | None = None,
        source: str | None = None,
        description_available: bool | None = None,
        posted_after: str | None = None,
        posted_before: str | None = None,
        sort: str = "first_seen",
        direction: str = "desc",
        offset: int | None = None,
    ) -> JobHuntJobPage:
        if page < 1 or page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise ValueError("invalid pagination")
        if sort not in _SORT_COLUMNS or direction not in {"asc", "desc"}:
            raise ValueError("invalid ordering")
        if offset is not None and (offset < 0 or offset > MAX_SCANNED_ROWS):
            raise ValueError("invalid offset")
        if offset is not None and page > 1 and offset != (page - 1) * page_size:
            raise ValueError("page and offset do not agree")
        if posted_after and self._valid_date(posted_after) != posted_after:
            raise ValueError("invalid posted_after")
        if posted_before and self._valid_date(posted_before) != posted_before:
            raise ValueError("invalid posted_before")

        with self._connection() as connection:
            columns = self._columns(connection, "jobs")
            if not _REQUIRED_COLUMNS.issubset(columns):
                raise JobHuntSnapshotUnavailable
            if "job_description_snapshots" in {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }:
                description_columns = self._columns(
                    connection, "job_description_snapshots"
                )
                if {"job_key", "status"}.issubset(description_columns):
                    description_sql = (
                        "(SELECT d.status FROM job_description_snapshots d "
                        "WHERE d.job_key=j.key ORDER BY d.id DESC LIMIT 1)"
                    )
                else:
                    description_sql = "NULL"
            else:
                description_sql = "NULL"

            matches: list[str] = []
            params: list[str] = []
            if query:
                escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                matches.append(
                    "(j.title LIKE ? ESCAPE '!' OR "
                    "COALESCE(j.company, '') LIKE ? ESCAPE '!' OR "
                    "COALESCE(j.location, '') LIKE ? ESCAPE '!')"
                )
                params.extend((f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"))
            if location:
                escaped_location = (
                    location.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                )
                matches.append("COALESCE(j.location, '') LIKE ? ESCAPE '!'")
                params.append(f"%{escaped_location}%")
            if source:
                matches.append("COALESCE(j.source, '') = ? COLLATE NOCASE")
                params.append(source)
            if posted_after:
                matches.append("j.posted_date >= ?")
                params.append(posted_after)
            if posted_before:
                matches.append("j.posted_date <= ?")
                params.append(posted_before)
            # Description status is looked up via a correlated, latest-row query.
            if description_available is not None:
                matches.append(
                    f"(COALESCE(({description_sql}), 'missing') "
                    "IN ('complete','partial','manual')) = ?"
                )
                params.append(int(description_available))
            where = " WHERE " + " AND ".join(matches) if matches else ""
            row_count = connection.execute(
                f"SELECT COUNT(*) FROM jobs j{where}", params
            ).fetchone()[0]
            if row_count > MAX_SCANNED_ROWS:
                raise JobHuntSnapshotTooLarge

            rows = connection.execute(
                f"""SELECT j.key, j.title, j.company, j.location, j.url,
                           j.source, j.posted_date, j.first_seen,
                           {description_sql} AS description_status
                    FROM jobs j{where}
                    ORDER BY {_SORT_COLUMNS[sort]} {direction.upper()}, j.key ASC""",
                params,
            ).fetchall()
            duplicate_keys = self._duplicate_values(
                connection, "key", [row["key"] for row in rows]
            )
            duplicate_urls = self._duplicate_values(
                connection, "url", [row["url"] for row in rows]
            )
        if duplicate_keys or duplicate_urls:
            logger.warning(
                "Skipped ambiguous JobHunt records (duplicate_keys=%d, duplicate_urls=%d)",
                len(duplicate_keys),
                len(duplicate_urls),
            )

        jobs: list[JobHuntJobSummary] = []
        malformed = 0
        for row in rows:
            key = row["key"]
            title = row["title"]
            url = self._valid_url(row["url"])
            if (
                not isinstance(key, str)
                or not key.strip()
                or len(key) > 256
                or key in duplicate_keys
                or row["url"] in duplicate_urls
                or not isinstance(title, str)
                or not title.strip()
                or len(title.strip()) > 500
                or url is None
            ):
                malformed += 1
                continue

            status = row["description_status"]
            if status not in _DESCRIPTION_STATUSES:
                status = "missing" if status is None else "unknown"
            jobs.append(
                JobHuntJobSummary(
                    job_key=key.strip(),
                    title=title.strip(),
                    company=self._clean_optional(row["company"], 300),
                    location=self._clean_optional(row["location"], 300),
                    source_url=url,
                    source=self._clean_optional(row["source"], 100),
                    posted_date=self._valid_date(row["posted_date"]),
                    first_seen=self._valid_date(row["first_seen"]),
                    description_status=status,
                    description_available=status in {"complete", "partial", "manual"},
                )
            )
        if malformed:
            logger.warning("Skipped malformed JobHunt records (count=%d)", malformed)

        total = len(jobs)
        pages = (total + page_size - 1) // page_size
        start = offset if offset is not None else (page - 1) * page_size
        actual_page = start // page_size + 1
        return JobHuntJobPage(
            items=jobs[start : start + page_size],
            page=actual_page,
            page_size=page_size,
            total=total,
            pages=pages,
            offset=start,
        )

    def get_job(self, job_key: str) -> JobHuntJobSummary | None:
        page = self.list_jobs(page=1, page_size=MAX_PAGE_SIZE)
        while page.items:
            for job in page.items:
                if job.job_key == job_key:
                    return job
            if page.page >= page.pages:
                break
            page = self.list_jobs(page=page.page + 1, page_size=MAX_PAGE_SIZE)
        return None

    def get_job_description(self, job_key: str) -> str | None:
        """Fetch the original text of the job description if present in the snapshot."""
        record = self.get_job_description_record(job_key)
        return record["description_text"] if record else None

    def get_job_description_record(self, job_key: str) -> dict[str, str | None] | None:
        """Return description text with its stored extraction provenance."""
        try:
            with self._connection() as connection:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "job_description_snapshots" not in tables:
                    return None
                columns = self._columns(connection, "job_description_snapshots")
                if not {"job_key", "original_text"}.issubset(columns):
                    return None
                status_column = "status" if "status" in columns else "NULL AS status"
                method_column = "method" if "method" in columns else "NULL AS method"
                retrieved_column = (
                    "retrieved_at"
                    if "retrieved_at" in columns
                    else "NULL AS retrieved_at"
                )
                row = connection.execute(
                    "SELECT original_text, "
                    f"{status_column}, {method_column}, {retrieved_column} "
                    "FROM job_description_snapshots "
                    "WHERE job_key = ? AND original_text IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (job_key,),
                ).fetchone()
                if row and row["original_text"] and str(row["original_text"]).strip():
                    return {
                        "description_text": str(row["original_text"]).strip(),
                        "description_status": row["status"],
                        "description_method": row["method"],
                        "description_retrieved_at": row["retrieved_at"],
                    }
                return None
        except Exception:
            return None
