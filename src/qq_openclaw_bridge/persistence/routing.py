"""SQLite persistence for shadow-routing configurations and decisions."""

import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .database import SqliteDatabase, sqlite_timestamp_microseconds


SQLITE_MAX_INTEGER = 2**63 - 1
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z"
)
_SAFE_ERROR_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}\Z")
_LEVELS = ("L0", "L1", "L2", "L3")
_MODEL_LEVELS = ("L1", "L2", "L3")
_TASK_KINDS = {
    "smalltalk", "question", "analysis", "creative", "coding", "planning",
    "summarization", "translation", "task", "external_action", "brainstorm",
    "other", "local_command", "forced_risk",
}
_VERDICTS = {"correct", "upgrade", "downgrade"}
_DECISION_COLUMNS = (
    "id", "user_id", "source_message_sha256", "message_sha256",
    "message_preview", "routing_config_version", "rule_version",
    "dispatch_overhead_ms", "status", "rule_level", "model_level",
    "final_level", "confidence", "task_kind", "risk_flags_json",
    "reasons_json", "brainstorm_required", "brainstorm_mode",
    "classifier_provider", "classifier_model", "classifier_duration_ms",
    "safe_error_class", "created_at", "updated_at", "finished_at",
    "classifier_attempted", "classifier_attempted_at",
)
_CONFIG_COLUMNS = (
    "version", "config_json", "config_sha256", "status", "created_at",
    "activated_at",
)
_LABEL_COLUMNS = (
    "id", "decision_id", "user_id", "verdict", "correct_level", "note",
    "created_at", "updated_at",
)
_ROUTING_CONFIG_KEYS = {
    "enabled", "classifier_enabled", "classifier_model",
    "confidence_threshold_percent", "classifier_timeout_seconds",
    "max_daily_calls", "failure_threshold", "cooldown_seconds", "rule_version",
}
_DASHBOARD_ROUTING_CONFIG_KEYS = _ROUTING_CONFIG_KEYS - {"enabled"}


def _row_dict(columns, row) -> Dict[str, object]:
    return dict(zip(columns, row))


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0 or value > SQLITE_MAX_INTEGER:
        raise ValueError("%s must be a positive SQLite integer" % field)
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0 or value > SQLITE_MAX_INTEGER:
        raise ValueError("%s must be a non-negative SQLite integer" % field)
    return value


def _hash(value: object, field: str) -> str:
    if type(value) is not str or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("%s must be lowercase SHA-256" % field)
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError("%s must be a bounded identifier" % field)
    if len(value) > 128:
        raise ValueError("%s must be a bounded identifier" % field)
    return value


def _level(value: object, field: str, allowed=_LEVELS) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError("%s must be a routing level" % field)
    return value


def _optional_level(value: object, field: str, allowed=_LEVELS) -> Optional[str]:
    if value is None:
        return None
    return _level(value, field, allowed)


def _task_kind(value: object) -> str:
    if type(value) is not str or value not in _TASK_KINDS:
        raise ValueError("task_kind is not allowed")
    return value


def _confidence(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("confidence must be finite and numeric")
    if value < 0 or value > 1:
        raise ValueError("confidence must be between zero and one")
    return float(value)


def _json_strings(value: object, field: str) -> Tuple[str, ...]:
    if type(value) is not tuple or len(value) > 8:
        raise ValueError("%s must be a tuple of at most eight strings" % field)
    if any(
        type(item) is not str
        or not item
        or len(item) > 64
        or any(not char.isprintable() for char in item)
        for item in value
    ):
        raise ValueError("%s must contain bounded printable strings" % field)
    return value


def _canonical_array(value: Tuple[str, ...]) -> str:
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def _preview(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 160
        or any(not char.isprintable() for char in value)
    ):
        raise ValueError("message_preview must be bounded printable text")
    return value


def _safe_error_class(value: object) -> str:
    if type(value) is not str or _SAFE_ERROR_PATTERN.fullmatch(value) is None:
        raise ValueError("safe_error_class must be a safe class identifier")
    return value


def _note(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 512
        or any(not char.isprintable() for char in value)
    ):
        raise ValueError("note must be bounded printable text")
    return value


def _timestamp(value: Optional[datetime]) -> str:
    if value is not None:
        try:
            aware = value.tzinfo is not None and value.utcoffset() is not None
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("now must be timezone-aware") from error
        if not aware:
            raise ValueError("now must be timezone-aware")
    return sqlite_timestamp_microseconds(value)


def _next_timestamp(previous: str, now: Optional[datetime]) -> str:
    candidate = _timestamp(now)
    if candidate > previous:
        return candidate
    try:
        prior = datetime.fromisoformat(previous)
    except ValueError:
        return candidate
    return (prior + timedelta(microseconds=1)).isoformat(timespec="microseconds")


def _decision_dict(row) -> Dict[str, object]:
    result = _row_dict(_DECISION_COLUMNS, row)
    result["risk_flags"] = tuple(json.loads(str(result["risk_flags_json"])))
    result["reasons"] = tuple(json.loads(str(result["reasons_json"])))
    result["brainstorm_required"] = bool(result["brainstorm_required"])
    result["classifier_attempted"] = bool(result["classifier_attempted"])
    return result


def _config_dict(row) -> Dict[str, object]:
    result = _row_dict(_CONFIG_COLUMNS, row)
    result["config"] = json.loads(str(result["config_json"]))
    return result


def _canonical_config(config: object) -> Tuple[str, str]:
    if type(config) is not dict or set(config) != _ROUTING_CONFIG_KEYS:
        raise ValueError("routing config has unknown or missing keys")
    if type(config["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(config["classifier_enabled"]) is not bool:
        raise ValueError("classifier_enabled must be boolean")
    _identifier(config["classifier_model"], "classifier_model")
    _identifier(config["rule_version"], "rule_version")
    bounds = (
        ("confidence_threshold_percent", 50, 100),
        ("classifier_timeout_seconds", 1, 120),
        ("max_daily_calls", 0, 10000),
        ("failure_threshold", 1, 20),
        ("cooldown_seconds", 1, 3600),
    )
    for field, minimum, maximum in bounds:
        value = config[field]
        if type(value) is not int or value < minimum or value > maximum:
            raise ValueError("%s is outside its allowed range" % field)
    canonical = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_routing_config(config: object, expected_sha256: object) -> None:
    """Validate a persisted config and bind it to its canonical digest."""
    _, actual_sha256 = _canonical_config(config)
    persisted_sha256 = _hash(expected_sha256, "config_sha256")
    if not hmac.compare_digest(actual_sha256, persisted_sha256):
        raise ValueError("routing config digest does not match payload")


def _bounded_limit(value: object) -> int:
    if type(value) is not int:
        return 1
    return min(100, max(1, value))


def _percentile(samples: List[int], percent: int) -> Optional[int]:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = (percent * len(ordered) + 99) // 100
    return ordered[rank - 1]


class RoutingRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create_config(
        self,
        config,
        activate: bool = False,
        now: Optional[datetime] = None,
        force_new: bool = False,
    ) -> Dict[str, object]:
        if type(activate) is not bool:
            raise ValueError("activate must be a boolean")
        if type(force_new) is not bool:
            raise ValueError("force_new must be a boolean")
        canonical, digest = _canonical_config(config)
        timestamp = _timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                selected = self._create_config_in_connection(
                    connection, canonical, digest, timestamp, activate, force_new
                )
                self._database.harden_files()
        return _config_dict(selected)

    def append_active_config(
        self, config, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        """Append an active dashboard update without racing the hidden flag."""
        if type(config) is not dict or set(config) != _DASHBOARD_ROUTING_CONFIG_KEYS:
            raise ValueError("routing dashboard config has unknown or missing keys")
        timestamp = _timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT config_json FROM routing_config_versions "
                    "WHERE status='active' LIMIT 1"
                ).fetchone()
                enabled = True
                if row is not None:
                    try:
                        enabled = json.loads(str(row[0]))["enabled"]
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError("active routing config is invalid") from exc
                    if type(enabled) is not bool:
                        raise ValueError("active routing config is invalid")
                canonical, digest = _canonical_config(dict(config, enabled=enabled))
                selected = self._create_config_in_connection(
                    connection, canonical, digest, timestamp, True, True
                )
                self._database.harden_files()
        return _config_dict(selected)

    def get_config(self, version: int) -> Optional[Dict[str, object]]:
        version = _positive_integer(version, "version")
        with self._database.connect() as connection:
            row = self._select_config(connection, version)
        return None if row is None else _config_dict(row)

    def active_config(self) -> Optional[Dict[str, object]]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT %s FROM routing_config_versions WHERE status='active' "
                "LIMIT 1" % ", ".join(_CONFIG_COLUMNS)
            ).fetchone()
        return None if row is None else _config_dict(row)

    def classifier_circuit(self, config_version: int) -> Optional[Dict[str, object]]:
        version = _positive_integer(config_version, "config_version")
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT state, opened_at, updated_at FROM routing_classifier_circuit "
                "WHERE config_version=?",
                (version,),
            ).fetchone()
        if row is None:
            return None
        return {"state": row[0], "opened_at": row[1], "updated_at": row[2]}

    def record_classifier_circuit(
        self, config_version: int, state: str, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        version = _positive_integer(config_version, "config_version")
        if state not in {"closed", "cooling"}:
            raise ValueError("classifier circuit state is not allowed")
        timestamp = _timestamp(now)
        opened_at = timestamp if state == "cooling" else None
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    "INSERT INTO routing_classifier_circuit("
                    "config_version, state, opened_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(config_version) DO UPDATE SET state=excluded.state, "
                    "opened_at=excluded.opened_at, updated_at=excluded.updated_at",
                    (version, state, opened_at, timestamp),
                )
                row = connection.execute(
                    "SELECT state, opened_at, updated_at FROM routing_classifier_circuit "
                    "WHERE config_version=?",
                    (version,),
                ).fetchone()
                self._database.harden_files()
        return {"state": row[0], "opened_at": row[1], "updated_at": row[2]}

    def close_classifier_circuit_if_expired(
        self,
        config_version: int,
        expected_opened_at: object,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        version = _positive_integer(config_version, "config_version")
        if type(expected_opened_at) is not str or not expected_opened_at:
            return self.classifier_circuit(version)
        timestamp = _timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    "UPDATE routing_classifier_circuit SET state='closed', "
                    "opened_at=NULL, updated_at=? WHERE config_version=? "
                    "AND state='cooling' AND opened_at=?",
                    (timestamp, version, expected_opened_at),
                )
                row = connection.execute(
                    "SELECT state, opened_at, updated_at FROM routing_classifier_circuit "
                    "WHERE config_version=?",
                    (version,),
                ).fetchone()
                self._database.harden_files()
        if row is None:
            return None
        return {"state": row[0], "opened_at": row[1], "updated_at": row[2]}

    def activate_config(
        self, version: int, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        version = _positive_integer(version, "version")
        timestamp = _timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                if self._select_config(connection, version) is None:
                    raise ValueError("routing config does not exist")
                connection.execute(
                    "UPDATE routing_config_versions SET status='retired' "
                    "WHERE status='active' AND version != ?", (version,)
                )
                connection.execute(
                    "UPDATE routing_config_versions SET status='active', activated_at=? "
                    "WHERE version=?", (timestamp, version),
                )
                row = self._select_config(connection, version)
                self._ensure_classifier_circuit(connection, version, timestamp)
                self._database.harden_files()
        return _config_dict(row)

    def start_decision(
        self, *, user_id: int, source_message_sha256: str, message_sha256: str,
        message_preview: str, routing_config_version: int, rule_version: str,
        dispatch_overhead_ms: int, now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        owner = _positive_integer(user_id, "user_id")
        source = _hash(source_message_sha256, "source_message_sha256")
        message = _hash(message_sha256, "message_sha256")
        preview = _preview(message_preview)
        version = _positive_integer(routing_config_version, "routing_config_version")
        rules = _identifier(rule_version, "rule_version")
        overhead = _nonnegative_integer(dispatch_overhead_ms, "dispatch_overhead_ms")
        timestamp = _timestamp(now)
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    "INSERT INTO route_decisions("
                    "user_id, source_message_sha256, message_sha256, message_preview, "
                    "routing_config_version, rule_version, dispatch_overhead_ms, status, "
                    "risk_flags_json, reasons_json, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '[]', '[]', ?, ?) "
                    "ON CONFLICT(user_id, source_message_sha256, routing_config_version) "
                    "DO NOTHING",
                    (owner, source, message, preview, version, rules, overhead, timestamp, timestamp),
                )
                row = connection.execute(
                    "SELECT %s FROM route_decisions WHERE user_id=? "
                    "AND source_message_sha256=? AND routing_config_version=?"
                    % ", ".join(_DECISION_COLUMNS),
                    (owner, source, version),
                ).fetchone()
                self._database.harden_files()
        return _decision_dict(row)

    def reserve_classifier_attempt(
        self,
        decision_id: int,
        expected_updated_at: str,
        *,
        max_daily_calls: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Atomically reserve one persisted UTC-day classifier attempt."""
        identifier = _positive_integer(decision_id, "decision_id")
        expected = self._expected_timestamp(expected_updated_at)
        maximum = _nonnegative_integer(max_daily_calls, "max_daily_calls")
        attempted_at = _timestamp(now)
        current = datetime.fromisoformat(attempted_at)
        day_start = current.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(timespec="microseconds")
        day_end = (current.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)).isoformat(timespec="microseconds")
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._select_decision(connection, identifier)
                if (
                    row is None
                    or str(row[23]) != expected
                    or str(row[8]) != "pending"
                    or bool(row[25])
                ):
                    return {
                        "outcome": "duplicate_or_terminal",
                        "decision": None if row is None else _decision_dict(row),
                    }
                calls_today = connection.execute(
                    "SELECT COUNT(*) FROM route_decisions "
                    "WHERE classifier_attempted=1 AND classifier_attempted_at>=? "
                    "AND classifier_attempted_at<?",
                    (day_start, day_end),
                ).fetchone()[0]
                if int(calls_today) >= maximum:
                    return {
                        "outcome": "budget_exhausted",
                        "decision": _decision_dict(row),
                    }
                updated_at = _next_timestamp(str(row[23]), now)
                cursor = connection.execute(
                    "UPDATE route_decisions SET classifier_attempted=1, "
                    "classifier_attempted_at=?, updated_at=? "
                    "WHERE id=? AND status='pending' AND classifier_attempted=0 "
                    "AND updated_at=?",
                    (attempted_at, updated_at, identifier, expected),
                )
                if cursor.rowcount != 1:
                    row = self._select_decision(connection, identifier)
                    return {
                        "outcome": "duplicate_or_terminal",
                        "decision": None if row is None else _decision_dict(row),
                    }
                selected = self._select_decision(connection, identifier)
                self._database.harden_files()
        return {"outcome": "reserved", "decision": _decision_dict(selected)}

    def finish_decision(
        self, decision_id: int, expected_updated_at: str, *, status: str,
        rule_level: Optional[str], model_level: Optional[str], final_level: str,
        confidence: float, task_kind: str, risk_flags: Tuple[str, ...],
        reasons: Tuple[str, ...], brainstorm_required: bool, brainstorm_mode: str,
        classifier_provider: Optional[str], classifier_model: Optional[str],
        classifier_duration_ms: Optional[int], now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        identifier = _positive_integer(decision_id, "decision_id")
        expected = self._expected_timestamp(expected_updated_at)
        if status not in {"classified", "rule_only"}:
            raise ValueError("decision status transition is not allowed")
        rule = _optional_level(rule_level, "rule_level")
        model = _optional_level(model_level, "model_level", _MODEL_LEVELS)
        final = _level(final_level, "final_level")
        certainty = _confidence(confidence)
        kind = _task_kind(task_kind)
        flags = _json_strings(risk_flags, "risk_flags")
        decision_reasons = _json_strings(reasons, "reasons")
        if type(brainstorm_required) is not bool:
            raise ValueError("brainstorm_required must be boolean")
        if brainstorm_mode not in {"disabled", "standard", "required"}:
            raise ValueError("brainstorm_mode is not allowed")
        if status == "classified":
            if model is None:
                raise ValueError("classified decision requires model_level")
            provider = _identifier(classifier_provider, "classifier_provider")
            classifier = _identifier(classifier_model, "classifier_model")
            duration = _nonnegative_integer(
                classifier_duration_ms, "classifier_duration_ms"
            )
            attempted = 1
        else:
            if rule is None or model is not None or classifier_provider is not None:
                raise ValueError("rule_only decision must not include classifier data")
            if classifier_model is not None or classifier_duration_ms is not None:
                raise ValueError("rule_only decision must not include classifier data")
            provider = None
            classifier = None
            duration = None
            attempted = 0
        with self._database.locked():
            with self._database.connect() as connection:
                row = self._select_decision(connection, identifier)
                if row is None or str(row[23]) != expected:
                    raise ValueError("route decision state changed")
                updated_at = _next_timestamp(str(row[23]), now)
                cursor = connection.execute(
                    "UPDATE route_decisions SET status=?, rule_level=?, model_level=?, "
                    "final_level=?, confidence=?, task_kind=?, risk_flags_json=?, "
                    "reasons_json=?, brainstorm_required=?, brainstorm_mode=?, "
                    "classifier_provider=?, classifier_model=?, classifier_duration_ms=?, "
                    "classifier_attempted=CASE WHEN classifier_attempted=1 OR ?=1 "
                    "THEN 1 ELSE 0 END, classifier_attempted_at=CASE "
                    "WHEN classifier_attempted=1 THEN classifier_attempted_at "
                    "WHEN ?=1 THEN ? ELSE NULL END, updated_at=?, finished_at=? "
                    "WHERE id=? AND status='pending' AND updated_at=?",
                    (status, rule, model, final, certainty, kind, _canonical_array(flags),
                     _canonical_array(decision_reasons), int(brainstorm_required),
                     brainstorm_mode, provider, classifier, duration, attempted,
                     attempted, updated_at, updated_at, updated_at, identifier, expected),
                )
                if cursor.rowcount != 1:
                    raise ValueError("route decision state changed")
                selected = self._select_decision(connection, identifier)
                self._database.harden_files()
        return _decision_dict(selected)

    def fail_decision(
        self, decision_id: int, expected_updated_at: str, safe_error_class: str,
        final_level: str = "L2", now: Optional[datetime] = None,
        classifier_attempted: bool = True,
    ) -> Dict[str, object]:
        identifier = _positive_integer(decision_id, "decision_id")
        expected = self._expected_timestamp(expected_updated_at)
        error_class = _safe_error_class(safe_error_class)
        final = _level(final_level, "final_level")
        if type(classifier_attempted) is not bool:
            raise ValueError("classifier_attempted must be boolean")
        with self._database.locked():
            with self._database.connect() as connection:
                row = self._select_decision(connection, identifier)
                if row is None or str(row[23]) != expected:
                    raise ValueError("route decision state changed")
                updated_at = _next_timestamp(str(row[23]), now)
                cursor = connection.execute(
                    "UPDATE route_decisions SET status='error', final_level=?, confidence=0, "
                    "task_kind='other', reasons_json='[\"classifier_error\"]', "
                    "safe_error_class=?, classifier_attempted=CASE "
                    "WHEN classifier_attempted=1 OR ?=1 THEN 1 ELSE 0 END, "
                    "classifier_attempted_at=CASE WHEN classifier_attempted=1 "
                    "THEN classifier_attempted_at WHEN ?=1 THEN ? ELSE NULL END, "
                    "updated_at=?, finished_at=? "
                    "WHERE id=? AND status='pending' AND updated_at=?",
                    (final, error_class, int(classifier_attempted), int(classifier_attempted),
                     updated_at, updated_at, updated_at, identifier, expected),
                )
                if cursor.rowcount != 1:
                    raise ValueError("route decision state changed")
                selected = self._select_decision(connection, identifier)
                self._database.harden_files()
        return _decision_dict(selected)

    def recover_pending_as_interrupted(self) -> int:
        with self._database.locked():
            with self._database.connect() as connection:
                rows = connection.execute(
                    "SELECT id, updated_at FROM route_decisions WHERE status='pending'"
                ).fetchall()
                recovered = 0
                for decision_id, previous in rows:
                    updated_at = _next_timestamp(str(previous), None)
                    cursor = connection.execute(
                        "UPDATE route_decisions SET status='interrupted', final_level='L2', "
                        "confidence=0, task_kind='other', "
                        "reasons_json='[\"interrupted\"]', safe_error_class='Interrupted', "
                        "updated_at=?, finished_at=? WHERE id=? AND status='pending' "
                        "AND updated_at=?",
                        (updated_at, updated_at, decision_id, previous),
                    )
                    recovered += cursor.rowcount
                if recovered:
                    self._database.harden_files()
        return recovered

    def label(
        self, decision_id: int, user_id: int, verdict: str, correct_level: str,
        note: str, expected_updated_at: str,
    ) -> Dict[str, object]:
        identifier = _positive_integer(decision_id, "decision_id")
        owner = _positive_integer(user_id, "user_id")
        if type(verdict) is not str or verdict not in _VERDICTS:
            raise ValueError("verdict is not allowed")
        correct = _level(correct_level, "correct_level")
        annotation = _note(note)
        expected = self._expected_timestamp(expected_updated_at)
        with self._database.locked():
            with self._database.connect() as connection:
                row = self._select_decision(connection, identifier)
                if row is None or int(row[1]) != owner or str(row[23]) != expected:
                    raise ValueError("route decision state changed")
                if row[8] not in {"classified", "rule_only", "error"}:
                    raise ValueError("route decision is not ready for labeling")
                existing = connection.execute(
                    "SELECT id FROM route_labels WHERE decision_id=? AND user_id=?",
                    (identifier, owner),
                ).fetchone()
                if existing is not None:
                    raise ValueError("route decision already has a label")
                updated_at = _next_timestamp(str(row[23]), None)
                cursor = connection.execute(
                    "UPDATE route_decisions SET updated_at=? "
                    "WHERE id=? AND user_id=? AND updated_at=? "
                    "AND status IN ('classified', 'rule_only', 'error')",
                    (updated_at, identifier, owner, expected),
                )
                if cursor.rowcount != 1:
                    raise ValueError("route decision state changed")
                label = connection.execute(
                    "INSERT INTO route_labels("
                    "decision_id, user_id, verdict, correct_level, note, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (identifier, owner, verdict, correct, annotation, updated_at, updated_at),
                )
                selected = connection.execute(
                    "SELECT %s FROM route_labels WHERE id=?" % ", ".join(_LABEL_COLUMNS),
                    (label.lastrowid,),
                ).fetchone()
                self._database.harden_files()
        return _row_dict(_LABEL_COLUMNS, selected)

    def recent_decisions(
        self, limit: int, user_id: Optional[int] = None
    ) -> List[Dict[str, object]]:
        bounded = _bounded_limit(limit)
        if user_id is None:
            clause = ""
            params = (bounded,)
        else:
            clause = "WHERE d.user_id=? "
            params = (_positive_integer(user_id, "user_id"), bounded)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT %s, EXISTS(SELECT 1 FROM route_labels AS l "
                "WHERE l.decision_id=d.id) AS labeled "
                "FROM route_decisions AS d %s"
                "ORDER BY d.created_at DESC, d.id DESC LIMIT ?"
                % (", ".join("d.%s" % name for name in _DECISION_COLUMNS), clause),
                params,
            ).fetchall()
        decisions = []
        for row in rows:
            decision = _decision_dict(row[:-1])
            decision["labeled"] = bool(row[-1])
            decisions.append(decision)
        return decisions

    def metrics(self, now: Optional[datetime] = None) -> Dict[str, object]:
        _timestamp(now)
        with self._database.connect() as connection:
            connection.execute("BEGIN")
            try:
                decisions = self._load_metric_decisions(connection)
                labels = self._load_metric_labels(connection)
            finally:
                connection.rollback()
        by_id = {int(row[0]): row for row in decisions}
        status_counts = {
            "pending": 0, "classified": 0, "rule_only": 0, "error": 0,
            "interrupted": 0,
        }
        per_level_counts = {level: 0 for level in _LEVELS}
        matrix = {level: {correct: 0 for correct in _LEVELS} for level in _LEVELS}
        durations = []
        overheads = []
        forced_risk_misses = 0
        for _, status, _, rule_level, final, risk_flags_json, duration, overhead, _ in decisions:
            if status in status_counts:
                status_counts[str(status)] += 1
            if final in per_level_counts:
                per_level_counts[str(final)] += 1
            if duration is not None:
                durations.append(int(duration))
            overheads.append(int(overhead))
            if (
                rule_level == "L3"
                and json.loads(str(risk_flags_json))
                and final != "L3"
            ):
                forced_risk_misses += 1
        accurate = 0
        for decision_id, correct in labels:
            decision = by_id.get(int(decision_id))
            if decision is None:
                continue
            final = decision[4]
            if final in matrix and correct in _LEVELS:
                matrix[str(final)][str(correct)] += 1
            if final == correct:
                accurate += 1
        count = len(decisions)
        labeled = len(labels)
        classifier_attempts = sum(1 for row in decisions if bool(row[8]))
        classifier_errors = sum(
            1 for row in decisions if row[1] == "error" and bool(row[8])
        )
        return {
            "decision_count": count,
            "counts": status_counts,
            "status_counts": status_counts,
            "first_decision_at": min((str(row[2]) for row in decisions), default=None),
            "last_decision_at": max((str(row[2]) for row in decisions), default=None),
            "labeled_count": labeled,
            "label_accuracy": None if not labeled else accurate / labeled,
            "forced_risk_misses": forced_risk_misses,
            "classifier_error_rate": (
                0.0
                if not classifier_attempts
                else classifier_errors / classifier_attempts
            ),
            "classifier_duration_p50_ms": _percentile(durations, 50),
            "classifier_duration_p95_ms": _percentile(durations, 95),
            "dispatch_overhead_p95_ms": _percentile(overheads, 95),
            "per_level_counts": per_level_counts,
            "confusion_matrix": matrix,
        }

    @staticmethod
    def _load_metric_decisions(connection):
        return connection.execute(
            "SELECT id, status, created_at, rule_level, final_level, risk_flags_json, "
            "classifier_duration_ms, dispatch_overhead_ms, classifier_attempted "
            "FROM route_decisions"
        ).fetchall()

    @staticmethod
    def _load_metric_labels(connection):
        return connection.execute(
            "SELECT decision_id, correct_level FROM route_labels"
        ).fetchall()

    def _create_config_in_connection(
        self, connection, canonical: str, digest: str, timestamp: str,
        activate: bool, force_new: bool,
    ):
        row = None
        if not force_new:
            row = connection.execute(
                "SELECT %s FROM routing_config_versions WHERE config_sha256=? "
                "ORDER BY version DESC LIMIT 1" % ", ".join(_CONFIG_COLUMNS),
                (digest,),
            ).fetchone()
        if activate:
            connection.execute(
                "UPDATE routing_config_versions SET status='retired' "
                "WHERE status='active'"
            )
        if row is None:
            cursor = connection.execute(
                "INSERT INTO routing_config_versions("
                "config_json, config_sha256, status, created_at, activated_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    canonical,
                    digest,
                    "active" if activate else "draft",
                    timestamp,
                    timestamp if activate else None,
                ),
            )
            selected = self._select_config(connection, cursor.lastrowid)
            self._ensure_classifier_circuit(connection, int(selected[0]), timestamp)
            return selected
        version = int(row[0])
        if activate:
            connection.execute(
                "UPDATE routing_config_versions "
                "SET status='active', activated_at=? WHERE version=?",
                (timestamp, version),
            )
        selected = self._select_config(connection, version)
        self._ensure_classifier_circuit(connection, version, timestamp)
        return selected

    @staticmethod
    def _ensure_classifier_circuit(connection, config_version: int, timestamp: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO routing_classifier_circuit("
            "config_version, state, opened_at, updated_at) VALUES (?, 'closed', NULL, ?)",
            (config_version, timestamp),
        )

    @staticmethod
    def _expected_timestamp(value: object) -> str:
        if type(value) is not str or not value:
            raise ValueError("expected_updated_at must be a timestamp")
        return value

    @staticmethod
    def _select_decision(connection, decision_id):
        return connection.execute(
            "SELECT %s FROM route_decisions WHERE id=?" % ", ".join(_DECISION_COLUMNS),
            (decision_id,),
        ).fetchone()

    @staticmethod
    def _select_config(connection, version):
        return connection.execute(
            "SELECT %s FROM routing_config_versions WHERE version=?"
            % ", ".join(_CONFIG_COLUMNS), (version,)
        ).fetchone()


__all__ = ["RoutingRepository", "validate_routing_config"]
