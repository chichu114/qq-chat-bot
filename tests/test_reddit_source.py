import io
import json
import math
from pathlib import Path
import socket
import sys
import traceback
import unittest
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_openclaw_bridge.domain.community_style import SourceCandidate
from qq_openclaw_bridge.runtime.community_style_config import (
    AuthorizationSettings,
    SourceSettings,
)
from qq_openclaw_bridge.runtime.reddit_source import (
    MAX_REDDIT_RESPONSE_BYTES,
    RedditSource,
    SourcePausedError,
    SourceRateLimited,
    SourceTemporaryError,
)


def _settings(**changes):
    values = {
        "id": "reddit-python",
        "platform": "reddit",
        "community": "python",
        "profile": "reddit-python",
        "schedule_seconds": 3600,
        "authorizations": (
            AuthorizationSettings(
                subject_type="author",
                subject_key="approved_author",
                scope="all_posts_until_revoke",
                evidence_ref="consent-001",
                granted_at="2026-09-19T00:00:00Z",
            ),
        ),
    }
    values.update(changes)
    return SourceSettings(**values)


def _post(
    *,
    name="t3_abc123",
    author="approved_author",
    created_utc=1_700_000_000,
    permalink="/r/python/comments/abc123/synthetic_title/",
    title="Synthetic title",
    selftext="Synthetic body",
    subreddit="python",
    **extra,
):
    data = {
        "name": name,
        "author": author,
        "created_utc": created_utc,
        "permalink": permalink,
        "title": title,
        "selftext": selftext,
        "subreddit": subreddit,
    }
    data.update(extra)
    return {"kind": "t3", "data": data}


def _comment(
    *,
    name="t1_def456",
    author="approved_author",
    created_utc=1_700_000_001,
    permalink="/r/python/comments/abc123/synthetic_title/def456/",
    body="Synthetic comment",
    subreddit="python",
):
    return {
        "kind": "t1",
        "data": {
            "name": name,
            "author": author,
            "created_utc": created_utc,
            "permalink": permalink,
            "body": body,
            "subreddit": subreddit,
        },
    }


def _listing(*children, after="t3_next"):
    if not children:
        children = (_post(),)
    return {
        "kind": "Listing",
        "data": {"after": after, "before": None, "children": list(children)},
    }


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class FakeHeaders(dict):
    def get_content_type(self):
        return self.get("Content-Type", "").split(";", 1)[0].strip().lower()


class FakeResponse:
    def __init__(
        self,
        body,
        *,
        status=200,
        content_type="application/json; charset=utf-8",
        final_url=None,
    ):
        self.body = body
        self.status = status
        self.headers = FakeHeaders({"Content-Type": content_type})
        self.final_url = final_url
        self.offset = 0
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size < 0:
            result = self.body[self.offset:]
            self.offset = len(self.body)
            return result
        result = self.body[self.offset:self.offset + size]
        self.offset += len(result)
        return result

    def geturl(self):
        return self.final_url

    def close(self):
        self.closed = True


class ReadErrorResponse(FakeResponse):
    def __init__(self, error):
        super().__init__(b"")
        self.error = error

    def read(self, size=-1):
        self.read_sizes.append(size)
        raise self.error


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.timeouts = []

    @property
    def request(self):
        return self.requests[-1]

    def open(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.outcomes:
            raise AssertionError("unexpected network request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome.final_url is None:
            outcome.final_url = request.full_url
        return outcome


class HostileHTTPError(HTTPError):
    def __init__(self):
        super().__init__(
            "https://oauth.reddit.com/remote-secret-url",
            503,
            "remote-secret-reason",
            FakeHeaders(),
            io.BytesIO(b"remote-secret-body"),
        )

    def __getattribute__(self, name):
        if name == "code":
            raise RuntimeError("remote-secret-status")
        return super().__getattribute__(name)


class HostileComparisonValue:
    calls = 0

    def __eq__(self, other):
        del other
        type(self).calls += 1
        raise RuntimeError("remote-secret-comparison")


class HostileValueHTTPError(HTTPError):
    def __init__(self):
        super().__init__(
            "https://oauth.reddit.com/remote-secret-url",
            503,
            "remote-secret-reason",
            FakeHeaders(),
            io.BytesIO(b"remote-secret-body"),
        )

    def __getattribute__(self, name):
        if name == "code":
            return HostileComparisonValue()
        return super().__getattribute__(name)


class HostileToken(str):
    calls = 0

    def isascii(self):
        type(self).calls += 1
        raise RuntimeError("oauth-client-secret-isascii")

    def strip(self, *args, **kwargs):
        del args, kwargs
        type(self).calls += 1
        raise RuntimeError("oauth-client-secret-strip")


class HostileHeaderString(str):
    calls = 0

    def split(self, *args, **kwargs):
        del args, kwargs
        type(self).calls += 1
        raise RuntimeError("remote-secret-content-type")


class RawHostileHeaders:
    def get(self, name, default=None):
        del name, default
        return HostileHeaderString("application/json; remote-secret")


def _response(value, **kwargs):
    return FakeResponse(_json_bytes(value), **kwargs)


def _http_error(code, *, headers=None, body=b"remote-secret-body"):
    return HTTPError(
        "https://oauth.reddit.com/redacted",
        code,
        "remote-secret-reason",
        FakeHeaders({} if headers is None else headers),
        io.BytesIO(body),
    )


class RedditSourceTests(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener(_response(_listing()))
        self.source = RedditSource(
            _settings(), token_provider=lambda: "oauth-token", opener=self.opener
        )

    def assert_safe_exception(self, exception, *forbidden):
        pending = [exception]
        seen = set()
        rendered = []
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            rendered.append("%s:%s" % (type(current).__name__, str(current)))
            pending.extend((current.__context__, current.__cause__))
        joined = "\n".join(rendered)
        for value in forbidden:
            self.assertNotIn(value, joined)
        self.assertIsNone(exception.__context__)
        self.assertIsNone(exception.__cause__)

    def test_listing_uses_only_oauth_host_and_identifiable_user_agent(self):
        page = self.source.list_candidates({}, 25)

        self.assertEqual(
            self.opener.request.full_url,
            "https://oauth.reddit.com/r/python/new?raw_json=1&limit=25",
        )
        self.assertEqual(self.opener.request.headers["Authorization"], "Bearer oauth-token")
        self.assertIn("qq-community-style-agent", self.opener.request.headers["User-agent"])
        self.assertEqual(page.next_cursor, (("after", "t3_next"),))

    def test_listing_applies_valid_cursor_and_caps_limit(self):
        self.source.list_candidates({"after": "t3_old"}, 1000)

        self.assertEqual(
            self.opener.request.full_url,
            "https://oauth.reddit.com/r/python/new?raw_json=1&limit=100&after=t3_old",
        )

    def test_invalid_cursor_or_limit_is_rejected_before_token_or_network(self):
        calls = []
        opener = FakeOpener()
        source = RedditSource(
            _settings(), token_provider=lambda: calls.append("token") or "token", opener=opener
        )

        invalid_inputs = [
            ({"after": "https://www.reddit.com/secret"}, 10),
            ({"after": "t1_comment"}, 10),
            ({"after": "t3_ok", "token": "secret"}, 10),
            ({1: "t3_ok"}, 10),
            ({}, 0),
            ({}, True),
            ({}, math.inf),
        ]
        for cursor, limit in invalid_inputs:
            with self.subTest(cursor=cursor, limit=limit):
                with self.assertRaisesRegex(SourcePausedError, "reddit_(cursor|request)"):
                    source.list_candidates(cursor, limit)

        self.assertEqual(calls, [])
        self.assertEqual(opener.requests, [])

    def test_token_provider_failure_is_sanitized_before_network(self):
        opener = FakeOpener()

        def bad_token():
            raise RuntimeError("oauth-client-secret")

        source = RedditSource(_settings(), token_provider=bad_token, opener=opener)
        with self.assertRaises(SourcePausedError) as caught:
            source.list_candidates({}, 10)

        self.assertEqual(str(caught.exception), "reddit_auth")
        self.assertNotIn("secret", repr(caught.exception))
        self.assert_safe_exception(caught.exception, "oauth-client-secret")
        self.assertEqual(opener.requests, [])

    def test_invalid_token_is_rejected_before_network(self):
        for token in ("", " leading", "line\nbreak", "令牌", "x" * 4097, b"token"):
            with self.subTest(token=type(token).__name__):
                opener = FakeOpener()
                source = RedditSource(
                    _settings(), token_provider=lambda token=token: token, opener=opener
                )
                with self.assertRaisesRegex(SourcePausedError, "reddit_auth"):
                    source.list_candidates({}, 10)
                self.assertEqual(opener.requests, [])

    def test_hostile_string_subclass_token_is_rejected_without_invoking_its_methods(self):
        HostileToken.calls = 0
        opener = FakeOpener()
        source = RedditSource(
            _settings(),
            token_provider=lambda: HostileToken("oauth-client-secret"),
            opener=opener,
        )

        with self.assertRaises(SourcePausedError) as caught:
            source.list_candidates({}, 10)

        self.assertEqual(str(caught.exception), "reddit_auth")
        self.assert_safe_exception(caught.exception, "oauth-client-secret")
        self.assertEqual(HostileToken.calls, 0)
        self.assertEqual(opener.requests, [])

    def test_auth_failure_never_falls_back_to_reddit_html(self):
        auth_error = _http_error(401)
        opener = FakeOpener(auth_error, _response(_listing()))
        source = RedditSource(
            _settings(), token_provider=lambda: "oauth-token", opener=opener
        )

        with self.assertRaisesRegex(SourcePausedError, "reddit_auth") as caught:
            source.list_candidates({}, 10)

        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.request.full_url.split("/", 3)[2], "oauth.reddit.com")
        self.assertTrue(auth_error.fp.closed)
        self.assert_safe_exception(caught.exception, "remote-secret")

    def test_forbidden_auth_failure_is_paused(self):
        opener = FakeOpener(_http_error(403))
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
        with self.assertRaisesRegex(SourcePausedError, "reddit_auth"):
            source.list_candidates({}, 10)

    def test_rate_limit_exposes_only_bounded_retry_after(self):
        for value, expected in (("120", 120), ("999999999", 86400), (None, 60)):
            with self.subTest(value=value):
                headers = {} if value is None else {"Retry-After": value}
                opener = FakeOpener(_http_error(429, headers=headers))
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaises(SourceRateLimited) as caught:
                    source.list_candidates({}, 10)
                self.assertEqual(caught.exception.retry_after, expected)
                self.assertEqual(str(caught.exception), "reddit_rate_limited")

    def test_server_and_network_failures_are_retryable_and_sanitized(self):
        errors = (
            _http_error(503),
            URLError("upstream-secret"),
            socket.timeout("timeout-secret"),
            OSError("transport-secret"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                opener = FakeOpener(error)
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaises(SourceTemporaryError) as caught:
                    source.list_candidates({}, 10)
                self.assertEqual(str(caught.exception), "reddit_temporary")
                self.assertNotIn("secret", repr(caught.exception))

    def test_other_http_failures_pause_without_remote_details(self):
        opener = FakeOpener(_http_error(404))
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
        with self.assertRaises(SourcePausedError) as caught:
            source.list_candidates({}, 10)
        self.assertEqual(str(caught.exception), "reddit_http")
        self.assertNotIn("remote", repr(caught.exception))

    def test_hostile_http_error_metadata_is_closed_and_never_leaks(self):
        error = HostileHTTPError()
        opener = FakeOpener(error)
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

        with self.assertRaises(SourceTemporaryError) as caught:
            source.list_candidates({}, 10)

        rendered = "".join(
            traceback.format_exception(
                type(caught.exception), caught.exception, caught.exception.__traceback__
            )
        )
        self.assertNotIn("remote-secret", rendered)
        self.assert_safe_exception(caught.exception, "remote-secret")
        self.assertTrue(error.fp.closed)

    def test_hostile_http_status_value_is_rejected_without_comparison_or_leak(self):
        HostileComparisonValue.calls = 0
        error = HostileValueHTTPError()
        opener = FakeOpener(error)
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

        with self.assertRaises(SourceTemporaryError) as caught:
            source.list_candidates({}, 10)

        self.assertEqual(str(caught.exception), "reddit_temporary")
        self.assert_safe_exception(caught.exception, "remote-secret")
        self.assertEqual(HostileComparisonValue.calls, 0)
        self.assertTrue(error.fp.closed)

    def test_response_read_transport_errors_are_retryable_closed_and_sanitized(self):
        for error in (
            socket.timeout("remote-secret-timeout"),
            TimeoutError("remote-secret-timeout"),
            OSError("remote-secret-read"),
        ):
            with self.subTest(error=type(error).__name__):
                response = ReadErrorResponse(error)
                opener = FakeOpener(response)
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

                with self.assertRaises(SourceTemporaryError) as caught:
                    source.list_candidates({}, 10)

                self.assertEqual(str(caught.exception), "reddit_temporary")
                self.assert_safe_exception(caught.exception, "remote-secret")
                self.assertTrue(response.closed)

    def test_hostile_content_type_string_subclass_is_not_executed_or_retained(self):
        HostileHeaderString.calls = 0
        response = _response(_listing())
        response.headers = RawHostileHeaders()
        opener = FakeOpener(response)
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

        with self.assertRaises(SourcePausedError) as caught:
            source.list_candidates({}, 10)

        self.assertEqual(str(caught.exception), "reddit_schema")
        self.assert_safe_exception(caught.exception, "remote-secret")
        self.assertEqual(HostileHeaderString.calls, 0)
        self.assertTrue(response.closed)

    def test_redirect_is_rejected_without_followup_or_data_leak(self):
        opener = FakeOpener(
            _response(
                _listing(),
                final_url="https://www.reddit.com/r/python/new?leaked=oauth-token",
            )
        )
        source = RedditSource(_settings(), token_provider=lambda: "oauth-token", opener=opener)
        with self.assertRaises(SourcePausedError) as caught:
            source.list_candidates({}, 10)
        self.assertEqual(str(caught.exception), "reddit_redirect")
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn("oauth-token", repr(caught.exception))

    def test_non_json_and_malformed_json_pause_as_schema_errors(self):
        responses = (
            FakeResponse(b"<html>secret</html>", content_type="text/html"),
            FakeResponse(b'{"kind":"Listing","kind":"secret"}'),
            FakeResponse(b"not-json-secret"),
            FakeResponse(b"\xff"),
            FakeResponse(
                b'{"kind":"Listing","data":{"after":null,"children":[]},"score":NaN}'
            ),
            FakeResponse(
                b'{"kind":"Listing","data":{"after":null,"children":[]},"score":1e400}'
            ),
            FakeResponse(
                b'{"kind":"Listing","data":{"after":null,"children":[]},"score":'
                + (b"9" * 1000)
                + b"}"
            ),
        )
        for response in responses:
            with self.subTest(body=response.body[:20]):
                opener = FakeOpener(response)
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaises(SourcePausedError) as caught:
                    source.list_candidates({}, 10)
                self.assertEqual(str(caught.exception), "reddit_schema")
                self.assertNotIn("secret", repr(caught.exception))
                self.assert_safe_exception(caught.exception, "secret", "not-json")
                self.assertTrue(response.closed)

    def test_oversized_response_is_bounded_and_closed(self):
        response = FakeResponse(b"x" * (MAX_REDDIT_RESPONSE_BYTES + 1))
        opener = FakeOpener(response)
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

        with self.assertRaisesRegex(SourcePausedError, "reddit_response_too_large"):
            source.list_candidates({}, 10)

        self.assertTrue(response.closed)
        self.assertTrue(all(0 < size <= 65536 for size in response.read_sizes))
        self.assertLessEqual(sum(response.read_sizes), MAX_REDDIT_RESPONSE_BYTES + 65536)

    def test_listing_keeps_only_minimum_candidate_fields_and_omits_body(self):
        remote = _post(
            likes=True,
            distinguished="moderator",
            mod_note="private-note",
            user_reports=[["private report", 1]],
            selftext="authorized body must not survive candidate discovery",
        )
        opener = FakeOpener(_response(_listing(remote, after=None)))
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

        page = source.list_candidates({}, 10)

        self.assertEqual(len(page.items), 1)
        candidate = page.items[0]
        self.assertEqual(
            candidate.__dict__,
            {
                "source_id": "reddit-python",
                "platform": "reddit",
                "community_key": "python",
                "external_content_id": "t3_abc123",
                "author_key": "approved_author",
                "published_at": "2023-11-14T22:13:20Z",
                "canonical_url": "https://www.reddit.com/r/python/comments/abc123/synthetic_title/",
                "text": "",
            },
        )
        self.assertEqual(page.next_cursor, ())

    def test_listing_rejects_invalid_child_fields_and_excess_children(self):
        bad_children = (
            {"kind": "t5", "data": _post()["data"]},
            _post(name="abc123"),
            _post(author="[deleted]"),
            _post(created_utc=math.nan),
            _post(created_utc=10 ** 10_000),
            _post(permalink="https://evil.example/steal"),
            _post(permalink="/r/python/comments/abc123/x/?secret=yes"),
            _post(permalink="/r/python/comments/abc123/a bad slug/"),
            _post(subreddit="other"),
        )
        payloads = [_listing(child) for child in bad_children]
        payloads.append(_listing(*(_post(name="t3_x%03d" % n) for n in range(101))))
        for payload in payloads:
            with self.subTest(payload=str(payload)[:80]):
                opener = FakeOpener(_response(payload))
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaisesRegex(SourcePausedError, "reddit_schema"):
                    source.list_candidates({}, 100)

    def test_listing_rejects_invalid_after_cursor_from_server(self):
        for after in ("", "t1_comment", "https://evil.example", 123, "t3_" + "x" * 200):
            with self.subTest(after=after):
                opener = FakeOpener(_response(_listing(after=after)))
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaisesRegex(SourcePausedError, "reddit_schema"):
                    source.list_candidates({}, 10)

    def test_fetch_post_uses_oauth_info_endpoint_and_returns_authorized_text(self):
        candidate = SourceCandidate(
            source_id="reddit-python",
            platform="reddit",
            community_key="python",
            external_content_id="t3_abc123",
            author_key="approved_author",
            published_at="2023-11-14T22:13:20Z",
            canonical_url="https://www.reddit.com/r/python/comments/abc123/synthetic_title/",
        )
        response = _response(_listing(_post(), after=None))
        opener = FakeOpener(response)
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

        content = source.fetch_content(candidate)

        self.assertEqual(
            opener.request.full_url,
            "https://oauth.reddit.com/api/info?raw_json=1&id=t3_abc123",
        )
        self.assertIs(content.candidate, candidate)
        self.assertEqual(content.text, "Synthetic title\n\nSynthetic body")
        self.assertTrue(response.closed)

    def test_fetch_comment_extracts_body(self):
        candidate = SourceCandidate(
            source_id="reddit-python",
            platform="reddit",
            community_key="python",
            external_content_id="t1_def456",
            author_key="approved_author",
            published_at="2023-11-14T22:13:21Z",
            canonical_url="https://www.reddit.com/r/python/comments/abc123/synthetic_title/def456/",
        )
        opener = FakeOpener(_response(_listing(_comment(), after=None)))
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)

        content = source.fetch_content(candidate)

        self.assertEqual(content.text, "Synthetic comment")

    def test_fetch_rejects_cross_source_candidate_before_token_or_network(self):
        base = dict(
            source_id="reddit-python",
            platform="reddit",
            community_key="python",
            external_content_id="t3_abc123",
            author_key="approved_author",
            published_at="2023-11-14T22:13:20Z",
            canonical_url="https://www.reddit.com/r/python/comments/abc123/synthetic_title/",
        )
        invalid = []
        for key, value in (
            ("source_id", "reddit-other"),
            ("platform", "tieba"),
            ("community_key", "other"),
            ("external_content_id", "invalid"),
            ("canonical_url", "https://evil.example/path"),
            ("text", "body must not cross the candidate boundary"),
            ("author_key", "[deleted]"),
            ("published_at", ""),
            ("published_at", "2023-11-14T22:13:20+00:00"),
            ("published_at", "2023-11-14T22:13:20.000Z"),
            ("published_at", "2023-02-30T22:13:20Z"),
        ):
            values = dict(base)
            values[key] = value
            invalid.append(SourceCandidate(**values))
        calls = []
        opener = FakeOpener()
        source = RedditSource(
            _settings(), token_provider=lambda: calls.append("token") or "token", opener=opener
        )

        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(SourcePausedError, "reddit_candidate"):
                    source.fetch_content(candidate)

        self.assertEqual(calls, [])
        self.assertEqual(opener.requests, [])

    def test_fetch_rejects_mismatched_or_ambiguous_remote_identity(self):
        candidate = SourceCandidate(
            source_id="reddit-python",
            platform="reddit",
            community_key="python",
            external_content_id="t3_abc123",
            author_key="approved_author",
            published_at="2023-11-14T22:13:20Z",
            canonical_url="https://www.reddit.com/r/python/comments/abc123/synthetic_title/",
        )
        payloads = (
            _listing(_post(name="t3_other"), after=None),
            _listing(_post(author="different_author"), after=None),
            _listing(_post(created_utc=1_700_000_001), after=None),
            _listing(_post(), _post(), after=None),
            _listing(after=None),
        )
        # _listing() intentionally provides its default child; use an explicit
        # empty listing for the final malformed fetch case.
        payloads = payloads[:-1] + (
            {"kind": "Listing", "data": {"after": None, "children": []}},
        )
        for payload in payloads:
            with self.subTest(payload=str(payload)[:100]):
                opener = FakeOpener(_response(payload))
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaisesRegex(SourcePausedError, "reddit_schema"):
                    source.fetch_content(candidate)

    def test_fetch_rejects_empty_deleted_or_oversized_content(self):
        candidate = SourceCandidate(
            source_id="reddit-python",
            platform="reddit",
            community_key="python",
            external_content_id="t3_abc123",
            author_key="approved_author",
            published_at="2023-11-14T22:13:20Z",
            canonical_url="https://www.reddit.com/r/python/comments/abc123/synthetic_title/",
        )
        posts = (
            _post(title="", selftext=""),
            _post(title="[deleted]", selftext="[removed]"),
            _post(title="x" * (32 * 1024 + 1), selftext=""),
        )
        for post in posts:
            with self.subTest(title=post["data"]["title"][:20]):
                opener = FakeOpener(_response(_listing(post, after=None)))
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaisesRegex(SourcePausedError, "reddit_schema"):
                    source.fetch_content(candidate)

    def test_fetch_rejects_surrogates_and_dangerous_controls_but_allows_newline_and_tab(self):
        candidate = SourceCandidate(
            source_id="reddit-python",
            platform="reddit",
            community_key="python",
            external_content_id="t3_abc123",
            author_key="approved_author",
            published_at="2023-11-14T22:13:20Z",
            canonical_url="https://www.reddit.com/r/python/comments/abc123/synthetic_title/",
        )
        for body in ("bad\ud800", "bad\x01", "bad\u200b"):
            with self.subTest(body=ascii(body)):
                payload = _listing(_post(selftext=body), after=None)
                response = FakeResponse(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
                opener = FakeOpener(response)
                source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
                with self.assertRaisesRegex(SourcePausedError, "reddit_schema"):
                    source.fetch_content(candidate)

        payload = _listing(_post(selftext="line one\nline two\tindented"), after=None)
        opener = FakeOpener(_response(payload))
        source = RedditSource(_settings(), token_provider=lambda: "token", opener=opener)
        content = source.fetch_content(candidate)
        self.assertEqual(
            content.text,
            "Synthetic title\n\nline one\nline two\tindented",
        )

    def test_constructor_rejects_wrong_platform_invalid_community_and_opener(self):
        with self.assertRaises(ValueError):
            RedditSource(_settings(platform="tieba"), token_provider=lambda: "x", opener=FakeOpener())
        with self.assertRaises(ValueError):
            RedditSource(_settings(community="bad/name"), token_provider=lambda: "x", opener=FakeOpener())
        with self.assertRaises(ValueError):
            RedditSource(_settings(), token_provider=lambda: "x", opener=object())
        with self.assertRaises(ValueError):
            RedditSource(_settings(), token_provider="token", opener=FakeOpener())
        with self.assertRaises(ValueError):
            RedditSource(
                _settings(),
                token_provider=lambda: "x",
                opener=FakeOpener(),
                timeout_seconds=10 ** 10_000,
            )


if __name__ == "__main__":
    unittest.main()
