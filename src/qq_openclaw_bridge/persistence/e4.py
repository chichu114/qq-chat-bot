import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import re
from typing import Dict, List, Optional

from .database import (
    SqliteDatabase,
    sqlite_timestamp,
    sqlite_timestamp_microseconds,
)
from .messaging import (
    OutboxRepository,
    _claim_token_parts,
    _require_user_id,
    _strict_user_id,
)


TERMINAL_TASK_STATES = {"succeeded", "failed", "cancelled"}
ALLOWED_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
}
CLEANUP_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
_CLEANUP_RETRY_PATTERN = re.compile(r"(?:^|;)cleanup_retry_attempt=(\d+)(?:;|$)")
_TASK_FINAL_ECHO = re.compile(r"^task:([A-Za-z0-9_-]{1,80}):final$")
_TASK_NOTICE_KEY = re.compile(
    r"^task:([A-Za-z0-9_-]{1,80}):notice:(?:1|progress)$"
)
_TASK_LIST_LIMIT = 100
_HEALTH_TIMELINE_LIMIT = 200


_MEDIA_COLUMNS = (
    "id",
    "user_id",
    "message_id",
    "kind",
    "display_name",
    "mime",
    "byte_size",
    "sha256",
    "status",
    "error_detail",
    "created_at",
    "expires_at",
    "cache_path",
)
_TASK_COLUMNS = (
    "id",
    "user_id",
    "source_message_id",
    "kind",
    "status",
    "stage",
    "cancel_requested",
    "created_at",
    "started_at",
    "finished_at",
    "last_notified_at",
)
_EVENT_COLUMNS = (
    "id",
    "task_id",
    "event_type",
    "stage",
    "detail",
    "created_at",
)
_HEALTH_COLUMNS = (
    "id",
    "component",
    "status",
    "detail",
    "created_at",
)


def _row_dict(columns, row) -> Dict[str, object]:
    return dict(zip(columns, row))


def _bounded_limit(limit: object, maximum: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return 1
    return min(maximum, max(1, limit))


class E4Repository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create_media_asset(
        self,
        user_id: int,
        message_id: str,
        kind: str,
        display_name: str = "",
        mime: Optional[str] = None,
        byte_size: Optional[int] = None,
        sha256: Optional[str] = None,
        status: str = "pending",
        error_detail: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
        asset_id: Optional[str] = None,
        cache_path: Optional[str] = None,
    ) -> Dict[str, object]:
        user_id = _require_user_id(user_id)
        identifier = asset_id or uuid.uuid4().hex
        created_at = sqlite_timestamp(now)
        expiry = None if expires_at is None else sqlite_timestamp(expires_at)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO media_assets(
                  id, user_id, message_id, kind, display_name, mime, byte_size,
                  sha256, status, error_detail, created_at, expires_at, cache_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    user_id,
                    str(message_id),
                    kind,
                    display_name,
                    mime,
                    byte_size,
                    sha256,
                    status,
                    error_detail,
                    created_at,
                    expiry,
                    None if cache_path is None else str(cache_path),
                ),
            )
            row = connection.execute(
                "SELECT %s FROM media_assets WHERE id=?"
                % ", ".join(_MEDIA_COLUMNS),
                (identifier,),
            ).fetchone()
            self._database.harden_files()
        assert row is not None
        return _row_dict(_MEDIA_COLUMNS, row)

    def list_expired_media(self, now: datetime) -> List[Dict[str, object]]:
        timestamp = sqlite_timestamp(now)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT %s FROM media_assets "
                "WHERE status != 'expired' AND cache_path IS NOT NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ? "
                "ORDER BY expires_at, id"
                % ", ".join(_MEDIA_COLUMNS),
                (timestamp,),
            ).fetchall()
        return [_row_dict(_MEDIA_COLUMNS, row) for row in rows]

    def mark_media_expired(
        self, asset_id: str, detail: Optional[str] = None
    ) -> Optional[Dict[str, object]]:
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(
                "UPDATE media_assets SET status='expired', "
                "error_detail=COALESCE(?, error_detail) WHERE id=?",
                (detail, str(asset_id)),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT %s FROM media_assets WHERE id=?"
                % ", ".join(_MEDIA_COLUMNS),
                (str(asset_id),),
            ).fetchone()
        assert row is not None
        self._database.harden_files()
        return _row_dict(_MEDIA_COLUMNS, row)

    def record_media_cleanup_error(
        self,
        asset_id: str,
        detail: str,
        now: Optional[datetime] = None,
        max_attempts: int = 3,
        cache_path: Optional[str] = None,
    ) -> Optional[Dict[str, object]]:
        current = datetime.now().astimezone() if now is None else now
        maximum = max(1, min(int(max_attempts), len(CLEANUP_RETRY_DELAYS)))
        with self._database.locked(), self._database.connect() as connection:
            existing = connection.execute(
                "SELECT status, error_detail FROM media_assets WHERE id=?",
                (str(asset_id),),
            ).fetchone()
            if existing is None or existing[0] == "expired":
                return None
            match = _CLEANUP_RETRY_PATTERN.search(existing[1] or "")
            attempts = int(match.group(1)) if match is not None else 0
            reason = str(detail).replace("\n", " ").replace("\r", " ")
            if attempts >= maximum:
                stored_detail = (
                    "cleanup abandoned after %d attempts; reason=%s"
                    % (maximum, reason)
                )
                cursor = connection.execute(
                    "UPDATE media_assets SET status='expired', error_detail=?, "
                    "cache_path=COALESCE(?, cache_path) "
                    "WHERE id=? AND status != 'expired'",
                    (
                        stored_detail,
                        None if cache_path is None else str(cache_path),
                        str(asset_id),
                    ),
                )
            else:
                attempt = attempts + 1
                next_attempt = current + CLEANUP_RETRY_DELAYS[attempt - 1]
                stored_detail = (
                    "cleanup_retry_attempt=%d;next_at=%s;reason=%s"
                    % (attempt, sqlite_timestamp(next_attempt), reason)
                )
                cursor = connection.execute(
                    "UPDATE media_assets SET error_detail=?, expires_at=?, "
                    "cache_path=COALESCE(?, cache_path) "
                    "WHERE id=? AND status != 'expired'",
                    (
                        stored_detail,
                        sqlite_timestamp(next_attempt),
                        None if cache_path is None else str(cache_path),
                        str(asset_id),
                    ),
                )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT %s FROM media_assets WHERE id=?"
                % ", ".join(_MEDIA_COLUMNS),
                (str(asset_id),),
            ).fetchone()
            self._database.harden_files()
        assert row is not None
        return _row_dict(_MEDIA_COLUMNS, row)

    def create_task(
        self,
        user_id: int,
        source_message_id: str,
        kind: str,
        now: Optional[datetime] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, object]:
        user_id = _require_user_id(user_id)
        identifier = task_id or uuid.uuid4().hex
        created_at = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO long_tasks(
                  id, user_id, source_message_id, kind, status, stage,
                  cancel_requested, created_at
                ) VALUES (?, ?, ?, ?, 'queued', 'queued', 0, ?)
                """,
                (identifier, user_id, str(source_message_id), kind, created_at),
            )
            row = self._select_task(connection, identifier)
        assert row is not None
        self._database.harden_files()
        return _row_dict(_TASK_COLUMNS, row)

    def transition_task(
        self,
        task_id: str,
        expected: str,
        target: str,
        stage: str,
        detail: Optional[str] = None,
        now: Optional[datetime] = None,
        require_not_cancelled: bool = False,
    ) -> Dict[str, object]:
        if expected in TERMINAL_TASK_STATES:
            raise ValueError("terminal task cannot transition")
        if target not in ALLOWED_TRANSITIONS.get(expected, set()):
            raise ValueError("invalid task transition")

        timestamp = sqlite_timestamp(now)
        started_at = timestamp if target == "running" else None
        finished_at = timestamp if target in TERMINAL_TASK_STATES else None
        cancel_guard = " AND cancel_requested=0" if require_not_cancelled else ""
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._select_task(connection, task_id)
            if existing is None:
                raise KeyError("task not found")
            cursor = connection.execute(
                """
                UPDATE long_tasks
                SET status=?, stage=?,
                    started_at=COALESCE(started_at, ?),
                    finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND status=?
                %s
                """ % cancel_guard,
                (
                    target,
                    stage,
                    started_at,
                    finished_at,
                    str(task_id),
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("task state changed")
            self._insert_task_event(
                connection,
                str(task_id),
                "transition",
                stage,
                detail,
                timestamp,
            )
            if target in TERMINAL_TASK_STATES:
                connection.execute(
                    """
                    UPDATE outbox
                    SET status='failed', updated_at=?,
                        last_error='task notice suppressed'
                    WHERE kind='task_notice' AND status='pending'
                      AND dedupe_key LIKE ?
                    """,
                    (timestamp, "task:%s:notice:%%" % str(task_id)),
                )
            row = self._select_task(connection, task_id)
        assert row is not None
        return _row_dict(_TASK_COLUMNS, row)

    def request_cancel(
        self,
        task_id: str,
        user_id: int,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        timestamp = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE long_tasks
                SET cancel_requested=1
                WHERE id=? AND user_id=? AND status IN ('queued', 'running')
                  AND cancel_requested=0
                  AND stage != 'delivery_committed'
                """,
                (str(task_id), user_id),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_task_event(
                connection,
                str(task_id),
                "cancel_requested",
                None,
                None,
                timestamp,
            )
            row = self._select_task(connection, task_id)
            source_message_id = str(
                _row_dict(_TASK_COLUMNS, row)["source_message_id"]
            )
            if source_message_id:
                connection.execute(
                    """
                    UPDATE inbound_messages
                    SET status='completed', updated_at=?,
                        last_error='owner cancelled'
                    WHERE message_id=?
                    """,
                    (timestamp, source_message_id),
                )
            connection.execute(
                """
                UPDATE outbox
                SET status='failed', updated_at=?,
                    last_error='task notice suppressed'
                WHERE kind='task_notice' AND status='pending'
                  AND dedupe_key LIKE ?
                """,
                (timestamp, "task:%s:notice:%%" % str(task_id)),
            )
        return None if row is None else _row_dict(_TASK_COLUMNS, row)

    def commit_task_final(
        self,
        task_id: str,
        user_id: int,
        message: str,
        final_key: str,
        final_echo: str,
        observation_message: str,
        observation_key: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        user_id = _require_user_id(user_id)
        timestamp = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_task(connection, str(task_id))
            if task is None:
                raise KeyError("task not found")
            task_data = _row_dict(_TASK_COLUMNS, task)
            if task_data["stage"] == "delivery_committed":
                return (
                    task_data
                    if self._task_final_pair_is_valid(
                        connection,
                        task_data,
                        final_key,
                        final_echo,
                        observation_key,
                    )
                    else None
                )
            task_user_id = _strict_user_id(task_data["user_id"])
            requested_user_id = user_id
            if (
                final_key != "reply:%s" % task_data["source_message_id"]
                or observation_key
                != "observation:%s" % task_data["source_message_id"]
                or final_echo != "task:%s:final" % str(task_id)
                or task_user_id is None
                or requested_user_id != task_user_id
                or connection.execute(
                    "SELECT 1 FROM outbox WHERE dedupe_key IN (?, ?) LIMIT 1",
                    (final_key, observation_key),
                ).fetchone()
                is not None
            ):
                return None
            cursor = connection.execute(
                """
                UPDATE long_tasks SET stage='delivery_committed'
                WHERE id=? AND user_id=? AND status='running'
                  AND cancel_requested=0 AND stage != 'delivery_committed'
                """,
                (str(task_id), requested_user_id),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_task_event(
                connection,
                str(task_id),
                "delivery_committed",
                "delivery_committed",
                "最终回复已提交",
                timestamp,
            )
            self._insert_outbox(
                connection,
                "task_final",
                user_id,
                message,
                final_echo,
                final_key,
                timestamp,
            )
            self._insert_outbox(
                connection,
                "e3_observation",
                user_id,
                observation_message,
                final_key,
                observation_key,
                timestamp,
            )
            row = self._select_task(connection, str(task_id))
        self._database.harden_files()
        assert row is not None
        return _row_dict(_TASK_COLUMNS, row)

    @staticmethod
    def _task_final_pair_is_valid(
        connection, task, final_key, final_echo, observation_key
    ) -> bool:
        final = connection.execute(
            "SELECT kind, user_id, echo FROM outbox WHERE dedupe_key=?",
            (final_key,),
        ).fetchone()
        observation = connection.execute(
            "SELECT kind, user_id, echo FROM outbox WHERE dedupe_key=?",
            (observation_key,),
        ).fetchone()
        source = str(task["source_message_id"])
        task_user_id = _strict_user_id(task["user_id"])
        return bool(
            task_user_id is not None
            and final_key == "reply:%s" % source
            and observation_key == "observation:%s" % source
            and final_echo == "task:%s:final" % task["id"]
            and final is not None
            and final[0] == "task_final"
            and _strict_user_id(final[1]) == task_user_id
            and final[2] == final_echo
            and observation is not None
            and observation[0] == "e3_observation"
            and _strict_user_id(observation[1]) == task_user_id
            and observation[2] == final_key
        )

    def finish_task_delivery(
        self,
        task_id: str,
        succeeded: bool,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        target = "succeeded" if succeeded else "failed"
        stage = "succeeded" if succeeded else "delivery_failed"
        detail = "" if succeeded else "最终回复投递失败"
        timestamp = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE long_tasks
                SET status=?, stage=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND status='running' AND stage='delivery_committed'
                """,
                (target, stage, timestamp, str(task_id)),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_task_event(
                connection, str(task_id), "transition", stage, detail, timestamp
            )
            row = self._select_task(connection, str(task_id))
        assert row is not None
        return _row_dict(_TASK_COLUMNS, row)

    def mark_task_final_sent(
        self,
        outbox_id: int,
        claim_token: object,
        now: Optional[datetime] = None,
    ) -> bool:
        token = _claim_token_parts(claim_token)
        if token is None:
            return False
        timestamp = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT user_id, echo, dedupe_key FROM outbox
                WHERE id=? AND kind='task_final' AND status='sending'
                  AND updated_at=? AND attempts=?
                """,
                (int(outbox_id), token[0], token[1]),
            ).fetchone()
            if row is None or not isinstance(row[1], str):
                return False
            match = _TASK_FINAL_ECHO.fullmatch(row[1])
            key = str(row[2])
            if match is None or not key.startswith("reply:"):
                return False
            task_id = match.group(1)
            message_id = key.split(":", 1)[1]
            if not message_id:
                return False
            user_id = _strict_user_id(row[0])
            if user_id is None:
                return False
            task = connection.execute(
                """
                SELECT user_id, source_message_id, status, stage
                FROM long_tasks WHERE id=?
                """,
                (task_id,),
            ).fetchone()
            if (
                task is None
                or _strict_user_id(task[0]) != user_id
                or str(task[1]) != message_id
                or task[2:] != ("running", "delivery_committed")
            ):
                return False
            cursor = connection.execute(
                """
                UPDATE outbox
                SET status='sent', sent_at=?, updated_at=?, last_error=NULL
                WHERE id=? AND kind='task_final' AND status='sending'
                  AND updated_at=? AND attempts=?
                """,
                (
                    timestamp, timestamp, int(outbox_id), token[0], token[1]
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE inbound_messages
                SET status='completed', updated_at=?, last_error=NULL
                WHERE message_id=? AND user_id=?
                """,
                (timestamp, message_id, user_id),
            )
            task_cursor = connection.execute(
                """
                UPDATE long_tasks
                SET status='succeeded', stage='succeeded',
                    finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND user_id=? AND source_message_id=?
                  AND status='running' AND stage='delivery_committed'
                """,
                (timestamp, task_id, user_id, message_id),
            )
            if task_cursor.rowcount == 1:
                self._insert_task_event(
                    connection,
                    str(task_id),
                    "transition",
                    "succeeded",
                    "",
                    timestamp,
                )
        return True

    def mark_task_notice_sent(
        self,
        outbox_id: int,
        claim_token: object,
        now: Optional[datetime] = None,
    ) -> bool:
        token = _claim_token_parts(claim_token)
        if token is None:
            return False
        timestamp = sqlite_timestamp_microseconds(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT user_id, echo, dedupe_key FROM outbox
                WHERE id=? AND kind='task_notice' AND status='sending'
                  AND updated_at=? AND attempts=?
                """,
                (int(outbox_id), token[0], token[1]),
            ).fetchone()
            if row is None:
                return False
            key = str(row[2])
            match = _TASK_NOTICE_KEY.fullmatch(key)
            if match is None or row[1] != key:
                return False
            task_id = match.group(1)
            user_id = _strict_user_id(row[0])
            if user_id is None:
                return False
            task = connection.execute(
                "SELECT user_id FROM long_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task is None or _strict_user_id(task[0]) != user_id:
                return False
            cursor = connection.execute(
                """
                UPDATE outbox
                SET status='sent', sent_at=?, updated_at=?, last_error=NULL
                WHERE id=? AND kind='task_notice' AND status='sending'
                  AND updated_at=? AND attempts=?
                """,
                (
                    timestamp, timestamp, int(outbox_id), token[0], token[1]
                ),
            )
            if cursor.rowcount != 1:
                return False
            task_cursor = connection.execute(
                """
                UPDATE long_tasks SET last_notified_at=?
                WHERE id=? AND user_id=?
                """,
                (timestamp, task_id, user_id),
            )
            if task_cursor.rowcount == 1:
                self._insert_task_event(
                    connection,
                    str(task_id),
                    "notification_sent",
                    "notice",
                    "",
                    timestamp,
                )
        return True

    def mark_task_outbox_retry(
        self,
        outbox_id: int,
        claim_token: object,
        detail: str,
        retry_base_seconds: int,
        max_attempts: int,
        now: Optional[datetime] = None,
    ) -> str:
        accepted = OutboxRepository(self._database).mark_retry(
            outbox_id,
            claim_token,
            detail,
            retry_base_seconds,
            max_attempts,
            now,
        )
        if not accepted:
            return "stale"
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE id=?", (int(outbox_id),)
            ).fetchone()
        return "missing" if row is None else str(row[0])

    def append_task_event(
        self,
        task_id: str,
        event_type: str,
        stage: Optional[str],
        detail: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_task(connection, task_id)
            if task is None:
                raise KeyError("task not found")
            event_id = self._insert_task_event(
                connection,
                str(task_id),
                event_type,
                stage,
                detail,
                sqlite_timestamp(now),
            )
            row = connection.execute(
                "SELECT %s FROM task_events WHERE id=?"
                % ", ".join(_EVENT_COLUMNS),
                (event_id,),
            ).fetchone()
        assert row is not None
        return _row_dict(_EVENT_COLUMNS, row)

    def get_task(
        self, task_id: str, user_id: Optional[int] = None
    ) -> Optional[Dict[str, object]]:
        query = "SELECT %s FROM long_tasks WHERE id=?" % ", ".join(_TASK_COLUMNS)
        parameters = [str(task_id)]
        if user_id is not None:
            query += " AND user_id=?"
            parameters.append(user_id)
        with self._database.connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        return None if row is None else _row_dict(_TASK_COLUMNS, row)

    def record_task_progress(
        self,
        task_id: str,
        stage: str,
        detail: str = "",
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        timestamp = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE long_tasks SET stage=?
                WHERE id=? AND status IN ('queued', 'running')
                  AND stage != 'delivery_committed'
                """,
                (stage, str(task_id)),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_task_event(
                connection, str(task_id), "progress", stage, detail, timestamp
            )
            row = self._select_task(connection, str(task_id))
        assert row is not None
        return _row_dict(_TASK_COLUMNS, row)

    def record_task_notification_sent(
        self,
        task_id: str,
        stage: str,
        detail: str = "",
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        timestamp = sqlite_timestamp_microseconds(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE long_tasks SET last_notified_at=? WHERE id=?",
                (timestamp, str(task_id)),
            )
            if cursor.rowcount != 1:
                return None
            event_id = self._insert_task_event(
                connection, str(task_id), "notification_sent", stage, detail, timestamp
            )
            row = self._select_task(connection, str(task_id))
        assert row is not None
        return _row_dict(_TASK_COLUMNS, row)

    def record_task_notification_failure(
        self, task_id: str, now: Optional[datetime] = None
    ) -> Optional[Dict[str, object]]:
        timestamp = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._select_task(connection, str(task_id))
            if task is None:
                return None
            event_id = self._insert_task_event(
                connection,
                str(task_id),
                "notification_failed",
                None,
                "通知发送失败",
                timestamp,
            )
            row = connection.execute(
                "SELECT %s FROM task_events WHERE id=?"
                % ", ".join(_EVENT_COLUMNS),
                (event_id,),
            ).fetchone()
        assert row is not None
        return _row_dict(_EVENT_COLUMNS, row)

    def record_model_call(
        self,
        call_kind: str,
        provider: str,
        model: str,
        success: bool,
        user_id: Optional[int] = None,
        task_id: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_class: Optional[str] = None,
        estimated_cost: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        created_at = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO model_calls(
                  task_id, user_id, call_kind, provider, model, input_tokens,
                  output_tokens, duration_ms, success, error_class,
                  estimated_cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    user_id,
                    call_kind,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    duration_ms,
                    1 if success else 0,
                    error_class,
                    None if estimated_cost is None else float(estimated_cost),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM model_calls WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            columns = tuple(
                item[1]
                for item in connection.execute(
                    "PRAGMA table_info(model_calls)"
                ).fetchall()
            )
        assert row is not None
        return _row_dict(columns, row)

    def consumed_model_cost(
        self, day: date, provider: Optional[str] = None
    ) -> Decimal:
        start = datetime.combine(day, time.min, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        query = (
            "SELECT estimated_cost FROM model_calls "
            "WHERE created_at >= ? AND created_at < ? "
            "AND estimated_cost IS NOT NULL"
        )
        parameters = [sqlite_timestamp(start), sqlite_timestamp(end)]
        if provider is not None:
            query += " AND provider = ?"
            parameters.append(provider)
        with self._database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return sum(
            (Decimal(str(row[0])) for row in rows),
            Decimal("0"),
        )

    def record_health_change(
        self,
        component: str,
        status: str,
        detail: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        created_at = sqlite_timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                """
                SELECT status, created_at FROM health_events
                WHERE component=? ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (component,),
            ).fetchone()
            if latest is not None and (
                created_at < latest[1] or latest[0] == status
            ):
                return None
            cursor = connection.execute(
                """
                INSERT INTO health_events(component, status, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (component, status, detail, created_at),
            )
            row = connection.execute(
                "SELECT %s FROM health_events WHERE id=?"
                % ", ".join(_HEALTH_COLUMNS),
                (cursor.lastrowid,),
            ).fetchone()
        assert row is not None
        return _row_dict(_HEALTH_COLUMNS, row)

    def list_tasks(
        self,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, object]]:
        clauses = []
        parameters = []
        if user_id is not None:
            clauses.append("user_id=?")
            parameters.append(user_id)
        if status is not None:
            clauses.append("status=?")
            parameters.append(status)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(_bounded_limit(limit, _TASK_LIST_LIMIT))
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT %s FROM long_tasks%s ORDER BY created_at DESC, id DESC LIMIT ?"
                % (", ".join(_TASK_COLUMNS), where),
                tuple(parameters),
            ).fetchall()
        return [_row_dict(_TASK_COLUMNS, row) for row in rows]

    def task_events(
        self, task_id: str, limit: int = 200
    ) -> List[Dict[str, object]]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT %s FROM task_events
                WHERE task_id=? ORDER BY id ASC LIMIT ?
                """ % ", ".join(_EVENT_COLUMNS),
                (str(task_id), max(1, int(limit))),
            ).fetchall()
        return [_row_dict(_EVENT_COLUMNS, row) for row in rows]

    def model_stats(
        self,
        limit: int = 100,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[Dict[str, object]]:
        clauses = []
        parameters = []
        if since is not None:
            clauses.append("created_at >= ?")
            parameters.append(sqlite_timestamp(since))
        if until is not None:
            clauses.append("created_at <= ?")
            parameters.append(sqlite_timestamp(until))
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        bounded_limit = min(100, max(1, int(limit)))
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, call_kind, COUNT(*) AS calls,
                       SUM(success) AS successes,
                       CASE WHEN COUNT(input_tokens) = COUNT(*)
                            THEN SUM(input_tokens) END AS input_tokens,
                       CASE WHEN COUNT(output_tokens) = COUNT(*)
                            THEN SUM(output_tokens) END AS output_tokens,
                       CASE WHEN COUNT(duration_ms) = COUNT(*)
                            THEN AVG(duration_ms) END AS average_duration_ms,
                       CASE WHEN COUNT(estimated_cost) = COUNT(*)
                            THEN SUM(estimated_cost) END AS estimated_cost,
                       COUNT(input_tokens) AS known_input_token_calls,
                       COUNT(output_tokens) AS known_output_token_calls,
                       COUNT(duration_ms) AS known_duration_calls,
                       COUNT(estimated_cost) AS known_cost_calls
                FROM model_calls
                %s
                GROUP BY provider, model, call_kind
                ORDER BY calls DESC, provider, model, call_kind
                LIMIT ?
                """ % where,
                tuple(parameters + [bounded_limit]),
            ).fetchall()
            durations = {}
            for row in rows:
                duration_query = (
                    "SELECT duration_ms FROM model_calls "
                    "WHERE provider=? AND model=? AND call_kind=? "
                    "AND duration_ms IS NOT NULL"
                )
                duration_parameters = [row[0], row[1], row[2]]
                if since is not None:
                    duration_query += " AND created_at >= ?"
                    duration_parameters.append(sqlite_timestamp(since))
                if until is not None:
                    duration_query += " AND created_at <= ?"
                    duration_parameters.append(sqlite_timestamp(until))
                duration_query += " ORDER BY duration_ms ASC"
                samples = [
                    int(item[0])
                    for item in connection.execute(
                        duration_query, tuple(duration_parameters)
                    ).fetchall()
                ]
                durations[(row[0], row[1], row[2])] = samples
        columns = (
            "provider",
            "model",
            "call_kind",
            "calls",
            "successes",
            "input_tokens",
            "output_tokens",
            "average_duration_ms",
            "estimated_cost",
            "known_input_token_calls",
            "known_output_token_calls",
            "known_duration_calls",
            "known_cost_calls",
        )
        results = []
        for row in rows:
            result = _row_dict(columns, row)
            samples = durations[(row[0], row[1], row[2])]
            if samples:
                p50_rank = (50 * len(samples) + 99) // 100
                p95_rank = (95 * len(samples) + 99) // 100
                result["p50_latency_ms"] = samples[p50_rank - 1]
                result["p95_latency_ms"] = samples[p95_rank - 1]
            else:
                result["p50_latency_ms"] = None
                result["p95_latency_ms"] = None
            result["error_rate"] = (
                float((int(result["calls"]) - int(result["successes"])) / int(result["calls"]))
            )
            results.append(result)
        return results

    def health_timeline(
        self,
        component: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, object]]:
        where = "" if component is None else " WHERE component=?"
        parameters = [] if component is None else [component]
        parameters.append(_bounded_limit(limit, _HEALTH_TIMELINE_LIMIT))
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT %s FROM health_events%s "
                "ORDER BY created_at DESC, id DESC LIMIT ?"
                % (", ".join(_HEALTH_COLUMNS), where),
                tuple(parameters),
            ).fetchall()
        return [_row_dict(_HEALTH_COLUMNS, row) for row in rows]

    def media_stats(self) -> Dict[str, int]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM media_assets GROUP BY status"
            ).fetchall()
        result = {str(status): int(count) for status, count in rows}
        result["total"] = sum(result.values())
        return result

    def stats(self) -> Dict[str, int]:
        tables = {
            "media_assets": "media_assets",
            "tasks": "long_tasks",
            "task_events": "task_events",
            "model_calls": "model_calls",
            "health_events": "health_events",
        }
        with self._database.connect() as connection:
            return {
                name: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM %s" % table
                    ).fetchone()[0]
                )
                for name, table in tables.items()
            }

    @staticmethod
    def _select_task(connection, task_id: str):
        return connection.execute(
            "SELECT %s FROM long_tasks WHERE id=?"
            % ", ".join(_TASK_COLUMNS),
            (str(task_id),),
        ).fetchone()

    @staticmethod
    def _insert_task_event(
        connection,
        task_id: str,
        event_type: str,
        stage: Optional[str],
        detail: Optional[str],
        created_at: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO task_events(task_id, event_type, stage, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, event_type, stage, detail, created_at),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_outbox(
        connection, kind, user_id, message, echo, dedupe_key, timestamp
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO outbox(
              kind, user_id, message, echo, dedupe_key, status,
              attempts, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                kind,
                int(user_id),
                message,
                echo,
                dedupe_key,
                timestamp,
                timestamp,
                timestamp,
            ),
        )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "E4Repository",
    "TERMINAL_TASK_STATES",
]
