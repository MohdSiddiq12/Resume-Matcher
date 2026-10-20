"""SQLAlchemy data layer for Resume Matcher (SQLite, or PostgreSQL via DATABASE_URL).

This is a behavior-preserving replacement for the original TinyDB wrapper. The
``Database`` facade keeps the same method names/signatures and returns **plain
dicts** (never ORM rows), so the ~50 call sites only needed ``await`` added.

Two engines back one database:
- an **async** engine (``aiosqlite`` / ``psycopg``) for the document tables and applications;
- a **sync** engine for the encrypted ``api_keys`` table, which is read on the
  synchronous LLM hot path (``get_llm_config`` → ``resolve_api_key``).
"""

import copy
import logging
import shutil
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db_engine import (
    PG_LOCK_NAMESPACE,
    init_models_sync,
    make_async_engine,
    make_sync_engine,
)
from app.db_url import validate_schema_name
from app.models import (
    ApiKey,
    Application,
    ApplicationEvent,
    ApplicationOutreachDraft,
    Improvement,
    Job,
    JobHuntDiscoveryState,
    JobHuntJobLink,
    JobHuntLiveJob,
    JobHuntSavedSearch,
    JobHuntSearchProfile,
    JobHuntSearchRun,
    QueueItem,
    Resume,
    TailoringPreview,
)
from app.preview import (
    PreviewBusyError,
    PreviewClaim,
    PreviewConflictError,
    PreviewValidationError,
    job_fingerprint,
    resume_fingerprint,
)

logger = logging.getLogger(__name__)

# Columns that are first-class on the jobs table; everything else the pipeline
# attaches dynamically is stored in ``metadata_json`` (see Job model).
_JOB_CORE_FIELDS = frozenset({"job_id", "content", "resume_id", "created_at"})

# Application status columns (stable keys, decoupled from i18n labels).
APPLICATION_STATUSES: tuple[str, ...] = (
    "saved",
    "applied",
    "no_response",
    "response",
    "interview",
    "accepted",
    "rejected",
)
ProcessingFinishOutcome = Literal["committed", "stale", "missing"]
QueueDeleteOutcome = Literal["deleted", "missing", "in_progress"]

# A user keeps at most this many master resumes (career tracks).
MAX_MASTER_RESUMES = 5

# A resume's formatting remembers the save order of this many recent builder sessions.
TEMPLATE_SETTINGS_SESSIONS_KEPT = 20


class DatabaseBusyError(RuntimeError):
    """A write reservation could not be obtained; retry the unchanged request."""


class MasterResumeLimitError(Exception):
    """Raised when creating a master would exceed MAX_MASTER_RESUMES."""


# lock_not_available (lock_timeout), serialization_failure, deadlock_detected.
_PG_BUSY_SQLSTATES = frozenset({"55P03", "40001", "40P01"})
_PG_WRITE_LOCK = text("SELECT pg_advisory_xact_lock(:ns, hashtext(current_schema()))")
WRITE_LOCK_TIMEOUT_MS = 5000


@contextmanager
def _translate_write_errors() -> Iterator[None]:
    """Expose only database contention as retryable, for async and sync writers."""
    try:
        yield
    except OperationalError as error:
        code = getattr(error.orig, "sqlite_errorcode", None)
        if isinstance(code, int) and code & 0xFF in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        ):
            raise DatabaseBusyError("Database is busy") from error
        if getattr(error.orig, "sqlstate", None) in _PG_BUSY_SQLSTATES:
            raise DatabaseBusyError("Database is busy") from error
        raise


def _write_lock_statements(postgres: bool, lock_timeout_ms: int) -> list[Any]:
    """SQL that reserves the single writer: BEGIN IMMEDIATE, or a schema-wide advisory lock."""
    if not postgres:
        return [text("BEGIN IMMEDIATE")]
    return [
        text(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}"),
        _PG_WRITE_LOCK.bindparams(ns=PG_LOCK_NAMESPACE),
    ]


class ResumeNotFoundError(ValueError):
    """Raised when a write targets a resume ID that no longer exists.

    Subclasses ``ValueError`` deliberately: every pre-existing
    ``except ValueError`` handler around resume writes keeps working unchanged,
    while callers that need to distinguish "the row is gone" from any other
    bad-argument error can catch this type instead of string-matching the
    message (the message is not an API).
    """

    def __init__(self, resume_id: str) -> None:
        self.resume_id = resume_id
        super().__init__(f"Resume not found: {resume_id}")


def _now() -> str:
    """Current UTC time as an ISO-8601 string (TinyDB-era format)."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Async SQLAlchemy facade for resume matcher data."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        url: str | None = None,
        schema: str | None = None,
    ):
        if db_path is None and url is None and settings.database_url:
            url, schema = settings.database_url, settings.database_schema
        self.url = None if db_path is not None else url
        self.schema = validate_schema_name(schema) if self.url and schema else None
        self.db_path: Path | None = None
        if self.url is None:
            self.db_path = db_path or settings.sqlite_path
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_timeout_ms = WRITE_LOCK_TIMEOUT_MS
        self._async_engine = None
        self._async_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._sync_engine = None
        self._sync_session_factory: sessionmaker[Session] | None = None
        self._initialized = False

    # -- engine / session plumbing ------------------------------------------

    def _ensure_initialized(self) -> None:
        """Create engines and tables once (idempotent).

        Tables are created via the **sync** engine so both the sync (api_keys)
        and async (docs) paths see them immediately, without needing an event
        loop. Both engines point at the same database.
        """
        if self._initialized:
            return
        target = {"url": self.url, "schema": self.schema}
        self._sync_engine = make_sync_engine(self.db_path, **target)
        self._sync_session_factory = sessionmaker(
            self._sync_engine, expire_on_commit=False
        )
        init_models_sync(self._sync_engine, self.schema)
        self._async_engine = make_async_engine(self.db_path, **target)
        self._async_session_factory = async_sessionmaker(
            self._async_engine, expire_on_commit=False
        )
        self._initialized = True

    @property
    def _session(self) -> async_sessionmaker[AsyncSession]:
        self._ensure_initialized()
        assert self._async_session_factory is not None
        return self._async_session_factory

    @property
    def backend(self) -> Literal["sqlite", "postgresql"]:
        return "postgresql" if self.url else "sqlite"

    def _write_lock(self) -> list[Any]:
        return _write_lock_statements(self.url is not None, self.lock_timeout_ms)

    @asynccontextmanager
    async def _write_session(self) -> AsyncIterator[AsyncSession]:
        """Reserve the database's single writer before reading state that a write depends on.

        The reservation (SQLite ``BEGIN IMMEDIATE``, or a PostgreSQL advisory lock
        per schema) serializes across connections and processes, unlike an
        in-memory lock. Callers commit the complete operation; closing the session
        rolls back every change if any stage raises.
        """
        with _translate_write_errors():
            async with self._session() as session:
                for statement in self._write_lock():
                    await session.execute(statement)
                yield session

    @property
    def _sync(self) -> sessionmaker[Session]:
        self._ensure_initialized()
        assert self._sync_session_factory is not None
        return self._sync_session_factory

    @contextmanager
    def _sync_write_session(self) -> Iterator[Session]:
        """Reserve a synchronous key-store writer with the same busy contract."""
        with _translate_write_errors():
            with self._sync() as session:
                for statement in self._write_lock():
                    session.execute(statement)
                yield session

    async def close(self) -> None:
        """Dispose engines and release file handles."""
        if self._async_engine is not None:
            await self._async_engine.dispose()
            self._async_engine = None
            self._async_session_factory = None
        if self._sync_engine is not None:
            self._sync_engine.dispose()
            self._sync_engine = None
            self._sync_session_factory = None
        self._initialized = False

    # -- row -> dict converters ---------------------------------------------

    @staticmethod
    def _resume_to_dict(row: Resume) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "resume_id": row.resume_id,
            "content": row.content,
            "content_type": row.content_type,
            "filename": row.filename,
            "is_master": row.is_master,
            "is_default_master": row.is_default_master,
            "parent_id": row.parent_id,
            "processed_data": row.processed_data,
            "processing_status": row.processing_status,
            "cover_letter": row.cover_letter,
            "outreach_message": row.outreach_message,
            "interview_prep": row.interview_prep,
            "title": row.title,
            "template_settings": row.template_settings,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        # Preserve TinyDB absence semantics: omit the key entirely when None.
        if row.original_markdown is not None:
            doc["original_markdown"] = row.original_markdown
        return doc

    @staticmethod
    def _job_to_dict(row: Job) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "job_id": row.job_id,
            "content": row.content,
            "resume_id": row.resume_id,
            "created_at": row.created_at,
        }
        meta = row.metadata_json or {}
        if isinstance(meta, dict):
            doc.update(meta)  # flatten dynamic fields to top level
        return doc

    @staticmethod
    def _improvement_to_dict(row: Improvement) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "original_resume_id": row.original_resume_id,
            "tailored_resume_id": row.tailored_resume_id,
            "job_id": row.job_id,
            "improvements": row.improvements,
            "ats_score": row.ats_score,
            "created_at": row.created_at,
        }

    @staticmethod
    def _application_to_dict(row: Application) -> dict[str, Any]:
        return {
            "application_id": row.application_id,
            "job_id": row.job_id,
            "resume_id": row.resume_id,
            "master_resume_id": row.master_resume_id,
            "status": row.status,
            "company": row.company,
            "role": row.role,
            "applied_at": row.applied_at,
            "notes": row.notes,
            "position": row.position,
            "legacy_application_id": row.legacy_application_id,
            "legacy_status": row.legacy_status,
            "follow_up_date": row.follow_up_date,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _application_event_to_dict(row: ApplicationEvent) -> dict[str, Any]:
        return {
            "event_id": row.event_id,
            "application_id": row.application_id,
            "from_status": row.from_status,
            "to_status": row.to_status,
            "confirmed": row.confirmed,
            "note": row.note,
            "created_at": row.created_at,
        }

    @staticmethod
    def _queue_item_to_dict(row: QueueItem) -> dict[str, Any]:
        return {
            "item_id": row.item_id,
            "resume_id": row.resume_id,
            "job_description": row.job_description,
            "role": row.role,
            "company": row.company,
            "job_req_id": row.job_req_id,
            "status": row.status,
            "job_id": row.job_id,
            "tailored_resume_id": row.tailored_resume_id,
            "title": row.title,
            "error": row.error,
            "rate_limit_seconds": row.rate_limit_seconds,
            "prompt_id": row.prompt_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # -- Resume operations --------------------------------------------------

    async def create_resume(
        self,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        is_master: bool = False,
        parent_id: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        title: str | None = None,
        original_markdown: str | None = None,
        interview_prep: str | None = None,
        is_default_master: bool = False,
        template_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new resume entry.

        processing_status: "pending", "processing", "ready", "failed"
        """
        row = self._new_resume(
            content=content,
            content_type=content_type,
            filename=filename,
            is_master=is_master,
            is_default_master=is_default_master,
            parent_id=parent_id,
            processed_data=processed_data,
            processing_status=processing_status,
            cover_letter=cover_letter,
            outreach_message=outreach_message,
            interview_prep=interview_prep,
            title=title,
            original_markdown=original_markdown,
            template_settings=template_settings,
        )
        async with self._write_session() as session:
            session.add(row)
            await session.commit()
        return self._resume_to_dict(row)

    @staticmethod
    def _new_resume(**values: Any) -> Resume:
        """Construct a resume row for standalone or compound transactions."""
        now = _now()
        return Resume(resume_id=str(uuid4()), created_at=now, updated_at=now, **values)

    async def create_resume_atomic_master(
        self,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        original_markdown: str | None = None,
        title: str | None = None,
        interview_prep: str | None = None,
        template_settings: dict[str, Any] | None = None,
        *,
        take_over_stuck_default: bool = True,
        replay_if: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Create a new master track in one transaction.

        The first master becomes the default. A default stuck in failed/processing
        hands the default to the new upload (it stays a master) unless
        take_over_stuck_default is False, in which case the new row is default only
        when no default exists at all. Raises MasterResumeLimitError when
        MAX_MASTER_RESUMES masters already exist. When ``replay_if`` accepts an
        existing master (oldest first), that master is returned and nothing is
        written; the check runs inside the write transaction, so concurrent
        identical requests cannot both insert.
        """
        async with self._write_session() as session:
            masters = (
                (
                    await session.execute(
                        select(Resume)
                        .where(Resume.is_master.is_(True))
                        .order_by(Resume.created_at)
                    )
                )
                .scalars()
                .all()
            )
            if replay_if is not None:
                for master in masters:
                    existing = self._resume_to_dict(master)
                    if replay_if(existing):
                        return existing
            if len(masters) >= MAX_MASTER_RESUMES:
                raise MasterResumeLimitError(
                    f"Master resume limit reached ({MAX_MASTER_RESUMES})"
                )
            current_default = next((m for m in masters if m.is_default_master), None)
            is_default = current_default is None
            if (
                take_over_stuck_default
                and current_default is not None
                and current_default.processing_status in ("failed", "processing")
            ):
                current_default.is_default_master = False
                # Release the partial unique-index slot within this transaction.
                # An insertion failure still rolls this demotion back.
                await session.flush()
                is_default = True
            row = self._new_resume(
                content=content,
                content_type=content_type,
                filename=filename,
                is_master=True,
                is_default_master=is_default,
                processed_data=processed_data,
                processing_status=processing_status,
                cover_letter=cover_letter,
                outreach_message=outreach_message,
                interview_prep=interview_prep,
                title=title,
                original_markdown=original_markdown,
                template_settings=template_settings,
            )
            session.add(row)
            await session.commit()
            return self._resume_to_dict(row)

    async def get_resume(self, resume_id: str) -> dict[str, Any] | None:
        """Get resume by ID."""
        async with self._session() as session:
            row = await session.get(Resume, resume_id)
            return self._resume_to_dict(row) if row else None

    async def get_master_resume(self) -> dict[str, Any] | None:
        """Get the default master resume (earliest master as a fallback)."""
        async with self._session() as session:
            result = await session.execute(
                select(Resume)
                .where(Resume.is_master.is_(True))
                .order_by(Resume.is_default_master.desc(), Resume.created_at)
            )
            row = result.scalars().first()
            return self._resume_to_dict(row) if row else None

    async def list_master_resumes(self) -> list[dict[str, Any]]:
        """List every master track, oldest first."""
        async with self._session() as session:
            result = await session.execute(
                select(Resume)
                .where(Resume.is_master.is_(True))
                .order_by(Resume.created_at)
            )
            return [self._resume_to_dict(row) for row in result.scalars().all()]

    async def update_resume(
        self, resume_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Update resume by ID.

        Raises:
            ResumeNotFoundError: If resume not found. It subclasses
                ``ValueError``, so existing ``except ValueError`` callers are
                unaffected.
        """
        async with self._write_session() as session:
            row = await session.get(Resume, resume_id)
            if row is None:
                raise ResumeNotFoundError(resume_id)
            for key, value in updates.items():
                if hasattr(row, key):
                    setattr(row, key, value)
                else:
                    logger.warning("Ignoring unknown resume field on update: %s", key)
            row.updated_at = _now()
            await session.commit()
            return self._resume_to_dict(row)

    async def save_template_settings(
        self, resume_id: str, settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Store a resume's formatting unless its session already stored a newer save.

        The stored JSON keeps the highest revision accepted from each recent
        session under ``seen`` (API responses drop it), so saves from other
        sessions in between cannot make a session's older request valid again.
        Returns the stored settings. The read and the write share one write
        reservation, so two concurrent saves cannot both pass the check.

        Raises:
            ResumeNotFoundError: If resume not found.
        """
        async with self._write_session() as session:
            row = await session.get(Resume, resume_id)
            if row is None:
                raise ResumeNotFoundError(resume_id)
            stored = row.template_settings or {}
            seen: dict[str, int] = dict(stored.get("seen") or {})
            saver = settings.get("session")
            revision = settings.get("revision", 0)
            if saver:
                if revision < seen.get(saver, -1):
                    return stored
                seen.pop(saver, None)
                seen[saver] = revision
                seen = dict(list(seen.items())[-TEMPLATE_SETTINGS_SESSIONS_KEPT:])
            row.template_settings = {**settings, "seen": seen}
            row.updated_at = _now()
            await session.commit()
            return row.template_settings

    async def claim_resume_processing(
        self,
        resume_id: str,
        *,
        allow_ready_at: str | None = None,
    ) -> str | None:
        """Rotate processing ownership and return its opaque operation token.

        Failed and processing rows are retryable. A legacy ready row is only
        claimable when the caller observed the same version and found its
        structured content empty. ``None`` means a concurrent completion made
        the row ineligible; a missing row raises ``ResumeNotFoundError``.
        """
        token = str(uuid4())
        eligible = Resume.processing_status.in_(("failed", "processing"))
        if allow_ready_at is not None:
            eligible = or_(
                eligible,
                and_(
                    Resume.processing_status == "ready",
                    Resume.updated_at == allow_ready_at,
                ),
            )

        async with self._write_session() as session:
            result = await session.execute(
                update(Resume)
                .where(Resume.resume_id == resume_id, eligible)
                .values(
                    processing_status="processing",
                    processing_token=token,
                    updated_at=_now(),
                )
            )
            if result.rowcount == 1:
                await session.commit()
                return token

            exists = await session.scalar(
                select(Resume.resume_id).where(Resume.resume_id == resume_id)
            )
            if exists is None:
                raise ResumeNotFoundError(resume_id)
            return None

    async def finish_resume_processing(
        self,
        resume_id: str,
        token: str | None,
        *,
        processing_status: Literal["ready", "failed"],
        processed_data: dict[str, Any] | None = None,
    ) -> ProcessingFinishOutcome:
        """Finish an owned attempt, or retire an unclaimed row with ``None``."""
        if token is None and processing_status != "failed":
            raise ValueError("Ready processing requires an ownership token")
        values: dict[str, Any] = {
            "processing_status": processing_status,
            "processing_token": None,
            "updated_at": _now(),
        }
        values["processed_data"] = (
            processed_data if processing_status == "ready" else None
        )

        async with self._write_session() as session:
            result = await session.execute(
                update(Resume)
                .where(
                    Resume.resume_id == resume_id,
                    Resume.processing_token == token,
                    Resume.processing_status == "processing",
                )
                .values(**values)
            )
            if result.rowcount == 1:
                await session.commit()
                return "committed"

            exists = await session.scalar(
                select(Resume.resume_id).where(Resume.resume_id == resume_id)
            )
            return "stale" if exists is not None else "missing"

    async def delete_resume(self, resume_id: str) -> bool:
        """Delete resume by ID."""
        async with self._write_session() as session:
            row = await session.get(Resume, resume_id)
            if row is None:
                return False
            # Keep a content-free consumed marker for deleted results so retries
            # cannot recreate them, while removing their cached personal data.
            previews = await session.execute(
                select(TailoringPreview).where(
                    TailoringPreview.result_resume_id == resume_id,
                )
            )
            for preview in previews.scalars():
                preview.response_data = None
                preview.source_data = None
            await session.execute(
                delete(TailoringPreview).where(TailoringPreview.source_id == resume_id)
            )
            was_default = row.is_default_master
            await session.delete(row)
            if was_default:
                await session.flush()
                successor = (
                    await session.execute(
                        select(Resume)
                        .where(Resume.is_master.is_(True))
                        .order_by(Resume.created_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if successor is not None:
                    successor.is_default_master = True
            await session.commit()
            return True

    async def list_resumes(self) -> list[dict[str, Any]]:
        """List all resumes."""
        async with self._session() as session:
            result = await session.execute(select(Resume).order_by(Resume.created_at))
            return [self._resume_to_dict(row) for row in result.scalars().all()]

    async def set_default_master_resume(self, resume_id: str) -> bool:
        """Make an existing master the default; False if missing or not a master."""
        async with self._write_session() as session:
            target = await session.get(Resume, resume_id)
            if target is None or not target.is_master:
                logger.warning(
                    "Cannot set default master: %s is not a master", resume_id
                )
                return False
            current = await session.execute(
                select(Resume).where(Resume.is_default_master.is_(True))
            )
            for row in current.scalars().all():
                if row.resume_id != resume_id:
                    row.is_default_master = False
            # Flush the demotion before promoting to satisfy the unique index.
            await session.flush()
            target.is_default_master = True
            await session.commit()
            return True

    # -- Job operations -----------------------------------------------------

    async def create_job(
        self,
        content: str,
        resume_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one job description; ``metadata`` is stored with it in one transaction."""
        row = Job(
            job_id=str(uuid4()),
            content=content,
            resume_id=resume_id,
            created_at=_now(),
            metadata_json=copy.deepcopy(metadata) if metadata else {},
        )
        async with self._write_session() as session:
            session.add(row)
            await session.commit()
        return self._job_to_dict(row)

    async def create_jobs(
        self, contents: list[str], resume_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Persist a validated job-description batch atomically, in input order."""
        rows = [
            Job(
                job_id=str(uuid4()),
                content=content,
                resume_id=resume_id,
                created_at=_now(),
                metadata_json={},
            )
            for content in contents
        ]
        async with self._write_session() as session:
            session.add_all(rows)
            await session.commit()
        return [self._job_to_dict(row) for row in rows]

    async def create_or_get_jobhunt_job(
        self,
        *,
        job_key: str,
        content: str,
        content_sha256: str,
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Create one durable Matcher job per JobHunt key and description version."""
        async with self._write_session() as session:
            link = await session.scalar(
                select(JobHuntJobLink).where(
                    JobHuntJobLink.job_key == job_key,
                    JobHuntJobLink.content_sha256 == content_sha256,
                )
            )
            if link is not None:
                job = await session.get(Job, link.job_id)
                if job is None:
                    raise RuntimeError("JobHunt link references a missing Matcher job")
                return self._job_to_dict(job), False

            job_id = str(uuid4())
            job = Job(
                job_id=job_id,
                content=content,
                resume_id=None,
                created_at=_now(),
                metadata_json=copy.deepcopy(metadata),
            )
            session.add(job)
            session.add(
                JobHuntJobLink(
                    link_id=str(uuid4()),
                    job_key=job_key,
                    content_sha256=content_sha256,
                    job_id=job_id,
                    created_at=_now(),
                )
            )
            await session.commit()
            return self._job_to_dict(job), True

    async def get_jobhunt_prepared_description(
        self, job_key: str
    ) -> dict[str, Any] | None:
        """Return the latest manually supplied full description for a source listing."""
        async with self._session() as session:
            result = await session.execute(
                select(Job)
                .join(JobHuntJobLink, JobHuntJobLink.job_id == Job.job_id)
                .where(JobHuntJobLink.job_key == job_key)
                .order_by(JobHuntJobLink.created_at.desc())
            )
            for job in result.scalars():
                metadata = job.metadata_json or {}
                if not isinstance(metadata, dict):
                    continue
                if metadata.get("description_status") in {"complete", "manual"}:
                    return {
                        "description_text": job.content,
                        "description_status": metadata.get("description_status"),
                        "description_method": metadata.get("description_provenance"),
                        "description_retrieved_at": job.created_at,
                    }
        return None

    async def list_jobhunt_discovery_states(
        self, job_keys: list[str]
    ) -> dict[str, str]:
        """Return saved/skipped states for a bounded discovery page."""
        if not job_keys:
            return {}
        async with self._session() as session:
            rows = await session.scalars(
                select(JobHuntDiscoveryState).where(
                    JobHuntDiscoveryState.job_key.in_(job_keys)
                )
            )
            return {row.job_key: row.pipeline_state for row in rows}

    @staticmethod
    def _jobhunt_live_job_to_dict(row: JobHuntLiveJob) -> dict[str, Any]:
        return {
            "job_key": row.job_key,
            "canonical_id": row.canonical_id,
            "provider": row.provider,
            "source": row.source_name,
            "title": row.title,
            "company": row.company,
            "location": row.location,
            "source_url": row.source_url,
            "posted_date": row.posted_date,
            "description_text": row.description_text,
            "description_status": row.description_status,
            "description_method": row.description_method,
            "description_sha256": row.description_sha256,
            "retrieved_at": row.retrieved_at,
            "last_checked_at": row.last_checked_at,
            "salary": row.salary_json,
            "fit_score": row.score,
            "fit_evidence": row.score_json,
            "metadata": row.metadata_json or {},
        }

    async def get_jobhunt_search_profile(self) -> dict[str, Any] | None:
        async with self._session() as session:
            row = await session.get(JobHuntSearchProfile, 1)
            return dict(row.profile_json) if row is not None else None

    async def save_jobhunt_search_profile(
        self, profile: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._write_session() as session:
            row = await session.get(JobHuntSearchProfile, 1)
            if row is None:
                row = JobHuntSearchProfile(profile_id=1, profile_json=profile)
                session.add(row)
            else:
                row.profile_json = profile
                row.updated_at = _now()
            await session.commit()
            return dict(row.profile_json)

    @staticmethod
    def _jobhunt_saved_search_to_dict(row: JobHuntSavedSearch) -> dict[str, Any]:
        return {
            "search_id": row.search_id,
            "name": row.name,
            "query": row.query,
            "filters": dict(row.filters_json or {}),
            "enabled": row.enabled,
            "interval_minutes": row.interval_minutes,
            "next_run_at": row.next_run_at,
            "last_run_at": row.last_run_at,
            "last_run_status": row.last_run_status,
            "last_run_id": row.last_run_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def create_jobhunt_saved_search(
        self,
        *,
        name: str,
        query: str | None,
        filters: dict[str, Any],
        enabled: bool,
        interval_minutes: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        async with self._write_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(JobHuntSavedSearch)
            )
            if int(count or 0) >= 20:
                raise ValueError("At most 20 saved searches are allowed")
            row = JobHuntSavedSearch(
                search_id=str(uuid4()),
                name=name,
                query=query,
                filters_json=filters,
                enabled=enabled,
                interval_minutes=interval_minutes,
                next_run_at=(now + timedelta(minutes=interval_minutes)).isoformat()
                if enabled
                else None,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            session.add(row)
            await session.commit()
            return self._jobhunt_saved_search_to_dict(row)

    async def get_jobhunt_saved_search(self, search_id: str) -> dict[str, Any] | None:
        async with self._session() as session:
            row = await session.get(JobHuntSavedSearch, search_id)
            return self._jobhunt_saved_search_to_dict(row) if row else None

    async def list_jobhunt_saved_searches(self) -> list[dict[str, Any]]:
        async with self._session() as session:
            rows = await session.scalars(
                select(JobHuntSavedSearch).order_by(
                    JobHuntSavedSearch.created_at.desc()
                )
            )
            return [self._jobhunt_saved_search_to_dict(row) for row in rows]

    async def update_jobhunt_saved_search(
        self, search_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._write_session() as session:
            row = await session.get(JobHuntSavedSearch, search_id)
            if row is None:
                return None
            reset_schedule = "interval_minutes" in changes
            if "name" in changes:
                row.name = changes["name"]
            if "query" in changes:
                row.query = changes["query"]
            if "filters" in changes:
                row.filters_json = changes["filters"]
            if "interval_minutes" in changes:
                row.interval_minutes = changes["interval_minutes"]
            if "enabled" in changes:
                row.enabled = changes["enabled"]
                reset_schedule = True
            if not row.enabled:
                row.next_run_at = None
            elif reset_schedule or row.next_run_at is None:
                row.next_run_at = (
                    now + timedelta(minutes=row.interval_minutes)
                ).isoformat()
            row.updated_at = now.isoformat()
            await session.commit()
            return self._jobhunt_saved_search_to_dict(row)

    async def delete_jobhunt_saved_search(self, search_id: str) -> bool:
        async with self._write_session() as session:
            row = await session.get(JobHuntSavedSearch, search_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def claim_due_jobhunt_saved_searches(
        self, *, limit: int = 3
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self._write_session() as session:
            rows = await session.scalars(
                select(JobHuntSavedSearch)
                .where(
                    JobHuntSavedSearch.enabled.is_(True),
                    JobHuntSavedSearch.next_run_at.is_not(None),
                    JobHuntSavedSearch.next_run_at <= now_iso,
                )
                .order_by(JobHuntSavedSearch.next_run_at.asc())
                .limit(limit)
            )
            due = list(rows)
            for row in due:
                row.next_run_at = (
                    now + timedelta(minutes=row.interval_minutes)
                ).isoformat()
                row.updated_at = now_iso
            await session.commit()
            return [self._jobhunt_saved_search_to_dict(row) for row in due]

    async def finish_jobhunt_saved_search(
        self, *, search_id: str, run_id: str | None, status: str
    ) -> None:
        async with self._write_session() as session:
            row = await session.get(JobHuntSavedSearch, search_id)
            if row is None:
                return
            row.last_run_at = _now()
            row.last_run_status = status
            row.last_run_id = run_id
            row.updated_at = _now()
            await session.commit()

    async def create_jobhunt_search_run(
        self, *, run_id: str, queries: list[str], filters: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._write_session() as session:
            row = JobHuntSearchRun(
                run_id=run_id,
                queries_json=queries,
                filters_json=filters,
                status="running",
                started_at=_now(),
            )
            session.add(row)
            await session.commit()
            return {"run_id": row.run_id, "started_at": row.started_at}

    async def finish_jobhunt_search_run(
        self,
        *,
        run_id: str,
        status: str,
        found_count: int,
        new_count: int,
        errors: list[str],
    ) -> None:
        async with self._write_session() as session:
            row = await session.get(JobHuntSearchRun, run_id)
            if row is None:
                return
            row.status = status
            row.found_count = found_count
            row.new_count = new_count
            row.errors_json = errors
            row.finished_at = _now()
            await session.commit()

    async def save_jobhunt_live_jobs(
        self, jobs: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Idempotently upsert external postings without losing good descriptions."""
        saved: list[dict[str, Any]] = []
        new_count = 0
        async with self._write_session() as session:
            for item in jobs:
                row = await session.get(JobHuntLiveJob, item["job_key"])
                if row is None:
                    row = JobHuntLiveJob(
                        job_key=item["job_key"],
                        provider=item["provider"],
                        canonical_id=item["canonical_id"],
                        source_name=item.get("source"),
                        title=item["title"],
                        company=item.get("company"),
                        location=item.get("location"),
                        source_url=item["source_url"],
                        posted_date=item.get("posted_date"),
                        description_text=item.get("description_text"),
                        description_status=item["description_status"],
                        description_method=item["description_method"],
                        description_sha256=item.get("description_sha256"),
                        retrieved_at=item["retrieved_at"],
                        last_checked_at=item["last_checked_at"],
                        salary_json=item.get("salary"),
                        score=item.get("fit_score"),
                        score_json=item.get("fit_evidence"),
                        metadata_json=item.get("metadata") or {},
                    )
                    session.add(row)
                    new_count += 1
                else:
                    row.title = item["title"]
                    row.company = item.get("company")
                    row.location = item.get("location")
                    row.source_url = item["source_url"]
                    row.source_name = item.get("source")
                    row.posted_date = item.get("posted_date")
                    row.salary_json = item.get("salary")
                    row.score = item.get("fit_score")
                    row.score_json = item.get("fit_evidence")
                    row.metadata_json = item.get("metadata") or {}
                    if item.get("description_text"):
                        old_text = row.description_text or ""
                        incoming_text = item["description_text"]
                        if (
                            row.description_status != "complete"
                            or item["description_status"] == "complete"
                            and len(incoming_text) >= len(old_text)
                        ):
                            row.description_text = incoming_text
                            row.description_status = item["description_status"]
                            row.description_sha256 = item.get("description_sha256")
                            row.description_method = item["description_method"]
                            row.retrieved_at = item["retrieved_at"]
                    row.last_checked_at = item["last_checked_at"]
                    row.updated_at = _now()
                saved.append(row)
            await session.flush()
            result = [self._jobhunt_live_job_to_dict(row) for row in saved]
            await session.commit()
        return result, new_count

    async def get_jobhunt_live_job(self, job_key: str) -> dict[str, Any] | None:
        async with self._session() as session:
            row = await session.get(JobHuntLiveJob, job_key)
            return self._jobhunt_live_job_to_dict(row) if row is not None else None

    async def list_jobhunt_live_jobs(
        self,
        *,
        query: str | None = None,
        location: str | None = None,
        min_score: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = []
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(
                or_(
                    JobHuntLiveJob.title.ilike(pattern),
                    JobHuntLiveJob.company.ilike(pattern),
                    JobHuntLiveJob.description_text.ilike(pattern),
                )
            )
        if location:
            conditions.append(JobHuntLiveJob.location.ilike(f"%{location.strip()}%"))
        if min_score is not None:
            conditions.append(JobHuntLiveJob.score >= min_score)
        async with self._session() as session:
            count = await session.scalar(
                select(func.count()).select_from(JobHuntLiveJob).where(*conditions)
            )
            rows = await session.scalars(
                select(JobHuntLiveJob)
                .where(*conditions)
                .order_by(
                    JobHuntLiveJob.score.desc().nullslast(),
                    JobHuntLiveJob.posted_date.desc().nullslast(),
                    JobHuntLiveJob.last_checked_at.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return (
                [self._jobhunt_live_job_to_dict(row) for row in rows],
                int(count or 0),
            )

    async def list_jobhunt_search_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session() as session:
            rows = await session.scalars(
                select(JobHuntSearchRun)
                .order_by(JobHuntSearchRun.started_at.desc())
                .limit(limit)
            )
            return [
                {
                    "run_id": row.run_id,
                    "provider": row.provider,
                    "queries": row.queries_json,
                    "filters": row.filters_json,
                    "status": row.status,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                    "found_count": row.found_count,
                    "new_count": row.new_count,
                    "errors": row.errors_json,
                }
                for row in rows
            ]

    async def set_jobhunt_discovery_state(
        self, job_key: str, pipeline_state: Literal["saved", "skipped"]
    ) -> dict[str, str]:
        """Persist user-owned discovery state independently of the source snapshot."""
        async with self._write_session() as session:
            row = await session.get(JobHuntDiscoveryState, job_key)
            if row is None:
                row = JobHuntDiscoveryState(
                    job_key=job_key,
                    pipeline_state=pipeline_state,
                    updated_at=_now(),
                )
                session.add(row)
            else:
                row.pipeline_state = pipeline_state
                row.updated_at = _now()
            await session.commit()
            return {"job_key": row.job_key, "pipeline_state": row.pipeline_state}

    async def get_application_by_legacy_id(self, job_key: str) -> dict[str, Any] | None:
        """Find the tracker card linked to a canonical JobHunt key."""
        async with self._session() as session:
            row = await session.scalar(
                select(Application).where(Application.legacy_application_id == job_key)
            )
            return self._application_to_dict(row) if row is not None else None

    async def skip_jobhunt_discovery_job(self, job_key: str) -> bool:
        """Mark a listing skipped unless a concurrent save already created its card."""
        async with self._write_session() as session:
            application = await session.scalar(
                select(Application).where(Application.legacy_application_id == job_key)
            )
            if application is not None:
                return False
            state = await session.get(JobHuntDiscoveryState, job_key)
            if state is None:
                state = JobHuntDiscoveryState(
                    job_key=job_key, pipeline_state="skipped", updated_at=_now()
                )
                session.add(state)
            else:
                state.pipeline_state = "skipped"
                state.updated_at = _now()
            await session.commit()
            return True

    async def import_jobhunt_application(
        self,
        *,
        job_key: str,
        content: str | None,
        metadata: dict[str, Any],
        status: str,
        company: str | None,
        role: str,
        applied_at: str | None,
        notes: str | None,
        follow_up_date: str | None,
        confirmed: bool,
        legacy_status: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create a historical JobHunt job and its linked tracker card."""
        async with self._write_session() as session:
            existing = await session.scalar(
                select(Application).where(Application.legacy_application_id == job_key)
            )
            if existing is not None:
                state = await session.get(JobHuntDiscoveryState, job_key)
                if state is None:
                    session.add(
                        JobHuntDiscoveryState(
                            job_key=job_key,
                            pipeline_state="saved",
                            updated_at=_now(),
                        )
                    )
                else:
                    state.pipeline_state = "saved"
                    state.updated_at = _now()
                await session.commit()
                return self._application_to_dict(existing), False
            job = Job(
                job_id=str(uuid4()),
                content=content,
                resume_id=None,
                created_at=_now(),
                metadata_json=copy.deepcopy(metadata),
            )
            session.add(job)
            await session.flush()
            application = await self._insert_application(
                session,
                job_id=job.job_id,
                status=status,
                company=company,
                role=role,
                applied_at=applied_at,
                notes=notes,
                legacy_application_id=job_key,
                legacy_status=legacy_status,
                follow_up_date=follow_up_date,
                confirmed=confirmed,
            )
            session.add(
                JobHuntDiscoveryState(
                    job_key=job_key,
                    pipeline_state="saved",
                    updated_at=_now(),
                )
            )
            await session.commit()
            return self._application_to_dict(application), True

    async def attach_jobhunt_tailored_resume(
        self,
        *,
        job_key: str,
        job_id: str,
        resume_id: str,
        master_resume_id: str,
        company: str | None,
        role: str | None,
    ) -> dict[str, Any]:
        """Attach a tailored resume to the saved source card without marking it applied."""
        async with self._write_session() as session:
            row = await session.scalar(
                select(Application).where(Application.legacy_application_id == job_key)
            )
            if row is None:
                row = await self._insert_application(
                    session,
                    job_id=job_id,
                    resume_id=resume_id,
                    master_resume_id=master_resume_id,
                    status="saved",
                    company=company,
                    role=role,
                    legacy_application_id=job_key,
                )
            else:
                row.job_id = job_id
                row.resume_id = resume_id
                row.master_resume_id = master_resume_id
                row.company = company
                row.role = role
                row.updated_at = _now()
                if row.status == "saved":
                    row.applied_at = None
            state = await session.get(JobHuntDiscoveryState, job_key)
            if state is None:
                session.add(
                    JobHuntDiscoveryState(
                        job_key=job_key,
                        pipeline_state="saved",
                        updated_at=_now(),
                    )
                )
            else:
                state.pipeline_state = "saved"
                state.updated_at = _now()
            await session.commit()
            return self._application_to_dict(row)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get job by ID (dynamic fields flattened to top level)."""
        async with self._session() as session:
            row = await session.get(Job, job_id)
            return self._job_to_dict(row) if row else None

    async def update_job(
        self, job_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a job by ID.

        Core columns are set directly; every other key is merged into
        ``metadata_json`` so dynamic pipeline fields (``preview_hash``,
        ``job_keywords``, ``company``/``role``, …) round-trip through
        ``get_job`` as top-level keys.
        """
        async with self._write_session() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return None
            meta = dict(row.metadata_json or {})
            for key, value in updates.items():
                if key in _JOB_CORE_FIELDS:
                    setattr(row, key, value)
                else:
                    meta[key] = value
            # Reassign so SQLAlchemy detects the JSON mutation.
            row.metadata_json = meta
            await session.commit()
            return self._job_to_dict(row)

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID (used to clean up an orphaned manual-add job)."""
        async with self._write_session() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return False
            await session.execute(
                delete(TailoringPreview).where(TailoringPreview.job_id == job_id)
            )
            await session.delete(row)
            await session.commit()
            return True

    # -- Preview and confirmation operations --------------------------------

    @staticmethod
    async def _validate_preview_inputs(
        session: AsyncSession,
        preview: TailoringPreview,
    ) -> None:
        source = await session.get(Resume, preview.source_id)
        job = await session.get(Job, preview.job_id)
        if (
            source is None
            or job is None
            or resume_fingerprint(
                source.content, source.processed_data, source.original_markdown
            )
            != preview.source_hash
            or job_fingerprint(job.content) != preview.job_hash
        ):
            raise PreviewConflictError(
                "Resume or job description changed. Please retry preview."
            )

    async def register_preview(
        self,
        *,
        source_id: str,
        job_id: str,
        payload_hash: str,
        source_hash: str,
        job_hash: str,
        prompt_id: str,
        ttl_seconds: int,
        improvements: list[dict[str, Any]] | None = None,
        source_data: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Register the exact input/output snapshot before acknowledging preview."""
        now = _now()
        row = TailoringPreview(
            preview_id=str(uuid4()),
            improvements=copy.deepcopy(improvements or []),
            source_data=copy.deepcopy(source_data),
            source_id=source_id,
            job_id=job_id,
            payload_hash=payload_hash,
            source_hash=source_hash,
            job_hash=job_hash,
            created_at=now,
            expires_at=(
                datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)
            ).isoformat(),
        )
        async with self._write_session() as session:
            await self._validate_preview_inputs(session, row)
            await session.execute(
                delete(TailoringPreview).where(
                    TailoringPreview.expires_at <= now,
                    TailoringPreview.result_resume_id.is_(None),
                    or_(
                        TailoringPreview.claim_token.is_(None),
                        TailoringPreview.claim_expires_at <= now,
                    ),
                )
            )
            job = await session.get(Job, job_id)
            assert job is not None  # Validated in the same reserved transaction.
            metadata = dict(job.metadata_json or {})
            hashes = metadata.get("preview_hashes")
            hashes = dict(hashes) if isinstance(hashes, dict) else {}
            hashes[prompt_id] = payload_hash
            metadata.update(
                preview_hash=payload_hash,
                preview_prompt_id=prompt_id,
                preview_hashes=hashes,
            )
            job.metadata_json = metadata
            session.add(row)
            await session.commit()
        return {"preview_id": row.preview_id, "expires_at": row.expires_at}

    async def claim_preview(
        self,
        *,
        preview_id: str | None,
        source_id: str,
        job_id: str,
        payload_hash: str,
        lease_seconds: int,
    ) -> PreviewClaim:
        """Claim once across workers; committed retries bypass generation."""
        async with self._write_session() as session:
            if preview_id:
                row = await session.get(TailoringPreview, preview_id)
            else:
                # Compatibility for clients that omit the new operation ID.
                row = (
                    await session.execute(
                        select(TailoringPreview)
                        .where(
                            TailoringPreview.source_id == source_id,
                            TailoringPreview.job_id == job_id,
                            TailoringPreview.payload_hash == payload_hash,
                            or_(
                                TailoringPreview.result_resume_id.is_not(None),
                                TailoringPreview.expires_at > _now(),
                            ),
                        )
                        .order_by(
                            TailoringPreview.result_resume_id.is_not(None).desc(),
                            TailoringPreview.created_at.desc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if row is None:
                raise PreviewValidationError(
                    "Preview required before confirmation. Please retry preview."
                )
            if row.source_id != source_id or row.job_id != job_id:
                raise PreviewConflictError(
                    "Preview belongs to different inputs. Please retry preview."
                )
            if row.payload_hash != payload_hash:
                raise PreviewValidationError(
                    "Invalid improved resume data. Please retry preview."
                )
            if row.result_resume_id is not None:
                if (
                    row.response_data is None
                    or await session.get(Resume, row.result_resume_id) is None
                ):
                    raise PreviewConflictError(
                        "Confirmed resume was deleted. Please retry preview."
                    )
                return PreviewClaim(
                    row.preview_id, response=copy.deepcopy(row.response_data)
                )
            now = _now()
            if row.expires_at <= now:
                raise PreviewConflictError("Preview expired. Please retry preview.")
            await self._validate_preview_inputs(session, row)
            if row.claim_token and row.claim_expires_at and row.claim_expires_at > now:
                raise PreviewBusyError(
                    "Confirmation is already in progress. Please retry shortly."
                )
            row.claim_token = str(uuid4())
            row.claim_expires_at = (
                datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)
            ).isoformat()
            await session.commit()
            return PreviewClaim(
                row.preview_id,
                token=row.claim_token,
                improvements=copy.deepcopy(row.improvements or []),
                source_data=copy.deepcopy(row.source_data),
            )

    async def release_preview_claim(self, claim: PreviewClaim) -> None:
        """Release only this request's uncommitted claim, including on cancellation."""
        if not claim.token:
            return
        async with self._write_session() as session:
            row = await session.get(TailoringPreview, claim.preview_id)
            if row is not None and claim.token and row.claim_token == claim.token:
                row.claim_token = None
                row.claim_expires_at = None
                await session.commit()

    async def complete_preview(
        self,
        *,
        claim: PreviewClaim,
        resume_fields: dict[str, Any],
        response_data: dict[str, Any],
        improvements: list[dict[str, Any]],
        ats_score: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit resume, required relation and replay snapshot atomically."""
        async with self._write_session() as session:
            preview = await session.get(TailoringPreview, claim.preview_id)
            now = _now()
            if (
                preview is None
                or not claim.token
                or preview.claim_token != claim.token
                or not preview.claim_expires_at
                or preview.claim_expires_at <= now
            ):
                raise PreviewConflictError(
                    "Confirmation ownership expired. Please retry preview."
                )
            await self._validate_preview_inputs(session, preview)
            row = self._new_resume(**resume_fields)
            result = copy.deepcopy(response_data)
            result.update(
                resume_id=row.resume_id,
                preview_id=preview.preview_id,
                preview_expires_at=preview.expires_at,
            )
            session.add(row)
            await session.flush()
            session.add(
                Improvement(
                    request_id=result["request_id"],
                    original_resume_id=preview.source_id,
                    tailored_resume_id=row.resume_id,
                    job_id=preview.job_id,
                    improvements=improvements,
                    ats_score=copy.deepcopy(ats_score),
                    created_at=now,
                )
            )
            preview.response_data = result
            # Replays read response_data only; drop the condensed master copy (personal data).
            preview.source_data = None
            preview.result_resume_id = row.resume_id
            preview.claim_token = None
            preview.claim_expires_at = None
            await session.commit()
            return result

    # -- Improvement operations ---------------------------------------------

    async def create_tailored_resume(
        self,
        *,
        request_id: str,
        original_resume_id: str,
        job_id: str,
        resume_fields: dict[str, Any],
        improvements: list[dict[str, Any]],
        ats_score: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a direct tailoring result and its required relation together."""
        row = self._new_resume(**resume_fields)
        async with self._write_session() as session:
            session.add(row)
            await session.flush()
            session.add(
                Improvement(
                    request_id=request_id,
                    original_resume_id=original_resume_id,
                    tailored_resume_id=row.resume_id,
                    job_id=job_id,
                    improvements=copy.deepcopy(improvements),
                    ats_score=copy.deepcopy(ats_score),
                    created_at=_now(),
                )
            )
            await session.commit()
        return self._resume_to_dict(row)

    async def create_improvement(
        self,
        original_resume_id: str,
        tailored_resume_id: str,
        job_id: str,
        improvements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create an improvement result entry."""
        request_id = str(uuid4())
        now = _now()
        async with self._write_session() as session:
            session.add(
                Improvement(
                    request_id=request_id,
                    original_resume_id=original_resume_id,
                    tailored_resume_id=tailored_resume_id,
                    job_id=job_id,
                    improvements=improvements,
                    created_at=now,
                )
            )
            await session.commit()
        return {
            "request_id": request_id,
            "original_resume_id": original_resume_id,
            "tailored_resume_id": tailored_resume_id,
            "job_id": job_id,
            "improvements": improvements,
            "created_at": now,
        }

    async def get_improvement_by_job(self, job_id: str) -> dict[str, Any] | None:
        """Get the latest improvement record tailored against a job, if any."""
        async with self._session() as session:
            result = await session.execute(
                select(Improvement)
                .where(Improvement.job_id == job_id)
                .order_by(Improvement.created_at.desc())
                .limit(1)
            )
            row = result.scalars().first()
            return self._improvement_to_dict(row) if row else None

    async def get_improvement_by_tailored_resume(
        self, tailored_resume_id: str
    ) -> dict[str, Any] | None:
        """Get improvement record by tailored resume ID."""
        async with self._session() as session:
            result = await session.execute(
                select(Improvement).where(
                    Improvement.tailored_resume_id == tailored_resume_id
                )
            )
            row = result.scalars().first()
            return self._improvement_to_dict(row) if row else None

    async def set_ats_score(
        self, tailored_resume_id: str, ats_score: dict[str, Any]
    ) -> bool:
        """Store the last calculated ATS score on a tailored resume's job link.

        Returns False when the resume has no improvement (job) link.
        """
        async with self._write_session() as session:
            result = await session.execute(
                select(Improvement).where(
                    Improvement.tailored_resume_id == tailored_resume_id
                )
            )
            row = result.scalars().first()
            if row is None:
                return False
            row.ats_score = copy.deepcopy(ats_score)
            await session.commit()
            return True

    # -- Application (tracker) operations -----------------------------------

    async def _next_position(self, session: AsyncSession, status: str) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Application)
            .where(Application.status == status)
        )
        return int(result.scalar() or 0)

    async def _renumber(self, session: AsyncSession, status: str) -> None:
        """Renumber a column's positions to a contiguous 0..n-1 sequence."""
        result = await session.execute(
            select(Application)
            .where(Application.status == status)
            .order_by(Application.position, Application.created_at)
        )
        for index, row in enumerate(result.scalars().all()):
            if row.position != index:
                row.position = index

    async def _insert_application(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        resume_id: str | None = None,
        master_resume_id: str | None = None,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        applied_at: str | None = None,
        notes: str | None = None,
        legacy_application_id: str | None = None,
        legacy_status: str | None = None,
        follow_up_date: str | None = None,
        confirmed: bool = False,
    ) -> Application:
        """Stage one card with shared date and ordering rules, without committing."""
        now = _now()
        if applied_at is None and status != "saved":
            applied_at = now
        position = await self._next_position(session, status)
        row = Application(
            application_id=str(uuid4()),
            job_id=job_id,
            resume_id=resume_id,
            master_resume_id=master_resume_id,
            status=status,
            company=company,
            role=role,
            applied_at=applied_at,
            notes=notes,
            position=position,
            legacy_application_id=legacy_application_id,
            legacy_status=legacy_status,
            follow_up_date=follow_up_date,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        event = ApplicationEvent(
            event_id=str(uuid4()),
            application_id=row.application_id,
            from_status=None,
            to_status=status,
            confirmed=confirmed,
            note=None,
            created_at=now,
        )
        session.add(event)
        return row

    async def create_manual_application(
        self,
        *,
        content: str,
        resume_id: str,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Commit a pasted job and its tracker card together, or roll back both."""
        async with self._write_session() as session:
            job = Job(
                job_id=str(uuid4()),
                content=content,
                resume_id=resume_id,
                created_at=_now(),
                metadata_json=(
                    {"company": company, "role": role} if company or role else {}
                ),
            )
            session.add(job)
            await session.flush()
            row = await self._insert_application(
                session,
                job_id=job.job_id,
                resume_id=resume_id,
                status=status,
                company=company,
                role=role,
                notes=notes,
                confirmed=confirmed,
            )
            await session.commit()
            return self._application_to_dict(row)

    async def create_application(
        self,
        job_id: str,
        resume_id: str | None = None,
        master_resume_id: str | None = None,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        applied_at: str | None = None,
        notes: str | None = None,
        legacy_application_id: str | None = None,
        legacy_status: str | None = None,
        follow_up_date: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Create a tracker card, deduped on (job_id, resume_id).

        If a card for the same job+resume already exists it is returned as-is
        (survives double-submit / retried confirms).
        """
        where_clause = [Application.job_id == job_id]
        if resume_id is None:
            where_clause.append(Application.resume_id.is_(None))
        else:
            where_clause.append(Application.resume_id == resume_id)

        # A replay needs only a read. Recheck under the reservation before an
        # insert so concurrent new cards still share the position allocation.
        async with self._session() as session:
            found = await session.scalar(select(Application).where(*where_clause))
            if found is not None:
                return self._application_to_dict(found)
        async with self._write_session() as session:
            existing = await session.execute(select(Application).where(*where_clause))
            found = existing.scalars().first()
            if found is not None:
                return self._application_to_dict(found)

            row = await self._insert_application(
                session,
                job_id=job_id,
                resume_id=resume_id,
                master_resume_id=master_resume_id,
                status=status,
                company=company,
                role=role,
                applied_at=applied_at,
                notes=notes,
                legacy_application_id=legacy_application_id,
                legacy_status=legacy_status,
                follow_up_date=follow_up_date,
                confirmed=confirmed,
            )
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent create won the unique constraint — return the existing card instead of duplicating.
                await session.rollback()
                dup = await session.execute(select(Application).where(*where_clause))
                found = dup.scalars().first()
                if found is not None:
                    logger.debug(
                        "Deduped concurrent application create for job=%s resume=%s",
                        job_id,
                        resume_id,
                    )
                    return self._application_to_dict(found)
                raise
            return self._application_to_dict(row)

    async def list_applications(
        self, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List applications ordered by (status, position)."""
        async with self._session() as session:
            stmt = select(Application)
            if status is not None:
                stmt = stmt.where(Application.status == status)
            stmt = stmt.order_by(Application.status, Application.position)
            result = await session.execute(stmt)
            return [self._application_to_dict(row) for row in result.scalars().all()]

    async def get_application(self, application_id: str) -> dict[str, Any] | None:
        """Get an application by ID."""
        async with self._session() as session:
            row = await session.get(Application, application_id)
            return self._application_to_dict(row) if row else None

    async def update_application(
        self, application_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an application; renumber columns when status/position change.

        ``position`` is interpreted as the desired index within the (possibly
        new) ``status`` column; siblings are renumbered server-side so the
        column stays a contiguous 0..n-1 sequence.
        """
        async with self._write_session() as session:
            row = await session.get(Application, application_id)
            if row is None:
                return None

            old_status = row.status
            new_status = updates.get("status", old_status)
            target_position = updates.get("position", None)
            confirmed = bool(updates.get("confirmed", False))
            note = updates.get("note", None)

            for key in ("company", "role", "applied_at", "notes", "follow_up_date"):
                if key in updates:
                    setattr(row, key, updates[key])

            if (
                old_status == "saved"
                and new_status != "saved"
                and row.applied_at is None
                and "applied_at" not in updates
            ):
                row.applied_at = _now()

            status_changed = old_status != new_status
            if status_changed:
                row.legacy_status = None
                event = ApplicationEvent(
                    event_id=str(uuid4()),
                    application_id=row.application_id,
                    from_status=old_status,
                    to_status=new_status,
                    confirmed=confirmed,
                    note=note,
                    created_at=_now(),
                )
                session.add(event)

            moved = "status" in updates or "position" in updates
            if moved:
                row.status = new_status
                # Park it out of the way, renumber both columns, then reinsert.
                row.position = 10_000_000
                await session.flush()
                if old_status != new_status:
                    await self._renumber(session, old_status)
                # Renumber the target column excluding this row, then splice in.
                siblings = await session.execute(
                    select(Application)
                    .where(
                        Application.status == new_status,
                        Application.application_id != application_id,
                    )
                    .order_by(Application.position, Application.created_at)
                )
                ordered = list(siblings.scalars().all())
                if target_position is None or target_position > len(ordered):
                    target_position = len(ordered)
                if target_position < 0:
                    target_position = 0
                ordered.insert(target_position, row)
                for index, item in enumerate(ordered):
                    item.position = index

            row.updated_at = _now()
            await session.commit()
            return self._application_to_dict(row)

    async def bulk_update_applications(
        self,
        application_ids: list[str],
        status: str,
        confirmed: bool = False,
    ) -> int:
        """Move many applications to the end of ``status``. Returns count moved."""
        moved = 0
        now = _now()
        async with self._write_session() as session:
            affected_old: set[str] = set()
            for application_id in application_ids:
                row = await session.get(Application, application_id)
                if row is None:
                    continue
                old_status = row.status
                if old_status != status:
                    row.legacy_status = None
                    event = ApplicationEvent(
                        event_id=str(uuid4()),
                        application_id=row.application_id,
                        from_status=old_status,
                        to_status=status,
                        confirmed=confirmed,
                        note=None,
                        created_at=now,
                    )
                    session.add(event)
                affected_old.add(old_status)
                if (
                    old_status == "saved"
                    and status != "saved"
                    and row.applied_at is None
                ):
                    row.applied_at = now
                row.status = status
                row.position = 20_000_000 + moved  # provisional, renumbered below
                row.updated_at = now
                moved += 1
            await session.flush()
            for old_status in affected_old - {status}:
                await self._renumber(session, old_status)
            await self._renumber(session, status)
            await session.commit()
        return moved

    async def track_discovery_job(
        self,
        *,
        job_key: str,
        title: str,
        company: str | None = None,
        source_url: str,
        location: str | None = None,
        posted_date: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Track a discovery snapshot job into the saved column idempotently."""
        # Link the tracker card and discovery state under one writer reservation.
        async with self._write_session() as session:
            existing = await session.scalar(
                select(Application).where(Application.legacy_application_id == job_key)
            )
            if existing is not None:
                state = await session.get(JobHuntDiscoveryState, job_key)
                if state is None:
                    session.add(
                        JobHuntDiscoveryState(
                            job_key=job_key,
                            pipeline_state="saved",
                            updated_at=_now(),
                        )
                    )
                else:
                    state.pipeline_state = "saved"
                    state.updated_at = _now()
                await session.commit()
                return self._application_to_dict(existing)

            meta = {
                "source": "jobhunt",
                "source_url": source_url,
                "title": title,
                "company": company,
                "role": title,
                "location": location,
                "posted_date": posted_date,
                "jobhunt_key": job_key,
            }
            job_row = Job(
                job_id=str(uuid4()),
                content=description,
                resume_id=None,
                created_at=_now(),
                metadata_json=meta,
            )
            session.add(job_row)
            await session.flush()

            app_row = await self._insert_application(
                session,
                job_id=job_row.job_id,
                resume_id=None,
                master_resume_id=None,
                status="saved",
                company=company,
                role=title,
                applied_at=None,
                notes=None,
                legacy_application_id=job_key,
                legacy_status=None,
                follow_up_date=None,
                confirmed=False,
            )
            session.add(
                JobHuntDiscoveryState(
                    job_key=job_key,
                    pipeline_state="saved",
                    updated_at=_now(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                dup = await session.scalar(
                    select(Application).where(
                        Application.legacy_application_id == job_key
                    )
                )
                if dup is not None:
                    return self._application_to_dict(dup)
                raise
            return self._application_to_dict(app_row)

    async def list_application_events(
        self, application_id: str
    ) -> list[dict[str, Any]]:
        """List audit events for an application in chronological order."""
        async with self._session() as session:
            stmt = (
                select(ApplicationEvent)
                .where(ApplicationEvent.application_id == application_id)
                .order_by(
                    ApplicationEvent.created_at.asc(), ApplicationEvent.event_id.asc()
                )
            )
            result = await session.execute(stmt)
            return [
                self._application_event_to_dict(row) for row in result.scalars().all()
            ]

    async def get_application_outreach_draft(
        self, application_id: str
    ) -> dict[str, Any] | None:
        async with self._session() as session:
            row = await session.get(ApplicationOutreachDraft, application_id)
            if row is None:
                return None
            return {
                "application_id": row.application_id,
                "content": row.content,
                "updated_at": row.updated_at,
            }

    async def save_application_outreach_draft(
        self, application_id: str, content: str
    ) -> dict[str, Any] | None:
        async with self._write_session() as session:
            application = await session.get(Application, application_id)
            if application is None:
                return None
            row = await session.get(ApplicationOutreachDraft, application_id)
            if row is None:
                row = ApplicationOutreachDraft(
                    application_id=application_id,
                    content=content,
                    updated_at=_now(),
                )
                session.add(row)
            else:
                row.content = content
                row.updated_at = _now()
            await session.commit()
            return {
                "application_id": row.application_id,
                "content": row.content,
                "updated_at": row.updated_at,
            }

    async def delete_application(self, application_id: str) -> bool:
        """Delete an application; renumber its column."""
        async with self._write_session() as session:
            row = await session.get(Application, application_id)
            if row is None:
                return False
            status = row.status
            draft = await session.get(ApplicationOutreachDraft, application_id)
            if draft is not None:
                await session.delete(draft)
            await session.delete(row)
            await session.flush()
            await self._renumber(session, status)
            await session.commit()
            return True

    async def bulk_delete_applications(self, application_ids: list[str]) -> int:
        """Delete many applications; renumber affected columns. Returns count."""
        deleted = 0
        async with self._write_session() as session:
            affected: set[str] = set()
            for application_id in application_ids:
                row = await session.get(Application, application_id)
                if row is None:
                    continue
                affected.add(row.status)
                draft = await session.get(ApplicationOutreachDraft, application_id)
                if draft is not None:
                    await session.delete(draft)
                await session.delete(row)
                deleted += 1
            await session.flush()
            for status in affected:
                await self._renumber(session, status)
            await session.commit()
        return deleted

    # -- Encrypted API key store (sync; read on the LLM hot path) -----------

    def get_api_key_ciphertexts(self) -> dict[str, str]:
        """Return ``{provider: ciphertext}`` for all stored keys (sync)."""
        with self._sync() as session:
            rows = session.execute(select(ApiKey)).scalars().all()
            return {row.provider: row.ciphertext for row in rows}

    def set_api_key_ciphertext(self, provider: str, ciphertext: str) -> None:
        """Upsert one provider's ciphertext (sync)."""
        with self._sync_write_session() as session:
            row = session.get(ApiKey, provider)
            if row is None:
                session.add(
                    ApiKey(provider=provider, ciphertext=ciphertext, updated_at=_now())
                )
            else:
                row.ciphertext = ciphertext
                row.updated_at = _now()
            session.commit()

    def delete_api_key(self, provider: str) -> None:
        """Delete one provider's key (sync)."""
        with self._sync_write_session() as session:
            row = session.get(ApiKey, provider)
            if row is not None:
                session.delete(row)
                session.commit()

    def clear_api_keys(self) -> None:
        """Delete all stored keys (sync)."""
        with self._sync_write_session() as session:
            session.execute(delete(ApiKey))
            session.commit()

    def replace_api_keys(self, ciphertexts: dict[str, str]) -> None:
        """Atomically replace the whole key store (clear + insert in one txn).

        A single transaction means a failure mid-write can't leave the store
        half-cleared and wipe a user's previously saved keys.
        """
        with self._sync_write_session() as session:
            session.execute(delete(ApiKey))
            now = _now()
            for provider, ciphertext in ciphertexts.items():
                if ciphertext:
                    session.add(
                        ApiKey(provider=provider, ciphertext=ciphertext, updated_at=now)
                    )
            session.commit()

    # -- Stats / maintenance ------------------------------------------------

    async def get_stats(self) -> dict[str, Any]:
        """Get database statistics."""
        async with self._session() as session:
            resumes = await session.scalar(select(func.count()).select_from(Resume))
            jobs = await session.scalar(select(func.count()).select_from(Job))
            improvements = await session.scalar(
                select(func.count()).select_from(Improvement)
            )
            master = await session.execute(
                select(Resume.resume_id).where(Resume.is_master.is_(True)).limit(1)
            )
            return {
                "backend": self.backend,
                "total_resumes": int(resumes or 0),
                "total_jobs": int(jobs or 0),
                "total_improvements": int(improvements or 0),
                "has_master_resume": master.first() is not None,
            }

    async def reset_database(self) -> None:
        """Reset by truncating user-document tables and clearing uploads.

        Clears resumes/jobs/improvements, preview replay data, and tracker applications (leaving
        orphaned cards after a full data reset would be a bug). Encrypted
        ``api_keys`` are preserved — matching the pre-existing behavior where a
        reset never wiped the user's stored credentials.

        The tailoring worker is stopped while the tables are cleared, so an item
        that is mid-LLM cannot save a job or resume into them afterwards.
        """
        # Imported here because the worker module imports this one.
        from app.services.queue_worker import queue_manager

        async with queue_manager.paused():
            async with self._write_session() as session:
                await session.execute(delete(QueueItem))
                await session.execute(delete(TailoringPreview))
                await session.execute(delete(Application))
                await session.execute(delete(Improvement))
                await session.execute(delete(Job))
                await session.execute(delete(Resume))
                await session.commit()

            uploads_dir = settings.data_dir / "uploads"
            if uploads_dir.exists():
                shutil.rmtree(uploads_dir)
                uploads_dir.mkdir(parents=True, exist_ok=True)

    # -- Tailoring queue -----------------------------------------------------

    async def create_queue_item(
        self,
        item_id: str,
        resume_id: str,
        job_description: str,
        role: str = "",
        company: str = "",
        job_req_id: str = "",
        rate_limit_seconds: float = 5.0,
        prompt_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one pending tailoring request."""
        now = _now()
        row = QueueItem(
            item_id=item_id,
            resume_id=resume_id,
            job_description=job_description,
            role=role,
            company=company,
            job_req_id=job_req_id,
            status="pending",
            rate_limit_seconds=rate_limit_seconds,
            prompt_id=prompt_id,
            created_at=now,
            updated_at=now,
        )
        async with self._write_session() as session:
            session.add(row)
            await session.commit()
        return self._queue_item_to_dict(row)

    async def get_queue_item(self, item_id: str) -> dict[str, Any] | None:
        """Get one queue item by ID."""
        async with self._session() as session:
            row = await session.get(QueueItem, item_id)
            return self._queue_item_to_dict(row) if row else None

    async def get_queue(
        self, resume_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List queue items, newest first, optionally for one master resume."""
        stmt = select(QueueItem)
        if resume_id:
            stmt = stmt.where(QueueItem.resume_id == resume_id)
        stmt = stmt.order_by(
            QueueItem.created_at.desc(), QueueItem.item_id.desc()
        ).limit(limit)
        async with self._session() as session:
            result = await session.execute(stmt)
            return [self._queue_item_to_dict(row) for row in result.scalars().all()]

    async def update_queue_item(
        self, item_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a queue item's columns; None when it no longer exists."""
        async with self._write_session() as session:
            row = await session.get(QueueItem, item_id)
            if row is None:
                return None
            for key, value in updates.items():
                if key != "item_id" and hasattr(QueueItem, key):
                    setattr(row, key, value)
            row.updated_at = _now()
            await session.commit()
            return self._queue_item_to_dict(row)

    async def delete_queue_item(self, item_id: str) -> QueueDeleteOutcome:
        """Delete one queue item, unless the worker is tailoring it right now."""
        async with self._write_session() as session:
            row = await session.get(QueueItem, item_id)
            if row is None:
                return "missing"
            if row.status == "processing":
                return "in_progress"
            await session.delete(row)
            await session.commit()
            return "deleted"

    async def claim_next_queue_item(self) -> dict[str, Any] | None:
        """Mark the oldest pending item ``processing`` and return it; None when idle."""
        async with self._write_session() as session:
            result = await session.execute(
                select(QueueItem)
                .where(QueueItem.status == "pending")
                .order_by(QueueItem.created_at, QueueItem.item_id)
                .limit(1)
            )
            row = result.scalars().first()
            if row is None:
                return None
            row.status = "processing"
            row.updated_at = _now()
            await session.commit()
            return self._queue_item_to_dict(row)

    async def requeue_interrupted_queue_items(self) -> int:
        """Return items a previous run left ``processing`` to ``pending``. Returns count."""
        async with self._write_session() as session:
            result = await session.execute(
                update(QueueItem)
                .where(QueueItem.status == "processing")
                .values(status="pending", updated_at=_now())
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def clear_queue(
        self, status: str | None = None, resume_id: str | None = None
    ) -> int:
        """Delete queue items, optionally by status and master resume. Returns count.

        The item the worker is tailoring is never cleared; it finishes first.
        """
        stmt = delete(QueueItem).where(QueueItem.status != "processing")
        if status:
            stmt = stmt.where(QueueItem.status == status)
        if resume_id:
            stmt = stmt.where(QueueItem.resume_id == resume_id)
        async with self._write_session() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)


# Global database instance
db = Database()
