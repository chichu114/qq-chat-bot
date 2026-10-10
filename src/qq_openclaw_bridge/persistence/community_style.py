"""Transactional storage of derived style data; source and message bodies never enter here."""

from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta
import hashlib
import json
import re
import sqlite3
import unicodedata
from typing import Mapping, Optional, Sequence, Tuple

from ..domain.community_style import (
    Authorization, DerivedSample, StyleFeatures, StyleProfile, StyleRule,
    aggregate_style_profile, normalize_subject,
)
from ..runtime.community_style_config import CommunityStyleSettings
from .database import SqliteDatabase, parse_sqlite_timestamp, sqlite_timestamp, sqlite_timestamp_microseconds, utc_datetime


def _text_projection(columns, validity_column):
    """Build SQL from module-owned column declarations only, never user input.

    Validate the complete byte sequence in SQLite before returning text to Python.
    The validity flag distinguishes a legitimate optional NULL from rejected text.
    """
    selections, predicates = [], []
    for expression, alias, byte_limit, nullable in columns:
        valid = "(typeof({0})='text' AND length(CAST({0} AS BLOB))<={1} AND instr({0},char(0))=0)".format(expression, byte_limit)
        selections.append("CASE WHEN {0} THEN {1} ELSE NULL END AS {2}".format(valid, expression, alias))
        predicates.append("({0} IS NULL OR {1})".format(expression, valid) if nullable else valid)
    selections.append("CASE WHEN " + " AND ".join(predicates) + " THEN 1 ELSE 0 END AS " + validity_column)
    return ",".join(selections)


_SOURCE_TEXT = tuple((name, name, limit, optional) for name, limit, optional in (
    ("id", 512, False), ("platform", 128, False), ("community_key", 512, False),
    ("profile_key", 512, False), ("cursor_json", 65536, False),
    ("last_attempt_at", 64, True), ("last_success_at", 64, True), ("status", 64, False),
    ("safe_error_class", 64, True), ("next_attempt_at", 64, False),
    ("config_fingerprint", 64, False), ("created_at", 64, False), ("updated_at", 64, False),
))
_SOURCE_SELECT = "SELECT enabled,schedule_seconds,max_items_per_sync," + _text_projection(_SOURCE_TEXT, "_source_text_valid") + " FROM community_style_sources"
_AUTH_COLUMNS = _text_projection((
    ("a.id", "grant_id", 64, False), ("a.source_id", "grant_source_id", 512, False),
    ("a.subject_type", "subject_type", 128, False), ("a.subject_key", "subject_key", 512, False),
    ("a.scope", "scope", 256, False), ("a.evidence_ref", "evidence_ref", 64, False),
    ("a.granted_at", "granted_at", 64, False), ("a.expires_at", "expires_at", 64, True),
    ("a.created_at", "grant_created_at", 64, False), ("a.updated_at", "grant_updated_at", 64, False),
    ("a.revoked_at", "grant_revoked_at", 64, True),
    ("a.status", "grant_status", 64, False), ("s.id", "bound_source_id", 512, False),
    ("s.platform", "platform", 128, False), ("s.community_key", "community_key", 512, False),
    ("s.profile_key", "bound_profile_key", 512, False), ("s.model", "source_model", 512, False),
), "_grant_text_valid") + ",s.enabled AS source_enabled,a.rowid AS grant_rowid"
_AUTH_SELECT = "SELECT " + _AUTH_COLUMNS + " FROM style_authorizations a LEFT JOIN community_style_sources s ON a.source_id=s.id"
_SAMPLE_TEXT = tuple(("m." + name, name, limit, optional) for name, limit, optional in (
    ("sample_id", 512, False), ("source_id", 512, False), ("profile_key", 512, False),
    ("authorization_id", 64, False), ("external_content_id", 512, False),
    ("canonical_url_hash", 64, False), ("author_key_hash", 64, False), ("content_sha256", 64, False),
    ("features_json", 65536, False), ("model", 512, False), ("analyzed_at", 64, False),
    ("status", 64, False), ("removed_at", 64, True),
))
_SAMPLE_SELECT = (
    "SELECT m.id,m.feature_schema_version," + _text_projection(_SAMPLE_TEXT, "_sample_text_valid") + "," + _AUTH_COLUMNS +
    " FROM community_style_samples m LEFT JOIN style_authorizations a ON m.authorization_id=a.id"
    " LEFT JOIN community_style_sources s ON m.source_id=s.id"
)
_PROFILE_TEXT = tuple((name, name, limit, optional) for name, limit, optional in (
    ("profile_key", 512, False), ("status", 64, False), ("rules_json", 65536, False),
    ("source_fingerprint", 64, False), ("created_at", 64, False),
    ("activated_at", 64, True), ("invalidated_at", 64, True),
))
_PROFILE_SELECT = "SELECT id,version,sample_count,author_count," + _text_projection(_PROFILE_TEXT, "_profile_text_valid") + " FROM community_style_profiles"
_AUTH_ID_SELECT = "SELECT " + _text_projection((("id", "id", 64, False),), "_id_valid") + " FROM style_authorizations"
_SAMPLE_PROFILE_SELECT = "SELECT DISTINCT " + _text_projection((("profile_key", "profile_key", 512, False),), "_key_valid") + " FROM community_style_samples"
_PROFILE_KEY_SELECT = "SELECT DISTINCT " + _text_projection((("profile_key", "profile_key", 512, False),), "_key_valid") + " FROM community_style_profiles"
_REVOKE_SOURCE_SELECT = "SELECT " + _text_projection((
    ("a.source_id", "grant_source_id", 512, False), ("s.id", "bound_source_id", 512, False),
    ("s.profile_key", "bound_profile_key", 512, False),
), "_source_binding_valid") + " FROM style_authorizations a LEFT JOIN community_style_sources s ON a.source_id=s.id"
_BINDING_SELECT = "SELECT group_id,profile_version,enabled,cooldown_seconds,max_reply_chars," + _text_projection((
    ("profile_key", "profile_key", 512, False), ("updated_at", "updated_at", 64, False),
), "_binding_text_valid") + " FROM group_style_bindings"

_MAX_SOURCE_RETRY_DELAY = timedelta(hours=24)
_GROUP_RECEIPT_MAX_ATTEMPTS = 2
_GROUP_RECEIPT_RETRY_DELAY = timedelta(minutes=5)
_GROUP_RECEIPT_ERROR_CLASSES = frozenset((
    "timeout", "connection", "invalid_ack", "rpc_error", "filter_error",
))
_SOURCE_ERROR_CLASSES = frozenset((
    "analysis_failed", "persistence_failed", "interrupted", "rate_limited",
    "temporary", "source_unavailable", "source_schema", "reddit_auth",
    "reddit_candidate", "reddit_cursor", "reddit_http", "reddit_redirect",
    "reddit_request", "reddit_response_too_large", "reddit_schema",
    "robots_disallow", "robots_unavailable", "captcha", "login_required",
    "http_403", "http_429", "http_error", "redirect", "network",
    "invalid_request", "invalid_cursor", "invalid_candidate",
    "response_exceeded", "invalid_markup", "identity_mismatch",
    "ollama_timeout", "ollama_connection", "ollama_http_status",
    "ollama_model_missing", "ollama_invalid_response",
    "ollama_response_too_large", "ollama_redirect_rejected",
))


def _timestamp(value: Optional[datetime] = None) -> str:
    if value is not None:
        if (not isinstance(value, datetime) or value.tzinfo is None
                or value.utcoffset() is None
                or value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset()):
            raise ValueError("invalid_timestamp")
    return sqlite_timestamp_microseconds(value)


def _configured_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _timestamp(parsed)
        result = parse_sqlite_timestamp(value.replace("Z", "+00:00"))
        return _timestamp(result)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError("invalid_timestamp") from None


def _claim_token(value: object) -> str:
    if type(value) is not str or len(value) > 64 or "\x00" in value:
        raise ValueError("invalid_claim_token")
    canonical = _configured_timestamp(value)
    if canonical != value:
        raise ValueError("invalid_claim_token")
    return canonical


def _identity_timestamp(value: str) -> str:
    """Retain v7's second-precision grant identity unless subsecond precision matters."""
    parsed = parse_sqlite_timestamp(_configured_timestamp(value))
    return sqlite_timestamp(parsed) if parsed.microsecond == 0 else sqlite_timestamp_microseconds(parsed)


def _authorization_ids(source_id, platform, community, subject_type, subject, scope, evidence, granted, expires):
    base = ["authorization-v1", source_id, platform, community, subject_type, subject, scope, evidence]
    current = _digest(base + [_identity_timestamp(granted), _identity_timestamp(expires) if expires is not None else None])
    legacy = _digest(base + [sqlite_timestamp(parse_sqlite_timestamp(_configured_timestamp(granted))),
                            sqlite_timestamp(parse_sqlite_timestamp(_configured_timestamp(expires))) if expires is not None else None])
    return current, legacy


def _safe_sqlite_text(value: bytes):
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        # A non-text sentinel is rejected even by validators for optional fields.
        return b"invalid_sqlite_utf8"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    # Structured encoding preserves field boundaries (unlike delimiter concatenation).
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_json")
        result[key] = value
    return result


def _load(raw: str):
    if not isinstance(raw, str) or len(raw) > 65536 or len(raw.encode("utf-8")) > 65536 or "\x00" in raw:
        raise ValueError("invalid_json")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid_json") from None


def _token(value: object, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_identifier")
    value = unicodedata.normalize("NFC", value)
    if (not value.strip() or len(value) > maximum or value != value.strip()
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise ValueError("invalid_identifier")
    return value


def _safe_error_class(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None
        or value not in _SOURCE_ERROR_CLASSES
    ):
        raise ValueError("invalid_error_class")
    return value


def _projected_profile_key(row):
    if row["_key_valid"] != 1:
        raise ValueError("invalid_profile_key")
    key = _token(row["profile_key"])
    if key != row["profile_key"]:
        raise ValueError("invalid_profile_key")
    return key


def _positive(value: object, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def _snapshot(value, maximum, error):
    """Materialize actual iteration, never trusting a user container's reported length."""
    try:
        iterator = iter(value)
        result = []
        for _ in range(maximum + 1):
            try:
                item = next(iterator)
            except StopIteration:
                return tuple(result)
            result.append(item)
        raise ValueError(error)
    except Exception:
        raise ValueError(error) from None


def _mapping_snapshot(value, allowed, error):
    try:
        if not isinstance(value, Mapping):
            raise ValueError(error)
        items = _snapshot(value.items(), len(allowed), error)
        result = {}
        for pair in items:
            if type(pair) not in (tuple, list) or len(pair) != 2:
                raise ValueError(error)
            key, item = pair
            if type(key) is not str or key not in allowed or key in result:
                raise ValueError(error)
            result[key] = item
        return result
    except Exception:
        raise ValueError(error) from None


def _cursor(value: object, platform: str) -> dict:
    allowed = {"reddit": {"after", "before"},
               "tieba": {"page", "pn", "last_post_id", "etag", "last_modified"}}
    try:
        snapshot = _mapping_snapshot(value, allowed.get(platform, set()), "invalid_cursor")
        return {key: _token(item, 512) for key, item in snapshot.items()}
    except Exception:
        raise ValueError("invalid_cursor") from None


def _author_fingerprint(platform, subject):
    """Domain qq-community-style-digest-v1: UTF-8 parts prefixed by 8-byte lengths."""
    digest = hashlib.sha256(b"qq-community-style-digest-v1")
    for part in (platform, normalize_subject(platform, subject)):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _grant(row, timestamp, require_current=True):
    """Validate the stored grant including its immutable source-binding digest."""
    if row["_grant_text_valid"] != 1:
        raise ValueError("invalid_authorization")
    source_id = _token(row["bound_source_id"])
    platform = _token(row["platform"], 32)
    community = _token(row["community_key"])
    _token(row["bound_profile_key"])
    _token(row["source_model"])
    identifier = _token(row["grant_id"], 64)
    evidence = _token(row["evidence_ref"], 64)
    if re.fullmatch(r"[a-f0-9]{64}", identifier) is None or re.fullmatch(r"[a-f0-9]{64}", evidence) is None:
        raise ValueError("invalid_authorization")
    if platform not in ("reddit", "tieba") or row["grant_source_id"] != source_id:
        raise ValueError("invalid_authorization")
    subject_type, scope = row["subject_type"], row["scope"]
    if (subject_type, scope) not in (("author", "all_posts_until_revoke"), ("post", "single_post")):
        raise ValueError("invalid_authorization")
    subject = normalize_subject(platform, row["subject_key"]) if subject_type == "author" else _token(row["subject_key"])
    granted = _configured_timestamp(row["granted_at"])
    expires = _configured_timestamp(row["expires_at"]) if row["expires_at"] is not None else None
    _configured_timestamp(row["grant_created_at"])
    _configured_timestamp(row["grant_updated_at"])
    if row["grant_revoked_at"] is not None:
        raise ValueError("invalid_authorization")
    granted_time, expiry_time = parse_sqlite_timestamp(granted), parse_sqlite_timestamp(expires) if expires else None
    current_time = parse_sqlite_timestamp(timestamp)
    if expiry_time is not None and expiry_time <= granted_time:
        raise ValueError("invalid_authorization")
    if (row["grant_status"] != "active" or type(row["source_enabled"]) is not int
            or row["source_enabled"] not in (0, 1)):
        raise ValueError("invalid_authorization")
    if require_current and (row["source_enabled"] != 1 or granted_time > current_time
                            or (expiry_time is not None and expiry_time <= current_time)):
        raise ValueError("inactive_authorization")
    accepted_ids = _authorization_ids(source_id, platform, community, subject_type, subject, scope, evidence, granted, expires)
    if identifier not in accepted_ids:
        raise ValueError("invalid_authorization")
    return Authorization(identifier, subject_type, subject, scope, expiry_time,
                         "active", source_id, platform, community)


def _sample_matches(sample, authorization):
    if sample.source_id != authorization.source_id or sample.authorization_id != authorization.authorization_id:
        return False
    if authorization.subject_type == "post":
        return sample.external_content_id == authorization.subject_key
    return sample.author_fingerprint == _author_fingerprint(authorization.platform, authorization.subject_key)


def _normalized_settings(settings):
    try:
        if not isinstance(settings, CommunityStyleSettings) or type(settings.enabled) is not bool:
            raise ValueError("invalid_configuration")
        sources, profiles, source_ids = [], set(), set()
        for source in _snapshot(settings.sources, 100, "invalid_configuration"):
            source = replace(source, id=_token(source.id), platform=_token(source.platform, 32),
                             community=_token(source.community), profile=_token(source.profile))
            if source.id in source_ids or source.profile in profiles or source.platform not in ("reddit", "tieba"):
                raise ValueError("invalid_configuration")
            source_ids.add(source.id)
            profiles.add(source.profile)
            if _positive(source.schedule_seconds, 2**31 - 1) < 300:
                raise ValueError("invalid_configuration")
            grants = []
            for grant in _snapshot(source.authorizations, 1000, "invalid_configuration"):
                grant = replace(grant, subject_type=_token(grant.subject_type, 32),
                                subject_key=_token(grant.subject_key), scope=_token(grant.scope, 64))
                if (grant.subject_type, grant.scope) not in (("author", "all_posts_until_revoke"), ("post", "single_post")):
                    raise ValueError("invalid_configuration")
                grants.append(grant)
            sources.append(replace(source, authorizations=tuple(grants)))
        groups, group_ids = [], set()
        for group in _snapshot(settings.groups, 1000, "invalid_configuration"):
            group = replace(group, profile=_token(group.profile))
            _positive(group.group_id)
            _positive(group.cooldown_seconds)
            _positive(group.max_reply_chars)
            if group.group_id in group_ids or group.profile not in profiles:
                raise ValueError("invalid_configuration")
            group_ids.add(group.group_id)
            groups.append(group)
        return replace(settings, sources=tuple(sources), groups=tuple(groups),
                       ollama=replace(settings.ollama, model=_token(settings.ollama.model)))
    except Exception:
        raise ValueError("invalid_configuration") from None


def _binding_snapshot(value):
    keys = {"group_id", "profile_key", "profile_version", "enabled", "cooldown_seconds", "max_reply_chars", "updated_at"}
    try:
        result = _mapping_snapshot(value, keys, "invalid_binding")
        if set(result) != keys or type(result["enabled"]) is not int or result["enabled"] != 1:
            return None
        for key in ("group_id", "cooldown_seconds", "max_reply_chars"):
            _positive(result[key])
        if result["profile_version"] is not None:
            _positive(result["profile_version"])
        if _token(result["profile_key"]) != result["profile_key"]:
            return None
        result["updated_at"] = _configured_timestamp(_token(result["updated_at"], 64))
        return result
    except Exception:
        return None


def _binding_row(row):
    if row is None or row["_binding_text_valid"] != 1:
        return None
    return _binding_snapshot({key: row[key] for key in (
        "group_id", "profile_key", "profile_version", "enabled", "cooldown_seconds", "max_reply_chars", "updated_at",
    )})


def _features(value: object) -> StyleFeatures:
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(StyleFeatures)}:
        raise ValueError("invalid_features")
    decoded = dict(value)
    for name in ("punctuation", "address_terms", "discourse_markers", "avoid_patterns"):
        if not isinstance(decoded[name], list) or len(decoded[name]) > 64:
            raise ValueError("invalid_features")
        decoded[name] = tuple(decoded[name])
    return StyleFeatures(**decoded)


def _profile_row(row) -> Optional[dict]:
    if row is None:
        return None
    try:
        if row["_profile_text_valid"] != 1:
            return None
        identifier = _positive(row["id"])
        version = _positive(row["version"])
        profile_key = _token(row["profile_key"])
        if profile_key != row["profile_key"] or row["status"] not in ("candidate", "active", "retired"):
            return None
        sample_count, author_count = row["sample_count"], row["author_count"]
        if (type(sample_count) is not int or not 0 <= sample_count <= 10000
                or type(author_count) is not int or not 0 <= author_count <= sample_count):
            return None
        fingerprint = row["source_fingerprint"]
        if type(fingerprint) is not str or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            return None
        created_at = _configured_timestamp(row["created_at"])
        activated_at = _configured_timestamp(row["activated_at"]) if row["activated_at"] is not None else None
        invalidated_at = _configured_timestamp(row["invalidated_at"]) if row["invalidated_at"] is not None else None
        if row["status"] == "active" and (activated_at is None or invalidated_at is not None):
            return None
        if invalidated_at is not None and row["status"] != "retired":
            return None
        values = _load(row["rules_json"])
        if not isinstance(values, list) or len(values) > 64:
            return None
        rules = []
        for value in values:
            if not isinstance(value, dict) or set(value) != {"key", "value", "support_count", "weight"}:
                return None
            rule = StyleRule(**value)
            if rule.support_count > sample_count:
                return None
            rules.append(rule)
        profile = StyleProfile(row["profile_key"], row["version"], row["status"], tuple(rules),
                               row["sample_count"], row["author_count"], row["source_fingerprint"])
        return {
            "id": identifier, "profile_key": profile_key, "version": version,
            "status": profile.status, "sample_count": sample_count, "author_count": author_count,
            "source_fingerprint": fingerprint, "created_at": created_at,
            "activated_at": activated_at, "invalidated_at": invalidated_at,
            "rules": [asdict(rule) for rule in profile.rules],
        }
    except (ValueError, TypeError, OverflowError, KeyError):
        return None


class CommunityStyleRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    @contextmanager
    def _connection(self, write=False):
        with self._database.locked():
            connection = self._database.connect()
            connection.text_factory = _safe_sqlite_text
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                with connection:
                    if write:
                        connection.execute("BEGIN IMMEDIATE")
                    yield connection
            finally:
                connection.close()
                if write:
                    self._database.harden_files()

    @staticmethod
    def _invalidate(connection, profile_key, timestamp):
        # Historical versions may be pinned; invalidate them too after consent withdrawal.
        connection.execute("UPDATE community_style_profiles SET status='retired', invalidated_at=? "
                           "WHERE profile_key=? AND invalidated_at IS NULL", (timestamp, profile_key))

    def _revoke(self, connection, authorization_id, timestamp, status="revoked"):
        row = connection.execute("SELECT rowid FROM style_authorizations WHERE id=?", (authorization_id,)).fetchone()
        return self._revoke_row(connection, row[0], timestamp, status) if row else set()

    def _revoke_row(self, connection, rowid, timestamp, status="revoked"):
        # Keep the potentially corrupt identity entirely inside SQLite. Integer rowid
        # still addresses the grant when its TEXT primary key cannot be decoded.
        identity = "(SELECT id FROM style_authorizations WHERE rowid=?)"
        rows = connection.execute(_SAMPLE_PROFILE_SELECT + " WHERE authorization_id=" + identity, (rowid,)).fetchall()
        profile_keys = set()
        source = connection.execute(_REVOKE_SOURCE_SELECT + " WHERE a.rowid=?", (rowid,)).fetchone()
        try:
            if source is None or source["_source_binding_valid"] != 1:
                raise ValueError("invalid_source_binding")
            source_id = _token(source["grant_source_id"])
            bound_key = _token(source["bound_profile_key"])
            if (source_id != source["grant_source_id"] or source_id != source["bound_source_id"]
                    or bound_key != source["bound_profile_key"]):
                raise ValueError("invalid_source_binding")
            profile_keys.add(bound_key)
            for row in rows:
                _projected_profile_key(row)
        except (ValueError, TypeError):
            # An unverifiable sample link leaves the contribution scope unknown,
            # even when the source key is well-formed. Retire every usable version,
            # including unpinned history, for either kind of broken link.
            usable = " WHERE (status='active' OR invalidated_at IS NULL)"
            rows.extend(connection.execute(_PROFILE_KEY_SELECT + usable).fetchall())
            connection.execute("UPDATE community_style_profiles SET status='retired',invalidated_at=?" + usable, (timestamp,))
        else:
            # A safe source also provides the scope when no sample history exists.
            self._invalidate(connection, bound_key, timestamp)
        for row in rows:
            try:
                profile_keys.add(_projected_profile_key(row))
            except (ValueError, TypeError):
                pass
        connection.execute("UPDATE community_style_profiles SET status='retired',invalidated_at=? "
                           "WHERE invalidated_at IS NULL AND profile_key IN "
                           "(SELECT profile_key FROM community_style_samples WHERE authorization_id=" + identity + ")",
                           (timestamp, rowid))
        connection.execute("UPDATE community_style_samples SET status='removed', removed_at=? "
                           "WHERE authorization_id=" + identity + " AND status='active'", (timestamp, rowid))
        connection.execute("UPDATE style_authorizations SET status=?, revoked_at=?, updated_at=? WHERE rowid=?",
                           (status, timestamp if status == "revoked" else None, timestamp, rowid))
        return profile_keys

    def _expire(self, connection, timestamp):
        affected = set()
        current_time = parse_sqlite_timestamp(timestamp)
        rows = connection.execute(_AUTH_SELECT + " WHERE a.status='active' ORDER BY a.rowid").fetchall()
        for row in rows:
            try:
                grant = _grant(row, timestamp, require_current=False)
            except (ValueError, TypeError, OverflowError, KeyError):
                affected.update(self._revoke_row(connection, row["grant_rowid"], timestamp))
                continue
            if grant.expires_at is not None and grant.expires_at <= current_time:
                affected.update(self._revoke_row(connection, row["grant_rowid"], timestamp, "expired"))
        return affected

    def sync_configuration(self, settings: CommunityStyleSettings, now: datetime) -> Tuple[str, ...]:
        timestamp = _timestamp(now)
        settings = _normalized_settings(settings)
        # Parse all timestamps and derive identities before touching persistent state.
        sources = []
        source_ids = set()
        for source in settings.sources:
            if source.id in source_ids:
                raise ValueError("duplicate_source")
            source_ids.add(source.id)
            authorizations = []
            subjects = set()
            for grant in source.authorizations:
                subject = (normalize_subject(source.platform, grant.subject_key)
                           if grant.subject_type == "author" else _token(grant.subject_key))
                if (grant.subject_type, subject) in subjects:
                    raise ValueError("duplicate_authorization")
                subjects.add((grant.subject_type, subject))
                granted = _configured_timestamp(grant.granted_at)
                expires = _configured_timestamp(grant.expires_at) if grant.expires_at else None
                if expires is not None and expires <= granted:
                    raise ValueError("invalid_authorization_period")
                # Store only the digest of the operator's evidence reference, never its contents.
                evidence = _digest(["evidence-ref-v1", grant.evidence_ref])
                identifiers = _authorization_ids(source.id, source.platform, source.community,
                                                grant.subject_type, subject, grant.scope, evidence, granted, expires)
                authorizations.append((identifiers, grant.subject_type, subject, grant.scope,
                                       evidence, granted, expires))
            sources.append((source, authorizations))
        affected = set()
        with self._connection(write=True) as connection:
            resolved_sources = []
            for source, grants in sources:
                resolved = []
                for identifiers, subject_type, subject, scope, evidence, granted, expires in grants:
                    identifier, legacy_id = identifiers
                    current = connection.execute("SELECT rowid FROM style_authorizations WHERE id=?", (identifier,)).fetchone()
                    if current is None and identifier != legacy_id:
                        legacy = connection.execute(_AUTH_SELECT + " WHERE a.id=? AND a.status='active'", (legacy_id,)).fetchone()
                        if legacy is not None:
                            try:
                                _grant(legacy, timestamp, require_current=False)
                                # Legacy identity encodes the source and consent fields;
                                # old subsecond information itself is unrecoverable.
                                identifier = legacy_id
                                connection.execute("UPDATE style_authorizations SET granted_at=?,expires_at=? WHERE rowid=?",
                                                   (granted, expires, legacy["grant_rowid"]))
                            except (ValueError, TypeError, OverflowError, KeyError):
                                pass
                    resolved.append((identifier, subject_type, subject, scope, evidence, granted, expires))
                resolved_sources.append((source, resolved))
            sources = resolved_sources
            affected.update(self._expire(connection, timestamp))
            old_sources = {}
            for row in connection.execute(_SOURCE_SELECT):
                old = self._source_row(row)
                if old is None:
                    raise ValueError("invalid_persisted_source")
                old_sources[old["id"]] = old
            for source_id, old in old_sources.items():
                if source_id not in source_ids:
                    grants = connection.execute(_AUTH_ID_SELECT + " WHERE source_id=? AND status='active'", (source_id,)).fetchall()
                    active = connection.execute("SELECT 1 FROM community_style_samples WHERE source_id=? AND status='active' LIMIT 1", (source_id,)).fetchone()
                    for row in grants:
                        if row["_id_valid"] != 1:
                            raise ValueError("invalid_persisted_authorization")
                        affected.update(self._revoke(connection, row["id"], timestamp))
                    if old["enabled"] == 1 or grants or active:
                        self._invalidate(connection, old["profile_key"], timestamp)
                        affected.add(old["profile_key"])
                        connection.execute("UPDATE community_style_samples SET status='removed',removed_at=? WHERE source_id=? AND status='active'", (timestamp, source_id))
                    connection.execute("UPDATE community_style_sources SET enabled=0, status='paused', updated_at=? WHERE id=?", (timestamp, source_id))
            for source, grants in sources:
                old = old_sources.get(source.id)
                changed_identity = old and (old["platform"], old["community_key"], old["profile_key"]) != (source.platform, source.community, source.profile)
                if changed_identity:
                    self._invalidate(connection, old["profile_key"], timestamp)
                    affected.add(old["profile_key"])
                    connection.execute("UPDATE community_style_samples SET status='removed', removed_at=? WHERE source_id=? AND status='active'", (timestamp, source.id))
                connection.execute(
                    "INSERT INTO community_style_sources(id,platform,community_key,profile_key,enabled,schedule_seconds,max_items_per_sync,"
                    "model,status,next_attempt_at,config_fingerprint,created_at,updated_at) VALUES (?,?,?,?,?,?,100,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET platform=excluded.platform,community_key=excluded.community_key,profile_key=excluded.profile_key,"
                    "enabled=excluded.enabled,schedule_seconds=excluded.schedule_seconds,model=excluded.model,config_fingerprint=excluded.config_fingerprint,"
                    "updated_at=excluded.updated_at,status=CASE WHEN excluded.enabled=0 THEN 'paused' WHEN community_style_sources.enabled=0 THEN 'idle' ELSE community_style_sources.status END",
                    (source.id, source.platform, source.community, source.profile, int(settings.enabled), source.schedule_seconds,
                     _token(settings.ollama.model), "idle" if settings.enabled else "paused", timestamp,
                     _digest([asdict(source), settings.ollama.model]), timestamp, timestamp))
                if changed_identity:
                    connection.execute("UPDATE community_style_sources SET cursor_json='{}', next_attempt_at=? WHERE id=?", (timestamp, source.id))
                wanted = {grant[0] for grant in grants}
                for row in connection.execute(_AUTH_ID_SELECT + " WHERE source_id=? AND status='active'", (source.id,)).fetchall():
                    if row["_id_valid"] != 1:
                        raise ValueError("invalid_persisted_authorization")
                    if row["id"] not in wanted or changed_identity:
                        affected.update(self._revoke(connection, row["id"], timestamp))
                for identifier, subject_type, subject, scope, evidence, granted, expires in grants:
                    status = "expired" if expires is not None and expires <= timestamp else "active"
                    connection.execute(
                        "INSERT INTO style_authorizations(id,source_id,subject_type,subject_key,scope,evidence_ref,granted_at,expires_at,status,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,revoked_at=NULL,"
                        "granted_at=excluded.granted_at,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                        (identifier, source.id, subject_type, subject, scope, evidence, granted, expires, status, timestamp, timestamp))
            connection.execute("UPDATE group_style_bindings SET enabled=0, updated_at=?", (timestamp,))
            for group in settings.groups:
                connection.execute(
                    "INSERT INTO group_style_bindings(group_id,profile_key,profile_version,enabled,cooldown_seconds,max_reply_chars,config_fingerprint,updated_at) "
                    "VALUES (?,?,NULL,?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET profile_key=excluded.profile_key,"
                    "profile_version=CASE WHEN group_style_bindings.profile_key=excluded.profile_key THEN group_style_bindings.profile_version ELSE NULL END,"
                    "enabled=excluded.enabled,cooldown_seconds=excluded.cooldown_seconds,max_reply_chars=excluded.max_reply_chars,"
                    "config_fingerprint=excluded.config_fingerprint,updated_at=excluded.updated_at",
                    (group.group_id, group.profile, int(settings.enabled), group.cooldown_seconds, group.max_reply_chars, _digest(asdict(group)), timestamp))
        return tuple(sorted(affected))

    def active_authorizations(self, source_id: str, now: datetime) -> Tuple[Authorization, ...]:
        timestamp = _timestamp(now)
        source_id = _token(source_id)
        with self._connection(write=True) as connection:
            self._expire(connection, timestamp)
            rows = connection.execute(_AUTH_SELECT + " WHERE a.source_id=? AND a.status='active' "
                "AND s.enabled=1 ORDER BY a.id LIMIT 1001", (source_id,)).fetchall()
        if len(rows) > 1000:
            return ()
        result = []
        for row in rows:
            try:
                grant = _grant(row, timestamp, require_current=False)
                if parse_sqlite_timestamp(_configured_timestamp(row["granted_at"])) <= parse_sqlite_timestamp(timestamp):
                    result.append(grant)
            except (ValueError, TypeError, OverflowError, KeyError):
                return ()
        return tuple(result)

    def claimed_authorizations(
        self, source_id: str, claim_token: str, now: datetime
    ) -> Optional[Tuple[Authorization, ...]]:
        """Return current grants only while the exact running claim is still owned.

        Claim validation and the authorization snapshot share one transaction so
        recovery/reclaim cannot be observed between those two decisions.  None
        means the generation is no longer owned; an empty tuple is a valid claim
        with no usable grants.
        """
        timestamp = _timestamp(now)
        source_id = _token(source_id)
        claim_token = _claim_token(claim_token)
        with self._connection(write=True) as connection:
            self._expire(connection, timestamp)
            row = connection.execute(
                _SOURCE_SELECT + " WHERE id=? AND enabled=1 AND status='running' "
                "AND last_attempt_at=?",
                (source_id, claim_token),
            ).fetchone()
            source = self._source_row(row)
            if source is None or source["last_attempt_at"] != claim_token:
                return None
            rows = connection.execute(
                _AUTH_SELECT + " WHERE a.source_id=? AND a.status='active' "
                "AND s.enabled=1 ORDER BY a.id LIMIT 1001",
                (source_id,),
            ).fetchall()
            if len(rows) > 1000:
                return ()
            result = []
            for authorization_row in rows:
                try:
                    grant = _grant(
                        authorization_row, timestamp, require_current=False
                    )
                    granted_at = _configured_timestamp(
                        authorization_row["granted_at"]
                    )
                    if parse_sqlite_timestamp(granted_at) <= parse_sqlite_timestamp(timestamp):
                        result.append(grant)
                except (ValueError, TypeError, OverflowError, KeyError):
                    return ()
            return tuple(result)

    @staticmethod
    def _source_row(row):
        if row is None:
            return None
        try:
            result = dict(row)
            if result.pop("_source_text_valid") != 1:
                return None
            for key in ("id", "community_key", "profile_key"):
                if _token(result[key]) != result[key]:
                    return None
            if result["platform"] not in ("reddit", "tieba") or result["status"] not in ("idle", "running", "retry", "paused"):
                return None
            if type(result["enabled"]) is not int or result["enabled"] not in (0, 1):
                return None
            if _positive(result["schedule_seconds"], 2**31 - 1) < 300:
                return None
            _positive(result["max_items_per_sync"], 100)
            for key in ("last_attempt_at", "last_success_at", "next_attempt_at", "created_at", "updated_at"):
                if result[key] is None and key in ("last_attempt_at", "last_success_at"):
                    continue
                _token(result[key], 64)
                result[key] = _configured_timestamp(result[key])
            error = result["safe_error_class"]
            if error is not None and (type(error) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error) is None):
                return None
            if type(result["config_fingerprint"]) is not str or re.fullmatch(r"[a-f0-9]{64}", result["config_fingerprint"]) is None:
                return None
            result["cursor"] = _cursor(_load(result.pop("cursor_json")), result["platform"])
            result.pop("config_fingerprint")
            return result
        except (ValueError, TypeError, KeyError, OverflowError):
            return None

    def source(self, source_id: str) -> Optional[Mapping[str, object]]:
        source_id = _token(source_id)
        with self._connection() as connection:
            return self._source_row(connection.execute(_SOURCE_SELECT + " WHERE id=?", (source_id,)).fetchone())

    def _insert_sample(self, connection, sample, timestamp):
        if not isinstance(sample, DerivedSample) or sample.status != "active":
            raise ValueError("invalid_sample")
        row = connection.execute(_AUTH_SELECT + " WHERE a.id=? AND a.source_id=?",
                                 (sample.authorization_id, sample.source_id)).fetchone()
        if row is None:
            raise ValueError("inactive_authorization")
        try:
            authorization = _grant(row, timestamp)
            if not _sample_matches(sample, authorization):
                raise ValueError("invalid_sample_authorization")
            _token(row["source_model"])
        except Exception:
            raise ValueError("invalid_sample_authorization") from None
        payload = _json(asdict(sample.features))
        _features(_load(payload))
        inserted = connection.execute(
            "INSERT INTO community_style_samples(sample_id,source_id,profile_key,authorization_id,external_content_id,canonical_url_hash,"
            "author_key_hash,content_sha256,feature_schema_version,features_json,model,analyzed_at,status) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,'active') ON CONFLICT(source_id,external_content_id,content_sha256) DO UPDATE SET "
            "sample_id=excluded.sample_id,profile_key=excluded.profile_key,authorization_id=excluded.authorization_id,"
            "canonical_url_hash=excluded.canonical_url_hash,author_key_hash=excluded.author_key_hash,"
            "features_json=excluded.features_json,model=excluded.model,analyzed_at=excluded.analyzed_at,status='active',removed_at=NULL "
            "WHERE community_style_samples.status='removed'",
            (sample.sample_id, sample.source_id, row["bound_profile_key"], sample.authorization_id, sample.external_content_id,
             sample.canonical_url_fingerprint, sample.author_fingerprint, sample.content_fingerprint, payload, row["source_model"], timestamp)).rowcount
        if inserted:
            connection.execute("UPDATE community_style_samples SET status='removed',removed_at=? "
                               "WHERE source_id=? AND external_content_id=? AND content_sha256<>? AND status='active'",
                               (timestamp, sample.source_id, sample.external_content_id, sample.content_fingerprint))
        return inserted

    def save_sample(self, sample: DerivedSample) -> bool:
        with self._connection(write=True) as connection:
            return bool(self._insert_sample(connection, sample, _timestamp()))

    def active_samples(self, profile_key: str) -> Tuple[DerivedSample, ...]:
        profile_key = _token(profile_key)
        timestamp = _timestamp()
        with self._connection(write=True) as connection:
            self._expire(connection, timestamp)
            return self._active_samples(connection, profile_key, timestamp)

    @staticmethod
    def _active_samples(connection, profile_key, timestamp):
        rows = connection.execute(_SAMPLE_SELECT + " WHERE m.profile_key=? AND m.status='active' ORDER BY m.id LIMIT 10001", (profile_key,)).fetchall()
        if len(rows) > 10000 or any(row["feature_schema_version"] != 1 or row["_sample_text_valid"] != 1 for row in rows):
            return ()
        try:
            samples = []
            for row in rows:
                # Audit fields are not included in DerivedSample, but remain part
                # of the trust boundary (including the UTF-8 sentinel).
                _token(row["model"])
                _configured_timestamp(row["analyzed_at"])
                if row["removed_at"] is not None:
                    return ()
                authorization = _grant(row, timestamp)
                sample = DerivedSample(row["sample_id"], row["source_id"], row["authorization_id"], row["external_content_id"],
                    row["author_key_hash"], row["canonical_url_hash"], row["content_sha256"], _features(_load(row["features_json"])), row["status"])
                if row["profile_key"] != row["bound_profile_key"] or not _sample_matches(sample, authorization):
                    return ()
                samples.append(sample)
            return tuple(samples)
        except (ValueError, TypeError, OverflowError, KeyError, RecursionError):
            return ()

    def commit_batch(self, source_id: str, next_cursor: Mapping[str, str], samples: Sequence[DerivedSample], now: datetime) -> int:
        result = self._commit_batch(source_id, None, next_cursor, samples, now)
        if result is None:
            raise ValueError("inactive_source")
        return result

    def commit_claimed_batch(
        self,
        source_id: str,
        claim_token: str,
        next_cursor: Mapping[str, str],
        samples: Sequence[DerivedSample],
        now: datetime,
    ) -> Optional[int]:
        """Commit only while the exact running claim generation is still owned."""
        return self._commit_batch(
            source_id, _claim_token(claim_token), next_cursor, samples, now
        )

    def _commit_batch(self, source_id, claim_token, next_cursor, samples, now):
        timestamp = _timestamp(now)
        source_id = _token(source_id)
        if not isinstance(samples, Sequence):
            raise ValueError("invalid_batch")
        samples = _snapshot(samples, 100, "invalid_batch")
        with self._connection(write=True) as connection:
            suffix = " AND status='running' AND last_attempt_at=?" if claim_token is not None else ""
            parameters = (source_id, claim_token) if claim_token is not None else (source_id,)
            row = self._source_row(connection.execute(
                _SOURCE_SELECT + " WHERE id=? AND enabled=1" + suffix, parameters
            ).fetchone())
            if row is None:
                return None
            if claim_token is not None and row["last_attempt_at"] != claim_token:
                return None
            cursor = _json(_cursor(next_cursor, row["platform"]))
            total = 0
            for sample in samples:
                if not isinstance(sample, DerivedSample) or sample.source_id != source_id:
                    raise ValueError("invalid_sample_source")
                total += self._insert_sample(connection, sample, timestamp)
            next_attempt = _timestamp(utc_datetime(now) + timedelta(seconds=row["schedule_seconds"]))
            update_suffix = " AND status='running' AND last_attempt_at=?" if claim_token is not None else ""
            update_parameters = (
                cursor, timestamp, timestamp, next_attempt, timestamp, source_id,
            ) + ((claim_token,) if claim_token is not None else ())
            updated = connection.execute(
                "UPDATE community_style_sources SET cursor_json=?, last_attempt_at=?, last_success_at=?, "
                "next_attempt_at=?, status='idle', safe_error_class=NULL, updated_at=? WHERE id=?"
                + update_suffix,
                update_parameters,
            ).rowcount
            if updated != 1:
                raise ValueError("lost_claim")
            return total

    def update_source_cursor(self, source_id: str, cursor: Mapping[str, str], now: datetime) -> None:
        timestamp = _timestamp(now)
        source_id = _token(source_id)
        with self._connection(write=True) as connection:
            row = self._source_row(connection.execute(_SOURCE_SELECT + " WHERE id=?", (source_id,)).fetchone())
            if row is None:
                raise ValueError("unknown_source")
            connection.execute("UPDATE community_style_sources SET cursor_json=?,updated_at=? WHERE id=?", (_json(_cursor(cursor, row["platform"])), timestamp, source_id))

    def due_sources(self, now: datetime, limit: int) -> Tuple[Mapping[str, object], ...]:
        timestamp = _timestamp(now)
        _positive(limit, 100)
        with self._connection() as connection:
            rows = connection.execute(_SOURCE_SELECT + " WHERE enabled=1 AND status IN ('idle','retry') "
                                      "AND next_attempt_at<=? ORDER BY next_attempt_at,id LIMIT ?", (timestamp, limit)).fetchall()
        results = tuple(self._source_row(row) for row in rows)
        return () if any(result is None for result in results) else results

    def claim_source(self, source_id: str, now: datetime) -> Optional[Mapping[str, object]]:
        """Atomically claim one enabled, due source for a single sync attempt."""
        timestamp = _timestamp(now)
        source_id = _token(source_id)
        with self._connection(write=True) as connection:
            row = connection.execute(
                _SOURCE_SELECT + " WHERE id=? AND enabled=1 AND status IN ('idle','retry') "
                "AND next_attempt_at<=?", (source_id, timestamp),
            ).fetchone()
            source = self._source_row(row)
            if source is None:
                return None
            claim_time = parse_sqlite_timestamp(timestamp)
            if source["last_attempt_at"] is not None:
                previous_claim = parse_sqlite_timestamp(source["last_attempt_at"])
                if previous_claim >= claim_time:
                    try:
                        claim_time = previous_claim + timedelta(microseconds=1)
                    except OverflowError:
                        return None
            claim_token = _timestamp(claim_time)
            previous_retry_seconds = None
            if (source["status"] == "retry" and source["last_attempt_at"] is not None
                    and source["safe_error_class"] in (
                        "temporary", "analysis_failed", "ollama_timeout", "ollama_connection",
                    )):
                previous = (
                    parse_sqlite_timestamp(source["next_attempt_at"])
                    - parse_sqlite_timestamp(source["last_attempt_at"])
                ).total_seconds()
                if 0.0 <= previous <= _MAX_SOURCE_RETRY_DELAY.total_seconds():
                    previous_retry_seconds = previous
            changed = connection.execute(
                "UPDATE community_style_sources SET status='running',last_attempt_at=?,"
                "safe_error_class=NULL,updated_at=? WHERE id=? AND enabled=1 "
                "AND status IN ('idle','retry') AND next_attempt_at<=?",
                (claim_token, timestamp, source_id, timestamp),
            ).rowcount
            if changed != 1:
                return None
            claimed = self._source_row(connection.execute(
                _SOURCE_SELECT + " WHERE id=?", (source_id,),
            ).fetchone())
            if claimed is None or claimed["status"] != "running":
                return None
            claimed = dict(claimed)
            claimed["previous_retry_seconds"] = previous_retry_seconds
            claimed["claim_token"] = claim_token
            return claimed

    def retry_source(
        self,
        source_id: str,
        claim_token: str,
        safe_error_class: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> bool:
        """Move the caller's running source to bounded, persisted retry state."""
        source_id = _token(source_id)
        claim_token = _claim_token(claim_token)
        error_class = _safe_error_class(safe_error_class)
        timestamp = _timestamp(now)
        retry_at = _timestamp(next_attempt_at)
        current = parse_sqlite_timestamp(timestamp)
        retry = parse_sqlite_timestamp(retry_at)
        if retry < current or retry > current + _MAX_SOURCE_RETRY_DELAY:
            raise ValueError("invalid_retry_time")
        with self._connection(write=True) as connection:
            row = connection.execute(
                _SOURCE_SELECT + " WHERE id=? AND enabled=1 AND status='running' AND last_attempt_at=?",
                (source_id, claim_token),
            ).fetchone()
            source = self._source_row(row)
            if source is None or source["last_attempt_at"] != claim_token:
                return False
            return connection.execute(
                "UPDATE community_style_sources SET status='retry',safe_error_class=?,"
                "next_attempt_at=?,updated_at=? WHERE id=? AND enabled=1 AND status='running' "
                "AND last_attempt_at=?",
                (error_class, retry_at, timestamp, source_id, claim_token),
            ).rowcount == 1

    def pause_source(self, source_id: str, claim_token: str, safe_error_class: str, now: datetime) -> bool:
        """Pause the caller's running source using only a stable error category."""
        source_id = _token(source_id)
        claim_token = _claim_token(claim_token)
        error_class = _safe_error_class(safe_error_class)
        timestamp = _timestamp(now)
        with self._connection(write=True) as connection:
            row = connection.execute(
                _SOURCE_SELECT + " WHERE id=? AND enabled=1 AND status='running' AND last_attempt_at=?",
                (source_id, claim_token),
            ).fetchone()
            source = self._source_row(row)
            if source is None or source["last_attempt_at"] != claim_token:
                return False
            return connection.execute(
                "UPDATE community_style_sources SET status='paused',safe_error_class=?,"
                "updated_at=? WHERE id=? AND enabled=1 AND status='running' AND last_attempt_at=?",
                (error_class, timestamp, source_id, claim_token),
            ).rowcount == 1

    def recover_interrupted_sources(self, now: datetime, stale_before: datetime) -> int:
        """Recover stale running attempts without invoking any external source."""
        timestamp = _timestamp(now)
        cutoff = _timestamp(stale_before)
        if parse_sqlite_timestamp(cutoff) > parse_sqlite_timestamp(timestamp):
            raise ValueError("invalid_recovery_time")
        with self._connection(write=True) as connection:
            rows = connection.execute(
                _SOURCE_SELECT + " WHERE enabled=1 AND status='running' "
                "AND last_attempt_at IS NOT NULL AND last_attempt_at<=? ORDER BY id LIMIT 101",
                (cutoff,),
            ).fetchall()
            if len(rows) > 100:
                return 0
            sources = tuple(self._source_row(row) for row in rows)
            if any(source is None for source in sources):
                return 0
            recovered = 0
            for source in sources:
                recovered += connection.execute(
                    "UPDATE community_style_sources SET status='retry',safe_error_class='interrupted',"
                    "next_attempt_at=?,updated_at=? WHERE id=? AND enabled=1 AND status='running' "
                    "AND last_attempt_at IS NOT NULL AND last_attempt_at<=?",
                    (timestamp, timestamp, source["id"], cutoff),
                ).rowcount
            return recovered

    def publish_profile(self, profile: StyleProfile, now: datetime) -> Mapping[str, object]:
        timestamp = _timestamp(now)
        if not isinstance(profile, StyleProfile) or profile.status not in {"candidate", "active"}:
            raise ValueError("invalid_profile")
        payload = _json([asdict(rule) for rule in profile.rules])
        with self._connection(write=True) as connection:
            self._expire(connection, timestamp)
            # One publication gate for first, replacement, repeated and post-revocation candidates.
            rebuilt = aggregate_style_profile(profile.profile_key, self._active_samples(connection, profile.profile_key, timestamp), minimum_samples=5)
            if rebuilt is None or (rebuilt.source_fingerprint, rebuilt.rules, rebuilt.sample_count, rebuilt.author_count) != (
                    profile.source_fingerprint, profile.rules, profile.sample_count, profile.author_count):
                raise ValueError("invalid_profile_candidate")
            current = connection.execute(_PROFILE_SELECT + " WHERE profile_key=? AND status='active'", (profile.profile_key,)).fetchone()
            if current is not None and current["source_fingerprint"] == profile.source_fingerprint:
                if (current["rules_json"], current["sample_count"], current["author_count"]) != (payload, profile.sample_count, profile.author_count):
                    raise ValueError("profile_fingerprint_conflict")
                return _profile_row(current)
            version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM community_style_profiles WHERE profile_key=?", (profile.profile_key,)).fetchone()[0]
            connection.execute("UPDATE community_style_profiles SET status='retired' WHERE profile_key=? AND status='active'", (profile.profile_key,))
            identifier = connection.execute("INSERT INTO community_style_profiles(profile_key,version,status,rules_json,sample_count,author_count,"
                "source_fingerprint,created_at,activated_at) VALUES (?,?,'active',?,?,?,?,?,?)",
                (profile.profile_key, version, payload, profile.sample_count, profile.author_count, profile.source_fingerprint, timestamp, timestamp)).lastrowid
            return _profile_row(connection.execute(_PROFILE_SELECT + " WHERE id=?", (identifier,)).fetchone())

    def profile(self, profile_id: int) -> Optional[Mapping[str, object]]:
        _positive(profile_id)
        with self._connection() as connection:
            return _profile_row(connection.execute(_PROFILE_SELECT + " WHERE id=?", (profile_id,)).fetchone())

    def active_profile(self, profile_key: str) -> Optional[Mapping[str, object]]:
        profile_key = _token(profile_key)
        with self._connection(write=True) as connection:
            self._expire(connection, _timestamp())
            return _profile_row(connection.execute(_PROFILE_SELECT + " WHERE profile_key=? AND status='active' AND invalidated_at IS NULL", (profile_key,)).fetchone())

    def group_binding(self, group_id: int) -> Optional[Mapping[str, object]]:
        _positive(group_id)
        with self._connection() as connection:
            row = connection.execute(_BINDING_SELECT + " WHERE group_id=? AND enabled=1", (group_id,)).fetchone()
            return _binding_row(row)

    def bound_profile(self, binding: Mapping[str, object]) -> Optional[Mapping[str, object]]:
        snapshot = _binding_snapshot(binding)
        if snapshot is None:
            return None
        with self._connection(write=True) as connection:
            self._expire(connection, _timestamp())
            row = connection.execute(_BINDING_SELECT + " WHERE group_id=? AND enabled=1", (snapshot["group_id"],)).fetchone()
            current = _binding_row(row)
            if current != snapshot:
                return None
            version = current["profile_version"]
            if version is None:
                return _profile_row(connection.execute(_PROFILE_SELECT + " WHERE profile_key=? AND status='active' "
                                                       "AND invalidated_at IS NULL", (current["profile_key"],)).fetchone())
            return _profile_row(connection.execute(_PROFILE_SELECT + " WHERE profile_key=? AND version=? "
                                                   "AND status IN ('active','retired') AND invalidated_at IS NULL", (current["profile_key"], version)).fetchone())

    def claim_group_message(
        self,
        group_id: int,
        inbound_message_id: str,
        now: datetime,
        lease_seconds: int = 300,
    ) -> Optional[str]:
        """Claim one inbound group message and return its opaque generation."""
        timestamp = _timestamp(now)
        _positive(group_id)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid_lease_seconds")
        digest = _digest(["group-inbound-v1", _token(inbound_message_id)])
        lease_until = _timestamp(
            parse_sqlite_timestamp(timestamp) + timedelta(seconds=lease_seconds)
        )
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT status,attempts,next_attempt_at,updated_at "
                "FROM group_message_receipts WHERE group_id=? AND inbound_message_sha256=?",
                (group_id, digest),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO group_message_receipts(direction,group_id,inbound_message_sha256,status,attempts,"
                    "next_attempt_at,created_at,updated_at) VALUES ('inbound',?,?,'processing',1,?,?,?)",
                    (group_id, digest, lease_until, timestamp, timestamp),
                )
                return timestamp
            if (
                type(row[0]) is not str
                or type(row[1]) is not int
                or type(row[2]) is not str
                or type(row[3]) is not str
                or row[0] != "failed"
                or not 1 <= row[1] < _GROUP_RECEIPT_MAX_ATTEMPTS
            ):
                return None
            try:
                due = parse_sqlite_timestamp(_configured_timestamp(row[2]))
                previous = parse_sqlite_timestamp(_claim_token(row[3]))
                current = parse_sqlite_timestamp(timestamp)
            except (TypeError, ValueError, OverflowError):
                return None
            if due > current:
                return None
            claim_time = current
            if previous >= claim_time:
                try:
                    claim_time = previous + timedelta(microseconds=1)
                except OverflowError:
                    return None
            claim_token = _timestamp(claim_time)
            changed = connection.execute(
                "UPDATE group_message_receipts SET status='processing',attempts=attempts+1,"
                "next_attempt_at=?,updated_at=?,safe_error_class=NULL "
                "WHERE group_id=? AND inbound_message_sha256=? AND status=? AND attempts=? "
                "AND next_attempt_at=? AND updated_at=?",
                (lease_until, claim_token, group_id, digest, row[0], row[1], row[2], row[3]),
            ).rowcount
            return claim_token if changed == 1 else None

    def complete_group_message(
        self,
        group_id: int,
        inbound_message_id: str,
        claim_token: str,
        outbound_message_id: str,
        now: datetime,
    ) -> bool:
        timestamp = _timestamp(now)
        _positive(group_id)
        digest = _digest(["group-inbound-v1", _token(inbound_message_id)])
        claim_token = _claim_token(claim_token)
        if type(outbound_message_id) is int:
            outbound_number = outbound_message_id
        elif (
            type(outbound_message_id) is str
            and len(outbound_message_id) <= 20
            and outbound_message_id.isascii()
            and outbound_message_id.isdecimal()
        ):
            outbound_number = int(outbound_message_id)
        else:
            raise ValueError("invalid_outbound_message_id")
        if not 0 < outbound_number <= 10 ** 20 - 1:
            raise ValueError("invalid_outbound_message_id")
        outbound = str(outbound_number)
        with self._connection(write=True) as connection:
            return connection.execute(
                "UPDATE group_message_receipts SET status='sent',outbound_message_id=?,sent_at=?,updated_at=?,safe_error_class=NULL "
                "WHERE group_id=? AND inbound_message_sha256=? AND status='processing' AND updated_at=?",
                (outbound, timestamp, timestamp, group_id, digest, claim_token),
            ).rowcount == 1

    def fail_group_message(
        self,
        group_id: int,
        inbound_message_id: str,
        claim_token: str,
        safe_error_class: str,
        now: datetime,
    ) -> bool:
        timestamp = _timestamp(now)
        _positive(group_id)
        claim_token = _claim_token(claim_token)
        if (
            type(safe_error_class) is not str
            or safe_error_class not in _GROUP_RECEIPT_ERROR_CLASSES
        ):
            raise ValueError("invalid_error_class")
        digest = _digest(["group-inbound-v1", _token(inbound_message_id)])
        retry_at = _timestamp(
            parse_sqlite_timestamp(timestamp) + _GROUP_RECEIPT_RETRY_DELAY
        )
        with self._connection(write=True) as connection:
            return connection.execute(
                "UPDATE group_message_receipts SET status='failed',safe_error_class=?,next_attempt_at=?,updated_at=? "
                "WHERE group_id=? AND inbound_message_sha256=? AND status='processing' AND updated_at=?",
                (safe_error_class, retry_at, timestamp, group_id, digest, claim_token),
            ).rowcount == 1

    def is_bot_message(self, group_id: int, message_id: str) -> bool:
        _positive(group_id)
        _token(message_id)
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM group_message_receipts WHERE group_id=? AND outbound_message_id=? AND status='sent' LIMIT 1", (group_id, message_id)).fetchone() is not None

    def status_snapshot(self) -> Mapping[str, object]:
        with self._connection() as connection:
            result = {}
            for name, table, states in (
                ("sources", "community_style_sources", ("idle", "running", "retry", "paused")),
                ("authorizations", "style_authorizations", ("active", "revoked", "expired")),
                ("samples", "community_style_samples", ("active", "removed")),
                ("profiles", "community_style_profiles", ("candidate", "active", "retired")),
                ("receipts", "group_message_receipts", ("processing", "sent", "failed")),
            ):
                counts = connection.execute("SELECT " + ",".join("COALESCE(SUM(status=?),0)" for _ in states) + " FROM " + table, states).fetchone()
                result[name] = dict(zip(states, counts))
            result["enabled_groups"] = connection.execute("SELECT COUNT(*) FROM group_style_bindings WHERE enabled=1").fetchone()[0]
            return result
