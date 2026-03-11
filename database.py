from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# Use SQLite for simplicity and portability
DATABASE_URL = "sqlite:///./tactical.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Apply performance-critical PRAGMAs on every new SQLite connection.

    * WAL journal mode – allows concurrent readers while a writer is active,
      which is the single biggest improvement for a multi-threaded server.
    * Synchronous NORMAL – safe with WAL and much faster than FULL.
    * cache_size -64000 – ~64 MB page cache keeps hot data in memory.
    * mmap_size 268435456 – 256 MB memory-mapped I/O reduces syscalls.
    * temp_store MEMORY – temp tables / indices live in RAM.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-64000")
    cursor.execute("PRAGMA mmap_size=268435456")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
