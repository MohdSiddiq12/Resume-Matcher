"""Engine plumbing for the SQLAlchemy data layer: SQLite by default, PostgreSQL optional.

Every ``Database`` instance owns its own engines (one async for the document
tables, one sync for the encrypted ``api_keys`` table read on the synchronous
LLM hot path) built from these factories. Keeping construction here lets tests
spin up fully isolated engines against a temp-file database or a scratch schema.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import Column, create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateColumn

from app.models import Application, Base, Improvement, Resume

__all__ = [
    "Base",
    "PG_LOCK_NAMESPACE",
    "init_models_sync",
    "make_async_engine",
    "make_sync_engine",
    "rebuild_applications_table_sqlite",
]

# Advisory-lock namespace ("RM"); the second key is hashtext(<schema>).
PG_LOCK_NAMESPACE = 0x524D
PG_INIT_LOCK_NAMESPACE = PG_LOCK_NAMESPACE + 1


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Set per-connection SQLite PRAGMAs.

    WAL improves concurrent read/write between the async (doc tables) and sync
    (api_keys) engines pointed at the same file; ``busy_timeout`` rides out the
    brief lock contention that creates; ``foreign_keys`` enforces relational
    integrity (off by default in SQLite).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _url(path: Path, *, driver: str) -> str:
    """Build a SQLite URL. Absolute paths yield the required four slashes."""
    return f"sqlite+{driver}:///{path}" if driver else f"sqlite:///{path}"


def _postgres_options(schema: str | None) -> dict[str, Any]:
    # READ COMMITTED: reads after the write lock see the previous holder's commit.
    options: dict[str, Any] = {
        "future": True,
        "pool_pre_ping": True,
        "isolation_level": "READ COMMITTED",
    }
    if schema:
        options["connect_args"] = {"options": f"-c search_path={schema}"}
    return options


def make_async_engine(
    path: Path | None = None, *, url: str | None = None, schema: str | None = None
) -> AsyncEngine:
    """Create the async engine (``aiosqlite`` or ``psycopg``) for the document tables."""
    if url:
        return create_async_engine(url, **_postgres_options(schema))
    if path is None:
        raise ValueError("make_async_engine needs either a url or a path.")
    engine = create_async_engine(_url(path, driver="aiosqlite"), future=True)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def make_sync_engine(
    path: Path | None = None, *, url: str | None = None, schema: str | None = None
) -> Engine:
    """Create the sync engine used for the encrypted api_keys table.

    Key reads happen synchronously (``get_llm_config`` → ``load_config_file`` →
    ``resolve_api_key``), so a sync engine avoids threading async through
    ``llm.py``. It points at the same database as the async engine.
    """
    if url:
        return create_engine(url, **_postgres_options(schema))
    if path is None:
        raise ValueError("make_sync_engine needs either a url or a path.")
    engine = create_engine(_url(path, driver=""), future=True)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def _add_missing_column(
    conn: Connection, column: Column[Any], schema: str | None = None
) -> None:
    """Additive migration: add a model column that an existing table lacks.

    ``create_all`` never alters existing tables. The column DDL is rendered by
    the connection's dialect, so this works on SQLite and PostgreSQL alike.
    """
    existing = inspect(conn).get_columns(column.table.name, schema=schema)
    if column.name in {c["name"] for c in existing}:
        return
    table = conn.dialect.identifier_preparer.format_table(column.table)
    ddl = CreateColumn(column).compile(dialect=conn.dialect)
    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _init_postgres(engine: Engine, schema: str | None) -> None:
    """Create the schema and tables once, even when several processes start together."""
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:schema))"),
            {"ns": PG_INIT_LOCK_NAMESPACE, "schema": schema or "public"},
        )
        if schema:
            conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        Base.metadata.create_all(conn)
        _add_missing_column(conn, Improvement.__table__.c.ats_score, schema)
        _add_missing_column(conn, Resume.__table__.c.template_settings, schema)
        _add_missing_column(conn, Application.__table__.c.legacy_application_id, schema)
        _add_missing_column(conn, Application.__table__.c.legacy_status, schema)
        _add_missing_column(conn, Application.__table__.c.follow_up_date, schema)
        try:
            conn.exec_driver_sql(
                "ALTER TABLE applications ALTER COLUMN resume_id DROP NOT NULL"
            )
        except Exception:
            pass
        try:
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_application_job_untailored "
                "ON applications (job_id) WHERE resume_id IS NULL"
            )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_legacy_id "
                "ON applications (legacy_application_id) WHERE legacy_application_id IS NOT NULL"
            )
        except Exception:
            pass


def rebuild_jobs_table_sqlite(conn: Connection) -> dict[str, Any]:
    """Rebuild SQLite jobs table in a transaction to make content nullable if needed."""
    columns_info = conn.exec_driver_sql("PRAGMA table_info(jobs)").mappings().all()
    if not columns_info:
        return {"rebuilt": False, "reason": "table does not exist"}

    content_col = next((c for c in columns_info if c["name"] == "content"), None)
    if not content_col or content_col["notnull"] == 0:
        return {"rebuilt": False, "reason": "already nullable"}

    count_before = conn.exec_driver_sql("SELECT COUNT(*) FROM jobs").scalar() or 0

    conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        conn.exec_driver_sql("""
            CREATE TABLE jobs__new (
                job_id VARCHAR NOT NULL PRIMARY KEY,
                content TEXT,
                resume_id VARCHAR,
                created_at VARCHAR NOT NULL,
                metadata_json JSON NOT NULL
            )
        """)
        conn.exec_driver_sql("""
            INSERT INTO jobs__new (job_id, content, resume_id, created_at, metadata_json)
            SELECT job_id, content, resume_id, created_at, metadata_json FROM jobs
        """)
        conn.exec_driver_sql("DROP TABLE jobs")
        conn.exec_driver_sql("ALTER TABLE jobs__new RENAME TO jobs")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_resume_id ON jobs (resume_id)"
        )
        count_after = conn.exec_driver_sql("SELECT COUNT(*) FROM jobs").scalar() or 0
        if count_after != count_before:
            raise RuntimeError(
                f"Jobs rebuild mismatch: {count_before} vs {count_after}"
            )
        fk_errors = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        if fk_errors:
            raise RuntimeError(f"Jobs foreign key check failed: {fk_errors}")
    finally:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
    return {"rebuilt": True, "count": count_after}


def rebuild_applications_table_sqlite(conn: Connection) -> dict[str, Any]:
    """Rebuild SQLite applications table in a transaction to make resume_id nullable and add tracking fields.

    Verifies row counts and foreign keys before and after; raises RuntimeError on failure.
    """
    columns_info = (
        conn.exec_driver_sql("PRAGMA table_info(applications)").mappings().all()
    )
    if not columns_info:
        return {"rebuilt": False, "reason": "table does not exist"}

    col_names = {c["name"] for c in columns_info}
    resume_id_col = next((c for c in columns_info if c["name"] == "resume_id"), None)

    needs_rebuild = False
    if resume_id_col and resume_id_col["notnull"] == 1:
        needs_rebuild = True
    if (
        "legacy_application_id" not in col_names
        or "legacy_status" not in col_names
        or "follow_up_date" not in col_names
    ):
        needs_rebuild = True

    if not needs_rebuild:
        return {"rebuilt": False, "reason": "already up to date"}

    count_before = (
        conn.exec_driver_sql("SELECT COUNT(*) FROM applications").scalar() or 0
    )

    conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        conn.exec_driver_sql("""
            CREATE TABLE applications__new (
                application_id VARCHAR NOT NULL PRIMARY KEY,
                job_id VARCHAR NOT NULL,
                resume_id VARCHAR,
                master_resume_id VARCHAR,
                status VARCHAR NOT NULL DEFAULT 'applied',
                company VARCHAR,
                role VARCHAR,
                applied_at VARCHAR,
                notes TEXT,
                position INTEGER NOT NULL DEFAULT 0,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                legacy_application_id VARCHAR,
                legacy_status VARCHAR,
                follow_up_date VARCHAR,
                CONSTRAINT uq_application_job_resume UNIQUE (job_id, resume_id)
            )
        """)

        common_cols = [
            col
            for col in [
                "application_id",
                "job_id",
                "resume_id",
                "master_resume_id",
                "status",
                "company",
                "role",
                "applied_at",
                "notes",
                "position",
                "created_at",
                "updated_at",
                "legacy_application_id",
                "legacy_status",
                "follow_up_date",
            ]
            if col in col_names
        ]
        cols_str = ", ".join(common_cols)
        conn.exec_driver_sql(f"""
            INSERT INTO applications__new ({cols_str})
            SELECT {cols_str} FROM applications
        """)

        conn.exec_driver_sql("DROP TABLE applications")
        conn.exec_driver_sql("ALTER TABLE applications__new RENAME TO applications")

        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_applications_job_id ON applications (job_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_applications_resume_id ON applications (resume_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_applications_status ON applications (status)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_application_job_untailored "
            "ON applications (job_id) WHERE resume_id IS NULL"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_legacy_id "
            "ON applications (legacy_application_id) WHERE legacy_application_id IS NOT NULL"
        )

        count_after = (
            conn.exec_driver_sql("SELECT COUNT(*) FROM applications").scalar() or 0
        )
        if count_after != count_before:
            raise RuntimeError(
                f"Application table rebuild row count mismatch: before={count_before}, after={count_after}"
            )

        fk_errors = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        if fk_errors:
            raise RuntimeError(
                f"Foreign key violations detected after rebuild: {fk_errors}"
            )

        return {"rebuilt": True, "rows": count_after}
    finally:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")


def init_models_sync(engine: Engine, schema: str | None = None) -> None:
    """Create all tables (idempotent) using a sync engine connection."""
    if engine.dialect.name == "postgresql":
        _init_postgres(engine, schema)
        return
    Base.metadata.create_all(engine)

    # ``create_all`` does not ALTER existing SQLite tables. Keep this additive
    # migration idempotent so older local databases can load resumes safely.
    with engine.begin() as conn:
        rebuild_jobs_table_sqlite(conn)
        rebuild_applications_table_sqlite(conn)

        columns = conn.exec_driver_sql("PRAGMA table_info(resumes)").mappings().all()
        existing_columns = {column["name"] for column in columns}
        if columns and "interview_prep" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN interview_prep TEXT")
        if columns and "processing_token" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN processing_token TEXT")

        if columns and "is_master" in existing_columns:
            if "is_default_master" not in existing_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE resumes ADD COLUMN is_default_master BOOLEAN NOT NULL DEFAULT 0"
                )
            # Multi-track masters: the single-master slot is replaced by a
            # single-default slot. create_all never drops indexes on existing tables.
            conn.exec_driver_sql("DROP INDEX IF EXISTS ux_resumes_single_master")
            if "created_at" in existing_columns:
                conn.exec_driver_sql(
                    "UPDATE resumes SET is_default_master = 1 WHERE resume_id = ("
                    "SELECT resume_id FROM resumes WHERE is_master = 1 "
                    "ORDER BY created_at, resume_id LIMIT 1) "
                    "AND NOT EXISTS (SELECT 1 FROM resumes WHERE is_default_master = 1)"
                )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_resumes_single_default_master "
                "ON resumes (is_default_master) WHERE is_default_master = 1"
            )

        _add_missing_column(conn, Improvement.__table__.c.ats_score)
        _add_missing_column(conn, Resume.__table__.c.template_settings)
        _add_missing_column(conn, Application.__table__.c.legacy_application_id)
        _add_missing_column(conn, Application.__table__.c.legacy_status)
        _add_missing_column(conn, Application.__table__.c.follow_up_date)
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_legacy_id "
            "ON applications (legacy_application_id) "
            "WHERE legacy_application_id IS NOT NULL"
        )

        preview_columns = (
            conn.exec_driver_sql("PRAGMA table_info(tailoring_previews)")
            .mappings()
            .all()
        )
        if preview_columns and "improvements" not in {
            column["name"] for column in preview_columns
        }:
            conn.exec_driver_sql(
                "ALTER TABLE tailoring_previews ADD COLUMN improvements JSON"
            )
        if preview_columns and "source_data" not in {
            column["name"] for column in preview_columns
        }:
            conn.exec_driver_sql(
                "ALTER TABLE tailoring_previews ADD COLUMN source_data JSON"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_preview_compatibility ON tailoring_previews (source_id, job_id, payload_hash, created_at)"
        )
