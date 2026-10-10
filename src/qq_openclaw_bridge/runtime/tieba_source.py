"""Robots-aware, bounded reader for public Tieba pages.

Discovery intentionally returns metadata-only candidates.  Post text is fetched
separately after the caller has applied its authorization policy.  Every public
exception is reduced to a stable category so remote data cannot leak through an
exception chain or log message.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import math
import queue
import re
import socket
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from qq_openclaw_bridge.domain.community_style import (
    AuthorizedContent,
    SourceCandidate,
    SourcePage,
)
from qq_openclaw_bridge.runtime.community_style_config import SourceSettings


MAX_TIEBA_RESPONSE_BYTES = 1024 * 1024
MAX_TIEBA_CONTENT_CHARS = 32 * 1024

_MAX_ROBOTS_BYTES = 256 * 1024
_MAX_PAGE_ITEMS = 100
_MAX_OFFSET = 1_000_000_000
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_HTML_NODES = 20_000
_MAX_ATTRIBUTES = 32
_MAX_ATTRIBUTE_CHARS = 1024
_READ_CHUNK_BYTES = 64 * 1024
_MAX_TRANSPORT_WORKERS = 4
_ROBOTS_TTL = timedelta(hours=24)
_ORIGIN = "https://tieba.baidu.com"
_ROBOTS_URL = _ORIGIN + "/robots.txt"
_USER_AGENT = "qq-community-style-agent/0.1 (authorized-style-learning)"

_POST_ID_RE = re.compile(r"[1-9][0-9]{0,19}\Z")
_OFFSET_RE = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]?)\Z")
_PUBLISHED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
_ACCESS_REASONS = frozenset((
    "robots_disallow",
    "robots_unavailable",
    "captcha",
    "login_required",
    "http_403",
    "http_429",
    "http_error",
    "redirect",
    "network",
))
_MARKUP_REASONS = frozenset((
    "invalid_request",
    "invalid_cursor",
    "invalid_candidate",
    "response_exceeded",
    "invalid_markup",
    "identity_mismatch",
))

_TRANSPORT_SLOTS = threading.BoundedSemaphore(_MAX_TRANSPORT_WORKERS)


class TiebaAccessPaused(RuntimeError):
    """Stable reason why this source must pause instead of bypassing access."""


class TiebaMarkupError(RuntimeError):
    """Stable reason why untrusted markup could not satisfy the fixture contract."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        del request, fp, code, message, headers, new_url
        return None


def _raise_public(kind: str, reason: str) -> None:
    if kind == "access":
        raise TiebaAccessPaused(reason)
    raise TiebaMarkupError(reason)


def _safe_reason(error, allowed, fallback: str) -> str:
    if type(error) not in (TiebaAccessPaused, TiebaMarkupError):
        return fallback
    args = getattr(error, "args", ())
    if type(args) is tuple and len(args) == 1 and type(args[0]) is str and args[0] in allowed:
        return args[0]
    return fallback


class TiebaSource:
    """Read public listing and thread pages without login or redirect fallback."""

    ROBOTS_URL = _ROBOTS_URL
    USER_AGENT = _USER_AGENT

    def __init__(
        self,
        settings: SourceSettings,
        *,
        opener=None,
        clock=None,
        deadline_clock=None,
        robot_parser_factory=RobotFileParser,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not isinstance(settings, SourceSettings) or settings.platform != "tieba":
            raise ValueError("invalid Tieba source settings")
        _validate_token(settings.id, 128)
        _validate_token(settings.community, 128, allow_space=True)
        if type(timeout_seconds) not in (int, float):
            raise ValueError("invalid Tieba timeout")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("invalid Tieba timeout") from None
        if not math.isfinite(timeout) or timeout <= 0.0 or timeout > _MAX_TIMEOUT_SECONDS:
            raise ValueError("invalid Tieba timeout")
        if opener is None:
            self._open = build_opener(_NoRedirectHandler()).open
        elif callable(opener):
            self._open = opener
        elif callable(getattr(opener, "open", None)):
            self._open = opener.open
        else:
            raise ValueError("invalid Tieba opener")
        if clock is None:
            self._clock = lambda: datetime.now(timezone.utc)
        elif callable(clock):
            self._clock = clock
        else:
            raise ValueError("invalid Tieba clock")
        if deadline_clock is None:
            self._deadline_clock = time.monotonic
        elif callable(deadline_clock):
            self._deadline_clock = deadline_clock
        else:
            raise ValueError("invalid Tieba deadline clock")
        if not callable(robot_parser_factory):
            raise ValueError("invalid robots parser factory")

        self.settings = settings
        self._robot_parser_factory = robot_parser_factory
        self._timeout_seconds = timeout
        self._robots_parser = None
        self._robots_expires_at = None
        self._robots_etag = None
        self._robots_last_modified = None

    def list_candidates(self, cursor: Mapping[str, str], limit: int) -> SourcePage:
        try:
            return self._list_candidates(cursor, limit)
        except TiebaAccessPaused as exc:
            outcome = ("access", _safe_reason(exc, _ACCESS_REASONS, "network"))
        except TiebaMarkupError as exc:
            outcome = ("markup", _safe_reason(exc, _MARKUP_REASONS, "invalid_markup"))
        except Exception:
            outcome = ("markup", "invalid_markup")
        _raise_public(*outcome)

    def _list_candidates(self, cursor: Mapping[str, str], limit: int) -> SourcePage:
        offset, index = _validate_cursor(cursor)
        page_limit = _validate_limit(limit)
        url = _ORIGIN + "/f?" + urlencode({
            "kw": self.settings.community,
            "ie": "utf-8",
            "pn": offset,
        })
        if not _is_listing_url(url):
            raise TiebaMarkupError("invalid_request")
        self._require_robots_allowed(url)
        raw = self._request(url, expected_content_type="text/html")
        text = _decode_markup(raw)
        _reject_access_wall(text)
        parser = _ListingParser(self.settings.id, self.settings.community)
        try:
            parser.feed(text)
            parser.close()
            items, next_offset = parser.result()
            if index >= len(items) and index != 0:
                raise TiebaMarkupError("invalid_cursor")
            end = min(index + page_limit, len(items))
            selected = tuple(items[index:end])
            if end < len(items):
                cursor_value = (("pn", str(offset)), ("page", str(end)))
            elif next_offset is not None:
                cursor_value = (("pn", next_offset), ("page", "0"))
            else:
                cursor_value = ()
            return SourcePage(items=selected, next_cursor=cursor_value)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise TiebaMarkupError("invalid_markup") from None

    def fetch_content(self, candidate: SourceCandidate) -> AuthorizedContent:
        try:
            return self._fetch_content(candidate)
        except TiebaAccessPaused as exc:
            outcome = ("access", _safe_reason(exc, _ACCESS_REASONS, "network"))
        except TiebaMarkupError as exc:
            outcome = ("markup", _safe_reason(exc, _MARKUP_REASONS, "invalid_markup"))
        except Exception:
            outcome = ("markup", "invalid_markup")
        _raise_public(*outcome)

    def _fetch_content(self, candidate: SourceCandidate) -> AuthorizedContent:
        _validate_candidate(self.settings, candidate)
        url = _ORIGIN + "/p/" + candidate.external_content_id
        self._require_robots_allowed(url)
        raw = self._request(url, expected_content_type="text/html")
        text = _decode_markup(raw)
        _reject_access_wall(text)
        parser = _ThreadParser()
        try:
            parser.feed(text)
            parser.close()
            remote_id, author, published_at, body = parser.result()
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise TiebaMarkupError("invalid_markup") from None
        if (
            remote_id != candidate.external_content_id
            or author != candidate.author_key
            or published_at != candidate.published_at
        ):
            raise TiebaMarkupError("identity_mismatch")
        try:
            return AuthorizedContent(candidate=candidate, text=body)
        except ValueError:
            raise TiebaMarkupError("invalid_markup") from None

    def _require_robots_allowed(self, url: str) -> None:
        now = self._now()
        if (
            self._robots_parser is None
            or self._robots_expires_at is None
            or now >= self._robots_expires_at
        ):
            self._refresh_robots(now)
        parser = self._robots_parser
        if parser is None:
            raise TiebaAccessPaused("robots_unavailable")
        try:
            allowed = parser.can_fetch(_USER_AGENT, url)
        except Exception:
            raise TiebaAccessPaused("robots_unavailable") from None
        if allowed is not True:
            raise TiebaAccessPaused("robots_disallow")

    def _refresh_robots(self, now: datetime) -> None:
        headers = {"Accept": "text/plain"}
        if self._robots_etag is not None:
            headers["If-None-Match"] = self._robots_etag
        if self._robots_last_modified is not None:
            headers["If-Modified-Since"] = self._robots_last_modified
        try:
            status, raw, response_headers = self._request_result(
                _ROBOTS_URL,
                expected_content_type="text/plain",
                maximum=_MAX_ROBOTS_BYTES,
                headers=headers,
                allow_not_modified=True,
            )
            if status == 304:
                if self._robots_parser is None:
                    raise TiebaAccessPaused("robots_unavailable")
                self._robots_expires_at = now + _ROBOTS_TTL
                return
            text = _decode_robots(raw)
            if not _has_applicable_robots_group(text):
                raise ValueError("policy")
            parser = self._robot_parser_factory()
            if (
                parser is None
                or not callable(getattr(parser, "parse", None))
                or not callable(getattr(parser, "can_fetch", None))
            ):
                raise ValueError("parser")
            parser.parse(text.splitlines())
            self._robots_parser = parser
            self._robots_expires_at = now + _ROBOTS_TTL
            self._robots_etag = _safe_validator(response_headers, "ETag")
            self._robots_last_modified = _safe_validator(response_headers, "Last-Modified")
        except TiebaAccessPaused as exc:
            if (
                self._robots_parser is not None
                and self._robots_expires_at is not None
                and now < self._robots_expires_at
            ):
                return
            if _safe_reason(exc, _ACCESS_REASONS, "robots_unavailable") == "robots_unavailable":
                raise
            raise TiebaAccessPaused("robots_unavailable") from None
        except Exception:
            if (
                self._robots_parser is not None
                and self._robots_expires_at is not None
                and now < self._robots_expires_at
            ):
                return
            raise TiebaAccessPaused("robots_unavailable") from None

    def _now(self) -> datetime:
        try:
            value = self._clock()
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("clock")
            return value.astimezone(timezone.utc)
        except Exception:
            raise TiebaAccessPaused("robots_unavailable") from None

    def _request(self, url: str, *, expected_content_type: str) -> bytes:
        _, raw, _ = self._request_result(
            url,
            expected_content_type=expected_content_type,
            maximum=MAX_TIEBA_RESPONSE_BYTES,
            headers={"Accept": expected_content_type},
            allow_not_modified=False,
        )
        return raw

    def _request_result(
        self,
        url: str,
        *,
        expected_content_type: str,
        maximum: int,
        headers: dict,
        allow_not_modified: bool,
    ):
        if not _is_expected_url(url):
            raise TiebaMarkupError("invalid_request")
        try:
            started = self._deadline_now()
            deadline = started + self._timeout_seconds
        except Exception:
            raise TiebaAccessPaused("network") from None
        if not math.isfinite(deadline):
            raise TiebaAccessPaused("network")
        if not _TRANSPORT_SLOTS.acquire(blocking=False):
            raise TiebaAccessPaused("network")

        result_queue = queue.Queue(maxsize=1)
        abandoned = threading.Event()

        def run_transport():
            try:
                result = self._request_result_blocking(
                    url,
                    expected_content_type=expected_content_type,
                    maximum=maximum,
                    headers=headers,
                    allow_not_modified=allow_not_modified,
                    deadline=deadline,
                )
                outcome = ("ok", result)
            except TiebaAccessPaused as exc:
                outcome = ("access", _safe_reason(exc, _ACCESS_REASONS, "network"))
            except TiebaMarkupError as exc:
                outcome = ("markup", _safe_reason(exc, _MARKUP_REASONS, "invalid_markup"))
            except (KeyboardInterrupt, SystemExit) as exc:
                outcome = ("control", exc)
            except Exception:
                outcome = ("access", "network")
            finally:
                _TRANSPORT_SLOTS.release()
            if not abandoned.is_set():
                try:
                    result_queue.put_nowait(outcome)
                except queue.Full:
                    pass

        try:
            worker = threading.Thread(
                target=run_transport,
                name="tieba-http",
                daemon=True,
            )
            worker.start()
        except Exception:
            _TRANSPORT_SLOTS.release()
            raise TiebaAccessPaused("network") from None
        try:
            remaining = self._remaining(deadline)
            if remaining <= 0.0:
                raise queue.Empty
            outcome = result_queue.get(timeout=remaining)
        except queue.Empty:
            abandoned.set()
            raise TiebaAccessPaused("network") from None
        if outcome[0] == "ok":
            return outcome[1]
        if outcome[0] == "control":
            raise outcome[1]
        _raise_public(outcome[0], outcome[1])

    def _request_result_blocking(
        self,
        url: str,
        *,
        expected_content_type: str,
        maximum: int,
        headers: dict,
        allow_not_modified: bool,
        deadline: float,
    ):
        request_headers = dict(headers)
        request_headers["User-Agent"] = _USER_AGENT
        request = Request(url, headers=request_headers, method="GET")
        response = None
        try:
            try:
                response = self._open(request, timeout=max(0.001, self._remaining(deadline)))
                self._require_time(deadline)
            except HTTPError as exc:
                response = exc
                try:
                    status = exc.code
                except Exception:
                    status = None
                if allow_not_modified and type(status) is int and status == 304:
                    final_url = exc.geturl()
                    if type(final_url) is not str or final_url != url or not _is_expected_url(final_url):
                        raise TiebaAccessPaused("redirect")
                    return 304, b"", getattr(exc, "headers", None)
                _raise_http_status(status)
            except (URLError, socket.timeout, TimeoutError, OSError):
                raise TiebaAccessPaused("network") from None
            except (TiebaAccessPaused, TiebaMarkupError):
                raise
            except Exception:
                raise TiebaAccessPaused("network") from None

            try:
                status = getattr(response, "status", None)
                if status is None and callable(getattr(response, "getcode", None)):
                    status = response.getcode()
                if allow_not_modified and type(status) is int and status == 304:
                    final_url = response.geturl()
                    if type(final_url) is not str or final_url != url or not _is_expected_url(final_url):
                        raise TiebaAccessPaused("redirect")
                    return 304, b"", getattr(response, "headers", None)
                if type(status) is not int or status < 200 or status >= 300:
                    _raise_http_status(status)
                final_url = response.geturl()
                if type(final_url) is not str or final_url != url or not _is_expected_url(final_url):
                    raise TiebaAccessPaused("redirect")
                if _content_type(response) != expected_content_type:
                    raise TiebaMarkupError("invalid_markup")
                raw = _read_bounded(response, maximum, deadline, self._deadline_clock)
                return status, raw, getattr(response, "headers", None)
            except (TiebaAccessPaused, TiebaMarkupError):
                raise
            except (URLError, socket.timeout, TimeoutError, OSError):
                raise TiebaAccessPaused("network") from None
            except Exception:
                raise TiebaMarkupError("invalid_markup") from None
        finally:
            if response is not None:
                _safe_close(response)

    def _deadline_now(self) -> float:
        value = self._deadline_clock()
        if type(value) not in (int, float):
            raise ValueError("deadline")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("deadline")
        return result

    def _remaining(self, deadline: float) -> float:
        try:
            return max(0.0, deadline - self._deadline_now())
        except Exception:
            return 0.0

    def _require_time(self, deadline: float) -> None:
        if self._remaining(deadline) <= 0.0:
            raise TiebaAccessPaused("network")


def _validate_token(value, maximum: int, *, allow_space: bool = False) -> str:
    if type(value) is not str:
        raise ValueError("token")
    try:
        normalized = unicodedata.normalize("NFC", value)
        normalized.encode("utf-8", "strict")
    except (UnicodeError, ValueError):
        raise ValueError("token") from None
    if not normalized or normalized != value or normalized != normalized.strip() or len(normalized) > maximum:
        raise ValueError("token")
    for character in normalized:
        if unicodedata.category(character).startswith("C"):
            raise ValueError("token")
        if not allow_space and character.isspace():
            raise ValueError("token")
    return normalized


def _validate_cursor(cursor: Mapping[str, str]) -> tuple[int, int]:
    if not isinstance(cursor, Mapping):
        raise TiebaMarkupError("invalid_cursor")
    try:
        keys = tuple(cursor.keys())
    except Exception:
        raise TiebaMarkupError("invalid_cursor") from None
    if any(type(key) is not str for key in keys) or set(keys) - {"pn", "page"}:
        raise TiebaMarkupError("invalid_cursor")
    if "page" in keys and "pn" not in keys:
        raise TiebaMarkupError("invalid_cursor")
    value = cursor.get("pn", "0")
    if type(value) is not str or not _OFFSET_RE.fullmatch(value):
        raise TiebaMarkupError("invalid_cursor")
    try:
        offset = int(value)
    except (ValueError, OverflowError):
        raise TiebaMarkupError("invalid_cursor") from None
    if offset > _MAX_OFFSET:
        raise TiebaMarkupError("invalid_cursor")
    index_value = cursor.get("page", "0")
    if type(index_value) is not str or not _INDEX_RE.fullmatch(index_value):
        raise TiebaMarkupError("invalid_cursor")
    return offset, int(index_value)


def _validate_limit(limit) -> int:
    if type(limit) is not int or limit < 1:
        raise TiebaMarkupError("invalid_request")
    return min(limit, _MAX_PAGE_ITEMS)


def _is_expected_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname != "tieba.baidu.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False
    if parsed.path == "/robots.txt":
        return not parsed.query
    if parsed.path == "/f":
        return bool(parsed.query)
    return bool(re.fullmatch(r"/p/[1-9][0-9]{0,19}", parsed.path)) and not parsed.query


def _is_listing_url(url: str) -> bool:
    if not _is_expected_url(url):
        return False
    parsed = urlsplit(url)
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    return set(query) == {"kw", "ie", "pn"} and all(len(value) == 1 for value in query.values())


def _raise_http_status(status) -> None:
    if type(status) is not int:
        raise TiebaMarkupError("invalid_markup") from None
    if status == 403:
        raise TiebaAccessPaused("http_403") from None
    if status == 429:
        raise TiebaAccessPaused("http_429") from None
    if 300 <= status <= 399:
        raise TiebaAccessPaused("redirect") from None
    raise TiebaAccessPaused("http_error") from None


def _content_type(response) -> str:
    try:
        headers = response.headers
        if callable(getattr(headers, "get_content_type", None)):
            value = headers.get_content_type()
        else:
            value = headers.get("Content-Type", "")
            if type(value) is str:
                value = value.split(";", 1)[0].strip().lower()
        return value if type(value) is str else ""
    except Exception:
        return ""


def _safe_validator(headers, name: str):
    try:
        value = None if headers is None else headers.get(name)
    except Exception:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or not value.isascii()
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return None
    return value


def _read_bounded(response, maximum: int, deadline: float, deadline_clock) -> bytes:
    chunks = []
    size = 0
    while True:
        try:
            now = float(deadline_clock())
        except (TypeError, ValueError, OverflowError):
            raise TiebaAccessPaused("network") from None
        if not math.isfinite(now) or now >= deadline:
            raise TiebaAccessPaused("network")
        chunk = response.read(min(_READ_CHUNK_BYTES, maximum + 1 - size))
        if type(chunk) is not bytes:
            raise TiebaMarkupError("invalid_markup")
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > maximum:
            raise TiebaMarkupError("response_exceeded")
        chunks.append(chunk)


def _safe_close(response) -> None:
    try:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _decode_robots(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", "strict")
    except (UnicodeError, ValueError):
        raise TiebaAccessPaused("robots_unavailable") from None
    if not text or any(unicodedata.category(char).startswith("C") and char not in "\r\n\t" for char in text):
        raise TiebaAccessPaused("robots_unavailable")
    if len(text.splitlines()) > 10_000 or any(len(line) > 4096 for line in text.splitlines()):
        raise TiebaAccessPaused("robots_unavailable")
    return text


def _has_applicable_robots_group(text: str) -> bool:
    groups = []
    agents = []
    rules = []

    def finish_group():
        nonlocal agents, rules
        if agents or rules:
            groups.append((tuple(agents), tuple(rules)))
        agents = []
        rules = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            finish_group()
            continue
        if ":" not in line:
            continue
        field, value = line.split(":", 1)
        field = field.strip().casefold()
        value = value.strip()
        if field == "user-agent":
            if rules:
                finish_group()
            if not value or len(value) > 256:
                return False
            agents.append(value.casefold())
        elif field in ("allow", "disallow"):
            if not agents:
                return False
            rules.append((field, value))
    finish_group()
    if not groups or any(not group_agents or not group_rules for group_agents, group_rules in groups):
        return False
    user_agent = _USER_AGENT.casefold()
    return any(
        any(agent == "*" or agent in user_agent for agent in group_agents)
        and bool(group_rules)
        for group_agents, group_rules in groups
    )


def _decode_markup(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", "strict")
        text = unicodedata.normalize("NFC", text)
    except (UnicodeError, ValueError):
        raise TiebaMarkupError("invalid_markup") from None
    if not text or any(unicodedata.category(char).startswith("C") and char not in "\r\n\t" for char in text):
        raise TiebaMarkupError("invalid_markup")
    return text


def _reject_access_wall(text: str) -> None:
    parser = _AccessWallParser()
    try:
        parser.feed(text)
        parser.close()
        reason = parser.result()
    except TiebaAccessPaused:
        raise
    except Exception:
        raise TiebaMarkupError("invalid_markup") from None
    if reason is not None:
        raise TiebaAccessPaused(reason)


def _canonical_published_at(value) -> str:
    if type(value) is not str or not _PUBLISHED_AT_RE.fullmatch(value):
        raise ValueError("published")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("published") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("published")
    return value


def _remote_published_at(value) -> str:
    if type(value) is not str:
        raise ValueError("published")
    if _PUBLISHED_AT_RE.fullmatch(value):
        return _canonical_published_at(value)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("published") from None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _classes(value) -> frozenset[str]:
    if type(value) is not str or len(value) > _MAX_ATTRIBUTE_CHARS:
        return frozenset()
    tokens = value.split()
    if len(tokens) > 32 or any(not token or len(token) > 128 for token in tokens):
        raise ValueError("class")
    return frozenset(tokens)


def _bounded_json_object(value):
    if type(value) is not str or not value or len(value) > _MAX_ATTRIBUTE_CHARS:
        raise ValueError("json")
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("json")
            result[key] = item
        return result

    def bounded_int(raw):
        result = int(raw)
        if result < -(2 ** 63) or result > 2 ** 63 - 1:
            raise ValueError("json")
        return result

    def finite_float(raw):
        result = float(raw)
        if not math.isfinite(result):
            raise ValueError("json")
        return result

    def reject_constant(raw):
        del raw
        raise ValueError("json")

    try:
        result = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_int=bounded_int,
            parse_float=finite_float,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("json") from None
    count = 0

    def validate(item, depth):
        nonlocal count
        count += 1
        if count > 64 or depth > 6:
            raise ValueError("json")
        if item is None or type(item) in (bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("json")
            return
        if type(item) is str:
            _validate_token(item, 256, allow_space=True)
            return
        if type(item) is list:
            if len(item) > 32:
                raise ValueError("json")
            for child in item:
                validate(child, depth + 1)
            return
        if type(item) is dict:
            if len(item) > 32:
                raise ValueError("json")
            for key, child in item.items():
                _validate_token(key, 128)
                validate(child, depth + 1)
            return
        raise ValueError("json")

    validate(result, 0)
    if type(result) is not dict:
        raise ValueError("json")
    return result


def _same_or_set(current, value):
    if value is None:
        return current
    if current is not None and current != value:
        raise ValueError("conflict")
    return value


def _validate_candidate(settings: SourceSettings, candidate) -> None:
    try:
        if not isinstance(candidate, SourceCandidate):
            raise ValueError("type")
        if (
            candidate.source_id != settings.id
            or candidate.platform != "tieba"
            or candidate.community_key != settings.community
            or candidate.text != ""
            or not _POST_ID_RE.fullmatch(candidate.external_content_id)
        ):
            raise ValueError("scope")
        _validate_token(candidate.author_key, 128)
        _canonical_published_at(candidate.published_at)
        if candidate.canonical_url != _ORIGIN + "/p/" + candidate.external_content_id:
            raise ValueError("url")
    except (TypeError, ValueError):
        raise TiebaMarkupError("invalid_candidate") from None


class _BoundedHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._nodes = 0

    def _bump(self) -> None:
        self._nodes += 1
        if self._nodes > _MAX_HTML_NODES:
            raise ValueError("nodes")

    def _attrs(self, attrs):
        self._bump()
        if len(attrs) > _MAX_ATTRIBUTES:
            raise ValueError("attrs")
        result = {}
        for key, value in attrs:
            if (
                type(key) is not str
                or key in result
                or value is None
                or type(value) is not str
                or len(key) > 128
                or len(value) > _MAX_ATTRIBUTE_CHARS
            ):
                raise ValueError("attrs")
            result[key] = value
        return result

    def handle_endtag(self, tag):
        del tag
        self._bump()

    def handle_data(self, data):
        self._bump()
        if len(data) > MAX_TIEBA_CONTENT_CHARS + 1:
            raise ValueError("data")

    def handle_comment(self, data):
        self._bump()
        if len(data) > _MAX_ATTRIBUTE_CHARS:
            raise ValueError("comment")


class _AccessWallParser(_BoundedHTMLParser):
    _VOID = frozenset((
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    ))
    _NAVIGATION_TAGS = frozenset(("header", "nav", "menu", "a", "button"))
    _NAVIGATION_ROLES = frozenset((
        "navigation", "menu", "menubar", "menuitem", "button", "link",
    ))

    def __init__(self):
        super().__init__()
        self._stack = []
        self._title_depth = None
        self._body_depth = None
        self._skip_depth = None
        self._navigation_depth = None
        self._title_parts = []
        self._title_size = 0
        self._container_reason = None

    def handle_starttag(self, tag, attrs):
        values = self._attrs(attrs)
        depth = len(self._stack)
        if self._skip_depth is None:
            if tag == "title":
                if self._title_depth is not None:
                    raise ValueError("title")
                self._title_depth = depth
            if tag == "body":
                if self._body_depth is not None:
                    raise ValueError("body")
                self._body_depth = depth
            if (
                tag not in self._VOID
                and self._navigation_depth is None
                and self._is_navigation(tag, values)
            ):
                self._navigation_depth = depth
            self._inspect_container(tag, values)
            if tag in ("script", "style"):
                self._skip_depth = depth
        if tag not in self._VOID:
            self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        values = self._attrs(attrs)
        self._inspect_container(tag, values)

    def _is_navigation(self, tag, values):
        roles = unicodedata.normalize("NFKC", values.get("role", "")).casefold().split()
        return tag in self._NAVIGATION_TAGS or bool(self._NAVIGATION_ROLES.intersection(roles))

    def _inspect_container(self, tag, values):
        if (
            self._skip_depth is not None
            or self._navigation_depth is not None
            or self._is_navigation(tag, values)
            or self._body_depth is None
            or len(self._stack) <= self._body_depth
            or tag not in ("div", "form", "main", "section")
        ):
            return
        # Semantic ancestors define navigation. An unrelated class token such
        # as "action" or "trigger" cannot cancel an explicit wall marker.
        for name in ("id", "class", "role"):
            reason = _wall_reason(values.get(name, ""), attribute=True)
            if reason is not None:
                self._container_reason = reason
                break

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in self._VOID or not self._stack or self._stack[-1] != tag:
            raise ValueError("stack")
        depth = len(self._stack) - 1
        self._stack.pop()
        if self._skip_depth is not None and depth == self._skip_depth:
            self._skip_depth = None
        if self._navigation_depth is not None and depth == self._navigation_depth:
            self._navigation_depth = None
        if tag == "title" and self._title_depth == depth:
            self._title_depth = None
        if tag == "body" and self._body_depth == depth:
            self._body_depth = None

    def handle_data(self, data):
        super().handle_data(data)
        if self._skip_depth is None and self._title_depth is not None:
            self._title_parts.append(data)
            self._title_size += len(data)
            if self._title_size > 4096:
                raise ValueError("title")

    def result(self):
        if (
            self._stack
            or self._title_depth is not None
            or self._skip_depth is not None
            or self._navigation_depth is not None
        ):
            raise ValueError("stack")
        if self._container_reason is not None:
            return self._container_reason
        title_reason = _wall_reason("".join(self._title_parts), attribute=False)
        if title_reason is not None:
            return title_reason
        return None


def _wall_reason(value: str, *, attribute: bool):
    if type(value) is not str:
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    if attribute:
        separated = re.sub(r"[-_:.]+", " ", normalized)
        tokens = tuple(filter(None, re.split(r"[^0-9a-z\u4e00-\u9fff]+", separated)))
        words = frozenset(tokens)
        if "captcha" in words:
            return "captcha"
        if "login" in words or "passport" in words:
            return "login_required"
        if (
            {"verify", "human"}.issubset(words)
            or {"human", "verification"}.issubset(words)
            or {"verification", "challenge"}.issubset(words)
            or tokens == ("challenge",)
        ):
            return "captcha"
    compact = re.sub(r"\s+", " ", normalized)
    if any(marker in compact for marker in ("captcha", "verify human", "验证码", "安全验证")):
        return "captcha"
    if any(marker in compact for marker in ("login required", "请登录", "登录贴吧")):
        return "login_required"
    return None


class _ListingParser(_BoundedHTMLParser):
    _VOID = _AccessWallParser._VOID

    def __init__(self, source_id: str, community: str):
        super().__init__()
        self.source_id = source_id
        self.community = community
        self.stack = []
        self.thread_list_depth = None
        self.thread_list_open = False
        self.current = None
        self.items = []
        self.next_offset = None

    def handle_starttag(self, tag, attrs):
        values = self._attrs(attrs)
        classes = _classes(values.get("class", ""))
        depth = len(self.stack)
        if tag in ("ul", "div") and values.get("id") == "thread_list":
            if self.thread_list_depth is not None or self.thread_list_open:
                raise ValueError("thread-list")
            self.thread_list_depth = depth
            self.thread_list_open = True
        elif tag == "li" and "j_thread_list" in classes:
            if self.current is not None:
                raise ValueError("nested")
            if (
                not self.thread_list_open
                or self.thread_list_depth is None
                or depth != self.thread_list_depth + 1
            ):
                raise ValueError("owner")
            post_id = values.get("data-tid")
            if type(post_id) is not str or not _POST_ID_RE.fullmatch(post_id):
                raise ValueError("id")
            field = _bounded_json_object(values.get("data-field"))
            field_id = field.get("id")
            if str(field_id) != post_id:
                raise ValueError("id")
            author = field.get("author_name")
            published = field.get("create_time")
            self.current = {
                "depth": depth,
                "id": post_id,
                "author": None if author is None else _validate_token(author, 128),
                "published": None if published is None else _remote_published_at(published),
                "href": None,
                "title_depth": None,
                "author_depth": None,
            }
        elif self.current is not None:
            post_id = self.current["id"]
            href = values.get("href")
            if (
                tag == "div"
                and "threadlist_title" in classes
                and depth == self.current["depth"] + 1
            ):
                if self.current["title_depth"] is not None:
                    raise ValueError("title")
                self.current["title_depth"] = depth
            if (
                tag == "div"
                and "threadlist_author" in classes
                and depth == self.current["depth"] + 1
            ):
                if self.current["author_depth"] is not None:
                    raise ValueError("author")
                self.current["author_depth"] = depth
            in_title = (
                self.current["title_depth"] is not None
                and depth > self.current["title_depth"]
                and depth <= self.current["title_depth"] + 2
            )
            in_author = (
                self.current["author_depth"] is not None
                and depth > self.current["author_depth"]
                and depth <= self.current["author_depth"] + 2
            )
            if tag == "a" and in_title and href == "/p/" + post_id:
                self.current["href"] = _same_or_set(self.current["href"], href)
            if in_author and "tb_icon_author" in classes:
                field_value = values.get("data-field")
                if field_value is not None:
                    field = _bounded_json_object(field_value)
                    author = field.get("user_name", field.get("un"))
                    if author is not None:
                        self.current["author"] = _same_or_set(
                            self.current["author"], _validate_token(author, 128)
                        )
            if in_author and "is_show_create_time" in classes and values.get("title") is not None:
                self.current["published"] = _same_or_set(
                    self.current["published"], _remote_published_at(values["title"])
                )
        if self.current is None and tag == "a" and ({"next", "pagination-item"} & classes):
            href = values.get("href")
            if type(href) is str and href.startswith("/f?"):
                parsed = urlsplit(href)
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                offsets = query.get("pn")
                if parsed.scheme or parsed.netloc or parsed.fragment or offsets is None or len(offsets) != 1:
                    raise ValueError("cursor")
                offset = offsets[0]
                if not _OFFSET_RE.fullmatch(offset) or int(offset) > _MAX_OFFSET:
                    raise ValueError("cursor")
                self.next_offset = _same_or_set(self.next_offset, offset)
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        values = self._attrs(attrs)
        classes = _classes(values.get("class", ""))
        if tag == "li" and "j_thread_list" in classes:
            raise ValueError("item")

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in self._VOID or not self.stack or self.stack[-1] != tag:
            raise ValueError("stack")
        depth = len(self.stack) - 1
        self.stack.pop()
        if self.current is not None and self.current["title_depth"] == depth:
            self.current["title_depth"] = None
        if self.current is not None and self.current["author_depth"] == depth:
            self.current["author_depth"] = None
        if tag == "li" and self.current is not None and depth == self.current["depth"]:
            current = self.current
            self.current = None
            post_id = current["id"]
            author = current["author"]
            published = current["published"]
            href = current["href"]
            _validate_token(author, 128)
            _canonical_published_at(published)
            if href != "/p/" + post_id:
                raise ValueError("href")
            self.items.append(SourceCandidate(
                source_id=self.source_id,
                platform="tieba",
                community_key=self.community,
                external_content_id=post_id,
                author_key=author,
                published_at=published,
                canonical_url=_ORIGIN + href,
                text="",
            ))
            if len(self.items) > _MAX_PAGE_ITEMS:
                raise ValueError("items")
        if (
            self.thread_list_open
            and self.thread_list_depth == depth
            and tag in ("ul", "div")
        ):
            self.thread_list_open = False

    def result(self):
        if (
            self.stack
            or self.thread_list_depth is None
            or self.thread_list_open
            or self.current is not None
            or not self.items
        ):
            raise ValueError("contract")
        ids = [item.external_content_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate")
        return self.items, self.next_offset


class _ThreadParser(_BoundedHTMLParser):
    _VOID = _ListingParser._VOID

    def __init__(self):
        super().__init__()
        self.stack = []
        self.postlist_depth = None
        self.postlist_open = False
        self.current = None
        self.identity = None
        self.canonical_id = None
        self.body_depth = None
        self.body_seen = False
        self.skip_depth = None
        self.parts = []
        self.content_size = 0

    def handle_starttag(self, tag, attrs):
        values = self._attrs(attrs)
        classes = _classes(values.get("class", ""))
        depth = len(self.stack)
        if self.skip_depth is not None:
            if tag not in self._VOID:
                self.stack.append(tag)
            return
        if tag in ("script", "style"):
            self.skip_depth = depth
        elif tag == "link" and "canonical" in _classes(values.get("rel", "")):
            href = values.get("href")
            if type(href) is not str or not _is_expected_url(href):
                raise ValueError("canonical")
            parsed = urlsplit(href)
            if not parsed.path.startswith("/p/"):
                raise ValueError("canonical")
            self.canonical_id = _same_or_set(self.canonical_id, parsed.path[3:])
        elif tag == "div" and "p_postlist" in classes:
            if self.postlist_depth is not None or self.postlist_open or self.current is not None:
                raise ValueError("postlist")
            self.postlist_depth = depth
            self.postlist_open = True
        elif tag == "div" and {"l_post", "j_l_post"}.issubset(classes):
            if self.current is not None:
                raise ValueError("nested-post")
            if (
                not self.postlist_open
                or self.postlist_depth is None
                or depth != self.postlist_depth + 1
            ):
                raise ValueError("owner")
            post_id = values.get("data-pid")
            if type(post_id) is not str or not _POST_ID_RE.fullmatch(post_id):
                raise ValueError("post")
            field = _bounded_json_object(values.get("data-field"))
            author_field = field.get("author")
            content_field = field.get("content")
            if type(author_field) is not dict or type(content_field) is not dict:
                raise ValueError("post")
            author = _validate_token(author_field.get("user_name"), 128)
            field_post_id = content_field.get("post_id")
            if str(field_post_id) != post_id:
                raise ValueError("post")
            post_no = content_field.get("post_no")
            if type(post_no) is not int or post_no < 1 or post_no > 1_000_000:
                raise ValueError("post-number")
            published = _remote_published_at(content_field.get("date"))
            selected = post_no == 1
            if selected:
                if self.identity is not None:
                    raise ValueError("duplicate-root")
                self.identity = (author, published)
            self.current = {
                "depth": depth,
                "selected": selected,
                "post_id": post_id,
            }
        elif (
            tag == "div"
            and {"d_post_content", "j_d_post_content"} & classes
        ):
            if self.current is None:
                if self.postlist_open:
                    raise ValueError("orphan-body")
            elif self.current["selected"]:
                expected_id = "post_content_" + self.current["post_id"]
                if (
                    values.get("id") != expected_id
                    or depth <= self.current["depth"]
                    or depth > self.current["depth"] + 3
                    or self.body_seen
                    or self.body_depth is not None
                ):
                    raise ValueError("body")
                self.body_depth = depth
                self.body_seen = True
        elif tag == "br" and self.body_depth is not None:
            self._append_body("\n")
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        values = self._attrs(attrs)
        classes = _classes(values.get("class", ""))
        if tag == "div" and {"l_post", "j_l_post"}.issubset(classes):
            raise ValueError("post")
        if tag == "br" and self.body_depth is not None and self.skip_depth is None:
            self._append_body("\n")

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in self._VOID or not self.stack or self.stack[-1] != tag:
            raise ValueError("stack")
        depth = len(self.stack) - 1
        self.stack.pop()
        if self.skip_depth is not None:
            if depth == self.skip_depth:
                self.skip_depth = None
            return
        if tag == "div" and self.body_depth is not None and depth == self.body_depth:
            self.body_depth = None
        if tag == "div" and self.current is not None and depth == self.current["depth"]:
            if self.current["selected"] and (self.body_depth is not None or not self.body_seen):
                raise ValueError("body")
            self.current = None
        if (
            self.postlist_open
            and self.postlist_depth == depth
            and tag == "div"
        ):
            self.postlist_open = False

    def handle_data(self, data):
        super().handle_data(data)
        if self.body_depth is not None and self.skip_depth is None:
            self._append_body(data)

    def _append_body(self, value):
        self.content_size += len(value)
        if self.content_size > MAX_TIEBA_CONTENT_CHARS:
            raise ValueError("content")
        self.parts.append(value)

    def result(self):
        if (
            self.stack
            or self.postlist_depth is None
            or self.postlist_open
            or self.current is not None
            or self.identity is None
            or self.canonical_id is None
            or not self.body_seen
            or self.body_depth is not None
            or self.skip_depth is not None
        ):
            raise ValueError("contract")
        author, published = self.identity
        body = _normalize_body("".join(self.parts))
        return self.canonical_id, author, published, body


def _normalize_body(value: str) -> str:
    try:
        value = unicodedata.normalize("NFC", value)
        lines = []
        for raw_line in value.splitlines():
            line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
            if line:
                lines.append(line)
        result = "\n".join(lines)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("content") from None
    if not result or len(result) > MAX_TIEBA_CONTENT_CHARS:
        raise ValueError("content")
    if any(unicodedata.category(char).startswith("C") and char not in "\n\t" for char in result):
        raise ValueError("content")
    return result
