"""Database engine, session factory, and schema creation."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_settings = get_settings()

# check_same_thread=False so the APScheduler background thread can share the engine.
_is_sqlite = _settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
# For remote/serverless Postgres (e.g. Neon, which suspends idle connections),
# validate connections before use and recycle stale ones so we never hand out a
# dropped connection ("server closed the connection unexpectedly").
_engine_kwargs = {} if _is_sqlite else {"pool_pre_ping": True, "pool_recycle": 300}
engine = create_engine(
    _settings.database_url,
    echo=False,
    connect_args=_connect_args,
    **_engine_kwargs,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        # WAL allows concurrent reads during a write; busy_timeout makes writers
        # wait instead of immediately raising "database is locked" when the
        # scheduler and a manual discover overlap.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


_CANDIDATE_REVIEW_COLUMNS = {
    "approved_resume_path": ("VARCHAR NOT NULL DEFAULT ''", "VARCHAR NOT NULL DEFAULT ''"),
    "approved_raw_text": ("VARCHAR NOT NULL DEFAULT ''", "VARCHAR NOT NULL DEFAULT ''"),
    "approved_answer_bank": ("JSON NOT NULL DEFAULT '{}'", "JSON NOT NULL DEFAULT '{}'::json"),
    "approved_source_filename": ("VARCHAR NOT NULL DEFAULT ''", "VARCHAR NOT NULL DEFAULT ''"),
}


def ensure_schema(target_engine=engine) -> None:
    """Create new tables and apply the small compatibility upgrades we own."""
    import app.models  # noqa: F401  (registers tables on SQLModel.metadata)

    SQLModel.metadata.create_all(target_engine)
    with target_engine.begin() as connection:
        inspector = inspect(connection)
        table = "candidate_profile_review"
        if table not in inspector.get_table_names():
            return
        existing = {column["name"] for column in inspector.get_columns(table)}
        postgres = connection.dialect.name == "postgresql"
        for name, (portable_type, postgres_type) in _CANDIDATE_REVIEW_COLUMNS.items():
            if name in existing:
                continue
            column_type = postgres_type if postgres else portable_type
            if_exists = " IF NOT EXISTS" if postgres else ""
            connection.execute(text(
                f"ALTER TABLE {table} ADD COLUMN{if_exists} {name} {column_type}"
            ))


def init_db() -> None:
    """Initialize the configured database schema."""
    ensure_schema(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    with Session(engine) as session:
        yield session
