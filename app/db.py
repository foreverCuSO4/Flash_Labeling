from sqlmodel import SQLModel, Session, create_engine

from .config import DATA_DIR, DB_PATH

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
(DATA_DIR / "videos").mkdir(parents=True, exist_ok=True)
(DATA_DIR / "avatars").mkdir(parents=True, exist_ok=True)
(DATA_DIR / "exports").mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    # timeout: video workers commit progress from a background thread while
    # request threads read/write — wait on locks instead of erroring out.
    connect_args={"check_same_thread": False, "timeout": 30},
)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine)
    _migrate()


# Columns added after the initial schema; create_all won't alter existing tables,
# so patch them in with ALTER TABLE when missing.
_COLUMN_MIGRATIONS = {
    "user": {
        "avatar": "TEXT",
    },
    "project": {
        "mode": "VARCHAR DEFAULT 'detection'",
        "guidelines": "TEXT DEFAULT ''",
        "keypoints": "TEXT DEFAULT '[]'",
        "skeleton": "TEXT DEFAULT '[]'",
    },
    "projectclass": {
        "description": "TEXT DEFAULT ''",
    },
    "annotation": {
        "keypoints": "TEXT",
    },
    "image": {
        "labeled_by": "INTEGER",
    },
}


def _migrate() -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        for table, columns in _COLUMN_MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for col, ddl in columns.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


def get_session():
    with Session(engine) as session:
        yield session
