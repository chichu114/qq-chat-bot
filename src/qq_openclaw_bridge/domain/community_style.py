"""Bounded, privacy-preserving domain contracts for community style learning."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from statistics import median
from typing import Optional, Sequence, Tuple
import unicodedata


_MAX_IDENTIFIER_LENGTH = 128
_MAX_STRING_LENGTH = 512
_MAX_TEXT_LENGTH = 32 * 1024
_MAX_TUPLE_ITEMS = 64
_MAX_PAGE_ITEMS = 256
_MAX_SAMPLES = 10_000
_MAX_FEATURE_TOKEN_LENGTH = 96
_SUPPORTED_PLATFORMS = frozenset(("reddit", "tieba"))


def _string(value: object, field: str, maximum: int = _MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    normalized = unicodedata.normalize("NFC", value)
    if not normalized.strip() or len(normalized) > maximum:
        raise ValueError("%s is invalid" % field)
    return normalized


def _token(value: object, field: str, maximum: int = _MAX_STRING_LENGTH) -> str:
    normalized = _string(value, field, maximum)
    if normalized != normalized.strip() or any(unicodedata.category(char).startswith("C") for char in normalized):
        raise ValueError("%s is invalid" % field)
    return normalized


def _text(value: object, field: str, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or len(normalized) > maximum:
        raise ValueError("%s is invalid" % field)
    return normalized


def _optional_token(value: object, field: str, maximum: int = _MAX_STRING_LENGTH) -> str:
    if value == "":
        return ""
    return _token(value, field, maximum)


def _optional_text(value: object, field: str, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if value == "":
        return ""
    return _text(value, field, maximum)


def _tuple_of_strings(value: object, field: str, maximum: int = _MAX_FEATURE_TOKEN_LENGTH) -> Tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_TUPLE_ITEMS:
        raise ValueError("%s is invalid" % field)
    return tuple(_token(item, field, maximum) for item in value)


def _finite_score(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a finite number" % field)
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        raise ValueError("%s must be a finite number" % field)
    return 0.0 if result == 0.0 else result


def _aware_unambiguous(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        offset = value.utcoffset()
        earlier = value.replace(fold=0).utcoffset()
        later = value.replace(fold=1).utcoffset()
    except (OverflowError, ValueError):
        return False
    return offset is not None and earlier is not None and earlier == later


def _safe_digest(*parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"qq-community-style-digest-v1")
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceCandidate:
    source_id: str
    platform: str
    community_key: str
    external_content_id: str
    author_key: str
    published_at: str
    canonical_url: str
    text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _token(self.source_id, "source_id", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "platform", _token(self.platform, "platform", 32))
        object.__setattr__(self, "community_key", _token(self.community_key, "community_key", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "external_content_id", _token(self.external_content_id, "external_content_id", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "author_key", _token(self.author_key, "author_key", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "published_at", _optional_token(self.published_at, "published_at", 64))
        object.__setattr__(self, "canonical_url", _token(self.canonical_url, "canonical_url", _MAX_STRING_LENGTH))
        object.__setattr__(self, "text", _optional_text(self.text, "text", _MAX_TEXT_LENGTH))


@dataclass(frozen=True)
class SourcePage:
    items: Tuple[SourceCandidate, ...]
    next_cursor: Tuple[Tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or len(self.items) > _MAX_PAGE_ITEMS:
            raise ValueError("items is invalid")
        if not all(isinstance(item, SourceCandidate) for item in self.items):
            raise ValueError("items is invalid")
        if not isinstance(self.next_cursor, tuple) or len(self.next_cursor) > _MAX_TUPLE_ITEMS:
            raise ValueError("next_cursor is invalid")
        cursor = []
        for item in self.next_cursor:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("next_cursor is invalid")
            cursor.append((_token(item[0], "next_cursor", _MAX_IDENTIFIER_LENGTH),
                           _token(item[1], "next_cursor", _MAX_STRING_LENGTH)))
        if len({key for key, _ in cursor}) != len(cursor):
            raise ValueError("next_cursor is invalid")
        object.__setattr__(self, "next_cursor", tuple(cursor))


@dataclass(frozen=True)
class AuthorizedContent:
    candidate: SourceCandidate
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SourceCandidate):
            raise ValueError("candidate is invalid")
        object.__setattr__(self, "text", _text(self.text, "text", _MAX_TEXT_LENGTH))


@dataclass(frozen=True)
class Authorization:
    authorization_id: str
    subject_type: str
    subject_key: str
    scope: str
    expires_at: Optional[datetime]
    status: str = "active"
    source_id: str = ""
    platform: str = ""
    community_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "authorization_id", _token(self.authorization_id, "authorization_id", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "subject_type", _token(self.subject_type, "subject_type", 32))
        object.__setattr__(self, "subject_key", _token(self.subject_key, "subject_key", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "scope", _token(self.scope, "scope", 64))
        object.__setattr__(self, "status", _token(self.status, "status", 32))
        object.__setattr__(self, "source_id", _optional_token(self.source_id, "source_id", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "platform", _optional_token(self.platform, "platform", 32))
        object.__setattr__(self, "community_key", _optional_token(self.community_key, "community_key", _MAX_IDENTIFIER_LENGTH))
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise ValueError("expires_at is invalid")

    def is_expired(self, now: datetime) -> bool:
        if self.expires_at is None:
            return False
        if not _aware_unambiguous(now) or not _aware_unambiguous(self.expires_at):
            return True
        return self.expires_at <= now


@dataclass(frozen=True)
class StyleFeatures:
    sentence_length: str
    paragraph_length: str
    punctuation: Tuple[str, ...]
    emoji_tendency: str
    language_mix: str
    directness: float
    humor: str
    rhetorical_question: str
    address_terms: Tuple[str, ...]
    discourse_markers: Tuple[str, ...]
    avoid_patterns: Tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "sentence_length", "paragraph_length", "emoji_tendency", "language_mix",
            "humor", "rhetorical_question",
        ):
            object.__setattr__(self, field, _token(getattr(self, field), field, _MAX_FEATURE_TOKEN_LENGTH))
        for field in ("punctuation", "address_terms", "discourse_markers", "avoid_patterns"):
            object.__setattr__(self, field, _tuple_of_strings(getattr(self, field), field))
        object.__setattr__(self, "directness", _finite_score(self.directness, "directness"))


@dataclass(frozen=True)
class DerivedSample:
    sample_id: str
    source_id: str
    authorization_id: str
    external_content_id: str
    author_fingerprint: str
    canonical_url_fingerprint: str
    content_fingerprint: str
    features: StyleFeatures
    status: str = "active"

    def __post_init__(self) -> None:
        for field in ("sample_id", "source_id", "authorization_id", "external_content_id"):
            object.__setattr__(self, field, _token(getattr(self, field), field, _MAX_IDENTIFIER_LENGTH))
        for field in ("author_fingerprint", "canonical_url_fingerprint", "content_fingerprint"):
            value = _token(getattr(self, field), field, 64)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("%s is invalid" % field)
            object.__setattr__(self, field, value)
        if not isinstance(self.features, StyleFeatures):
            raise ValueError("features is invalid")
        object.__setattr__(self, "status", _token(self.status, "status", 32))

    @classmethod
    def from_content(
        cls,
        content: AuthorizedContent,
        authorization: Authorization,
        features: StyleFeatures,
        *,
        status: str = "active",
    ) -> "DerivedSample":
        if not isinstance(content, AuthorizedContent) or not isinstance(authorization, Authorization):
            raise ValueError("content and authorization are invalid")
        if not isinstance(features, StyleFeatures):
            raise ValueError("features is invalid")
        candidate = content.candidate
        author = normalize_subject(candidate.platform, candidate.author_key)
        return cls(
            sample_id=_safe_digest(candidate.source_id, candidate.external_content_id, authorization.authorization_id),
            source_id=candidate.source_id,
            authorization_id=authorization.authorization_id,
            external_content_id=candidate.external_content_id,
            author_fingerprint=_safe_digest(candidate.platform, author),
            canonical_url_fingerprint=_safe_digest(candidate.canonical_url),
            content_fingerprint=_safe_digest(content.text),
            features=features,
            status=status,
        )


@dataclass(frozen=True)
class StyleRule:
    key: str
    value: str
    support_count: int
    weight: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _token(self.key, "key", _MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "value", _token(self.value, "value", _MAX_FEATURE_TOKEN_LENGTH))
        if isinstance(self.support_count, bool) or not isinstance(self.support_count, int) or self.support_count < 1:
            raise ValueError("support_count is invalid")
        object.__setattr__(self, "weight", _finite_score(self.weight, "weight"))


@dataclass(frozen=True)
class StyleProfile:
    profile_key: str
    version: int
    status: str
    rules: Tuple[StyleRule, ...]
    sample_count: int
    author_count: int
    source_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_key", _token(self.profile_key, "profile_key", _MAX_IDENTIFIER_LENGTH))
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("version is invalid")
        object.__setattr__(self, "status", _token(self.status, "status", 32))
        if not isinstance(self.rules, tuple) or len(self.rules) > _MAX_TUPLE_ITEMS:
            raise ValueError("rules is invalid")
        if not all(isinstance(rule, StyleRule) for rule in self.rules):
            raise ValueError("rules is invalid")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 0:
            raise ValueError("sample_count is invalid")
        if isinstance(self.author_count, bool) or not isinstance(self.author_count, int) or self.author_count < 0:
            raise ValueError("author_count is invalid")
        fingerprint = _token(self.source_fingerprint, "source_fingerprint", 64)
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise ValueError("source_fingerprint is invalid")
        object.__setattr__(self, "source_fingerprint", fingerprint)


def normalize_subject(platform: str, value: str) -> str:
    normalized_platform = _token(platform, "platform", 32)
    normalized_value = _token(value, "subject", _MAX_IDENTIFIER_LENGTH)
    if normalized_platform == "reddit":
        if normalized_value[:2].casefold() == "u/":
            normalized_value = normalized_value[2:]
        return _token(normalized_value, "subject", _MAX_IDENTIFIER_LENGTH).casefold()
    if normalized_platform == "tieba":
        return normalized_value
    raise ValueError("platform is unsupported")


def authorization_matches(candidate: SourceCandidate, authorization: Authorization, now: datetime) -> bool:
    if not isinstance(candidate, SourceCandidate) or not isinstance(authorization, Authorization):
        return False
    if candidate.platform not in _SUPPORTED_PLATFORMS or not _aware_unambiguous(now):
        return False
    if authorization.status != "active" or authorization.is_expired(now):
        return False
    if not authorization.source_id or authorization.source_id != candidate.source_id:
        return False
    if not authorization.platform or authorization.platform != candidate.platform:
        return False
    if not authorization.community_key or authorization.community_key != candidate.community_key:
        return False
    if authorization.subject_type == "post" and authorization.scope == "single_post":
        return candidate.external_content_id == authorization.subject_key
    if authorization.subject_type == "author" and authorization.scope == "all_posts_until_revoke":
        try:
            return normalize_subject(candidate.platform, candidate.author_key) == normalize_subject(
                candidate.platform, authorization.subject_key
            )
        except ValueError:
            return False
    return False


def _rule(kind: str, value: str, support_count: int, sample_count: int) -> StyleRule:
    return StyleRule(
        key="%s:%s" % (kind, value) if kind != "directness" else kind,
        value=value,
        support_count=support_count,
        weight=support_count / float(sample_count),
    )


def _supported_values(
    samples: Tuple[DerivedSample, ...],
    kind: str,
    values: Sequence[Tuple[str, ...]],
    *,
    author_sensitive: bool = False,
) -> Tuple[StyleRule, ...]:
    support = {}
    authors = {}
    for sample, sample_values in zip(samples, values):
        for value in set(sample_values):
            support[value] = support.get(value, 0) + 1
            authors.setdefault(value, set()).add(sample.author_fingerprint)
    rules = []
    for value in sorted(support):
        if support[value] < 3:
            continue
        if author_sensitive and len(authors[value]) < 2:
            continue
        rules.append(_rule(kind, value, support[value], len(samples)))
    return tuple(rules)


def _categorical_rules(samples: Tuple[DerivedSample, ...]) -> Tuple[StyleRule, ...]:
    groups = (
        ("discourse_marker", tuple(sample.features.discourse_markers for sample in samples), True),
        ("address_term", tuple(sample.features.address_terms for sample in samples), True),
        ("punctuation", tuple(sample.features.punctuation for sample in samples), False),
        ("avoid_pattern", tuple(sample.features.avoid_patterns for sample in samples), False),
        ("sentence_length", tuple((sample.features.sentence_length,) for sample in samples), False),
        ("paragraph_length", tuple((sample.features.paragraph_length,) for sample in samples), False),
        ("emoji_tendency", tuple((sample.features.emoji_tendency,) for sample in samples), False),
        ("language_mix", tuple((sample.features.language_mix,) for sample in samples), False),
        ("humor", tuple((sample.features.humor,) for sample in samples), False),
        ("rhetorical_question", tuple((sample.features.rhetorical_question,) for sample in samples), False),
    )
    rules = []
    for kind, values, author_sensitive in groups:
        rules.extend(_supported_values(samples, kind, values, author_sensitive=author_sensitive))
    return tuple(rules)


def _profile_fingerprint(samples: Tuple[DerivedSample, ...]) -> str:
    # Deliberately excludes body, URL, and author fingerprint. Feature values are
    # structured model output, not source text, and must affect profile identity.
    records = []
    for sample in samples:
        records.append(json.dumps({
            "content_fingerprint": sample.content_fingerprint,
            "external_content_id": sample.external_content_id,
            "features": {
                "address_terms": sample.features.address_terms,
                "avoid_patterns": sample.features.avoid_patterns,
                "directness": sample.features.directness,
                "discourse_markers": sample.features.discourse_markers,
                "emoji_tendency": sample.features.emoji_tendency,
                "humor": sample.features.humor,
                "language_mix": sample.features.language_mix,
                "paragraph_length": sample.features.paragraph_length,
                "punctuation": sample.features.punctuation,
                "rhetorical_question": sample.features.rhetorical_question,
                "sentence_length": sample.features.sentence_length,
            },
            "source_id": sample.source_id,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return _safe_digest("profile-fingerprint-v1", *sorted(records))


def _unique_active_samples(samples: Sequence[DerivedSample]) -> Tuple[DerivedSample, ...]:
    by_identity = {}
    for sample in samples:
        if sample.status == "active":
            by_identity.setdefault((sample.source_id, sample.external_content_id), []).append(sample)
    unique = []
    for identity in sorted(by_identity):
        versions = {
            (sample.content_fingerprint, sample.features, sample.author_fingerprint)
            for sample in by_identity[identity]
        }
        if len(versions) != 1:
            raise ValueError("conflicting sample versions")
        unique.append(min(
            by_identity[identity],
            key=lambda sample: (
                sample.authorization_id,
                sample.sample_id,
                sample.canonical_url_fingerprint,
            ),
        ))
    return tuple(unique)


def aggregate_style_profile(
    profile_key: str,
    samples: Sequence[DerivedSample],
    minimum_samples: int = 5,
) -> Optional[StyleProfile]:
    normalized_key = _token(profile_key, "profile_key", _MAX_IDENTIFIER_LENGTH)
    if isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int) or not 1 <= minimum_samples <= _MAX_SAMPLES:
        raise ValueError("minimum_samples is invalid")
    if not isinstance(samples, Sequence) or len(samples) > _MAX_SAMPLES:
        raise ValueError("samples is invalid")
    if not all(isinstance(sample, DerivedSample) for sample in samples):
        raise ValueError("samples is invalid")
    valid = _unique_active_samples(samples)
    if len(valid) < minimum_samples:
        return None
    rules = list(_categorical_rules(valid))[:_MAX_TUPLE_ITEMS - 1]
    directness = median(tuple(sample.features.directness for sample in valid))
    rules.append(_rule("directness", "%.2f" % directness, len(valid), len(valid)))
    return StyleProfile(
        profile_key=normalized_key,
        version=0,
        status="candidate",
        rules=tuple(rules),
        sample_count=len(valid),
        author_count=len({sample.author_fingerprint for sample in valid}),
        source_fingerprint=_profile_fingerprint(valid),
    )
