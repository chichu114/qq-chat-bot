from datetime import datetime, timedelta
import re
from typing import Dict, Optional, Tuple

from .database import (
    SqliteDatabase,
    parse_sqlite_timestamp,
    sqlite_timestamp,
    sqlite_timestamp_microseconds,
    utc_datetime,
)


class InboundRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def claim(
        self,
        message_id: str,
        user_id: int,
        stale_after_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> bool:
        user_id = _require_user_id(user_id)
        timestamp = sqlite_timestamp(now)
        current = utc_datetime(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status, updated_at, attempts FROM inbound_messages WHERE message_id=?",
                    (str(message_id),),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO inbound_messages(
                          message_id, user_id, status, attempts, received_at, updated_at
                        ) VALUES (?, ?, 'processing', 1, ?, ?)
                        """,
                        (str(message_id), user_id, timestamp, timestamp),
                    )
                    return True

                status, updated_at, attempts = row
                if status == "completed":
                    return False
                if self._reconcile_sent_final(
                    connection, str(message_id), timestamp
                ):
                    return False
                updated = parse_sqlite_timestamp(str(updated_at))
                if (
                    status == "processing"
                    and updated is not None
                    and current
                    < updated + timedelta(seconds=stale_after_seconds)
                ):
                    return False
                connection.execute(
                    """
                    UPDATE inbound_messages
                    SET user_id=?, status='processing', attempts=?, updated_at=?, last_error=NULL
                    WHERE message_id=?
                    """,
                    (user_id, int(attempts) + 1, timestamp, str(message_id)),
                )
                return True

    def complete(
        self, message_id: str, now: Optional[datetime] = None
    ) -> None:
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    """
                    UPDATE inbound_messages
                    SET status='completed', updated_at=?, last_error=NULL
                    WHERE message_id=?
                    """,
                    (sqlite_timestamp(now), str(message_id)),
                )

    def is_duplicate(
        self,
        message_id: str,
        stale_after_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> bool:
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT status, updated_at FROM inbound_messages
                    WHERE message_id=?
                    """,
                    (str(message_id),),
                ).fetchone()
                if row is None:
                    return False
                status, updated_at = row
                if status == "completed":
                    return True
                if self._reconcile_sent_final(
                    connection, str(message_id), timestamp
                ):
                    return True
                updated = parse_sqlite_timestamp(str(updated_at))
                return bool(
                    status == "processing"
                    and updated is not None
                    and utc_datetime(now)
                    < updated + timedelta(seconds=stale_after_seconds)
                )

    @staticmethod
    def _reconcile_sent_final(connection, message_id, timestamp) -> bool:
        inbound = connection.execute(
            """
            SELECT user_id, status FROM inbound_messages WHERE message_id=?
            """,
            (message_id,),
        ).fetchone()
        if inbound is None or inbound[1] != "processing":
            return False
        user_id = _strict_user_id(inbound[0])
        if user_id is None:
            return False
        key = "reply:%s" % message_id
        final = connection.execute(
            """
            SELECT kind, user_id, echo, status FROM outbox WHERE dedupe_key=?
            """,
            (key,),
        ).fetchone()
        if (
            final is None
            or final[0] != "reply"
            or _strict_user_id(final[1]) != user_id
            or final[2] != key
            or final[3] != "sent"
        ):
            return False
        cursor = connection.execute(
            """
            UPDATE inbound_messages
            SET status='completed', updated_at=?, last_error=NULL
            WHERE message_id=? AND user_id=? AND status='processing'
            """,
            (timestamp, message_id, user_id),
        )
        return cursor.rowcount == 1

    def fail(
        self, message_id: str, detail: str, now: Optional[datetime] = None
    ) -> None:
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    """
                    UPDATE inbound_messages
                    SET status='failed', updated_at=?, last_error=?
                    WHERE message_id=?
                    """,
                    (sqlite_timestamp(now), detail[:1000], str(message_id)),
                )


_OUTBOX_COLUMNS = (
    "id",
    "kind",
    "user_id",
    "message",
    "echo",
    "dedupe_key",
    "status",
    "attempts",
    "next_attempt_at",
    "created_at",
    "updated_at",
    "sent_at",
    "last_error",
)
_OUTBOX_SELECT = "SELECT %s FROM outbox" % ", ".join(_OUTBOX_COLUMNS)
_TASK_NOTICE_KEY = re.compile(
    r"^task:([A-Za-z0-9_-]{1,80}):notice:(?:1|progress)$"
)
_TASK_FINAL_ECHO = re.compile(r"^task:([A-Za-z0-9_-]{1,80}):final$")
_RESERVED_OUTBOX_KINDS = frozenset(
    ("task_final", "task_notice", "e3_observation")
)
_LEASE_GRACE_USED = "delivery lease grace used"
_LEASE_EXHAUSTED = "delivery lease exhausted"
_MAX_USER_ID = 10**20 - 1


def _outbox_dict(row) -> Dict[str, object]:
    return dict(zip(_OUTBOX_COLUMNS, row))


def _claim_token(item: Dict[str, object]) -> Tuple[str, int]:
    return str(item["updated_at"]), int(item["attempts"])


def _claim_token_parts(token: object) -> Optional[Tuple[str, int]]:
    if (
        not isinstance(token, tuple)
        or len(token) != 2
        or not isinstance(token[0], str)
        or isinstance(token[1], bool)
        or not isinstance(token[1], int)
    ):
        return None
    return token[0], token[1]


def _strict_user_id(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= _MAX_USER_ID else None


def _require_user_id(value: object) -> int:
    user_id = _strict_user_id(value)
    if user_id is None:
        raise ValueError("invalid user ownership")
    return user_id


def _lease_timestamp(value: datetime) -> str:
    return sqlite_timestamp_microseconds(value)


class OutboxRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def enqueue(
        self,
        kind: str,
        user_id: int,
        message: str,
        dedupe_key: str,
        echo: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if kind in _RESERVED_OUTBOX_KINDS:
            raise ValueError("reserved outbox kind requires a controlled API")
        user_id = _require_user_id(user_id)
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO outbox(
                      kind, user_id, message, echo, dedupe_key, status,
                      attempts, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        kind,
                        user_id,
                        message,
                        echo,
                        dedupe_key,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "%s WHERE dedupe_key=?" % _OUTBOX_SELECT,
                    (dedupe_key,),
                ).fetchone()
            self._database.harden_files()
            return _outbox_dict(row)

    def enqueue_final_with_observation(
        self,
        kind: str,
        user_id: int,
        message: str,
        dedupe_key: str,
        echo: Optional[str],
        observation_message: str,
        observation_key: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        user_id = _require_user_id(user_id)
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._insert(
                    connection, kind, user_id, message, echo, dedupe_key, timestamp
                )
                self._insert(
                    connection,
                    "e3_observation",
                    user_id,
                    observation_message,
                    dedupe_key,
                    observation_key,
                    timestamp,
                )
                row = connection.execute(
                    "%s WHERE dedupe_key=?" % _OUTBOX_SELECT,
                    (dedupe_key,),
                ).fetchone()
            self._database.harden_files()
        return _outbox_dict(row)

    def enqueue_task_notice(
        self,
        task_id: str,
        user_id: int,
        message: str,
        role: str,
        now: Optional[datetime] = None,
        interval_seconds: int = 60,
    ) -> Dict[str, object]:
        if role not in ("ack", "progress"):
            raise ValueError("invalid task notice role")
        user_id = _require_user_id(user_id)
        timestamp = sqlite_timestamp_microseconds(now)
        suffix = "1" if role == "ack" else "progress"
        dedupe_key = "task:%s:notice:%s" % (task_id, suffix)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT user_id, status, cancel_requested, last_notified_at
                FROM long_tasks
                WHERE id=?
                """,
                (str(task_id),),
            ).fetchone()
            task_user_id = None if task is None else _strict_user_id(task[0])
            requested_user_id = user_id
            if (
                task is None
                or task_user_id is None
                or requested_user_id is None
                or task_user_id != requested_user_id
                or task[1] not in ("queued", "running")
                or bool(task[2])
            ):
                raise ValueError("task notice owner is not active")
            if role == "progress":
                existing = connection.execute(
                    "%s WHERE dedupe_key=? AND kind='task_notice'"
                    % _OUTBOX_SELECT,
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    item = _outbox_dict(existing)
                    if item["status"] == "pending":
                        connection.execute(
                            """
                            UPDATE outbox
                            SET user_id=?, message=?, echo=?, updated_at=?
                            WHERE id=? AND status='pending'
                            """,
                            (
                                user_id,
                                message,
                                dedupe_key,
                                timestamp,
                                int(item["id"]),
                            ),
                        )
                    elif (
                        item["status"] in ("sent", "failed")
                        and str(item["message"]) != message
                    ):
                        next_attempt = utc_datetime(now)
                        notified_at = (
                            parse_sqlite_timestamp(str(task[3]))
                            if task[3]
                            else None
                        )
                        if notified_at is not None:
                            due = notified_at + timedelta(
                                seconds=max(1, int(interval_seconds))
                            )
                            next_attempt = max(next_attempt, due)
                        connection.execute(
                            """
                            UPDATE outbox
                            SET user_id=?, message=?, echo=?, status='pending',
                                attempts=0, next_attempt_at=?, updated_at=?,
                                sent_at=NULL, last_error=NULL
                            WHERE id=? AND status IN ('sent', 'failed')
                            """,
                            (
                                user_id,
                                message,
                                dedupe_key,
                                sqlite_timestamp_microseconds(next_attempt),
                                timestamp,
                                int(item["id"]),
                            ),
                        )
            self._insert(
                connection, "task_notice", user_id, message,
                dedupe_key, dedupe_key, timestamp,
            )
            row = connection.execute(
                "%s WHERE dedupe_key=?" % _OUTBOX_SELECT,
                (dedupe_key,),
            ).fetchone()
        self._database.harden_files()
        return _outbox_dict(row)

    def get(self, dedupe_key: str) -> Optional[Dict[str, object]]:
        with self._database.locked():
            with self._database.connect() as connection:
                row = connection.execute(
                    "%s WHERE dedupe_key=?" % _OUTBOX_SELECT,
                    (dedupe_key,),
                ).fetchone()
        return None if row is None else _outbox_dict(row)

    def validate_reserved_claim(self, outbox_id: int) -> bool:
        with self._database.locked(), self._database.connect() as connection:
            row = connection.execute(
                "%s WHERE id=?" % _OUTBOX_SELECT, (int(outbox_id),)
            ).fetchone()
            if row is None:
                return False
            item = _outbox_dict(row)
            if item["status"] != "sending":
                return False
            kind = str(item["kind"])
            key = str(item["dedupe_key"])
            echo = item["echo"]
            user_id = _strict_user_id(item["user_id"])
            if user_id is None:
                return False
            if kind == "task_notice":
                match = _TASK_NOTICE_KEY.fullmatch(key)
                if match is None or echo != key:
                    return False
                task = connection.execute(
                    """
                    SELECT user_id, status, cancel_requested FROM long_tasks
                    WHERE id=?
                    """,
                    (match.group(1),),
                ).fetchone()
                return bool(
                    task is not None
                    and _strict_user_id(task[0]) == user_id
                    and task[1] in ("queued", "running")
                    and not bool(task[2])
                )
            if kind == "task_final":
                if not isinstance(echo, str):
                    return False
                match = _TASK_FINAL_ECHO.fullmatch(echo)
                if match is None or not key.startswith("reply:"):
                    return False
                message_id = key.split(":", 1)[1]
                if not message_id:
                    return False
                task = connection.execute(
                    """
                    SELECT user_id, source_message_id, status, stage,
                           cancel_requested
                    FROM long_tasks WHERE id=?
                    """,
                    (match.group(1),),
                ).fetchone()
                observation = connection.execute(
                    """
                    SELECT kind, user_id, echo, status FROM outbox
                    WHERE dedupe_key=?
                    """,
                    ("observation:%s" % message_id,),
                ).fetchone()
                return bool(
                    task is not None
                    and _strict_user_id(task[0]) == user_id
                    and str(task[1]) == message_id
                    and task[2] == "running"
                    and task[3] == "delivery_committed"
                    and not bool(task[4])
                    and observation is not None
                    and observation[0] == "e3_observation"
                    and _strict_user_id(observation[1]) == user_id
                    and observation[2] == key
                    and observation[3] == "pending"
                )
            if kind == "e3_observation":
                if not key.startswith("observation:"):
                    return False
                message_id = key.split(":", 1)[1]
                final_key = "reply:%s" % message_id
                if not message_id or echo != final_key:
                    return False
                dependency = connection.execute(
                    """
                    SELECT kind, user_id, echo, status FROM outbox
                    WHERE dedupe_key=?
                    """,
                    (final_key,),
                ).fetchone()
                if (
                    dependency is None
                    or dependency[0] not in ("reply", "task_final")
                    or _strict_user_id(dependency[1]) != user_id
                    or dependency[3] != "sent"
                ):
                    return False
                if dependency[0] == "reply":
                    return dependency[2] == final_key
                final_echo = dependency[2]
                if not isinstance(final_echo, str):
                    return False
                task_match = _TASK_FINAL_ECHO.fullmatch(final_echo)
                if task_match is None:
                    return False
                task = connection.execute(
                    """
                    SELECT user_id, source_message_id, status FROM long_tasks
                    WHERE id=?
                    """,
                    (task_match.group(1),),
                ).fetchone()
                return bool(
                    task is not None
                    and _strict_user_id(task[0]) == user_id
                    and str(task[1]) == message_id
                    and task[2] == "succeeded"
                )
            return False

    def quarantine_reserved(self, outbox_id: int, now=None) -> None:
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "%s WHERE id=? AND kind IN "
                "('task_final', 'task_notice', 'e3_observation') "
                "AND status IN ('pending', 'sending')" % _OUTBOX_SELECT,
                (int(outbox_id),),
            ).fetchone()
            if row is not None:
                self._quarantine_reserved_row(
                    connection, _outbox_dict(row), sqlite_timestamp(now)
                )

    def claim_due(
        self,
        now: Optional[datetime] = None,
        max_attempts: int = 8,
        lease_seconds: int = 300,
        task_notice_interval_seconds: int = 60,
    ) -> Optional[Dict[str, object]]:
        current = utc_datetime(now)
        timestamp = _lease_timestamp(current)
        lease = int(lease_seconds)
        if lease < 1 or lease > 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._recover_expired_leases(
                    connection,
                    timestamp,
                    _lease_timestamp(current - timedelta(seconds=lease)),
                    max_attempts,
                )
                self._maintain_pending_reserved_rows(connection, timestamp)
                self._prepare_task_notices(
                    connection,
                    current,
                    max(1, int(task_notice_interval_seconds)),
                )
                row = connection.execute(
                    "%s AS candidate WHERE candidate.status='pending' "
                    "AND candidate.attempts < ? AND candidate.next_attempt_at <= ? "
                    "AND (candidate.kind != 'e3_observation' OR EXISTS ("
                    "SELECT 1 FROM outbox AS dependency "
                    "WHERE dependency.dedupe_key=candidate.echo "
                    "AND dependency.status='sent')) "
                    "ORDER BY candidate.id LIMIT 1"
                    % _OUTBOX_SELECT,
                    (max_attempts, timestamp),
                ).fetchone()
                if row is None:
                    return None
                outbox_id = int(row[0])
                connection.execute(
                    """
                    UPDATE outbox
                    SET status='sending', attempts=attempts + 1, updated_at=?
                    WHERE id=?
                    """,
                    (timestamp, outbox_id),
                )
                updated = connection.execute(
                    "%s WHERE id=?" % _OUTBOX_SELECT, (outbox_id,)
                ).fetchone()
                claimed = _outbox_dict(updated)
                claimed["claim_token"] = _claim_token(claimed)
                return claimed

    @classmethod
    def _maintain_pending_reserved_rows(cls, connection, timestamp):
        rows = connection.execute(
            "%s WHERE status='pending' AND kind NOT IN "
            "('task_final', 'task_notice', 'e3_observation') ORDER BY id"
            % _OUTBOX_SELECT
        ).fetchall()
        for row in rows:
            item = _outbox_dict(row)
            if _strict_user_id(item["user_id"]) is None:
                connection.execute(
                    """
                    UPDATE outbox
                    SET status='failed', next_attempt_at=?, updated_at=?,
                        last_error='invalid outbox ownership'
                    WHERE id=? AND status='pending'
                    """,
                    (timestamp, timestamp, int(item["id"])),
                )

        rows = connection.execute(
            "%s WHERE status='pending' AND kind='task_final' ORDER BY id"
            % _OUTBOX_SELECT
        ).fetchall()
        for row in rows:
            item = _outbox_dict(row)
            if not cls._pending_task_final_is_valid(connection, item):
                cls._quarantine_reserved_row(connection, item, timestamp)

        rows = connection.execute(
            "%s WHERE status='pending' AND kind='task_notice' ORDER BY id"
            % _OUTBOX_SELECT
        ).fetchall()
        for row in rows:
            item = _outbox_dict(row)
            if not cls._pending_task_notice_is_well_owned(connection, item):
                cls._quarantine_reserved_row(connection, item, timestamp)

        rows = connection.execute(
            "%s WHERE status='pending' AND kind='e3_observation' ORDER BY id"
            % _OUTBOX_SELECT
        ).fetchall()
        for row in rows:
            item = _outbox_dict(row)
            if cls._observation_dependency_state(connection, item) == "invalid":
                cls._quarantine_reserved_row(connection, item, timestamp)

    @staticmethod
    def _pending_task_final_is_valid(connection, item):
        echo = item["echo"]
        key = str(item["dedupe_key"])
        if not isinstance(echo, str) or not key.startswith("reply:"):
            return False
        match = _TASK_FINAL_ECHO.fullmatch(echo)
        message_id = key.split(":", 1)[1]
        if match is None or not message_id:
            return False
        user_id = _strict_user_id(item["user_id"])
        if user_id is None:
            return False
        task = connection.execute(
            """
            SELECT user_id, source_message_id, status, stage, cancel_requested
            FROM long_tasks WHERE id=?
            """,
            (match.group(1),),
        ).fetchone()
        observation = connection.execute(
            """
            SELECT kind, user_id, echo, status FROM outbox
            WHERE dedupe_key=?
            """,
            ("observation:%s" % message_id,),
        ).fetchone()
        return bool(
            task is not None
            and _strict_user_id(task[0]) == user_id
            and str(task[1]) == message_id
            and task[2:] == ("running", "delivery_committed", 0)
            and observation is not None
            and observation[0] == "e3_observation"
            and _strict_user_id(observation[1]) == user_id
            and observation[2:] == (key, "pending")
        )

    @staticmethod
    def _pending_task_notice_is_well_owned(connection, item):
        key = str(item["dedupe_key"])
        match = _TASK_NOTICE_KEY.fullmatch(key)
        if match is None or item["echo"] != key:
            return False
        task = connection.execute(
            "SELECT user_id FROM long_tasks WHERE id=?", (match.group(1),)
        ).fetchone()
        row_user_id = _strict_user_id(item["user_id"])
        return bool(
            task is not None
            and row_user_id is not None
            and _strict_user_id(task[0]) == row_user_id
        )

    @staticmethod
    def _observation_dependency_state(connection, item):
        key = str(item["dedupe_key"])
        echo = item["echo"]
        if not key.startswith("observation:") or not isinstance(echo, str):
            return "invalid"
        message_id = key.split(":", 1)[1]
        final_key = "reply:%s" % message_id
        if not message_id or echo != final_key:
            return "invalid"
        dependency = connection.execute(
            """
            SELECT kind, user_id, echo, status FROM outbox
            WHERE dedupe_key=?
            """,
            (final_key,),
        ).fetchone()
        row_user_id = _strict_user_id(item["user_id"])
        if (
            dependency is None
            or dependency[0] not in ("reply", "task_final")
            or row_user_id is None
            or _strict_user_id(dependency[1]) != row_user_id
        ):
            return "invalid"
        if dependency[3] in ("pending", "sending"):
            return "waiting"
        if dependency[3] != "sent":
            return "invalid"
        if dependency[0] == "reply":
            return "ready" if dependency[2] == final_key else "invalid"
        final_echo = dependency[2]
        if not isinstance(final_echo, str):
            return "invalid"
        task_match = _TASK_FINAL_ECHO.fullmatch(final_echo)
        if task_match is None:
            return "invalid"
        task = connection.execute(
            """
            SELECT user_id, source_message_id, status FROM long_tasks
            WHERE id=?
            """,
            (task_match.group(1),),
        ).fetchone()
        if (
            task is None
            or _strict_user_id(task[0]) != row_user_id
            or str(task[1]) != message_id
            or task[2] != "succeeded"
        ):
            return "invalid"
        return "ready"

    @classmethod
    def _quarantine_reserved_row(cls, connection, item, timestamp):
        cursor = connection.execute(
            """
            UPDATE outbox SET status='failed', next_attempt_at=?, updated_at=?,
                last_error='invalid reserved outbox row'
            WHERE id=? AND status IN ('pending', 'sending')
            """,
            (timestamp, timestamp, int(item["id"])),
        )
        if cursor.rowcount != 1:
            return
        kind = str(item["kind"])
        if kind == "task_final":
            cls._close_corrupted_task_final(connection, item, timestamp)
        elif kind == "task_notice":
            cls._record_corrupted_notice_failure(connection, item, timestamp)

    @classmethod
    def _close_corrupted_task_final(cls, connection, item, timestamp):
        owner = cls._resolve_corrupted_task_final_owner(connection, item)
        if owner is None:
            cls._record_ambiguous_task_final_health(connection, timestamp)
            return
        task_id, user_id, message_id = owner
        final_key = "reply:%s" % message_id
        connection.execute(
            """
            UPDATE outbox
            SET status='failed', next_attempt_at=?, updated_at=?,
                last_error='invalid reserved outbox row'
            WHERE kind='e3_observation' AND user_id=?
              AND dedupe_key=? AND echo=?
              AND status IN ('pending', 'sending')
            """,
            (
                timestamp,
                timestamp,
                user_id,
                "observation:%s" % message_id,
                final_key,
            ),
        )
        task_cursor = connection.execute(
            """
            UPDATE long_tasks
            SET status='failed', stage='delivery_corrupt',
                finished_at=COALESCE(finished_at, ?)
            WHERE id=? AND user_id=? AND source_message_id=?
              AND status='running' AND stage='delivery_committed'
            """,
            (timestamp, task_id, user_id, message_id),
        )
        if task_cursor.rowcount == 1:
            connection.execute(
                """
                INSERT INTO task_events(
                  task_id, event_type, stage, detail, created_at
                ) VALUES (?, 'transition', 'delivery_corrupt', ?, ?)
                """,
                (task_id, "最终回复数据损坏", timestamp),
            )
            connection.execute(
                """
                UPDATE inbound_messages
                SET status='completed', updated_at=?,
                    last_error='final delivery corrupted'
                WHERE message_id=? AND user_id=?
                """,
                (timestamp, message_id, user_id),
            )

    @staticmethod
    def _resolve_corrupted_task_final_owner(connection, item):
        key = str(item["dedupe_key"])
        message_id = key.split(":", 1)[1] if key.startswith("reply:") else None
        if not message_id:
            message_id = None
        echo = item["echo"]
        echo_match = (
            _TASK_FINAL_ECHO.fullmatch(echo) if isinstance(echo, str) else None
        )
        echo_task_id = None if echo_match is None else echo_match.group(1)
        row_user_id = _strict_user_id(item["user_id"])
        if echo_task_id is not None and message_id is not None and row_user_id is not None:
            exact = connection.execute(
                """
                SELECT id, user_id, source_message_id FROM long_tasks
                WHERE id=? AND user_id=? AND source_message_id=?
                  AND status='running' AND stage='delivery_committed'
                """,
                (echo_task_id, row_user_id, message_id),
            ).fetchone()
            if exact is not None and _strict_user_id(exact[1]) == row_user_id:
                return str(exact[0]), row_user_id, str(exact[2])
        rows = connection.execute(
            """
            SELECT id, user_id, source_message_id FROM long_tasks
            WHERE status='running' AND stage='delivery_committed'
            ORDER BY id
            """
        ).fetchall()
        tasks = {}
        for task_id, user_id, source_message_id in rows:
            parsed_user_id = _strict_user_id(user_id)
            if parsed_user_id is not None:
                tasks[str(task_id)] = (
                    parsed_user_id, str(source_message_id)
                )
        echo_targets = (
            {echo_task_id} if echo_task_id is not None and echo_task_id in tasks
            else set()
        )
        key_targets = {
            task_id
            for task_id, (_, source_message_id) in tasks.items()
            if message_id is not None and source_message_id == message_id
        }
        user_targets = {
            task_id
            for task_id, (user_id, _) in tasks.items()
            if row_user_id is not None and user_id == row_user_id
        }
        signal_targets = (echo_targets, key_targets, user_targets)
        candidates = [
            task_id
            for task_id in tasks
            if sum(task_id in targets for targets in signal_targets) >= 2
        ]
        if len(candidates) != 1:
            return None
        task_id = candidates[0]
        if any(targets and task_id not in targets for targets in signal_targets):
            return None
        user_id, source_message_id = tasks[task_id]
        final_key = "reply:%s" % source_message_id
        observation = connection.execute(
            """
            SELECT kind, user_id, echo, status FROM outbox
            WHERE dedupe_key=?
            """,
            ("observation:%s" % source_message_id,),
        ).fetchone()
        inbound = connection.execute(
            """
            SELECT user_id FROM inbound_messages WHERE message_id=?
            """,
            (source_message_id,),
        ).fetchone()
        observation_valid = bool(
            observation is not None
            and observation[0] == "e3_observation"
            and _strict_user_id(observation[1]) == user_id
            and observation[2] == final_key
            and observation[3] == "pending"
        )
        inbound_valid = bool(
            inbound is not None and _strict_user_id(inbound[0]) == user_id
        )
        if not observation_valid or not inbound_valid:
            return None
        return task_id, user_id, source_message_id

    @staticmethod
    def _record_ambiguous_task_final_health(connection, timestamp):
        component = "outbox_delivery"
        status = "degraded"
        detail = "ambiguous task final ownership"
        latest = connection.execute(
            """
            SELECT status, detail FROM health_events
            WHERE component=? ORDER BY id DESC LIMIT 1
            """,
            (component,),
        ).fetchone()
        if latest != (status, detail):
            connection.execute(
                """
                INSERT INTO health_events(component, status, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (component, status, detail, timestamp),
            )

    @staticmethod
    def _record_corrupted_notice_failure(connection, item, timestamp):
        key = str(item["dedupe_key"])
        match = _TASK_NOTICE_KEY.fullmatch(key)
        if match is None:
            return
        task_id = match.group(1)
        task = connection.execute(
            "SELECT user_id FROM long_tasks WHERE id=?", (task_id,)
        ).fetchone()
        row_user_id = _strict_user_id(item["user_id"])
        if (
            task is not None
            and row_user_id is not None
            and _strict_user_id(task[0]) == row_user_id
        ):
            connection.execute(
                """
                INSERT INTO task_events(
                  task_id, event_type, stage, detail, created_at
                ) VALUES (?, 'notification_failed', NULL, ?, ?)
                """,
                (task_id, "通知发送失败", timestamp),
            )

    @classmethod
    def _recover_expired_leases(
        cls, connection, timestamp, expiry_threshold, max_attempts
    ):
        rows = connection.execute(
            """
            SELECT id, kind, user_id, echo, dedupe_key, attempts, last_error
            FROM outbox
            WHERE status='sending' AND updated_at <= ?
            ORDER BY id
            """,
            (expiry_threshold,),
        ).fetchall()
        for row in rows:
            outbox_id, kind, user_id, echo, key, attempts, last_error = row
            if last_error == _LEASE_GRACE_USED:
                cls._terminalize_expired_lease(
                    connection,
                    int(outbox_id),
                    str(kind),
                    _strict_user_id(user_id),
                    echo,
                    str(key),
                    timestamp,
                )
                continue

            recovered_attempts = max(
                0, min(int(attempts), int(max_attempts) - 1)
            )
            connection.execute(
                """
                UPDATE outbox
                SET status='pending', next_attempt_at=?, updated_at=?,
                    last_error=?, attempts=?
                WHERE id=? AND status='sending'
                """,
                (
                    timestamp,
                    timestamp,
                    _LEASE_GRACE_USED,
                    recovered_attempts,
                    int(outbox_id),
                ),
            )

    @staticmethod
    def _terminalize_expired_lease(
        connection, outbox_id, kind, user_id, echo, key, timestamp,
        last_error=_LEASE_EXHAUSTED,
    ):
        connection.execute(
            """
            UPDATE outbox
            SET status='failed', next_attempt_at=?, updated_at=?, last_error=?
            WHERE id=? AND status='sending'
            """,
            (timestamp, timestamp, last_error, outbox_id),
        )
        if kind == "e3_observation":
            return
        if kind == "task_notice":
            match = _TASK_NOTICE_KEY.fullmatch(key)
            if match is None or echo != key:
                return
            task_id = match.group(1)
            task = connection.execute(
                "SELECT user_id FROM long_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if (
                user_id is not None
                and task is not None
                and _strict_user_id(task[0]) == user_id
            ):
                connection.execute(
                    """
                    INSERT INTO task_events(
                      task_id, event_type, stage, detail, created_at
                    ) VALUES (?, 'notification_failed', NULL, ?, ?)
                    """,
                    (task_id, "通知发送失败", timestamp),
                )
            return

        if kind == "reply":
            if (
                user_id is None
                or not key.startswith("reply:")
                or echo != key
            ):
                return
            message_id = key.split(":", 1)[1]
            if not message_id:
                return
            connection.execute(
                """
                UPDATE outbox
                SET status='failed', next_attempt_at=?, updated_at=?,
                    last_error='dependency final failed'
                WHERE kind='e3_observation' AND user_id=?
                  AND dedupe_key=? AND echo=?
                  AND status IN ('pending', 'sending')
                """,
                (
                    timestamp,
                    timestamp,
                    user_id,
                    "observation:%s" % message_id,
                    key,
                ),
            )
            connection.execute(
                """
                UPDATE inbound_messages
                SET status='completed', updated_at=?,
                    last_error='final delivery failed'
                WHERE message_id=? AND user_id=?
                """,
                (timestamp, message_id, user_id),
            )
            return

        if kind != "task_final":
            return

        owner = OutboxRepository._resolve_corrupted_task_final_owner(
            connection,
            {"dedupe_key": key, "echo": echo, "user_id": user_id},
        )
        if owner is None:
            OutboxRepository._record_ambiguous_task_final_health(
                connection, timestamp
            )
            return
        task_id, user_id, message_id = owner
        final_key = "reply:%s" % message_id
        connection.execute(
            """
            UPDATE outbox
            SET status='failed', next_attempt_at=?, updated_at=?,
                last_error='dependency final failed'
            WHERE kind='e3_observation' AND user_id=?
              AND dedupe_key=? AND echo=?
              AND status IN ('pending', 'sending')
            """,
            (
                timestamp,
                timestamp,
                user_id,
                "observation:%s" % message_id,
                final_key,
            ),
        )
        task_cursor = connection.execute(
            """
            UPDATE long_tasks
            SET status='failed', stage='delivery_failed',
                finished_at=COALESCE(finished_at, ?)
            WHERE id=? AND user_id=? AND source_message_id=?
              AND status='running' AND stage='delivery_committed'
            """,
            (timestamp, task_id, user_id, message_id),
        )
        if task_cursor.rowcount == 1:
            connection.execute(
                """
                INSERT INTO task_events(
                  task_id, event_type, stage, detail, created_at
                ) VALUES (?, 'transition', 'delivery_failed', ?, ?)
                """,
                (task_id, "最终回复投递失败", timestamp),
            )
        connection.execute(
            """
            UPDATE inbound_messages
            SET status='completed', updated_at=?,
                last_error='final delivery failed'
            WHERE message_id=? AND user_id=?
            """,
            (timestamp, message_id, user_id),
        )

    @staticmethod
    def _prepare_task_notices(connection, current, interval_seconds):
        rows = connection.execute(
            """
            SELECT id, user_id, echo, dedupe_key FROM outbox
            WHERE kind='task_notice' AND status='pending'
              AND next_attempt_at <= ? ORDER BY id
            """,
            (sqlite_timestamp_microseconds(current),),
        ).fetchall()
        for outbox_id, user_id, echo, dedupe_key in rows:
            key = str(dedupe_key)
            match = _TASK_NOTICE_KEY.fullmatch(key)
            task = None
            if match is not None and echo == key:
                task = connection.execute(
                    """
                    SELECT user_id, status, cancel_requested, last_notified_at
                    FROM long_tasks WHERE id=?
                    """,
                    (match.group(1),),
                ).fetchone()
            row_user_id = _strict_user_id(user_id)
            invalid = bool(
                task is None
                or row_user_id is None
                or _strict_user_id(task[0]) != row_user_id
            )
            terminal = bool(
                task is not None
                and (task[1] not in ("queued", "running") or bool(task[2]))
            )
            if invalid or terminal:
                connection.execute(
                    """
                    UPDATE outbox SET status='failed', updated_at=?,
                        last_error=?
                    WHERE id=? AND status='pending'
                    """,
                    (
                        sqlite_timestamp_microseconds(current),
                        "invalid reserved outbox row"
                        if invalid else "task notice suppressed",
                        int(outbox_id),
                    ),
                )
                continue
            notified_at = parse_sqlite_timestamp(str(task[3])) if task[3] else None
            if notified_at is not None:
                due = notified_at + timedelta(seconds=interval_seconds)
                if current < due:
                    connection.execute(
                        """
                        UPDATE outbox SET next_attempt_at=?, updated_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (
                            sqlite_timestamp_microseconds(due),
                            sqlite_timestamp_microseconds(current),
                            int(outbox_id),
                        ),
                    )

    @staticmethod
    def _insert(connection, kind, user_id, message, echo, dedupe_key, timestamp):
        connection.execute(
            """
            INSERT OR IGNORE INTO outbox(
              kind, user_id, message, echo, dedupe_key, status,
              attempts, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                kind,
                user_id,
                message,
                echo,
                dedupe_key,
                timestamp,
                timestamp,
                timestamp,
            ),
        )

    def mark_sent(
        self,
        outbox_id: int,
        claim_token: object,
        now: Optional[datetime] = None,
    ) -> bool:
        token = _claim_token_parts(claim_token)
        if token is None:
            return False
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE outbox
                    SET status='sent', sent_at=?, updated_at=?, last_error=NULL
                    WHERE id=? AND status='sending'
                      AND updated_at=? AND attempts=?
                    """,
                    (timestamp, timestamp, outbox_id, token[0], token[1]),
                )
                return cursor.rowcount == 1

    def mark_final_sent(
        self,
        outbox_id: int,
        claim_token: object,
        now: Optional[datetime] = None,
    ) -> bool:
        token = _claim_token_parts(claim_token)
        if token is None:
            return False
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT user_id, echo, dedupe_key FROM outbox
                    WHERE id=? AND kind='reply' AND status='sending'
                      AND updated_at=? AND attempts=?
                    """,
                    (int(outbox_id), token[0], token[1]),
                ).fetchone()
                if row is None:
                    return False
                user_id = _strict_user_id(row[0])
                key = str(row[2])
                if (
                    user_id is None
                    or not key.startswith("reply:")
                    or row[1] != key
                ):
                    return False
                message_id = key.split(":", 1)[1]
                if not message_id:
                    return False
                inbound = connection.execute(
                    """
                    SELECT user_id, status FROM inbound_messages
                    WHERE message_id=?
                    """,
                    (message_id,),
                ).fetchone()
                if (
                    inbound is not None
                    and (
                        _strict_user_id(inbound[0]) != user_id
                        or inbound[1] != "processing"
                    )
                ):
                    return False
                cursor = connection.execute(
                    """
                    UPDATE outbox
                    SET status='sent', sent_at=?, updated_at=?, last_error=NULL
                    WHERE id=? AND kind='reply' AND status='sending'
                      AND updated_at=? AND attempts=?
                    """,
                    (timestamp, timestamp, outbox_id, token[0], token[1]),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                if inbound is not None:
                    inbound_cursor = connection.execute(
                        """
                        UPDATE inbound_messages
                        SET status='completed', updated_at=?, last_error=NULL
                        WHERE message_id=? AND user_id=? AND status='processing'
                        """,
                        (timestamp, message_id, user_id),
                    )
                    if inbound_cursor.rowcount != 1:
                        connection.rollback()
                        return False
                return True

    def mark_retry(
        self,
        outbox_id: int,
        claim_token: object,
        detail: str,
        retry_base_seconds: int,
        max_attempts: int,
        now: Optional[datetime] = None,
    ) -> bool:
        token = _claim_token_parts(claim_token)
        if token is None:
            return False
        current = utc_datetime(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT attempts, kind, user_id, echo, dedupe_key, last_error
                    FROM outbox
                    WHERE id=? AND status='sending'
                      AND updated_at=? AND attempts=?
                    """,
                    (outbox_id, token[0], token[1]),
                ).fetchone()
                if row is None:
                    return False
                attempts = int(row[0])
                kind = str(row[1])
                user_id = _strict_user_id(row[2])
                echo = row[3]
                dedupe_key = str(row[4])
                last_error = row[5]
                timestamp = sqlite_timestamp(current)
                if last_error == _LEASE_GRACE_USED:
                    self._terminalize_expired_lease(
                        connection, outbox_id, kind, user_id, echo,
                        dedupe_key, timestamp,
                    )
                    return True
                if attempts >= max_attempts:
                    self._terminalize_expired_lease(
                        connection, outbox_id, kind, user_id, echo,
                        dedupe_key, timestamp, detail[:1000],
                    )
                    return True
                if user_id is None:
                    connection.execute(
                        """
                        UPDATE outbox
                        SET status='failed', next_attempt_at=?, updated_at=?,
                            last_error='invalid outbox ownership'
                        WHERE id=? AND status='sending'
                          AND updated_at=? AND attempts=?
                        """,
                        (timestamp, timestamp, outbox_id, token[0], token[1]),
                    )
                    return True
                delay = retry_base_seconds * (2 ** max(0, attempts - 1))
                next_attempt = current + timedelta(seconds=min(delay, 3600))
                cursor = connection.execute(
                    """
                    UPDATE outbox
                    SET status='pending', next_attempt_at=?, updated_at=?,
                        last_error=?
                    WHERE id=? AND status='sending'
                      AND updated_at=? AND attempts=?
                    """,
                    (
                        sqlite_timestamp(next_attempt),
                        timestamp,
                        detail[:1000],
                        outbox_id,
                        token[0],
                        token[1],
                    ),
                )
                return cursor.rowcount == 1


class RuntimeMetricRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def set(self, name: str, value: str) -> None:
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO runtime_metrics(name, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                      value=excluded.value,
                      updated_at=excluded.updated_at
                    """,
                    (name, value, sqlite_timestamp()),
                )

    def transport_stats(self) -> Dict[str, object]:
        with self._database.locked():
            with self._database.connect() as connection:
                outbox_counts = dict(
                    connection.execute(
                        "SELECT status, COUNT(*) FROM outbox GROUP BY status"
                    ).fetchall()
                )
                active_inbound = connection.execute(
                    "SELECT COUNT(*) FROM inbound_messages WHERE status='processing'"
                ).fetchone()[0]
                metrics = dict(
                    connection.execute(
                        "SELECT name, value FROM runtime_metrics"
                    ).fetchall()
                )
        return {
            "outbox_pending": int(outbox_counts.get("pending", 0)),
            "outbox_sending": int(outbox_counts.get("sending", 0)),
            "outbox_failed": int(outbox_counts.get("failed", 0)),
            "active_inbound": int(active_inbound),
            "last_agent_success_at": metrics.get("last_agent_success_at"),
            "last_delivery_success_at": metrics.get(
                "last_delivery_success_at"
            ),
            "circuit_state": metrics.get("circuit_state", "closed"),
        }
