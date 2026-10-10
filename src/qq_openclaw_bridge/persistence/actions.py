import re
import sqlite3
from datetime import datetime
from typing import Dict, Optional

from ..domain.actions import ActionIntent
from .database import SqliteDatabase, sqlite_timestamp


ACTION_COLUMNS = (
    "intent_id",
    "approval_id",
    "user_id",
    "source_message_id",
    "session_key",
    "action_type",
    "adapter_id",
    "schema_version",
    "payload_json",
    "payload_sha256",
    "config_version",
    "risk_tier",
    "impact_summary",
    "status",
    "created_at",
    "expires_at",
    "approved_at",
    "attempt",
    "result_class",
    "safe_result_summary",
    "finished_at",
)
SQLITE_MAX_INTEGER = 2**63 - 1
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
RESULT_CLASS_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")


def _row_dict(row) -> Dict[str, object]:
    return dict(zip(ACTION_COLUMNS, row))


def _positive_integer(value: object, field: str) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > SQLITE_MAX_INTEGER
    ):
        raise ValueError("%s must be a positive SQLite integer" % field)
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 512:
        raise ValueError("%s must be a bounded non-empty string" % field)
    return value


def _payload_hash(value: object) -> str:
    if type(value) is not str or HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("payload_sha256 must be lowercase SHA-256")
    return value


def _result_class(value: object) -> str:
    if type(value) is not str or RESULT_CLASS_PATTERN.fullmatch(value) is None:
        raise ValueError("result_class must be a safe class identifier")
    return value


def _safe_summary(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 512
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError("safe_result_summary must be bounded printable text")
    return value


def _timestamp(value: Optional[datetime]) -> str:
    if value is not None:
        try:
            aware = value.tzinfo is not None and value.utcoffset() is not None
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("now must be timezone-aware") from error
        if not aware:
            raise ValueError("now must be timezone-aware")
    return sqlite_timestamp(value)


class ActionIntentRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def propose(self, intent: ActionIntent) -> Dict[str, object]:
        if type(intent) is not ActionIntent:
            raise ValueError("intent must be an ActionIntent")
        values = (
            intent.intent_id,
            intent.approval_id,
            intent.user_id,
            intent.source_message_id,
            intent.session_key,
            intent.action_type,
            intent.adapter_id,
            intent.schema_version,
            intent.payload_json,
            intent.payload_sha256,
            intent.config_version,
            intent.risk_tier,
            intent.impact_summary,
            sqlite_timestamp(intent.created_at),
            sqlite_timestamp(intent.expires_at),
        )
        try:
            with self._database.locked(), self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO action_intents(
                      intent_id, approval_id, user_id, source_message_id,
                      session_key, action_type, adapter_id, schema_version,
                      payload_json, payload_sha256, config_version, risk_tier,
                      impact_summary, status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'proposed', ?, ?)
                    """,
                    values,
                )
                row = self._select(connection, intent.intent_id)
                self._database.harden_files()
        except sqlite3.IntegrityError as error:
            raise ValueError("action intent already exists") from error
        assert row is not None
        return _row_dict(row)

    def approve(
        self,
        intent_id: str,
        approval_id: str,
        user_id: int,
        payload_sha256: str,
        config_version: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        identifier = _identifier(intent_id, "intent_id")
        approval = _identifier(approval_id, "approval_id")
        owner = _positive_integer(user_id, "user_id")
        digest = _payload_hash(payload_sha256)
        version = _positive_integer(config_version, "config_version")
        timestamp = _timestamp(now)
        return self._mutate(
            """
            UPDATE action_intents
            SET status='approved', approved_at=?
            WHERE intent_id=? AND approval_id=? AND user_id=?
              AND payload_sha256=? AND config_version=?
              AND status='proposed' AND expires_at>?
            """,
            (
                timestamp,
                identifier,
                approval,
                owner,
                digest,
                version,
                timestamp,
            ),
            identifier,
            "action intent cannot be approved",
        )

    def consume(
        self,
        intent_id: str,
        user_id: int,
        payload_sha256: str,
        config_version: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        identifier = _identifier(intent_id, "intent_id")
        owner = _positive_integer(user_id, "user_id")
        digest = _payload_hash(payload_sha256)
        version = _positive_integer(config_version, "config_version")
        timestamp = _timestamp(now)
        return self._mutate(
            """
            UPDATE action_intents
            SET status='executing', attempt=attempt+1
            WHERE intent_id=? AND user_id=? AND payload_sha256=?
              AND config_version=? AND status='approved' AND expires_at>?
            """,
            (identifier, owner, digest, version, timestamp),
            identifier,
            "action intent cannot be consumed",
        )

    def approve_and_consume(
        self,
        intent_id: str,
        approval_id: str,
        user_id: int,
        action_type: str,
        adapter_id: str,
        payload_json: str,
        payload_sha256: str,
        config_version: int,
        expires_at: datetime,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        identifier = _identifier(intent_id, "intent_id")
        approval = _identifier(approval_id, "approval_id")
        owner = _positive_integer(user_id, "user_id")
        action = _identifier(action_type, "action_type")
        adapter = _identifier(adapter_id, "adapter_id")
        if type(payload_json) is not str or len(payload_json) > 65536:
            raise ValueError("payload_json must be a bounded string")
        digest = _payload_hash(payload_sha256)
        version = _positive_integer(config_version, "config_version")
        expected_expiry = _timestamp(expires_at)
        timestamp = _timestamp(now)
        return self._mutate(
            """
            UPDATE action_intents
            SET status='executing', approved_at=?, attempt=attempt+1
            WHERE intent_id=? AND approval_id=? AND user_id=?
              AND action_type=? AND adapter_id=? AND payload_json=?
              AND payload_sha256=? AND config_version=?
              AND expires_at=? AND status='proposed' AND expires_at>?
            """,
            (
                timestamp,
                identifier,
                approval,
                owner,
                action,
                adapter,
                payload_json,
                digest,
                version,
                expected_expiry,
                timestamp,
            ),
            identifier,
            "action intent cannot be approved and consumed",
        )

    def find_executing(
        self,
        user_id: int,
        action_type: str,
        payload_sha256: str,
    ) -> Optional[Dict[str, object]]:
        owner = _positive_integer(user_id, "user_id")
        action = _identifier(action_type, "action_type")
        digest = _payload_hash(payload_sha256)
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT %s FROM action_intents "
                "WHERE user_id=? AND action_type=? AND payload_sha256=? "
                "AND status IN ('executing', 'unknown') "
                "ORDER BY approved_at DESC LIMIT 1"
                % ", ".join(ACTION_COLUMNS),
                (owner, action, digest),
            ).fetchone()
        return None if row is None else _row_dict(row)

    def recover_executing_as_unknown(
        self, now: Optional[datetime] = None
    ) -> int:
        timestamp = _timestamp(now)
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE action_intents
                SET status='unknown', result_class='InterruptedProcess',
                    safe_result_summary='process ended during local action',
                    finished_at=?
                WHERE status='executing'
                """,
                (timestamp,),
            )
            self._database.harden_files()
        return cursor.rowcount

    def mark_unknown(
        self,
        intent_id: str,
        user_id: int,
        payload_sha256: str,
        config_version: int,
        result_class: str,
        safe_result_summary: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        return self._transition_executing(
            intent_id,
            user_id,
            payload_sha256,
            config_version,
            "unknown",
            result_class,
            safe_result_summary,
            now,
        )

    def resolve_unknown(
        self,
        intent_id: str,
        user_id: int,
        payload_sha256: str,
        config_version: int,
        status: str,
        result_class: str,
        safe_result_summary: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if type(status) is not str or status not in {"succeeded", "failed"}:
            raise ValueError("unknown action resolution must be succeeded or failed")
        return self._transition(
            intent_id,
            user_id,
            payload_sha256,
            config_version,
            status,
            result_class,
            safe_result_summary,
            "unknown",
            now,
            "unknown action intent cannot be resolved",
        )

    def finish(
        self,
        intent_id: str,
        user_id: int,
        payload_sha256: str,
        config_version: int,
        status: str,
        result_class: str,
        safe_result_summary: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if type(status) is not str or status not in {"succeeded", "failed"}:
            raise ValueError("finish status must be succeeded or failed")
        return self._transition_executing(
            intent_id,
            user_id,
            payload_sha256,
            config_version,
            status,
            result_class,
            safe_result_summary,
            now,
        )

    def cancel(
        self,
        intent_id: str,
        user_id: int,
        payload_sha256: str,
        config_version: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        identifier = _identifier(intent_id, "intent_id")
        owner = _positive_integer(user_id, "user_id")
        digest = _payload_hash(payload_sha256)
        version = _positive_integer(config_version, "config_version")
        timestamp = _timestamp(now)
        return self._mutate(
            """
            UPDATE action_intents
            SET status='cancelled', finished_at=?
            WHERE intent_id=? AND user_id=? AND payload_sha256=?
              AND config_version=? AND status IN ('proposed', 'approved')
            """,
            (timestamp, identifier, owner, digest, version),
            identifier,
            "action intent cannot be cancelled",
        )

    def get(self, intent_id: str) -> Optional[Dict[str, object]]:
        identifier = _identifier(intent_id, "intent_id")
        with self._database.connect() as connection:
            row = self._select(connection, identifier)
        return None if row is None else _row_dict(row)

    def get_by_approval(
        self, approval_id: str
    ) -> Optional[Dict[str, object]]:
        approval = _identifier(approval_id, "approval_id")
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT %s FROM action_intents WHERE approval_id=?"
                % ", ".join(ACTION_COLUMNS),
                (approval,),
            ).fetchone()
        return None if row is None else _row_dict(row)

    def _mutate(self, sql, parameters, intent_id, message):
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(sql, parameters)
            if cursor.rowcount != 1:
                raise ValueError(message)
            row = self._select(connection, intent_id)
            self._database.harden_files()
        assert row is not None
        return _row_dict(row)

    def _transition_executing(
        self,
        intent_id,
        user_id,
        payload_sha256,
        config_version,
        status,
        result_class,
        safe_result_summary,
        now,
    ):
        return self._transition(
            intent_id,
            user_id,
            payload_sha256,
            config_version,
            status,
            result_class,
            safe_result_summary,
            "executing",
            now,
            "action intent cannot be finished",
        )

    def _transition(
        self,
        intent_id,
        user_id,
        payload_sha256,
        config_version,
        status,
        result_class,
        safe_result_summary,
        expected_status,
        now,
        message,
    ):
        identifier = _identifier(intent_id, "intent_id")
        owner = _positive_integer(user_id, "user_id")
        digest = _payload_hash(payload_sha256)
        version = _positive_integer(config_version, "config_version")
        result = _result_class(result_class)
        summary = _safe_summary(safe_result_summary)
        timestamp = _timestamp(now)
        return self._mutate(
            """
            UPDATE action_intents
            SET status=?, result_class=?, safe_result_summary=?, finished_at=?
            WHERE intent_id=? AND user_id=? AND payload_sha256=?
              AND config_version=? AND status=?
            """,
            (
                status,
                result,
                summary,
                timestamp,
                identifier,
                owner,
                digest,
                version,
                expected_status,
            ),
            identifier,
            message,
        )

    @staticmethod
    def _select(connection, intent_id):
        return connection.execute(
            "SELECT %s FROM action_intents WHERE intent_id=?"
            % ", ".join(ACTION_COLUMNS),
            (intent_id,),
        ).fetchone()


__all__ = ["ActionIntentRepository"]
