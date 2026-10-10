"""Strict, non-sensitive configuration for the community style agent."""

import hashlib
import ipaddress
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit


_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_STRING_LENGTH = 512
_MAX_IDENTIFIER_LENGTH = 128
_MAX_SOURCES = 100
_MAX_GROUPS = 1000
_MAX_AUTHORIZATIONS_PER_SOURCE = 1000
_SENSITIVE_KEY_PARTS = ("secret", "token", "password", "cookie")
_SUPPORTED_PLATFORMS = {"reddit", "tieba"}
_SUPPORTED_SUBJECT_TYPES = {"author", "post"}
_SUPPORTED_SCOPES = {"all_posts_until_revoke", "single_post"}


@dataclass(frozen=True)
class AuthorizationSettings:
    subject_type: str
    subject_key: str
    scope: str
    evidence_ref: str
    granted_at: str
    expires_at: Optional[str] = None


@dataclass(frozen=True)
class SourceSettings:
    id: str
    platform: str
    community: str
    profile: str
    schedule_seconds: int
    authorizations: Tuple[AuthorizationSettings, ...]


@dataclass(frozen=True)
class GroupBindingSettings:
    group_id: int
    profile: str
    cooldown_seconds: int
    max_reply_chars: int


@dataclass(frozen=True)
class OllamaSettings:
    base_url: str
    model: str
    allow_remote: bool


@dataclass(frozen=True)
class CommunityStyleSettings:
    enabled: bool
    ollama: OllamaSettings
    sources: Tuple[SourceSettings, ...]
    groups: Tuple[GroupBindingSettings, ...]
    fingerprint: str = ""


def load_community_style_settings(path: Path) -> CommunityStyleSettings:
    raw = Path(path).read_bytes()
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("community style config exceeds 1 MiB")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        _reject_sensitive_keys(value)
    except RecursionError as exc:
        raise ValueError("community style config is nested too deeply") from exc
    _require_exact_keys(value, {"enabled", "ollama", "sources", "groups"})
    settings = _decode_settings(value)
    _validate_cross_references(settings)
    return replace(settings, fingerprint=hashlib.sha256(raw).hexdigest())


def _unique_object(pairs: list) -> dict:
    value = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError("community style config contains duplicate fields")
        value[key] = nested
    return value


class CommunityStyleConfigReloader:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._settings = load_community_style_settings(self.path)

    def current(self) -> CommunityStyleSettings:
        return self._settings

    def reload_if_changed(self) -> bool:
        try:
            candidate = load_community_style_settings(self.path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False
        if candidate.fingerprint == self._settings.fingerprint:
            return False
        self._settings = candidate
        return True


def _decode_settings(value: Any) -> CommunityStyleSettings:
    if not isinstance(value, dict):
        raise ValueError("community style config must be an object")
    enabled = _require_bool(value["enabled"], "enabled")
    return CommunityStyleSettings(
        enabled=enabled,
        ollama=_decode_ollama(value["ollama"]),
        sources=_decode_sources(value["sources"]),
        groups=_decode_groups(value["groups"]),
    )


def _decode_ollama(value: Any) -> OllamaSettings:
    _require_exact_keys(value, {"base_url", "model", "allow_remote"})
    base_url = _bounded_string(value["base_url"], "ollama.base_url")
    model = _bounded_string(value["model"], "ollama.model", _MAX_IDENTIFIER_LENGTH)
    allow_remote = _require_bool(value["allow_remote"], "ollama.allow_remote")
    _validate_ollama_url(base_url, allow_remote)
    return OllamaSettings(base_url=base_url, model=model, allow_remote=allow_remote)


def _decode_sources(value: Any) -> Tuple[SourceSettings, ...]:
    _require_list(value, "sources", _MAX_SOURCES)
    return tuple(_decode_source(item) for item in value)


def _decode_source(value: Any) -> SourceSettings:
    _require_exact_keys(
        value,
        {"id", "platform", "community", "profile", "schedule_seconds", "authorizations"},
    )
    platform = _bounded_string(value["platform"], "source.platform", 32)
    if platform not in _SUPPORTED_PLATFORMS:
        raise ValueError("source.platform is unsupported")
    schedule_seconds = _positive_int(value["schedule_seconds"], "source.schedule_seconds")
    if schedule_seconds < 300:
        raise ValueError("source.schedule_seconds must be at least 300")
    authorizations = _decode_authorizations(value["authorizations"])
    if not authorizations:
        raise ValueError("source.authorizations must not be empty")
    return SourceSettings(
        id=_bounded_string(value["id"], "source.id", _MAX_IDENTIFIER_LENGTH),
        platform=platform,
        community=_bounded_string(value["community"], "source.community"),
        profile=_bounded_string(value["profile"], "source.profile", _MAX_IDENTIFIER_LENGTH),
        schedule_seconds=schedule_seconds,
        authorizations=authorizations,
    )


def _decode_authorizations(value: Any) -> Tuple[AuthorizationSettings, ...]:
    _require_list(value, "source.authorizations", _MAX_AUTHORIZATIONS_PER_SOURCE)
    return tuple(_decode_authorization(item) for item in value)


def _decode_authorization(value: Any) -> AuthorizationSettings:
    _require_exact_keys(
        value,
        {"subject_type", "subject_key", "scope", "evidence_ref", "granted_at"},
        optional={"expires_at"},
    )
    subject_type = _bounded_string(value["subject_type"], "authorization.subject_type", 32)
    scope = _bounded_string(value["scope"], "authorization.scope", 64)
    if subject_type not in _SUPPORTED_SUBJECT_TYPES:
        raise ValueError("authorization.subject_type is unsupported")
    if scope not in _SUPPORTED_SCOPES:
        raise ValueError("authorization.scope is unsupported")
    if (subject_type, scope) not in {
        ("author", "all_posts_until_revoke"),
        ("post", "single_post"),
    }:
        raise ValueError("authorization scope does not match subject type")
    expires_at = value.get("expires_at")
    if expires_at is not None:
        expires_at = _bounded_string(expires_at, "authorization.expires_at", 64)
    return AuthorizationSettings(
        subject_type=subject_type,
        subject_key=_bounded_string(value["subject_key"], "authorization.subject_key"),
        scope=scope,
        evidence_ref=_bounded_string(value["evidence_ref"], "authorization.evidence_ref"),
        granted_at=_bounded_string(value["granted_at"], "authorization.granted_at", 64),
        expires_at=expires_at,
    )


def _decode_groups(value: Any) -> Tuple[GroupBindingSettings, ...]:
    _require_list(value, "groups", _MAX_GROUPS)
    return tuple(_decode_group(item) for item in value)


def _decode_group(value: Any) -> GroupBindingSettings:
    _require_exact_keys(value, {"group_id", "profile", "cooldown_seconds", "max_reply_chars"})
    return GroupBindingSettings(
        group_id=_positive_int(value["group_id"], "group.group_id"),
        profile=_bounded_string(value["profile"], "group.profile", _MAX_IDENTIFIER_LENGTH),
        cooldown_seconds=_positive_int(value["cooldown_seconds"], "group.cooldown_seconds"),
        max_reply_chars=_positive_int(value["max_reply_chars"], "group.max_reply_chars"),
    )


def _validate_cross_references(settings: CommunityStyleSettings) -> None:
    source_ids = tuple(source.id for source in settings.sources)
    profiles = tuple(source.profile for source in settings.sources)
    group_ids = tuple(group.group_id for group in settings.groups)
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("source IDs must be unique")
    if len(set(profiles)) != len(profiles):
        raise ValueError("profile IDs must be unique")
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("group IDs must be unique")
    profile_set = set(profiles)
    for group in settings.groups:
        if group.profile not in profile_set:
            raise ValueError("group profile must reference a configured source profile")


def _validate_ollama_url(base_url: str, allow_remote: bool) -> None:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise ValueError("ollama.base_url is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is None
    ):
        raise ValueError("ollama.base_url is invalid")
    if not allow_remote and not _is_loopback_host(parsed.hostname):
        raise ValueError("remote Ollama requires allow_remote=true")


def _is_loopback_host(host: str) -> bool:
    if host.rstrip(".").casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("community style config keys must be strings")
            if any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
                raise ValueError("community style config must not contain sensitive fields")
            _reject_sensitive_keys(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)


def _require_exact_keys(value: Any, required: set, optional: set = set()) -> None:
    if not isinstance(value, dict):
        raise ValueError("config entry must be an object")
    actual = set(value)
    allowed = required | optional
    if actual != required and not (required <= actual <= allowed):
        raise ValueError("config entry contains unknown or missing fields")


def _require_list(value: Any, name: str, maximum: int) -> None:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("%s must be a bounded list" % name)


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError("%s must be a boolean" % name)
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s must be a positive integer" % name)
    return value


def _bounded_string(value: Any, name: str, maximum: int = _MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("%s must be a bounded non-empty string" % name)
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError("%s must be a bounded non-empty string" % name)
    return value
