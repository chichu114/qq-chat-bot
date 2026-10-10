import fcntl
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .schema import SCHEMA_VERSION, migrate_to_v5, migrate_to_v6, migrate_to_v7


class SqliteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._local = threading.local()
        self._thread_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def locked(self) -> Iterator[None]:
        depth = getattr(self._local, "lock_depth", 0)
        if depth:
            self._local.lock_depth = depth + 1
            try:
                yield
            finally:
                self._local.lock_depth -= 1
            return

        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+") as lock_file:
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                self._local.lock_depth = 1
                try:
                    yield
                finally:
                    self._local.lock_depth = 0
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def harden_files(self) -> None:
        for path in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                continue

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                  name TEXT PRIMARY KEY,
                  content TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                  approval_id TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  command TEXT NOT NULL,
                  description TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approvals_user_status
                  ON approvals(user_id, status);
                CREATE TABLE IF NOT EXISTS inbound_messages (
                  message_id TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  received_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS inbound_status_updated
                  ON inbound_messages(status, updated_at);
                CREATE TABLE IF NOT EXISTS outbox (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL,
                  user_id INTEGER NOT NULL,
                  message TEXT NOT NULL,
                  echo TEXT,
                  dedupe_key TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  sent_at TEXT,
                  last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS outbox_due
                  ON outbox(status, next_attempt_at, id);
                CREATE TABLE IF NOT EXISTS runtime_metrics (
                  name TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  role TEXT NOT NULL,
                  content TEXT NOT NULL,
                  dedupe_key TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversation_turns_user_id
                  ON conversation_turns(user_id, id);
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  start_turn_id INTEGER NOT NULL,
                  end_turn_id INTEGER NOT NULL,
                  content TEXT NOT NULL,
                  keywords_json TEXT NOT NULL,
                  vector_json TEXT NOT NULL,
                  model TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(user_id, start_turn_id, end_turn_id)
                );
                CREATE INDEX IF NOT EXISTS conversation_summaries_user_id
                  ON conversation_summaries(user_id, end_turn_id);
                CREATE TABLE IF NOT EXISTS memory_facts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  layer TEXT NOT NULL,
                  category TEXT NOT NULL,
                  content TEXT NOT NULL,
                  normalized_content TEXT NOT NULL,
                  status TEXT NOT NULL,
                  confidence REAL NOT NULL,
                  source_kind TEXT NOT NULL,
                  source_message_id TEXT,
                  keywords_json TEXT NOT NULL,
                  vector_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  confirmed_at TEXT,
                  UNIQUE(user_id, normalized_content)
                );
                CREATE INDEX IF NOT EXISTS memory_facts_user_status
                  ON memory_facts(user_id, status, id);
                CREATE TABLE IF NOT EXISTS memory_analysis_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  start_turn_id INTEGER NOT NULL,
                  end_turn_id INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  model TEXT NOT NULL,
                  detail TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_analysis_runs_user_status
                  ON memory_analysis_runs(user_id, status, id);
                CREATE TABLE IF NOT EXISTS media_assets (
                  id TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  message_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  display_name TEXT NOT NULL,
                  mime TEXT,
                  byte_size INTEGER,
                  sha256 TEXT,
                  status TEXT NOT NULL,
                  error_detail TEXT,
                  created_at TEXT NOT NULL,
                  expires_at TEXT,
                  cache_path TEXT
                );
                CREATE INDEX IF NOT EXISTS media_assets_message_id
                  ON media_assets(message_id);
                CREATE INDEX IF NOT EXISTS media_assets_status_expires
                  ON media_assets(status, expires_at);
                CREATE INDEX IF NOT EXISTS media_assets_expires_at
                  ON media_assets(expires_at);
                CREATE TABLE IF NOT EXISTS long_tasks (
                  id TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  source_message_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  status TEXT NOT NULL,
                  stage TEXT NOT NULL,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  last_notified_at TEXT
                );
                CREATE INDEX IF NOT EXISTS long_tasks_user_status
                  ON long_tasks(user_id, status, created_at, id);
                CREATE INDEX IF NOT EXISTS long_tasks_status_created
                  ON long_tasks(status, created_at, id);
                CREATE TABLE IF NOT EXISTS task_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  stage TEXT,
                  detail TEXT,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events_task_id
                  ON task_events(task_id, id);
                CREATE TABLE IF NOT EXISTS model_calls (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT,
                  user_id INTEGER,
                  call_kind TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  input_tokens INTEGER,
                  output_tokens INTEGER,
                  duration_ms INTEGER,
                  success INTEGER NOT NULL,
                  error_class TEXT,
                  estimated_cost REAL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS model_calls_stats
                  ON model_calls(provider, model, call_kind, created_at);
                CREATE INDEX IF NOT EXISTS model_calls_created_at
                  ON model_calls(created_at);
                CREATE TABLE IF NOT EXISTS health_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  component TEXT NOT NULL,
                  status TEXT NOT NULL,
                  detail TEXT,
                  created_at TEXT NOT NULL
                );
                """
            )
            media_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(media_assets)"
                ).fetchall()
            }
            if "cache_path" not in media_columns:
                connection.execute(
                    "ALTER TABLE media_assets ADD COLUMN cache_path TEXT"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS health_events_component_created_at
                  ON health_events(component, created_at)
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            migrate_to_v5(connection)
            migrate_to_v6(connection)
            migrate_to_v7(connection)
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (SCHEMA_VERSION,),
            )
            connection.commit()
        self.harden_files()


def utc_datetime(value: Optional[datetime] = None) -> datetime:
    current = datetime.now(tz=timezone.utc) if value is None else value
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def sqlite_timestamp(value: Optional[datetime] = None) -> str:
    return utc_datetime(value).isoformat(timespec="seconds")


def sqlite_timestamp_microseconds(value: Optional[datetime] = None) -> str:
    return utc_datetime(value).isoformat(timespec="microseconds")


def parse_sqlite_timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return utc_datetime(parsed)
