"""Bounded OAuth-only Reddit source for authorized style learning.

Candidate discovery deliberately drops post bodies.  A caller must complete
authorization checks before calling :meth:`fetch_content`, which obtains the
body through a second OAuth request.  Exceptions expose only stable categories;
remote payloads, OAuth tokens, and transport messages are never included.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import re
import socket
import unicodedata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from qq_openclaw_bridge.domain.community_style import (
    AuthorizedContent,
    SourceCandidate,
    SourcePage,
)
from qq_openclaw_bridge.runtime.community_style_config import SourceSettings


MAX_REDDIT_RESPONSE_BYTES = 1024 * 1024
MAX_REDDIT_CONTENT_CHARS = 32 * 1024

_MAX_PAGE_ITEMS = 100
_MAX_TOKEN_CHARS = 4096
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 20_000
_MAX_JSON_INTEGER = 2 ** 63 - 1
_MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60
_DEFAULT_RETRY_AFTER_SECONDS = 60
_READ_CHUNK_BYTES = 64 * 1024
_USER_AGENT = "qq-community-style-agent/0.1 (authorized-style-learning)"
_OAUTH_ORIGIN = "https://oauth.reddit.com"
_CANONICAL_ORIGIN = "https://www.reddit.com"

_COMMUNITY_RE = re.compile(r"[A-Za-z0-9_]{1,100}\Z")
_FULLNAME_RE = re.compile(r"t([13])_[A-Za-z0-9]{1,64}\Z")
_POST_CURSOR_RE = re.compile(r"t3_[A-Za-z0-9]{1,64}\Z")
_PUBLISHED_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")

_PAUSED_REASONS = frozenset((
    "reddit_auth",
    "reddit_candidate",
    "reddit_cursor",
    "reddit_http",
    "reddit_redirect",
    "reddit_request",
    "reddit_response_too_large",
    "reddit_schema",
))


class SourcePausedError(RuntimeError):
    """A stable, non-retryable source failure category."""


class SourceTemporaryError(RuntimeError):
    """A stable failure category that the scheduler may retry."""


class SourceRateLimited(SourceTemporaryError):
    """A Reddit rate limit carrying only a bounded delay."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("reddit_rate_limited")


def _safe_retry_value(error: SourceRateLimited) -> int:
    if type(error) is not SourceRateLimited:
        return _DEFAULT_RETRY_AFTER_SECONDS
    value = getattr(error, "retry_after", _DEFAULT_RETRY_AFTER_SECONDS)
    if type(value) is not int or value < 1 or value > _MAX_RETRY_AFTER_SECONDS:
        return _DEFAULT_RETRY_AFTER_SECONDS
    return value


def _safe_paused_reason(error: SourcePausedError) -> str:
    if type(error) is not SourcePausedError:
        return "reddit_schema"
    args = getattr(error, "args", ())
    if type(args) is tuple and len(args) == 1 and type(args[0]) is str:
        if args[0] in _PAUSED_REASONS:
            return args[0]
    return "reddit_schema"


def _raise_public_error(outcome) -> None:
    kind, value = outcome
    if kind == "rate":
        raise SourceRateLimited(value)
    if kind == "temporary":
        raise SourceTemporaryError("reddit_temporary")
    raise SourcePausedError(kind)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        del request, fp, code, message, headers, new_url
        return None


class RedditSource:
    """Read public Reddit posts exclusively through the OAuth API."""

    def __init__(
        self,
        settings: SourceSettings,
        *,
        token_provider,
        opener=None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not isinstance(settings, SourceSettings):
            raise ValueError("invalid Reddit source settings")
        if settings.platform != "reddit":
            raise ValueError("Reddit source requires platform=reddit")
        if not _COMMUNITY_RE.fullmatch(settings.community):
            raise ValueError("invalid Reddit community")
        if not callable(token_provider):
            raise ValueError("invalid Reddit token provider")
        if type(timeout_seconds) not in (int, float):
            raise ValueError("invalid Reddit timeout")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("invalid Reddit timeout") from None
        if (
            not math.isfinite(timeout)
            or timeout <= 0.0
            or timeout > _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("invalid Reddit timeout")
        if opener is None:
            self._open = build_opener(_NoRedirectHandler()).open
        elif callable(opener):
            self._open = opener
        elif callable(getattr(opener, "open", None)):
            self._open = opener.open
        else:
            raise ValueError("invalid Reddit opener")
        self.settings = settings
        self._token_provider = token_provider
        self._timeout_seconds = timeout

    def list_candidates(self, cursor: Mapping[str, str], limit: int) -> SourcePage:
        try:
            return self._list_candidates(cursor, limit)
        except SourceRateLimited as exc:
            outcome = ("rate", _safe_retry_value(exc))
        except SourceTemporaryError:
            outcome = ("temporary", 0)
        except SourcePausedError as exc:
            outcome = (_safe_paused_reason(exc), 0)
        except Exception:
            outcome = ("reddit_schema", 0)
        _raise_public_error(outcome)

    def _list_candidates(self, cursor: Mapping[str, str], limit: int) -> SourcePage:
        after = _validate_cursor(cursor)
        page_limit = _validate_limit(limit)
        query = {"raw_json": 1, "limit": page_limit}
        if after is not None:
            query["after"] = after
        url = "%s/r/%s/new?%s" % (
            _OAUTH_ORIGIN,
            quote(self.settings.community, safe=""),
            urlencode(query),
        )
        payload = self._request_json(url)
        return _parse_listing(
            self.settings.id,
            self.settings.community,
            payload,
        )

    def fetch_content(self, candidate: SourceCandidate) -> AuthorizedContent:
        try:
            return self._fetch_content(candidate)
        except SourceRateLimited as exc:
            outcome = ("rate", _safe_retry_value(exc))
        except SourceTemporaryError:
            outcome = ("temporary", 0)
        except SourcePausedError as exc:
            outcome = (_safe_paused_reason(exc), 0)
        except Exception:
            outcome = ("reddit_schema", 0)
        _raise_public_error(outcome)

    def _fetch_content(self, candidate: SourceCandidate) -> AuthorizedContent:
        _validate_candidate(self.settings, candidate)
        url = "%s/api/info?%s" % (
            _OAUTH_ORIGIN,
            urlencode({"raw_json": 1, "id": candidate.external_content_id}),
        )
        payload = self._request_json(url)
        text = _parse_content(payload, candidate, self.settings.community)
        try:
            return AuthorizedContent(candidate=candidate, text=text)
        except ValueError:
            raise SourcePausedError("reddit_schema") from None

    def _request_json(self, url: str) -> Any:
        if not _is_oauth_url(url):
            raise SourcePausedError("reddit_request")
        token = self._get_token()
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + token,
                "User-Agent": _USER_AGENT,
            },
            method="GET",
        )
        response = None
        try:
            try:
                response = self._open(request, timeout=self._timeout_seconds)
            except HTTPError as exc:
                try:
                    try:
                        status = exc.code
                        headers = exc.headers
                    except Exception:
                        raise SourceTemporaryError("reddit_temporary") from None
                    if type(status) is not int:
                        raise SourceTemporaryError("reddit_temporary")
                    _raise_http_error(status, headers)
                finally:
                    _safe_close(exc)
            except (URLError, socket.timeout, TimeoutError, OSError):
                raise SourceTemporaryError("reddit_temporary") from None
            except Exception:
                raise SourceTemporaryError("reddit_temporary") from None

            try:
                status = getattr(response, "status", None)
                if status is None and callable(getattr(response, "getcode", None)):
                    status = response.getcode()
                if type(status) is not int:
                    raise SourcePausedError("reddit_schema")
                if status < 200 or status >= 300:
                    _raise_http_error(status, getattr(response, "headers", None))
                final_url = response.geturl()
                if type(final_url) is not str or final_url != url:
                    raise SourcePausedError("reddit_redirect")
                if _content_type(response) != "application/json":
                    raise SourcePausedError("reddit_schema")
                raw = _read_bounded(response)
            except (SourcePausedError, SourceRateLimited, SourceTemporaryError):
                raise
            except (URLError, socket.timeout, TimeoutError, OSError):
                raise SourceTemporaryError("reddit_temporary") from None
            except Exception:
                raise SourcePausedError("reddit_schema") from None
        finally:
            if response is not None:
                _safe_close(response)
        return _strict_json(raw)

    def _get_token(self) -> str:
        failed = False
        try:
            token = self._token_provider()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            failed = True
            token = None
        if failed:
            raise SourcePausedError("reddit_auth")
        if (
            type(token) is not str
            or not token
            or not token.isascii()
            or token != token.strip()
            or len(token) > _MAX_TOKEN_CHARS
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
        ):
            raise SourcePausedError("reddit_auth")
        return token


def _validate_cursor(cursor: Mapping[str, str]):
    if not isinstance(cursor, Mapping) or set(cursor) - {"after"}:
        raise SourcePausedError("reddit_cursor")
    if any(not isinstance(key, str) for key in cursor):
        raise SourcePausedError("reddit_cursor")
    after = cursor.get("after")
    if after is None:
        return None
    if type(after) is not str or not _POST_CURSOR_RE.fullmatch(after):
        raise SourcePausedError("reddit_cursor")
    return after


def _validate_limit(limit: int) -> int:
    if type(limit) is not int or limit < 1:
        raise SourcePausedError("reddit_request")
    return min(limit, _MAX_PAGE_ITEMS)


def _is_oauth_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "oauth.reddit.com"
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and parsed.path.startswith("/")
    )


def _raise_http_error(status: object, headers) -> None:
    if type(status) is not int:
        raise SourceTemporaryError("reddit_temporary") from None
    if status in (401, 403):
        raise SourcePausedError("reddit_auth") from None
    if status == 429:
        raise SourceRateLimited(_retry_after(headers)) from None
    if isinstance(status, int) and 500 <= status <= 599:
        raise SourceTemporaryError("reddit_temporary") from None
    if isinstance(status, int) and 300 <= status <= 399:
        raise SourcePausedError("reddit_redirect") from None
    raise SourcePausedError("reddit_http") from None


def _retry_after(headers) -> int:
    try:
        value = None if headers is None else headers.get("Retry-After")
    except Exception:
        return _DEFAULT_RETRY_AFTER_SECONDS
    if type(value) is not str or not value.isascii() or not value.isdigit():
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        seconds = int(value)
    except (ValueError, OverflowError):
        return _DEFAULT_RETRY_AFTER_SECONDS
    if seconds < 1:
        return _DEFAULT_RETRY_AFTER_SECONDS
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _content_type(response) -> str:
    headers = response.headers
    if callable(getattr(headers, "get_content_type", None)):
        value = headers.get_content_type()
    else:
        value = headers.get("Content-Type", "")
        if type(value) is str:
            value = value.split(";", 1)[0].strip().lower()
    return value if type(value) is str else ""


def _read_bounded(response) -> bytes:
    chunks = []
    size = 0
    while True:
        chunk = response.read(min(_READ_CHUNK_BYTES, MAX_REDDIT_RESPONSE_BYTES + 1 - size))
        if type(chunk) is not bytes:
            raise SourcePausedError("reddit_schema")
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_REDDIT_RESPONSE_BYTES:
            raise SourcePausedError("reddit_response_too_large")
        chunks.append(chunk)


def _safe_close(response) -> None:
    try:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _strict_json(raw: bytes) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    failed = False
    value = None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=_reject_json_constant,
        )
        _bound_json_tree(value)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        failed = True
    if failed:
        raise SourcePausedError("reddit_schema")
    return value


def _reject_json_constant(value: str):
    del value
    raise ValueError("constant")


def _bound_json_tree(value: Any) -> None:
    nodes = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("tree")
        if type(item) is dict:
            if not all(type(key) is str for key in item):
                raise ValueError("key")
            stack.extend((nested, depth + 1) for nested in item.values())
        elif type(item) is list:
            stack.extend((nested, depth + 1) for nested in item)
        elif type(item) is int:
            if abs(item) > _MAX_JSON_INTEGER:
                raise ValueError("integer")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("number")
        elif item is not None and type(item) not in (str, bool):
            raise ValueError("value")


def _parse_listing(source_id: str, community: str, payload: Any) -> SourcePage:
    data = _listing_data(payload)
    children = data.get("children")
    if type(children) is not list or len(children) > _MAX_PAGE_ITEMS:
        raise SourcePausedError("reddit_schema")
    candidates = []
    seen = set()
    try:
        for child in children:
            candidate = _candidate_from_child(source_id, community, child)
            if candidate.external_content_id in seen:
                raise ValueError("duplicate")
            seen.add(candidate.external_content_id)
            candidates.append(candidate)
        after = data.get("after")
        if after is None:
            cursor = ()
        elif type(after) is str and _POST_CURSOR_RE.fullmatch(after):
            cursor = (("after", after),)
        else:
            raise ValueError("cursor")
        return SourcePage(items=tuple(candidates), next_cursor=cursor)
    except (TypeError, ValueError):
        raise SourcePausedError("reddit_schema") from None


def _listing_data(payload: Any) -> dict:
    if type(payload) is not dict or payload.get("kind") != "Listing":
        raise SourcePausedError("reddit_schema")
    data = payload.get("data")
    if type(data) is not dict or "children" not in data:
        raise SourcePausedError("reddit_schema")
    return data


def _candidate_from_child(source_id: str, community: str, child: Any) -> SourceCandidate:
    if type(child) is not dict:
        raise ValueError("child")
    kind = child.get("kind")
    data = child.get("data")
    if type(kind) is not str or kind not in ("t1", "t3") or type(data) is not dict:
        raise ValueError("child")
    fullname = _bounded_fullname(data.get("name"))
    if not fullname.startswith(kind + "_"):
        raise ValueError("kind")
    author = _bounded_author(data.get("author"))
    subreddit = data.get("subreddit")
    if type(subreddit) is not str or subreddit.casefold() != community.casefold():
        raise ValueError("community")
    published_at = _published_at(data.get("created_utc"))
    canonical_url = _canonical_url(data.get("permalink"), community)
    return SourceCandidate(
        source_id=source_id,
        platform="reddit",
        community_key=community,
        external_content_id=fullname,
        author_key=author,
        published_at=published_at,
        canonical_url=canonical_url,
        text="",
    )


def _bounded_fullname(value: Any) -> str:
    if type(value) is not str or not _FULLNAME_RE.fullmatch(value):
        raise ValueError("fullname")
    return value


def _bounded_author(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value in ("[deleted]", "[removed]")
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("author")
    return value


def _published_at(value: Any) -> str:
    if type(value) not in (int, float):
        raise ValueError("created")
    try:
        timestamp = float(value)
        if not math.isfinite(timestamp) or timestamp < 0.0:
            raise ValueError("created")
        result = datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("created") from None
    return result.isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_url(permalink: Any, community: str) -> str:
    if type(permalink) is not str or not permalink or len(permalink) > 480:
        raise ValueError("permalink")
    if any(character.isspace() or ord(character) == 0x7F for character in permalink):
        raise ValueError("permalink")
    parsed = urlsplit(permalink)
    required_prefix = "/r/%s/comments/" % community
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.path
        or not parsed.path.casefold().startswith(required_prefix.casefold())
    ):
        raise ValueError("permalink")
    return _CANONICAL_ORIGIN + parsed.path


def _validate_candidate(settings: SourceSettings, candidate: Any) -> None:
    try:
        if not isinstance(candidate, SourceCandidate):
            raise ValueError("type")
        if (
            candidate.source_id != settings.id
            or candidate.platform != "reddit"
            or candidate.community_key != settings.community
            or candidate.text != ""
        ):
            raise ValueError("scope")
        _bounded_fullname(candidate.external_content_id)
        _bounded_author(candidate.author_key)
        _canonical_published_at(candidate.published_at)
        expected_prefix = candidate.external_content_id[:2]
        if expected_prefix not in ("t1", "t3"):
            raise ValueError("kind")
        parsed = urlsplit(candidate.canonical_url)
        required_prefix = "/r/%s/comments/" % settings.community
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.reddit.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in parsed.path
            or any(character.isspace() for character in parsed.path)
            or not parsed.path.casefold().startswith(required_prefix.casefold())
        ):
            raise ValueError("url")
    except (TypeError, ValueError):
        raise SourcePausedError("reddit_candidate") from None


def _parse_content(payload: Any, candidate: SourceCandidate, community: str) -> str:
    data = _listing_data(payload)
    children = data.get("children")
    if type(children) is not list or len(children) != 1:
        raise SourcePausedError("reddit_schema")
    child = children[0]
    try:
        parsed = _candidate_from_child(candidate.source_id, community, child)
        if (
            parsed.external_content_id != candidate.external_content_id
            or parsed.author_key.casefold() != candidate.author_key.casefold()
            or parsed.published_at != candidate.published_at
            or parsed.canonical_url != candidate.canonical_url
        ):
            raise ValueError("identity")
        remote = child["data"]
        if child["kind"] == "t3":
            title = _content_part(remote.get("title"), maximum=1024)
            selftext = _content_part(remote.get("selftext"), maximum=MAX_REDDIT_CONTENT_CHARS)
            parts = [part for part in (title, selftext) if part not in ("", "[deleted]", "[removed]")]
            text = "\n\n".join(parts)
        else:
            body = _content_part(remote.get("body"), maximum=MAX_REDDIT_CONTENT_CHARS)
            text = "" if body in ("[deleted]", "[removed]") else body
        if not text or len(text) > MAX_REDDIT_CONTENT_CHARS:
            raise ValueError("content")
        return text
    except (KeyError, TypeError, ValueError):
        raise SourcePausedError("reddit_schema") from None


def _content_part(value: Any, *, maximum: int) -> str:
    if type(value) is not str:
        raise ValueError("content")
    try:
        normalized = unicodedata.normalize("NFC", value)
        normalized.encode("utf-8", "strict")
    except (UnicodeError, ValueError):
        raise ValueError("content") from None
    if len(normalized) > maximum:
        raise ValueError("content")
    for character in normalized:
        if unicodedata.category(character).startswith("C") and character not in ("\n", "\t"):
            raise ValueError("content")
    return normalized.strip()


def _canonical_published_at(value: Any) -> str:
    if type(value) is not str or not _PUBLISHED_AT_RE.fullmatch(value):
        raise ValueError("published_at")
    failed = False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError):
        failed = True
        parsed = None
    if failed or parsed is None or parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("published_at")
    return value
