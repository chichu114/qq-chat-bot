from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.robotparser import RobotFileParser


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_openclaw_bridge.domain.community_style import SourceCandidate
from qq_openclaw_bridge.runtime.community_style_config import (
    AuthorizationSettings,
    SourceSettings,
)
from qq_openclaw_bridge.runtime.tieba_source import (
    MAX_TIEBA_RESPONSE_BYTES,
    TiebaAccessPaused,
    TiebaMarkupError,
    TiebaSource,
)


NOW = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
ROBOTS = b"User-agent: qq-community-style-agent\nAllow: /f\nAllow: /p/\n"


def _settings(**changes):
    values = {
        "id": "tieba-synthetic",
        "platform": "tieba",
        "community": "synthetic board/测试",
        "profile": "tieba-synthetic",
        "schedule_seconds": 21600,
        "authorizations": (
            AuthorizationSettings(
                subject_type="author",
                subject_key="synthetic_alpha",
                scope="all_posts_until_revoke",
                evidence_ref="consent-synthetic-001",
                granted_at="2026-09-19T00:00:00Z",
            ),
        ),
    }
    values.update(changes)
    return SourceSettings(**values)


def _candidate(**changes):
    values = {
        "source_id": "tieba-synthetic",
        "platform": "tieba",
        "community_key": "synthetic board/测试",
        "external_content_id": "100001",
        "author_key": "synthetic_alpha",
        "published_at": "2026-09-18T01:02:03Z",
        "canonical_url": "https://tieba.baidu.com/p/100001",
        "text": "",
    }
    values.update(changes)
    return SourceCandidate(**values)


class FakeHeaders(dict):
    def get_content_type(self):
        return self.get("Content-Type", "").split(";", 1)[0].strip().lower()


class FakeResponse:
    def __init__(
        self,
        body=b"",
        *,
        status=200,
        content_type="text/html; charset=utf-8",
        final_url=None,
        headers=None,
    ):
        self.body = body
        self.status = status
        self.headers = FakeHeaders({"Content-Type": content_type})
        if headers:
            self.headers.update(headers)
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


class HostileResponse(FakeResponse):
    def geturl(self):
        raise RuntimeError("remote-secret-final-url")


class SlowChunksResponse(FakeResponse):
    def __init__(self, *, chunks=100, delay=0.005, **kwargs):
        super().__init__(b"x" * chunks, **kwargs)
        self.delay = delay

    def read(self, size=-1):
        del size
        time.sleep(self.delay)
        if self.offset >= len(self.body):
            return b""
        self.offset += 1
        return b"x"


class BlockingReadResponse(FakeResponse):
    def __init__(self, gate, **kwargs):
        super().__init__(b"", **kwargs)
        self.gate = gate

    def read(self, size=-1):
        del size
        self.gate.wait(0.25)
        return b""


class BlockingCloseResponse(FakeResponse):
    def __init__(self, gate, body=b"", **kwargs):
        super().__init__(body, **kwargs)
        self.gate = gate
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.gate.wait(0.25)
        self.closed = True


class InterruptReadResponse(FakeResponse):
    def __init__(self, signal, **kwargs):
        super().__init__(b"", **kwargs)
        self.signal = signal

    def read(self, size=-1):
        del size
        raise self.signal


class InterruptCloseResponse(FakeResponse):
    def __init__(self, signal, body, **kwargs):
        super().__init__(body, **kwargs)
        self.signal = signal

    def close(self):
        self.closed = True
        raise self.signal


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.timeouts = []

    def queue(self, *outcomes):
        self.outcomes.extend(outcomes)

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


class BlockingOpener:
    def __init__(self, gate):
        self.gate = gate
        self.requests = []
        self.started = 0
        self._lock = threading.Lock()

    def open(self, request, timeout):
        del timeout
        self.requests.append(request)
        with self._lock:
            self.started += 1
        self.gate.wait(0.25)
        raise URLError("remote-secret-blocked")


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class SpyRobotParser:
    def __init__(self):
        self.lines = None
        self.calls = []

    def parse(self, lines):
        self.lines = list(lines)

    def can_fetch(self, user_agent, url):
        self.calls.append((user_agent, url))
        if self.lines is None:
            raise AssertionError("robots used before parse")
        return not any(line.strip().casefold() == "disallow: /" for line in self.lines)


def _response(body, **kwargs):
    return FakeResponse(body, **kwargs)


def _robots_response(body=ROBOTS, **kwargs):
    return FakeResponse(body, content_type="text/plain; charset=utf-8", **kwargs)


def _http_error(code, headers=None):
    return HTTPError(
        "https://tieba.baidu.com/redacted",
        code,
        "remote-secret-reason",
        FakeHeaders({} if headers is None else headers),
        io.BytesIO(b"remote-secret-body"),
    )


class TiebaSourceTests(unittest.TestCase):
    def fixture(self, name):
        return (ROOT / "tests" / "fixtures" / "tieba" / name).read_bytes()

    def make_source(
        self,
        *outcomes,
        clock=None,
        deadline_clock=None,
        parser=None,
        parser_factory=None,
        settings=None,
        timeout_seconds=20.0,
        opener=None,
    ):
        opener = FakeOpener(*outcomes) if opener is None else opener
        factory = parser_factory
        if factory is None:
            factory = (lambda: SpyRobotParser()) if parser is None else (lambda: parser)
        kwargs = {}
        if deadline_clock is not None:
            kwargs["deadline_clock"] = deadline_clock
        source = TiebaSource(
            _settings() if settings is None else settings,
            opener=opener,
            clock=FakeClock() if clock is None else clock,
            robot_parser_factory=factory,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )
        return source, opener

    def assert_safe_exception(self, exception, *forbidden):
        rendered = "%s:%s" % (type(exception).__name__, str(exception))
        for value in forbidden:
            self.assertNotIn(value, rendered)
        self.assertIsNone(exception.__context__)
        self.assertIsNone(exception.__cause__)

    def test_listing_checks_robots_then_encodes_board_and_offset(self):
        source, opener = self.make_source(
            _robots_response(headers={"ETag": '"r1"', "Last-Modified": "Sat, 19 Sep 2026 00:00:00 GMT"}),
            _response(self.fixture("listing.html")),
        )

        page = source.list_candidates({"pn": "0"}, 20)

        self.assertEqual(opener.requests[0].full_url, "https://tieba.baidu.com/robots.txt")
        self.assertEqual(
            opener.requests[1].full_url,
            "https://tieba.baidu.com/f?kw=synthetic+board%2F%E6%B5%8B%E8%AF%95&ie=utf-8&pn=0",
        )
        self.assertIn("qq-community-style-agent", opener.requests[1].headers["User-agent"])
        self.assertEqual(len(page.items), 2)
        self.assertEqual(page.next_cursor, (("pn", "50"), ("page", "0")))
        self.assertEqual(page.items[0], _candidate())
        self.assertEqual(page.items[0].text, "")

    def test_listing_limit_is_bounded_and_slices_candidates(self):
        source, _ = self.make_source(_robots_response(), _response(self.fixture("listing.html")))
        page = source.list_candidates({}, 1)
        self.assertEqual([item.external_content_id for item in page.items], ["100001"])
        self.assertEqual(page.next_cursor, (("pn", "0"), ("page", "1")))

    def test_listing_intra_page_cursor_does_not_skip_second_candidate(self):
        source, _ = self.make_source(
            _robots_response(),
            _response(self.fixture("listing.html")),
            _response(self.fixture("listing.html")),
        )
        first = source.list_candidates({}, 1)
        second = source.list_candidates(dict(first.next_cursor), 1)

        self.assertEqual([item.external_content_id for item in first.items], ["100001"])
        self.assertEqual([item.external_content_id for item in second.items], ["100002"])
        self.assertEqual(second.next_cursor, (("pn", "50"), ("page", "0")))

    def test_listing_falls_back_to_common_author_and_time_elements(self):
        body = self.fixture("listing.html")
        body = body.replace(b',"author_name":"synthetic_alpha","create_time":"2026-09-18 01:02:03"', b"")
        source, _ = self.make_source(_robots_response(), _response(body))

        page = source.list_candidates({}, 1)

        self.assertEqual(page.items[0].author_key, "synthetic_alpha")
        self.assertEqual(page.items[0].published_at, "2026-09-18T01:02:03Z")

    def test_listing_rejects_nested_thread_item_metadata_stitching(self):
        nested = b"""<!doctype html><html><body><ul id="thread_list">
        <li class="j_thread_list" data-tid="100001" data-field='{"id":100001}'>
          <div><li class="j_thread_list" data-tid="900001" data-field='{"id":900001,"author_name":"unauthorized_nested","create_time":"2026-09-18 09:00:00"}'>
            <a href="/p/100001">Wrong owner link</a>
            <span class="tb_icon_author" data-field='{"user_name":"synthetic_alpha"}'></span>
            <span class="is_show_create_time" title="2026-09-18 01:02:03"></span>
          </li></div>
        </li></ul></body></html>"""
        source, _ = self.make_source(_robots_response(), _response(nested))

        with self.assertRaisesRegex(TiebaMarkupError, "invalid_markup") as caught:
            source.list_candidates({}, 20)

        self.assert_safe_exception(caught.exception, "unauthorized_nested")

    def test_invalid_cursor_limit_or_community_fails_before_network(self):
        invalid = [
            ({"pn": "-1"}, 10),
            ({"pn": "01"}, 10),
            ({"page": "1"}, 10),
            ({"pn": "0", "page": "01"}, 10),
            ({"pn": "0", "page": "100"}, 10),
            ({"pn": "0", "page": "0", "x": "0"}, 10),
            ({"offset": "0"}, 10),
            ({"index": "0"}, 10),
            ({1: "0"}, 10),
            ({}, 0),
            ({}, True),
        ]
        for cursor, limit in invalid:
            with self.subTest(cursor=cursor, limit=limit):
                source, opener = self.make_source()
                with self.assertRaises(TiebaMarkupError):
                    source.list_candidates(cursor, limit)
                self.assertEqual(opener.requests, [])
        for community in (" bad", "bad\nboard", "x" * 129):
            with self.subTest(community=repr(community)):
                with self.assertRaises(ValueError):
                    self.make_source(settings=_settings(community=community))

    def test_robots_disallow_pauses_without_fetching_listing(self):
        source, opener = self.make_source(
            _robots_response(b"User-agent: *\nDisallow: /\n"),
        )
        with self.assertRaisesRegex(TiebaAccessPaused, "robots_disallow"):
            source.list_candidates({}, 20)
        self.assertEqual(len(opener.requests), 1)

    def test_robots_unavailable_without_cache_pauses_closed(self):
        for outcome in (URLError("remote-secret"), _http_error(503), _response(b"", status=500)):
            with self.subTest(outcome=type(outcome).__name__):
                source, opener = self.make_source(outcome)
                with self.assertRaisesRegex(TiebaAccessPaused, "robots_unavailable") as caught:
                    source.list_candidates({}, 20)
                self.assert_safe_exception(caught.exception, "remote-secret")
                self.assertEqual(len(opener.requests), 1)

    def test_robots_requires_an_applicable_nonempty_group_with_a_rule(self):
        invalid_policies = (
            b"User-agent:\nDisallow: /\n",
            b"User-agent: unrelated-bot\nAllow: /\n",
            b"User-agent: *\n\nUser-agent: unrelated-bot\nDisallow: /\n",
        )
        for policy in invalid_policies:
            with self.subTest(policy=policy):
                source, opener = self.make_source(
                    _robots_response(policy),
                    parser_factory=RobotFileParser,
                )
                with self.assertRaisesRegex(TiebaAccessPaused, "robots_unavailable"):
                    source.list_candidates({}, 20)
                self.assertEqual(len(opener.requests), 1)

    def test_real_robot_file_parser_allows_explicit_wildcard_policy(self):
        source, _ = self.make_source(
            _robots_response(b"User-agent: *\nAllow: /f\nDisallow: /private\n"),
            _response(self.fixture("listing.html")),
            parser_factory=RobotFileParser,
        )

        page = source.list_candidates({}, 20)

        self.assertEqual(len(page.items), 2)

    def test_valid_robots_cache_is_used_for_each_url_without_refetch(self):
        parser = SpyRobotParser()
        source, opener = self.make_source(
            _robots_response(),
            _response(self.fixture("listing.html")),
            _response(self.fixture("thread.html")),
            parser=parser,
        )

        page = source.list_candidates({}, 20)
        content = source.fetch_content(page.items[0])

        self.assertEqual(len(opener.requests), 3)
        self.assertEqual([url for _, url in parser.calls], [
            "https://tieba.baidu.com/f?kw=synthetic+board%2F%E6%B5%8B%E8%AF%95&ie=utf-8&pn=0",
            "https://tieba.baidu.com/p/100001",
        ])
        self.assertIn("A synthetic opening.", content.text)

    def test_expired_robots_cache_uses_conditional_headers_and_304_renews(self):
        clock = FakeClock()
        source, opener = self.make_source(
            _robots_response(headers={"ETag": '"r1"', "Last-Modified": "Sat, 19 Sep 2026 00:00:00 GMT"}),
            _response(self.fixture("listing.html")),
            _robots_response(status=304),
            _response(self.fixture("listing.html")),
            clock=clock,
        )
        source.list_candidates({}, 20)
        clock.advance(hours=24, seconds=1)
        source.list_candidates({}, 20)

        refresh = opener.requests[2]
        self.assertEqual(refresh.headers["If-none-match"], '"r1"')
        self.assertEqual(refresh.headers["If-modified-since"], "Sat, 19 Sep 2026 00:00:00 GMT")

    def test_robots_304_redirect_is_not_accepted_as_cache_revalidation(self):
        clock = FakeClock()
        source, _ = self.make_source(
            _robots_response(),
            _response(self.fixture("listing.html")),
            _robots_response(status=304, final_url="https://evil.example/robots.txt"),
            clock=clock,
        )
        source.list_candidates({}, 20)
        clock.advance(hours=24, seconds=1)

        with self.assertRaisesRegex(TiebaAccessPaused, "robots_unavailable"):
            source.list_candidates({}, 20)

    def test_expired_cache_refresh_failure_is_not_treated_as_permission(self):
        clock = FakeClock()
        source, opener = self.make_source(
            _robots_response(),
            _response(self.fixture("listing.html")),
            URLError("remote-secret"),
            clock=clock,
        )
        source.list_candidates({}, 20)
        clock.advance(hours=24, microseconds=1)

        with self.assertRaisesRegex(TiebaAccessPaused, "robots_unavailable") as caught:
            source.list_candidates({}, 20)
        self.assert_safe_exception(caught.exception, "remote-secret")
        self.assertEqual(len(opener.requests), 3)

    def test_captcha_or_login_wall_pauses_instead_of_guessing_markup(self):
        for body, reason in (
            (self.fixture("captcha.html"), "captcha"),
            (b"<html><title>Log&#105;n required</title><form id='log&#105;n'></form></html>", "login_required"),
            ("<html><body><div id='captcha-wall'>&#39564;&#35777;&#30721;</div></body></html>".encode(), "captcha"),
        ):
            with self.subTest(reason=reason):
                source, _ = self.make_source(_robots_response(), _response(body))
                with self.assertRaisesRegex(TiebaAccessPaused, reason):
                    source.list_candidates({}, 20)

    def test_access_wall_title_joins_inline_text_but_ignores_normal_page_mentions(self):
        walls = (
            (b"<html><head><title>CAP<span>T</span>CHA required</title></head></html>", "captcha"),
            ("<html><head><title>请<span>登</span>录贴吧</title></head></html>".encode(), "login_required"),
            (b"<html><body><form id='passport-login'>Synthetic wall</form></body></html>", "login_required"),
            (b"<html><body><div id='captcha'>Synthetic wall</div></body></html>", "captcha"),
            (b"<html><body><main id='verify-human'>Synthetic wall</main></body></html>", "captcha"),
            (b"<html><body><div class='human_verification'>Synthetic wall</div></body></html>", "captcha"),
            (b"<html><body><form role='verification-challenge'>Synthetic wall</form></body></html>", "captcha"),
            (b"<html><body><section role='challenge'>Synthetic wall</section></body></html>", "captcha"),
            ("<html><body><div id='ｖｅｒｉｆｙ－ｈｕｍａｎ'>Synthetic wall</div></body></html>".encode(), "captcha"),
        )
        for body, reason in walls:
            with self.subTest(reason=reason, body=body):
                source, _ = self.make_source(_robots_response(), _response(body))
                with self.assertRaisesRegex(TiebaAccessPaused, reason):
                    source.list_candidates({}, 20)

        normal = self.fixture("listing.html").replace(
            b"<body>",
            "<body><button id='login-button'>Login</button><nav><button class='captcha-button' role='verify-human-button'>Verify</button></nav><div class='coding-challenge'>Ordinary challenge</div><p>正文讨论验证码、CAPTCHA 和 human verification。</p>".encode(),
        )
        source, _ = self.make_source(_robots_response(), _response(normal))
        page = source.list_candidates({}, 20)
        self.assertEqual(len(page.items), 2)

    def test_nested_access_walls_pause_listing_and_thread_content(self):
        walls = (
            (b"<main><form id='login'><h1>Login required</h1></form></main>", "login_required"),
            (b"<section><form class='captcha'><p>Verify human</p></form></section>", "captcha"),
            (b"<main><section><div><form id='passport'>Synthetic wall</form></div></section></main>", "login_required"),
            (b"<main><section><div id='verify-human'>Synthetic wall</div></section></main>", "captcha"),
            (b"<main><section><div class='human_verification'>Synthetic wall</div></section></main>", "captcha"),
            (b"<main><section role='verification-challenge'>Synthetic wall</section></main>", "captcha"),
            (b"<main><section><form id='captcha'/></section></main>", "captcha"),
            (b"<header><a id='login-button'>Login</a></header><main><form id='login'></form></main>", "login_required"),
            (b"<input type='button' role='button'><main><form id='login'></form></main>", "login_required"),
            (b"<nav/><main><section role='verification-challenge'></section></main>", "captcha"),
        )
        for fixture in ("listing.html", "thread.html"):
            for wall, reason in walls:
                with self.subTest(fixture=fixture, wall=wall):
                    body = self.fixture(fixture).replace(b"<body>", b"<body>" + wall, 1)
                    source, _ = self.make_source(_robots_response(), _response(body))
                    with self.assertRaisesRegex(TiebaAccessPaused, "^" + reason + "$"):
                        if fixture == "listing.html":
                            source.list_candidates({}, 20)
                        else:
                            source.fetch_content(_candidate())

    def test_explicit_wall_markers_are_not_cancelled_by_action_or_trigger_tokens(self):
        walls = (
            (b"<div class='captcha action'>Synthetic wall</div>", "captcha"),
            (b"<form class='passport login-action'>Synthetic wall</form>", "login_required"),
            (b"<section role='verification-challenge-trigger'>Synthetic wall</section>", "captcha"),
            (b"<main id='verify-human-action'>Synthetic wall</main>", "captcha"),
            (b"<div class='human_verification trigger'>Synthetic wall</div>", "captcha"),
            (b"<form id='login-trigger'>Synthetic wall</form>", "login_required"),
            (b"<form class='captcha action'/>", "captcha"),
        )
        for fixture in ("listing.html", "thread.html"):
            for wall, reason in walls:
                with self.subTest(fixture=fixture, wall=wall):
                    body = self.fixture(fixture).replace(b"<body>", b"<body>" + wall, 1)
                    source, _ = self.make_source(_robots_response(), _response(body))
                    with self.assertRaisesRegex(TiebaAccessPaused, "^" + reason + "$"):
                        if fixture == "listing.html":
                            source.list_candidates({}, 20)
                        else:
                            source.fetch_content(_candidate())

    def test_navigation_and_control_subtrees_do_not_become_access_walls(self):
        controls = (
            b"<header><form id='login'><button class='login-button'>Login</button></form></header>",
            b"<nav><section><div class='captcha'><a class='login-link'>Login</a></div></section></nav>",
            b"<menu><li><form role='verification-challenge'><button>Verify</button></form></li></menu>",
            b"<button class='login-button'><div id='passport'>Login</div></button>",
            b"<a class='login-link'><section role='verification-challenge'>Login</section></a>",
            b"<div role='navigation'><form id='login'>Login</form></div>",
            b"<section role='menu'><div class='captcha'>Verify</div></section>",
            b"<div role='button' id='login'><section class='captcha'>Login</section></div>",
            b"<div role='link' class='captcha'><form id='passport'>Login</form></div>",
            b"<header/><main><a id='login-button'/><button class='captcha-button'/></main>",
        )
        for fixture in ("listing.html", "thread.html"):
            for control in controls:
                with self.subTest(fixture=fixture, control=control):
                    body = self.fixture(fixture).replace(b"<body>", b"<body>" + control, 1)
                    source, _ = self.make_source(_robots_response(), _response(body))
                    if fixture == "listing.html":
                        page = source.list_candidates({}, 20)
                        self.assertEqual([item.external_content_id for item in page.items], ["100001", "100002"])
                    else:
                        content = source.fetch_content(_candidate())
                        self.assertEqual(content.text, "A synthetic opening.\nOnly invented fixture words. No copied post.")

    def test_thread_discussing_access_walls_is_not_itself_an_access_wall(self):
        discussion = "正文讨论验证码、CAPTCHA、human verification 和 login required。"
        body = self.fixture("thread.html").replace(b"A synthetic opening.", discussion.encode(), 1)
        source, _ = self.make_source(_robots_response(), _response(body))

        content = source.fetch_content(_candidate())

        self.assertEqual(content.text, discussion + "\nOnly invented fixture words. No copied post.")

    def test_access_wall_context_does_not_bypass_markup_or_resource_bounds(self):
        invalid = (
            b"<nav><section><form id='login'></section></form></nav>",
            b"<main><section><form id='login'></form></main></section>",
            b"<nav>" + b"<br>" * 20001 + b"</nav>",
            b"<nav><button>" + b"x" * (32 * 1024 + 2) + b"</button></nav>",
            b"<nav><button id='" + b"x" * 1025 + b"'>Login</button></nav>",
        )
        for index, fragment in enumerate(invalid):
            with self.subTest(index=index):
                body = self.fixture("listing.html").replace(b"<body>", b"<body>" + fragment, 1)
                source, _ = self.make_source(_robots_response(), _response(body))
                with self.assertRaisesRegex(TiebaMarkupError, "^invalid_markup$"):
                    source.list_candidates({}, 20)

    def test_403_and_429_pause_without_retry_or_body_parsing(self):
        for code, reason in ((403, "http_403"), (429, "http_429")):
            with self.subTest(code=code):
                source, opener = self.make_source(_robots_response(), _http_error(code))
                with self.assertRaisesRegex(TiebaAccessPaused, reason) as caught:
                    source.list_candidates({}, 20)
                self.assert_safe_exception(caught.exception, "remote-secret")
                self.assertEqual(len(opener.requests), 2)

    def test_unknown_markup_and_duplicate_or_missing_ids_are_rejected(self):
        bad_pages = [
            b"<html><body><article class='thread-item' data-thread-id='100001'></article></body></html>",
            self.fixture("listing.html").replace(b'data-tid="100002"', b'data-tid="100001"'),
            self.fixture("listing.html").replace(b'id="thread_list"', b'id="unknown_thread_list"'),
        ]
        for body in bad_pages:
            with self.subTest(body=body[:30]):
                source, _ = self.make_source(_robots_response(), _response(body))
                with self.assertRaisesRegex(TiebaMarkupError, "invalid_markup"):
                    source.list_candidates({}, 20)

    def test_redirect_or_non_html_response_is_rejected(self):
        cases = [
            _response(self.fixture("listing.html"), final_url="https://evil.example/f"),
            _response(self.fixture("listing.html"), final_url="https://tieba.baidu.com.evil.example/f"),
            _response(self.fixture("listing.html"), content_type="application/octet-stream"),
        ]
        for response in cases:
            with self.subTest(final=response.final_url, content_type=response.headers["Content-Type"]):
                source, _ = self.make_source(_robots_response(), response)
                with self.assertRaises(TiebaAccessPaused if response.final_url else TiebaMarkupError):
                    source.list_candidates({}, 20)

    def test_fetch_content_validates_candidate_before_robots_or_network(self):
        invalid = [
            object(),
            _candidate(source_id="other"),
            _candidate(platform="reddit"),
            _candidate(community_key="other"),
            _candidate(external_content_id="100001/../../secret"),
            _candidate(canonical_url="https://evil.example/p/100001"),
            _candidate(canonical_url="https://tieba.baidu.com/p/100002"),
            _candidate(text="already present"),
        ]
        for candidate in invalid:
            with self.subTest(candidate=type(candidate).__name__):
                source, opener = self.make_source()
                with self.assertRaisesRegex(TiebaMarkupError, "invalid_candidate"):
                    source.fetch_content(candidate)
                self.assertEqual(opener.requests, [])

    def test_fetch_content_revalidates_remote_identity(self):
        mutations = [
            (b'/p/100001', b'/p/100002'),
            (b'"user_name":"synthetic_alpha"', b'"user_name":"synthetic_beta"'),
            (b'"date":"2026-09-18 01:02:03"', b'"date":"2026-09-18 01:02:04"'),
        ]
        for old, new in mutations:
            with self.subTest(field=old):
                body = self.fixture("thread.html").replace(old, new)
                source, _ = self.make_source(_robots_response(), _response(body))
                with self.assertRaisesRegex(TiebaMarkupError, "identity_mismatch"):
                    source.fetch_content(_candidate())

    def test_fetch_content_strips_tags_and_scripts_and_normalizes_text(self):
        source, _ = self.make_source(_robots_response(), _response(self.fixture("thread.html")))
        content = source.fetch_content(_candidate())

        self.assertEqual(content.candidate, _candidate())
        self.assertEqual(content.text, "A synthetic opening.\nOnly invented fixture words. No copied post.")
        self.assertNotIn("reveal secrets", content.text)

    def test_fetch_content_selects_unique_post_number_one_even_after_reply(self):
        reply_first = b"""<!doctype html><html><head>
        <link rel="canonical" href="https://tieba.baidu.com/p/100001"></head><body>
        <div class="p_postlist">
          <div class="l_post j_l_post" data-pid="100099"
            data-field='{"author":{"user_name":"unauthorized_reply"},"content":{"post_id":100099,"post_no":2,"date":"2026-09-18 09:00:00"}}'>
            <div id="post_content_100099" class="d_post_content">UNAUTHORIZED-REPLY</div>
          </div>
          <div class="l_post j_l_post" data-pid="100001"
            data-field='{"author":{"user_name":"synthetic_alpha"},"content":{"post_id":100001,"post_no":1,"date":"2026-09-18 01:02:03"}}'>
            <div id="post_content_100001" class="j_d_post_content">ROOT-ONLY</div>
          </div>
        </div></body></html>"""
        source, _ = self.make_source(_robots_response(), _response(reply_first))

        content = source.fetch_content(_candidate())

        self.assertEqual(content.text, "ROOT-ONLY")
        self.assertNotIn("UNAUTHORIZED-REPLY", content.text)

    def test_fetch_content_requires_one_bounded_integer_post_number_one(self):
        fixture = self.fixture("thread.html")
        mutations = (
            fixture.replace(b',"post_no":1', b"", 1),
            fixture.replace(b'"post_no":1', b'"post_no":"1"', 1),
            fixture.replace(b'"post_no":1', b'"post_no":true', 1),
            fixture.replace(b'"post_no":1', b'"post_no":0', 1),
            fixture.replace(b'"post_no":1', b'"post_no":1000001', 1),
            fixture.replace(b'"post_no":1', b'"post_no":2', 1),
            fixture.replace(b'"post_no":2', b'"post_no":1', 1),
        )
        for body in mutations:
            with self.subTest(fragment=body[body.find(b'post_no') - 2:body.find(b'post_no') + 24]):
                source, _ = self.make_source(_robots_response(), _response(body))
                with self.assertRaisesRegex(TiebaMarkupError, "invalid_markup"):
                    source.fetch_content(_candidate())

    def test_fetch_content_rejects_nested_post_body_owned_by_another_post(self):
        nested = b"""<!doctype html><html><head>
        <link rel="canonical" href="https://tieba.baidu.com/p/100001"></head><body>
        <div class="p_postlist"><div class="l_post j_l_post" data-pid="100001"
          data-field='{"author":{"user_name":"synthetic_alpha"},"content":{"post_id":100001,"post_no":1,"date":"2026-09-18 01:02:03"}}'>
          <section><div class="l_post j_l_post" data-pid="900001"
            data-field='{"author":{"user_name":"unauthorized_nested"},"content":{"post_id":900001,"post_no":2,"date":"2026-09-18 09:00:00"}}'>
            <div class="d_post_content j_d_post_content">UNAUTHORIZED-NESTED-SECRET</div>
          </div></section>
        </div></div></body></html>"""
        source, _ = self.make_source(_robots_response(), _response(nested))

        with self.assertRaisesRegex(TiebaMarkupError, "invalid_markup") as caught:
            source.fetch_content(_candidate())

        self.assert_safe_exception(caught.exception, "UNAUTHORIZED-NESTED-SECRET")

    def test_data_field_json_rejects_duplicates_nonfinite_huge_and_overdeep_values(self):
        original = b'"create_time":"2026-09-18 01:02:03"}'
        bad_suffixes = (
            b',"id":100001}',
            b',"score":1e400}',
            b',"huge":999999999999999999999999999999999999999999}',
            b',"deep":[[[[[[[0]]]]]]]}',
            b',"many":[' + b','.join([b'0'] * 65) + b']}',
        )
        for suffix in bad_suffixes:
            with self.subTest(suffix=suffix[:32]):
                listing = self.fixture("listing.html").replace(
                    original,
                    b'"create_time":"2026-09-18 01:02:03"' + suffix,
                    1,
                )
                source, _ = self.make_source(_robots_response(), _response(listing))
                with self.assertRaisesRegex(TiebaMarkupError, "invalid_markup") as caught:
                    source.list_candidates({}, 20)
                self.assert_safe_exception(caught.exception, suffix.decode("ascii"))

    def test_fetch_content_rejects_oversized_thread(self):
        source, _ = self.make_source(_robots_response(), _response(b"x" * (MAX_TIEBA_RESPONSE_BYTES + 1)))
        with self.assertRaisesRegex(TiebaMarkupError, "response_exceeded"):
            source.fetch_content(_candidate())

    def test_listing_rejects_dangerous_unicode_and_thread_rejects_control_text(self):
        listing = self.fixture("listing.html").replace(b"synthetic_alpha", "synthetic_\u202ealpha".encode())
        source, _ = self.make_source(_robots_response(), _response(listing))
        with self.assertRaisesRegex(TiebaMarkupError, "invalid_markup"):
            source.list_candidates({}, 20)

        thread = self.fixture("thread.html").replace(b"A synthetic opening.", b"A synthetic\x00 opening.")
        source, _ = self.make_source(_robots_response(), _response(thread))
        with self.assertRaisesRegex(TiebaMarkupError, "invalid_markup"):
            source.fetch_content(_candidate())

    def test_malicious_opener_and_response_errors_are_sanitized(self):
        cases = [
            RuntimeError("remote-secret-opener"),
            HostileResponse(self.fixture("listing.html")),
            FakeResponse(self.fixture("listing.html"), status="remote-secret-status"),
        ]
        for outcome in cases:
            with self.subTest(outcome=type(outcome).__name__):
                source, _ = self.make_source(_robots_response(), outcome)
                with self.assertRaises((TiebaAccessPaused, TiebaMarkupError)) as caught:
                    source.list_candidates({}, 20)
                self.assert_safe_exception(caught.exception, "remote-secret")

    def test_transport_propagates_control_flow_exceptions_and_recovers_slot(self):
        for signal_type in (KeyboardInterrupt, SystemExit):
            for phase in ("open", "read", "close"):
                with self.subTest(signal_type=signal_type.__name__, phase=phase):
                    signal = signal_type("control-flow-token")
                    interrupted_response = None
                    if phase == "open":
                        first = signal
                    elif phase == "read":
                        interrupted_response = InterruptReadResponse(
                            signal,
                            content_type="text/plain",
                        )
                        first = interrupted_response
                    else:
                        interrupted_response = InterruptCloseResponse(
                            signal,
                            ROBOTS,
                            content_type="text/plain",
                        )
                        first = interrupted_response
                    source, opener = self.make_source(
                        first,
                        _robots_response(),
                        _response(self.fixture("listing.html")),
                    )

                    try:
                        source.list_candidates({}, 20)
                    except BaseException as caught:
                        self.assertIs(caught, signal)
                    else:
                        self.fail("control-flow exception was swallowed")
                    if interrupted_response is not None:
                        self.assertTrue(interrupted_response.closed)

                    page = source.list_candidates({}, 20)
                    self.assertEqual(len(page.items), 2)
                    self.assertEqual(len(opener.requests), 3)

    def test_response_reads_are_bounded_and_responses_are_closed(self):
        robots = _robots_response()
        listing = _response(self.fixture("listing.html"))
        source, _ = self.make_source(robots, listing)
        source.list_candidates({}, 20)
        self.assertTrue(robots.closed)
        self.assertTrue(listing.closed)
        self.assertTrue(all(size > 0 and size <= 65536 for size in robots.read_sizes + listing.read_sizes))

    def test_total_deadline_stops_a_peer_sending_endless_small_chunks(self):
        response = SlowChunksResponse(content_type="text/plain; charset=utf-8")
        source, _ = self.make_source(response, timeout_seconds=0.03)

        started = time.monotonic()
        with self.assertRaisesRegex(TiebaAccessPaused, "robots_unavailable"):
            source.list_candidates({}, 20)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)

    def test_total_deadline_bounds_blocking_open_read_and_close(self):
        cases = []
        open_gate = threading.Event()
        cases.append((BlockingOpener(open_gate), open_gate, None))
        read_gate = threading.Event()
        cases.append((FakeOpener(BlockingReadResponse(read_gate, content_type="text/plain")), read_gate, None))
        close_gate = threading.Event()
        close_response = BlockingCloseResponse(close_gate, body=ROBOTS, content_type="text/plain")
        cases.append((FakeOpener(close_response), close_gate, close_response))
        for opener, gate, tracked_close in cases:
            outcome_name = (
                opener.outcomes[0].__class__.__name__
                if getattr(opener, "outcomes", None)
                else "open"
            )
            with self.subTest(opener=type(opener).__name__, response=outcome_name):
                source, _ = self.make_source(opener=opener, timeout_seconds=0.03)
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(TiebaAccessPaused, "robots_unavailable"):
                        source.list_candidates({}, 20)
                    self.assertLess(time.monotonic() - started, 0.15)
                finally:
                    gate.set()
                if tracked_close is not None:
                    for _ in range(20):
                        if tracked_close.closed:
                            break
                        time.sleep(0.005)
                    self.assertTrue(tracked_close.closed)
                    self.assertEqual(tracked_close.close_calls, 1)

    def test_repeated_transport_timeouts_have_a_process_worker_cap(self):
        gate = threading.Event()
        opener = BlockingOpener(gate)
        sources = [self.make_source(opener=opener, timeout_seconds=0.03)[0] for _ in range(8)]
        errors = []

        def invoke(source):
            try:
                source.list_candidates({}, 20)
            except TiebaAccessPaused as exc:
                errors.append(str(exc))

        callers = [threading.Thread(target=invoke, args=(source,)) for source in sources]
        started = time.monotonic()
        try:
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(0.2)
            self.assertTrue(all(not caller.is_alive() for caller in callers))
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertEqual(len(errors), 8)
            self.assertLessEqual(opener.started, 4)
        finally:
            gate.set()
            for caller in callers:
                caller.join(0.3)

    def test_clock_parser_and_factory_failures_pause_safely(self):
        bad_clocks = [lambda: "remote-secret-time", lambda: (_ for _ in ()).throw(RuntimeError("remote-secret-clock"))]
        for clock in bad_clocks:
            with self.subTest(clock=clock):
                source, opener = self.make_source(_robots_response(), clock=clock)
                with self.assertRaises(TiebaAccessPaused) as caught:
                    source.list_candidates({}, 20)
                self.assert_safe_exception(caught.exception, "remote-secret")
                self.assertEqual(opener.requests, [])

        source, opener = self.make_source(
            _robots_response(),
            deadline_clock=lambda: (_ for _ in ()).throw(RuntimeError("remote-secret-deadline")),
        )
        with self.assertRaisesRegex(TiebaAccessPaused, "robots_unavailable") as caught:
            source.list_candidates({}, 20)
        self.assert_safe_exception(caught.exception, "remote-secret")
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
