import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from .database import (
    SqliteDatabase,
    sqlite_timestamp,
    sqlite_timestamp_microseconds,
)


ALLOWED_RUN_TRANSITIONS = {
    "received": {"planned", "fallback", "rejected", "failed", "cancelled"},
    "planned": {"executing", "fallback", "rejected", "failed", "cancelled"},
    "executing": {"verifying", "fallback", "rejected", "failed", "cancelled"},
    "verifying": {"accepted", "fallback", "rejected", "failed", "cancelled"},
    "accepted": {"delivered", "failed", "cancelled"},
    "delivered": set(),
    "fallback": set(),
    "rejected": set(),
    "failed": set(),
    "cancelled": set(),
}
TERMINAL_RUN_STATES = {"delivered", "fallback", "rejected", "failed", "cancelled"}
F0_MODES = {"legacy_direct", "direct"}
SQLITE_MAX_INTEGER = 2**63 - 1
RUN_LIST_LIMIT = 100
EVENT_LIST_LIMIT = 500
EVENT_DETAIL_LIMIT = 512
SAFE_ERROR_CLASS_LIMIT = 128
SOURCE_MESSAGE_ID_INPUT_LIMIT = 1024
TRACE_STAGES = {"planned", "executing", "verifying"}
TRACE_TERMINAL_STATES = {"accepted", "rejected", "failed"}
TRACE_STAGE_ORDER = ("received", "planned", "executing", "verifying")

_RUN_COLUMNS = (
    "run_id",
    "user_id",
    "source_message_id",
    "session_key",
    "mode",
    "risk_level",
    "status",
    "config_version",
    "started_at",
    "finished_at",
    "fallback_reason",
    "safe_error_class",
)
_EVENT_COLUMNS = ("id", "run_id", "event_type", "stage", "detail", "created_at")
_CONFIG_COLUMNS = (
    "version",
    "config_json",
    "config_sha256",
    "status",
    "created_at",
    "activated_at",
)


def _row_dict(columns, row):
    return dict(zip(columns, row))


def _require_positive_integer(value, field):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > SQLITE_MAX_INTEGER
    ):
        raise ValueError(f"{field} must be a positive SQLite integer")
    return value


def _require_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _event_detail(value):
    if not isinstance(value, str):
        raise ValueError("detail must be a string")
    if len(value) > EVENT_DETAIL_LIMIT:
        raise ValueError("detail exceeds 512 characters")
    return value


def _bounded_limit(value, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return min(maximum, max(1, value))


def _safe_error_class(value):
    if not isinstance(value, str):
        return "Error"
    candidate = value.strip()
    if (
        len(candidate) <= SAFE_ERROR_CLASS_LIMIT
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", candidate)
    ):
        return candidate
    return "Error"


def _opaque_source_message_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        return None
    if not raw or len(raw) > SOURCE_MESSAGE_ID_INPUT_LIMIT:
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class SqliteOrchestrationTraceSink:
    def __init__(self, repository):
        self._repository = repository

    def start(self, contract, mode: str, config_version: int) -> str:
        request = contract.request
        message_id = _opaque_source_message_id(request.message_id)
        session_key = f"agent:main:qq-{request.user_id}"
        run = self._repository.create_run(
            user_id=request.user_id,
            source_message_id=message_id,
            session_key=session_key,
            mode=mode,
            risk_level=contract.risk_level,
            config_version=config_version,
        )
        return str(run["run_id"])

    def transition(self, run_id: str, stage: str, detail: str = "") -> None:
        if stage not in TRACE_STAGES:
            raise ValueError("unknown orchestration trace stage")
        self._transition(run_id, stage, stage, "")

    def finish(self, run_id: str, status: str, detail: str = "") -> None:
        if status not in TRACE_TERMINAL_STATES:
            raise ValueError("unknown orchestration trace status")
        if status == "failed":
            safe_detail = _safe_error_class(detail)
        elif status == "rejected":
            safe_detail = (
                "budget_rejected"
                if detail == "budget_rejected"
                else "verification_rejected"
            )
        else:
            safe_detail = ""
        self._transition(run_id, status, status, safe_detail)

    def _transition(
        self, run_id: str, target: str, event_type: str, detail: str
    ) -> None:
        run = self._repository.get_run(run_id)
        if run is None:
            raise ValueError("orchestration trace run does not exist")
        current = str(run["status"])
        if current == target:
            return
        if current not in TRACE_STAGE_ORDER:
            raise ValueError("orchestration trace run is already terminal")
        if target in TRACE_STAGES:
            target_index = TRACE_STAGE_ORDER.index(target)
            current_index = TRACE_STAGE_ORDER.index(current)
            if target_index <= current_index:
                return
            for stage in TRACE_STAGE_ORDER[current_index + 1 : target_index + 1]:
                self._repository.transition_run(
                    run_id,
                    current,
                    stage,
                    stage,
                    detail if stage == target else "",
                )
                current = stage
            return
        if target == "accepted" and current != "verifying":
            current_index = TRACE_STAGE_ORDER.index(current)
            for stage in TRACE_STAGE_ORDER[current_index + 1 :]:
                self._repository.transition_run(
                    run_id, current, stage, stage, ""
                )
                current = stage
        self._repository.transition_run(
            run_id, current, target, event_type, detail
        )


def _config_dict(row):
    result = _row_dict(_CONFIG_COLUMNS, row)
    result["config"] = json.loads(str(result["config_json"]))
    return result


def _canonical_f0_config(config):
    if type(config) is not dict or set(config) != {"mode"}:
        raise ValueError("F0 orchestration config must contain only mode")
    mode = config.get("mode")
    if type(mode) is not str or mode not in F0_MODES:
        raise ValueError("F0 orchestration mode must be legacy_direct or direct")
    canonical = json.dumps(
        {"mode": mode},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


class OrchestrationRepository:
    def __init__(self, database: SqliteDatabase):
        self._database = database
        self._reconciliation_cursor = ("", "")

    def create_run(
        self,
        user_id: int,
        source_message_id: Optional[str],
        session_key: str,
        mode: str,
        risk_level: str,
        config_version: int,
        now: Optional[datetime] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, object]:
        owner = _require_positive_integer(user_id, "user_id")
        identifier = (
            uuid.uuid4().hex
            if run_id is None
            else _require_string(run_id, "run_id")
        )
        if source_message_id is not None:
            source_message_id = _require_string(
                source_message_id, "source_message_id"
            )
        session = _require_string(session_key, "session_key")
        selected_mode = _require_string(mode, "mode")
        if selected_mode not in F0_MODES:
            raise ValueError("mode must be legacy_direct or direct")
        risk = _require_string(risk_level, "risk_level")
        version = _require_positive_integer(config_version, "config_version")
        timestamp = sqlite_timestamp_microseconds(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO orchestration_runs(
                      run_id, user_id, source_message_id, session_key, mode,
                      risk_level, status, config_version, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'received', ?, ?)
                    """,
                    (
                        identifier,
                        owner,
                        source_message_id,
                        session,
                        selected_mode,
                        risk,
                        version,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO orchestration_events(
                      run_id, event_type, stage, detail, created_at
                    ) VALUES (?, 'received', 'received', '', ?)
                    """,
                    (identifier, timestamp),
                )
                row = self._select_run(connection, identifier)
                self._database.harden_files()
        return _row_dict(_RUN_COLUMNS, row)

    def transition_run(
        self,
        run_id: str,
        expected: str,
        target: str,
        event_type: str,
        detail: str = "",
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        identifier = _require_string(run_id, "run_id")
        current = _require_string(expected, "expected")
        destination = _require_string(target, "target")
        event = _require_string(event_type, "event_type")
        safe_detail = _event_detail(detail)
        if current not in ALLOWED_RUN_TRANSITIONS:
            raise ValueError("unknown orchestration run state")
        if destination not in ALLOWED_RUN_TRANSITIONS[current]:
            raise ValueError("orchestration run transition is not allowed")
        timestamp = sqlite_timestamp(now)
        finished_at = (
            timestamp if destination in TERMINAL_RUN_STATES else None
        )
        fallback_reason = (
            safe_detail
            if destination in {"rejected", "fallback"} and safe_detail
            else None
        )
        safe_error_class = safe_detail if destination == "failed" else None
        with self._database.locked():
            with self._database.connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE orchestration_runs
                    SET status=?, finished_at=?, fallback_reason=?,
                        safe_error_class=?
                    WHERE run_id=? AND status=?
                    """,
                    (
                        destination,
                        finished_at,
                        fallback_reason,
                        safe_error_class,
                        identifier,
                        current,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("orchestration run state changed")
                connection.execute(
                    """
                    INSERT INTO orchestration_events(
                      run_id, event_type, stage, detail, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (identifier, event, destination, safe_detail, timestamp),
                )
                row = self._select_run(connection, identifier)
                self._database.harden_files()
        return _row_dict(_RUN_COLUMNS, row)

    def complete_delivery(
        self,
        user_id: int,
        source_message_id: object,
        now: Optional[datetime] = None,
    ) -> bool:
        owner = _require_positive_integer(user_id, "user_id")
        source_hash = _opaque_source_message_id(source_message_id)
        if source_hash is None:
            return False
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT run_id FROM orchestration_runs "
                    "WHERE user_id=? AND source_message_id=? AND status='accepted' "
                    "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                    (owner, source_hash),
                ).fetchone()
                if row is None:
                    return False
                run_id = str(row[0])
                cursor = connection.execute(
                    "UPDATE orchestration_runs SET status='delivered', finished_at=? "
                    "WHERE run_id=? AND status='accepted'",
                    (timestamp, run_id),
                )
                if cursor.rowcount != 1:
                    return False
                connection.execute(
                    """
                    INSERT INTO orchestration_events(
                      run_id, event_type, stage, detail, created_at
                    ) VALUES (?, 'delivered', 'delivered', '', ?)
                    """,
                    (run_id, timestamp),
                )
                self._database.harden_files()
        return True

    def reconcile_deliveries(
        self, now: Optional[datetime] = None, limit: int = 50
    ) -> int:
        timestamp = sqlite_timestamp(now)
        bounded = _bounded_limit(limit, RUN_LIST_LIMIT)
        completed = 0
        with self._database.locked():
            with self._database.connect() as connection:
                accepted = connection.execute(
                    "SELECT run_id, user_id, source_message_id, started_at "
                    "FROM orchestration_runs "
                    "WHERE status='accepted' AND (started_at, run_id) > (?, ?) "
                    "ORDER BY started_at ASC, run_id ASC LIMIT ?",
                    (*self._reconciliation_cursor, bounded),
                ).fetchall()
                if not accepted:
                    self._reconciliation_cursor = ("", "")
                    return 0
                self._reconciliation_cursor = (
                    str(accepted[-1][3]), str(accepted[-1][0])
                )
                delivered_by_user = {}
                for user_id in {int(row[1]) for row in accepted}:
                    rows = connection.execute(
                        "SELECT dedupe_key FROM outbox "
                        "WHERE user_id=? AND status='sent' "
                        "AND kind IN ('reply','task_final') "
                        "AND dedupe_key LIKE 'reply:%'",
                        (user_id,),
                    ).fetchall()
                    delivered_by_user[user_id] = {
                        _opaque_source_message_id(str(row[0]).split(":", 1)[1])
                        for row in rows
                        if ":" in str(row[0])
                    }
                for run_id, user_id, source_hash, _ in accepted:
                    if source_hash not in delivered_by_user.get(int(user_id), set()):
                        continue
                    cursor = connection.execute(
                        "UPDATE orchestration_runs "
                        "SET status='delivered', finished_at=? "
                        "WHERE run_id=? AND status='accepted'",
                        (timestamp, run_id),
                    )
                    if cursor.rowcount != 1:
                        continue
                    connection.execute(
                        """
                        INSERT INTO orchestration_events(
                          run_id, event_type, stage, detail, created_at
                        ) VALUES (?, 'delivered', 'delivered', '', ?)
                        """,
                        (run_id, timestamp),
                    )
                    completed += 1
                if completed:
                    self._database.harden_files()
        return completed

    def append_event(
        self,
        run_id: str,
        event_type: str,
        stage: str,
        detail: str = "",
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        identifier = _require_string(run_id, "run_id")
        event = _require_string(event_type, "event_type")
        event_stage = _require_string(stage, "stage")
        safe_detail = _event_detail(detail)
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                if self._select_run(connection, identifier) is None:
                    raise ValueError("orchestration run does not exist")
                cursor = connection.execute(
                    """
                    INSERT INTO orchestration_events(
                      run_id, event_type, stage, detail, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        event,
                        event_stage,
                        safe_detail,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT %s FROM orchestration_events WHERE id=?"
                    % ", ".join(_EVENT_COLUMNS),
                    (cursor.lastrowid,),
                ).fetchone()
                self._database.harden_files()
        return _row_dict(_EVENT_COLUMNS, row)

    def get_run(self, run_id: str) -> Optional[Dict[str, object]]:
        identifier = _require_string(run_id, "run_id")
        with self._database.connect() as connection:
            row = self._select_run(connection, identifier)
        return None if row is None else _row_dict(_RUN_COLUMNS, row)

    def latest_run(self, user_id: int) -> Optional[Dict[str, object]]:
        owner = _require_positive_integer(user_id, "user_id")
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT %s FROM orchestration_runs WHERE user_id=? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1"
                % ", ".join(_RUN_COLUMNS),
                (owner,),
            ).fetchone()
        return None if row is None else _row_dict(_RUN_COLUMNS, row)

    def list_runs(self, limit: int) -> List[Dict[str, object]]:
        bounded = _bounded_limit(limit, RUN_LIST_LIMIT)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT %s FROM orchestration_runs "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?"
                % ", ".join(_RUN_COLUMNS),
                (bounded,),
            ).fetchall()
        return [_row_dict(_RUN_COLUMNS, row) for row in rows]

    def events(self, run_id: str, limit: int = EVENT_LIST_LIMIT):
        identifier = _require_string(run_id, "run_id")
        bounded = _bounded_limit(limit, EVENT_LIST_LIMIT)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT %s FROM orchestration_events WHERE run_id=? "
                "ORDER BY id ASC LIMIT ?" % ", ".join(_EVENT_COLUMNS),
                (identifier, bounded),
            ).fetchall()
        return [_row_dict(_EVENT_COLUMNS, row) for row in rows]

    def create_config_version(
        self,
        config,
        activate: bool,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if type(activate) is not bool:
            raise ValueError("activate must be a boolean")
        canonical, digest = _canonical_f0_config(config)
        timestamp = sqlite_timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT %s FROM orchestration_config_versions "
                    "WHERE config_sha256=?" % ", ".join(_CONFIG_COLUMNS),
                    (digest,),
                ).fetchone()
                if activate:
                    connection.execute(
                        "UPDATE orchestration_config_versions "
                        "SET status='retired' WHERE status='active'"
                    )
                if row is None:
                    status = "active" if activate else "draft"
                    cursor = connection.execute(
                        """
                        INSERT INTO orchestration_config_versions(
                          config_json, config_sha256, status, created_at,
                          activated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            canonical,
                            digest,
                            status,
                            timestamp,
                            timestamp if activate else None,
                        ),
                    )
                    version = cursor.lastrowid
                else:
                    version = row[0]
                    if activate:
                        connection.execute(
                            """
                            UPDATE orchestration_config_versions
                            SET status='active', activated_at=?
                            WHERE version=?
                            """,
                            (timestamp, version),
                        )
                selected = connection.execute(
                    "SELECT %s FROM orchestration_config_versions "
                    "WHERE version=?" % ", ".join(_CONFIG_COLUMNS),
                    (version,),
                ).fetchone()
                self._database.harden_files()
        return _config_dict(selected)

    def get_config(self, version: int) -> Optional[Dict[str, object]]:
        identifier = _require_positive_integer(version, "version")
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT %s FROM orchestration_config_versions WHERE version=?"
                % ", ".join(_CONFIG_COLUMNS),
                (identifier,),
            ).fetchone()
        return None if row is None else _config_dict(row)

    def active_config(self) -> Optional[Dict[str, object]]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT %s FROM orchestration_config_versions "
                "WHERE status='active' LIMIT 1" % ", ".join(_CONFIG_COLUMNS)
            ).fetchone()
        return None if row is None else _config_dict(row)

    @staticmethod
    def _select_run(connection, run_id):
        return connection.execute(
            "SELECT %s FROM orchestration_runs WHERE run_id=?"
            % ", ".join(_RUN_COLUMNS),
            (run_id,),
        ).fetchone()
