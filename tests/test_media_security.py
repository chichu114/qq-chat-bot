from datetime import datetime, timedelta, timezone
import errno
import fcntl
import hashlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def pdf_bytes(text="Hello PDF"):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream"
        % (
            len(b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET"),
            b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET",
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(("%d 0 obj\n" % number).encode("ascii"))
        result.extend(value)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(("%010d 00000 n \n" % offset).encode("ascii"))
    result.extend(
        (
            "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref)
        ).encode("ascii")
    )
    return bytes(result)


JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-data"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-data"
WEBP = b"RIFF\x08\x00\x00\x00WEBP" + b"webp-data"
GIF = b"GIF89a" + b"gif-data"
PDF = pdf_bytes()
tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())


def cache_payload_entries(root):
    return [item for item in root.iterdir() if item.name != ".media-cache.lock"]


def docx_bytes(include_document=True):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if include_document:
            archive.writestr(
                "word/document.xml",
                b'<w:document xmlns:w="urn:test"><w:body/></w:document>',
            )
        else:
            archive.writestr("word/styles.xml", b"<styles/>")
    return output.getvalue()


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None, close_error=None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = headers or {}
        self.read_sizes = []
        self.closed = False
        self.close_error = close_error

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self._offset >= len(self._body):
            return b""
        end = len(self._body) if size < 0 else self._offset + size
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
        self.pinned_peers = []

    def __call__(self, url, pinned_peer):
        self.urls.append(url)
        self.pinned_peers.append(pinned_peer)
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


class FakeRepository:
    def __init__(self):
        self.created = []
        self.expired = []
        self.to_cleanup = []
        self.cleanup_errors = []
        self.cleanup_error_calls = []

    def create_media_asset(self, **values):
        self.created.append(values)
        result = dict(values)
        result["id"] = values.get("asset_id", "asset-1")
        return result

    def list_expired_media(self, now):
        return list(self.to_cleanup)

    def mark_media_expired(self, asset_id, detail=None):
        self.expired.append((asset_id, detail))

    def record_media_cleanup_error(self, asset_id, detail, **kwargs):
        self.cleanup_errors.append((asset_id, detail))
        self.cleanup_error_calls.append((asset_id, detail, kwargs))
        return {"id": asset_id, "error_detail": detail}


class FakePdfSandbox:
    def __init__(self, rejection=None):
        self.rejection = rejection
        self.validated = []

    def validate(self, snapshot):
        self.validated.append(snapshot)
        if self.rejection is not None:
            raise self.rejection


class FakeRemoteDownloadSandbox:
    def __init__(self, body=PNG, content_type="image/png", rejection=None, before=None):
        self.body = body
        self.content_type = content_type
        self.rejection = rejection
        self.before = before
        self.calls = []

    def fetch(
        self,
        url,
        allowed_hosts,
        max_redirects,
        byte_limit,
        timeout_seconds=60,
    ):
        self.calls.append(
            (url, tuple(allowed_hosts), max_redirects, byte_limit, timeout_seconds)
        )
        if self.before is not None:
            self.before()
        if self.rejection is not None:
            raise self.rejection
        return type(
            "RemoteResult",
            (),
            {"body": self.body, "content_type": self.content_type},
        )()


def public_resolver(host):
    return ("93.184.216.34",)


def image_ref(url="https://example.invalid/a.png", declared_mime="image/png"):
    from qq_openclaw_bridge.domain.inbound import AttachmentRef

    return AttachmentRef(
        kind="image",
        file_id="image-id",
        url=url,
        name="original.png",
        declared_mime=declared_mime,
    )


def file_ref(url="https://example.invalid/a.pdf", declared_mime="application/pdf"):
    from qq_openclaw_bridge.domain.inbound import AttachmentRef

    return AttachmentRef(
        kind="file",
        file_id="file-id",
        url=url,
        name="document.pdf",
        declared_mime=declared_mime,
    )


class MediaSecurityTests(unittest.TestCase):
    def _service(
        self,
        cache,
        opener,
        resolver=public_resolver,
        repository=None,
        limits=None,
        local_source=None,
        now_provider=None,
        max_redirects=3,
        pdf_sandbox=None,
        allowed_remote_hosts=(
            "example.invalid",
            "cdn.invalid",
            "files.example.invalid",
            "internal.invalid",
        ),
        download_timeout_seconds=60,
        monotonic_clock=None,
    ):
        from qq_openclaw_bridge.application.media_ingestion import (
            MediaIngestionService,
        )

        return MediaIngestionService(
            cache,
            repository or FakeRepository(),
            resolver=resolver,
            opener=opener,
            limits=limits,
            local_source=local_source,
            now_provider=now_provider,
            max_redirects=max_redirects,
            pdf_sandbox=pdf_sandbox or FakePdfSandbox(),
            allowed_remote_hosts=allowed_remote_hosts,
            download_timeout_seconds=download_timeout_seconds,
            monotonic_clock=monotonic_clock,
        )

    def test_media_limits_use_required_kind_defaults(self):
        from qq_openclaw_bridge.domain.media import MediaLimits

        limits = MediaLimits()

        self.assertEqual(limits.image_bytes, 10 * 1024 * 1024)
        self.assertEqual(limits.audio_bytes, 20 * 1024 * 1024)
        self.assertEqual(limits.file_bytes, 20 * 1024 * 1024)
        self.assertEqual(limits.max_images, 4)

    def test_cache_root_cannot_be_a_symbolic_link(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            target.mkdir()
            link = Path(temp_dir) / "cache-link"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(MediaRejected, "cache root.*symbolic link"):
                self._service(link, FakeOpener([]))

    def test_cache_and_napcat_roots_reject_symlink_ancestors(self):
        from qq_openclaw_bridge.application.media_ingestion import (
            NapCatLocalMediaSource,
        )
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_parent = root / "real"
            real_parent.mkdir()
            (real_parent / "napcat").mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(MediaRejected, "ancestor.*symbolic link"):
                self._service(linked_parent / "cache", FakeOpener([]))
            with self.assertRaisesRegex(MediaRejected, "ancestor.*symbolic link"):
                NapCatLocalMediaSource((linked_parent / "napcat",))

    def test_cache_lock_is_secure_and_shared_by_services_for_same_root(self):
        from qq_openclaw_bridge.application import media_ingestion

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            first = self._service(root, FakeOpener([]))
            second = self._service(root, FakeOpener([]))
            self.addCleanup(first.close)
            self.addCleanup(second.close)

            lock_path = root / media_ingestion.CACHE_LOCK_NAME
            lock_stat = lock_path.stat()
            self.assertTrue(stat.S_ISREG(lock_stat.st_mode))
            self.assertEqual(lock_stat.st_nlink, 1)
            self.assertEqual(stat.S_IMODE(lock_stat.st_mode), 0o600)
            self.assertIs(first._operation_lock, second._operation_lock)

    def test_separate_lock_descriptors_serialize_like_two_bridge_processes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            first = self._service(root, FakeOpener([]))
            second = self._service(root, FakeOpener([]))
            self.addCleanup(first.close)
            self.addCleanup(second.close)

            fcntl.flock(first._cache_lock_fd, fcntl.LOCK_EX)
            try:
                with self.assertRaises((BlockingIOError, OSError)):
                    fcntl.flock(
                        second._cache_lock_fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
            finally:
                fcntl.flock(first._cache_lock_fd, fcntl.LOCK_UN)

            fcntl.flock(second._cache_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(second._cache_lock_fd, fcntl.LOCK_UN)

    def test_fetch_cleanup_and_compensation_run_inside_flock(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.domain.media import MediaRejected

        state = {"locked": False}
        calls = []

        def fake_flock(file_descriptor, operation):
            calls.append(operation)
            if operation == fcntl.LOCK_EX:
                self.assertFalse(state["locked"])
                state["locked"] = True
            elif operation == fcntl.LOCK_UN:
                self.assertTrue(state["locked"])
                state["locked"] = False

        class RejectingRepository(FakeRepository):
            def create_media_asset(self, **values):
                self.assert_locked()
                raise MediaRejected("repository rejected")

            def assert_locked(self):
                if not state["locked"]:
                    raise AssertionError("repository call escaped flock")

            def list_expired_media(self, now):
                self.assert_locked()
                return []

        repository = RejectingRepository()
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache",
                FakeOpener([FakeResponse(PNG, headers={"Content-Type": "image/png"})]),
                repository=repository,
            )
            real_compensation = service._unlink_expected_cache_entry_quietly

            def checked_compensation(*args, **kwargs):
                self.assertTrue(state["locked"])
                return real_compensation(*args, **kwargs)

            with mock.patch.object(media_ingestion.fcntl, "flock", side_effect=fake_flock):
                with mock.patch.object(
                    service,
                    "_unlink_expected_cache_entry_quietly",
                    side_effect=checked_compensation,
                ):
                    with self.assertRaisesRegex(MediaRejected, "repository rejected"):
                        service.fetch(image_ref(), 1, "m-lock-failure")
                service.cleanup_expired(datetime(2026, 7, 22, tzinfo=timezone.utc))

        self.assertEqual(
            calls,
            [fcntl.LOCK_EX, fcntl.LOCK_UN, fcntl.LOCK_EX, fcntl.LOCK_UN],
        )
        self.assertFalse(state["locked"])

    def test_remote_sandbox_fetch_finishes_before_flock_and_publication_stays_locked(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.application.media_ingestion import MediaIngestionService
        from qq_openclaw_bridge.domain.media import MediaLimits

        state = {"locked": False}

        def fake_flock(file_descriptor, operation):
            if operation == fcntl.LOCK_EX:
                self.assertFalse(state["locked"])
                state["locked"] = True
            elif operation == fcntl.LOCK_UN:
                self.assertTrue(state["locked"])
                state["locked"] = False

        class LockedRepository(FakeRepository):
            def create_media_asset(self, **values):
                if not state["locked"]:
                    raise AssertionError("cache publication escaped flock")
                return super().create_media_asset(**values)

        repository = LockedRepository()
        sandbox = FakeRemoteDownloadSandbox(
            before=lambda: self.assertFalse(state["locked"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            service = MediaIngestionService(
                root,
                repository,
                remote_download_sandbox=sandbox,
                allowed_remote_hosts=("EXAMPLE.invalid.",),
                limits=MediaLimits(image_bytes=len(PNG), audio_bytes=20, file_bytes=20),
                pdf_sandbox=FakePdfSandbox(),
            )
            with mock.patch.object(
                media_ingestion.fcntl,
                "flock",
                side_effect=fake_flock,
            ):
                media = service.fetch(image_ref(), 1, "m-sandbox-lock")

            self.assertEqual(media.path.read_bytes(), PNG)
            self.assertEqual(len(repository.created), 1)
            self.assertFalse(state["locked"])

        self.assertEqual(
            sandbox.calls,
            [
                (
                    "https://example.invalid/a.png",
                    ("example.invalid",),
                    3,
                    len(PNG),
                    60.0,
                )
            ],
        )

    def test_default_remote_sandbox_is_used_before_flock_without_transport_fallback(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.application.media_ingestion import MediaIngestionService
        from qq_openclaw_bridge.domain.media import MediaLimits

        state = {"locked": False}

        def fake_flock(file_descriptor, operation):
            if operation == fcntl.LOCK_EX:
                self.assertFalse(state["locked"])
                state["locked"] = True
            elif operation == fcntl.LOCK_UN:
                self.assertTrue(state["locked"])
                state["locked"] = False

        class LockedRepository(FakeRepository):
            def create_media_asset(self, **values):
                if not state["locked"]:
                    raise AssertionError("cache publication escaped flock")
                return super().create_media_asset(**values)

        repository = LockedRepository()
        sandbox = FakeRemoteDownloadSandbox(
            before=lambda: self.assertFalse(state["locked"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            with mock.patch.object(
                media_ingestion,
                "RemoteDownloadSandbox",
                return_value=sandbox,
            ) as sandbox_factory:
                with mock.patch.object(
                    media_ingestion,
                    "_default_resolver",
                    side_effect=AssertionError("default resolver must not run"),
                ):
                    with mock.patch.object(
                        media_ingestion,
                        "_default_opener",
                        side_effect=AssertionError("default opener must not run"),
                    ):
                        service = MediaIngestionService(
                            root,
                            repository,
                            allowed_remote_hosts=("example.invalid",),
                            limits=MediaLimits(
                                image_bytes=len(PNG), audio_bytes=20, file_bytes=20
                            ),
                            pdf_sandbox=FakePdfSandbox(),
                        )
                        self.addCleanup(service.close)
                        self.assertIsNone(service._resolver)
                        self.assertIsNone(service._opener)
                        with mock.patch.object(
                            media_ingestion.fcntl,
                            "flock",
                            side_effect=fake_flock,
                        ):
                            media = service.fetch(image_ref(), 1, "m-default-sandbox")

            self.assertEqual(media.path.read_bytes(), PNG)
            sandbox_factory.assert_called_once_with()
            self.assertFalse(state["locked"])

        self.assertEqual(len(sandbox.calls), 1)

    def test_transport_injection_is_unambiguous_before_cache_filesystem_effects(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.application.media_ingestion import MediaIngestionService

        resolver = lambda host: ("93.184.216.34",)
        opener = FakeOpener([])
        sandbox = FakeRemoteDownloadSandbox()
        invalid = (
            {"resolver": resolver},
            {"opener": opener},
            {
                "resolver": resolver,
                "opener": opener,
                "remote_download_sandbox": sandbox,
            },
        )
        with mock.patch.object(
            media_ingestion,
            "_open_trusted_directory",
            side_effect=AssertionError("cache filesystem must not be touched"),
        ) as open_directory:
            for values in invalid:
                with self.subTest(values=tuple(values)):
                    with self.assertRaises(ValueError):
                        MediaIngestionService(Path("/unused"), FakeRepository(), **values)
        open_directory.assert_not_called()

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(
                media_ingestion,
                "RemoteDownloadSandbox",
                side_effect=AssertionError("sync seam must not construct a sandbox"),
            ):
                service = MediaIngestionService(
                    Path(temp_dir) / "cache",
                    FakeRepository(),
                    resolver=resolver,
                    opener=opener,
                    pdf_sandbox=FakePdfSandbox(),
                )
            self.addCleanup(service.close)
            self.assertFalse(service._use_remote_download_sandbox)
            self.assertIs(service._resolver, resolver)
            self.assertIs(service._opener, opener)
            self.assertIsNone(service._remote_download_sandbox)

    def test_remote_sandbox_rejection_creates_no_temp_cache_or_repository_row(self):
        from qq_openclaw_bridge.application.media_ingestion import MediaIngestionService
        from qq_openclaw_bridge.domain.media import MediaRejected

        for rejection in (
            MediaRejected("remote download sandbox rejected media"),
            MediaRejected("remote download sandbox rejected media"),
        ):
            with self.subTest(rejection=rejection):
                repository = FakeRepository()
                sandbox = FakeRemoteDownloadSandbox(rejection=rejection)
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir) / "cache"
                    service = MediaIngestionService(
                        root,
                        repository,
                        remote_download_sandbox=sandbox,
                        allowed_remote_hosts=("example.invalid",),
                        pdf_sandbox=FakePdfSandbox(),
                    )
                    with self.assertRaisesRegex(
                        MediaRejected,
                        "remote download sandbox rejected media",
                    ):
                        service.fetch(image_ref(), 1, "m-sandbox-rejected")

                    self.assertEqual(cache_payload_entries(root), [])
                    self.assertEqual(repository.created, [])

    def test_downloader_rejects_non_https_and_private_dns_before_request(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        blocked = (
            "127.0.0.1",
            "10.1.2.3",
            "169.254.1.2",
            "224.0.0.1",
            "240.0.0.1",
            "100.64.0.1",
            "0.0.0.0",
            "192.0.2.1",
            "::1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for address in blocked:
                with self.subTest(address=address):
                    opener = FakeOpener([])
                    service = self._service(
                        Path(temp_dir) / address.replace(":", "_"),
                        opener,
                        resolver=lambda host, value=address: (value,),
                    )
                    with self.assertRaisesRegex(MediaRejected, "private address"):
                        service.fetch(image_ref(), 1, "m1")
                    self.assertEqual(opener.urls, [])

            opener = FakeOpener([])
            service = self._service(Path(temp_dir) / "http", opener)
            with self.assertRaisesRegex(MediaRejected, "HTTPS"):
                service.fetch(image_ref("http://example.invalid/a.png"), 1, "m1")
            self.assertEqual(opener.urls, [])

    def test_downloader_rejects_unallowlisted_public_host_before_resolution_or_open(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        resolver_calls = []

        def resolver(host):
            resolver_calls.append(host)
            return ("93.184.216.34",)

        with tempfile.TemporaryDirectory() as temp_dir:
            opener = FakeOpener([])
            service = self._service(
                Path(temp_dir) / "cache",
                opener,
                resolver=resolver,
                allowed_remote_hosts=(),
            )

            with self.assertRaisesRegex(MediaRejected, "allowlist"):
                service.fetch(image_ref("https://PUBLIC.example./a.png"), 1, "m-denied")

        self.assertEqual(resolver_calls, [])
        self.assertEqual(opener.urls, [])

    def test_redirect_to_unallowlisted_host_is_rejected_before_resolution_or_open(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        resolver_calls = []

        def resolver(host):
            resolver_calls.append(host)
            return ("93.184.216.34",)

        response = FakeResponse(
            status=302,
            headers={"Location": "https://unlisted.example/final.png"},
        )
        opener = FakeOpener([response])
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache",
                opener,
                resolver=resolver,
                allowed_remote_hosts=("ORIGIN.example.",),
            )

            with self.assertRaisesRegex(MediaRejected, "allowlist"):
                service.fetch(image_ref("https://origin.example/a.png"), 1, "m-redirect")

        self.assertEqual(resolver_calls, ["origin.example"])
        self.assertEqual(opener.urls, ["https://origin.example/a.png"])
        self.assertTrue(response.closed)

    def test_idna_equivalent_trailing_separators_share_exact_allowlist_semantics(self):
        variants = ("example.", "example\u3002", "example\uff0e", "example\uff61")
        resolver_calls = []
        responses = [
            FakeResponse(PNG, headers={"Content-Type": "image/png"})
            for _ in variants
        ]

        def resolver(host):
            resolver_calls.append(host)
            return ("93.184.216.34",)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache",
                FakeOpener(responses),
                resolver=resolver,
                allowed_remote_hosts=("example\u3002",),
            )
            for index, host in enumerate(variants):
                with self.subTest(host=host):
                    media = service.fetch(
                        image_ref("https://%s/image-%d.png" % (host, index)),
                        1,
                        "m-idna-%d" % index,
                    )
                    self.assertEqual(media.mime, "image/png")

        self.assertEqual(resolver_calls, ["example"] * len(variants))

        for invalid in ("bad..example", "example..", "*.example"):
            with self.subTest(invalid=invalid):
                with tempfile.TemporaryDirectory() as temp_dir:
                    with self.assertRaises(ValueError):
                        self._service(
                            Path(temp_dir) / "cache",
                            FakeOpener([]),
                            allowed_remote_hosts=(invalid,),
                        )

        with tempfile.TemporaryDirectory() as temp_dir:
            opener = FakeOpener([])
            service = self._service(
                Path(temp_dir) / "cache",
                opener,
                allowed_remote_hosts=("example",),
            )
            with self.assertRaisesRegex(Exception, "allowlist"):
                service.fetch(
                    image_ref("https://sub.example/image.png"),
                    1,
                    "m-subdomain",
                )
            self.assertEqual(opener.calls if hasattr(opener, "calls") else opener.urls, [])

    def test_downloader_rejects_non_443_ports_and_mixed_dns_results(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for url in (
                "https://example.invalid:444/a.png",
                "https://example.invalid:80/a.png",
            ):
                with self.subTest(url=url):
                    opener = FakeOpener([])
                    service = self._service(root / ("cache-" + url.rsplit(":", 1)[-1]), opener)
                    with self.assertRaisesRegex(MediaRejected, "port 443"):
                        service.fetch(image_ref(url), 1, "m-port")
                    self.assertEqual(opener.urls, [])

            opener = FakeOpener([])
            service = self._service(
                root / "cache-mixed",
                opener,
                resolver=lambda host: ("93.184.216.34", "127.0.0.1"),
            )
            with self.assertRaisesRegex(MediaRejected, "public address"):
                service.fetch(image_ref(), 1, "m-mixed")
            self.assertEqual(opener.urls, [])

    def test_each_https_hop_passes_its_validated_peer_to_transport(self):
        resolutions = {
            "example.invalid": ("93.184.216.34", "93.184.216.35"),
            "cdn.invalid": ("142.250.72.14",),
        }

        def resolver(host):
            return resolutions[host]

        opener = FakeOpener(
            [
                FakeResponse(
                    status=302,
                    headers={"Location": "https://cdn.invalid/final.png"},
                ),
                FakeResponse(PNG, headers={"Content-Type": "image/png"}),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir) / "cache", opener, resolver=resolver)

            media = service.fetch(image_ref(), 1, "m-pinned")

        self.assertEqual(media.mime, "image/png")
        self.assertEqual(
            list(zip(opener.urls, opener.pinned_peers)),
            [
                ("https://example.invalid/a.png", "93.184.216.34"),
                ("https://cdn.invalid/final.png", "142.250.72.14"),
            ],
        )
        for url, peer in zip(opener.urls, opener.pinned_peers):
            self.assertIn(peer, resolutions[url.split("/", 3)[2]])

    def test_default_https_connection_pins_tcp_peer_and_preserves_sni_and_host(self):
        from qq_openclaw_bridge.application import media_ingestion

        raw_socket = object()

        class FakeContext:
            def __init__(self):
                self.calls = []
                self.verify_mode = media_ingestion.ssl.CERT_REQUIRED
                self.check_hostname = True
                self.post_handshake_auth = None

            def wrap_socket(self, sock, server_hostname=None):
                self.calls.append((sock, server_hostname))
                return object()

        context = FakeContext()
        connection = media_ingestion._PinnedHTTPSConnection(
            "files.example.invalid",
            "93.184.216.34",
            port=443,
            timeout=17,
            context=context,
        )
        with mock.patch.object(
            media_ingestion.socket,
            "create_connection",
            return_value=raw_socket,
        ) as create_connection:
            connection.connect()

        create_connection.assert_called_once_with(
            ("93.184.216.34", 443), 17, None
        )
        self.assertEqual(context.calls, [(raw_socket, "files.example.invalid")])

        class FakeConnection:
            instances = []

            def __init__(self, host, peer, port, timeout, context):
                self.values = (host, peer, port, timeout, context)
                self.request_call = None
                self.response = FakeResponse(PNG, headers={"Content-Type": "image/png"})
                self.__class__.instances.append(self)

            def request(self, method, target, headers):
                self.request_call = (method, target, headers)

            def getresponse(self):
                return self.response

            def close(self):
                pass

        with mock.patch.object(
            media_ingestion, "_PinnedHTTPSConnection", FakeConnection
        ):
            response = media_ingestion._default_opener(
                "https://files.example.invalid/path?q=1", "93.184.216.34"
            )

        instance = FakeConnection.instances[0]
        self.assertEqual(instance.values[:4], ("files.example.invalid", "93.184.216.34", 443, 30))
        self.assertEqual(instance.request_call[0:2], ("GET", "/path?q=1"))
        self.assertEqual(instance.request_call[2]["Host"], "files.example.invalid")
        response.close()

    def test_each_redirect_is_resolved_and_private_redirect_is_rejected(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        calls = []

        def resolver(host):
            calls.append(host)
            if host == "internal.invalid":
                return ("127.0.0.1",)
            return ("93.184.216.34",)

        opener = FakeOpener(
            [FakeResponse(status=302, headers={"Location": "https://internal.invalid/x"})]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir) / "cache", opener, resolver=resolver)
            with self.assertRaisesRegex(MediaRejected, "private address"):
                service.fetch(image_ref(), 1, "m1")

        self.assertEqual(calls, ["example.invalid", "internal.invalid"])
        self.assertEqual(opener.urls, ["https://example.invalid/a.png"])
        self.assertEqual(opener.pinned_peers, ["93.184.216.34"])

    def test_redirect_limit_is_enforced_without_following_an_extra_hop(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        opener = FakeOpener(
            [
                FakeResponse(status=302, headers={"Location": "/b.png"}),
                FakeResponse(status=302, headers={"Location": "/c.png"}),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache", opener, max_redirects=1
            )
            with self.assertRaisesRegex(MediaRejected, "redirect limit"):
                service.fetch(image_ref(), 1, "m1")

        self.assertEqual(
            opener.urls,
            ["https://example.invalid/a.png", "https://example.invalid/b.png"],
        )

    def test_signatures_are_recognized_and_cached_with_random_secure_names(self):
        cases = (
            (JPEG, "image/jpeg", image_ref(declared_mime="image/jpeg")),
            (PNG, "image/png", image_ref()),
            (WEBP, "image/webp", image_ref(declared_mime="image/webp")),
            (GIF, "image/gif", image_ref(declared_mime="image/gif")),
            (PDF, "application/pdf", file_ref()),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, (body, mime, ref) in enumerate(cases):
                with self.subTest(mime=mime):
                    cache = Path(temp_dir) / ("cache-%s" % index)
                    repository = FakeRepository()
                    response = FakeResponse(body, headers={"Content-Type": mime})
                    service = self._service(
                        cache, FakeOpener([response]), repository=repository
                    )

                    media = service.fetch(ref, 7, "m-signature")

                    self.assertEqual(media.mime, mime)
                    self.assertEqual(media.size, len(body))
                    self.assertEqual(media.sha256, hashlib.sha256(body).hexdigest())
                    self.assertEqual(media.path.read_bytes(), body)
                    self.assertNotIn(ref.name, media.path.name)
                    self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o700)
                    self.assertEqual(stat.S_IMODE(media.path.stat().st_mode), 0o600)
                    self.assertEqual(repository.created[0]["cache_path"], str(media.path))
                    self.assertTrue(all(size == 65536 for size in response.read_sizes))

    def test_mime_mismatch_is_rejected_and_partial_cache_is_removed(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache"
            service = self._service(
                cache,
                FakeOpener(
                    [FakeResponse(PNG, headers={"Content-Type": "image/jpeg"})]
                ),
            )
            with self.assertRaisesRegex(MediaRejected, "MIME mismatch"):
                service.fetch(image_ref(declared_mime="image/png"), 1, "m1")
            self.assertEqual(cache_payload_entries(cache), [])

    def test_pdf_requires_signature_even_when_mime_claims_pdf(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache"
            service = self._service(
                cache,
                FakeOpener(
                    [
                        FakeResponse(
                            b"plain text pretending to be PDF",
                            headers={"Content-Type": "application/pdf"},
                        )
                    ]
                ),
            )

            with self.assertRaisesRegex(MediaRejected, "PDF signature"):
                service.fetch(file_ref(), 1, "m-forged-pdf")
            self.assertEqual(cache_payload_entries(cache), [])

    def test_pdf_requires_bounded_structural_validation_before_ready(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache"
            repository = FakeRepository()
            pseudo = b"%PDF-1.7\npdf-data"
            rejecting_sandbox = FakePdfSandbox(
                MediaRejected("PDF sandbox rejected document")
            )
            service = self._service(
                cache,
                FakeOpener(
                    [
                        FakeResponse(
                            pseudo,
                            headers={"Content-Type": "application/pdf"},
                        )
                    ]
                ),
                repository=repository,
                pdf_sandbox=rejecting_sandbox,
            )

            with self.assertRaisesRegex(MediaRejected, "PDF sandbox rejected"):
                service.fetch(file_ref(), 1, "m-pseudo-pdf")

            self.assertEqual(repository.created, [])
            self.assertEqual(cache_payload_entries(cache), [])
            self.assertEqual(rejecting_sandbox.validated, [pseudo])

            accepting_sandbox = FakePdfSandbox()
            valid_service = self._service(
                Path(temp_dir) / "valid-cache",
                FakeOpener(
                    [FakeResponse(PDF, headers={"Content-Type": "application/pdf"})]
                ),
                pdf_sandbox=accepting_sandbox,
            )
            media = valid_service.fetch(file_ref(), 1, "m-valid-pdf")
            self.assertEqual(media.path.read_bytes(), PDF)
            self.assertEqual(accepting_sandbox.validated, [PDF])

    def test_docx_requires_valid_zip_with_readable_document_xml(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        cases = (
            (b"PK\x03\x04not-a-zip", "invalid DOCX"),
            (docx_bytes(include_document=False), "word/document.xml"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, (body, message) in enumerate(cases):
                with self.subTest(message=message):
                    cache = Path(temp_dir) / ("cache-%d" % index)
                    service = self._service(
                        cache,
                        FakeOpener(
                            [FakeResponse(body, headers={"Content-Type": mime})]
                        ),
                    )
                    with self.assertRaisesRegex(MediaRejected, message):
                        service.fetch(
                            file_ref(
                                url="https://example.invalid/a.docx",
                                declared_mime=mime,
                            ),
                            1,
                            "m-forged-docx",
                        )
                    self.assertEqual(cache_payload_entries(cache), [])

    def test_streaming_overflow_stops_and_removes_partial_file(self):
        from qq_openclaw_bridge.domain.media import MediaLimits, MediaRejected

        response = FakeResponse(PNG + b"x" * 100, headers={"Content-Type": "image/png"})
        limits = MediaLimits(image_bytes=12, audio_bytes=20, file_bytes=20)
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache"
            service = self._service(
                cache, FakeOpener([response]), limits=limits
            )
            with self.assertRaisesRegex(MediaRejected, "size limit"):
                service.fetch(image_ref(), 1, "m1")

            self.assertEqual(response.read_sizes, [65536])
            self.assertEqual(cache_payload_entries(cache), [])

    def test_write_failure_does_not_delete_replacement_temporary_entry(self):
        class ReplacingResponse:
            status = 200
            headers = {"Content-Type": "image/png"}

            def __init__(self, cache):
                self.cache = cache
                self.closed = False

            def read(self, size):
                temporary = next(iter(cache_payload_entries(self.cache)))
                temporary.unlink()
                temporary.write_bytes(b"replacement")
                raise OSError("source failed")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache"
            response = ReplacingResponse(cache)
            service = self._service(cache, FakeOpener([response]))

            with self.assertRaisesRegex(OSError, "source failed"):
                service.fetch(image_ref(), 1, "m-write-race")

            self.assertTrue(response.closed)
            self.assertIn(
                b"replacement",
                [item.read_bytes() for item in cache_payload_entries(cache)],
            )

    def test_fetch_rejects_replaced_cache_root_and_cleans_held_directory(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            cache = parent / "cache"
            repository = FakeRepository()
            response = FakeResponse(PNG, headers={"Content-Type": "image/png"})
            service = self._service(
                cache,
                FakeOpener([response]),
                repository=repository,
            )
            held_root = parent / "held-root"
            cache.rename(held_root)
            cache.mkdir()

            with self.assertRaisesRegex(MediaRejected, "cache root.*changed"):
                service.fetch(image_ref(), 1, "m-root-race")

            self.assertEqual(cache_payload_entries(held_root), [])
            self.assertEqual(cache_payload_entries(cache), [])
            self.assertEqual(repository.created, [])

    def test_cleanup_rejects_replaced_cache_root_before_scanning_repository(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        class CountingRepository(FakeRepository):
            def __init__(self):
                super().__init__()
                self.scans = 0

            def list_expired_media(self, now):
                self.scans += 1
                return super().list_expired_media(now)

        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            cache = parent / "cache"
            repository = CountingRepository()
            service = self._service(cache, FakeOpener([]), repository=repository)
            held_root = parent / "held-root"
            cache.rename(held_root)
            cache.mkdir()
            (held_root / "old.bin").write_bytes(b"old")
            (cache / "new.bin").write_bytes(b"new")
            repository.to_cleanup = [
                {"id": "old", "cache_path": str(held_root / "old.bin")},
            ]

            with self.assertRaisesRegex(MediaRejected, "cache root.*changed"):
                service.cleanup_expired(datetime(2026, 7, 23, tzinfo=timezone.utc))

            self.assertEqual(repository.scans, 0)
            self.assertEqual(repository.expired, [])
            self.assertTrue((held_root / "old.bin").exists())
            self.assertTrue((cache / "new.bin").exists())

    def test_fetch_rejects_directory_entry_replacement_during_publish(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            for replacement in ("symlink", "hardlink", "inode"):
                with self.subTest(replacement=replacement):
                    cache = parent / ("cache-" + replacement)
                    repository = FakeRepository()
                    service = self._service(
                        cache,
                        FakeOpener(
                            [FakeResponse(PNG, headers={"Content-Type": "image/png"})]
                        ),
                        repository=repository,
                    )
                    outside = parent / ("outside-" + replacement)
                    outside.write_bytes(b"x" * len(PNG))
                    real_rename = os.rename

                    def replace_after_publish(
                        source,
                        destination,
                        *,
                        src_dir_fd=None,
                        dst_dir_fd=None,
                    ):
                        real_rename(
                            source,
                            destination,
                            src_dir_fd=src_dir_fd,
                            dst_dir_fd=dst_dir_fd,
                        )
                        os.unlink(destination, dir_fd=dst_dir_fd)
                        if replacement == "symlink":
                            os.symlink(str(outside), destination, dir_fd=dst_dir_fd)
                        elif replacement == "hardlink":
                            os.link(str(outside), destination, dst_dir_fd=dst_dir_fd)
                        else:
                            descriptor = os.open(
                                destination,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=dst_dir_fd,
                            )
                            with os.fdopen(descriptor, "wb") as replacement_file:
                                replacement_file.write(b"y" * len(PNG))

                    with mock.patch.object(
                        media_ingestion.os,
                        "rename",
                        side_effect=replace_after_publish,
                    ):
                        with self.assertRaisesRegex(MediaRejected, "cache entry"):
                            service.fetch(image_ref(), 1, "m-publish-race")

                    self.assertEqual(repository.created, [])
                    self.assertEqual(outside.read_bytes(), b"x" * len(PNG))

    def test_fetch_rolls_back_ready_row_and_file_when_database_hardening_fails(self):
        from qq_openclaw_bridge.application.media_ingestion import MediaIngestionService
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            database = SqliteDatabase(root / "state.sqlite3")
            repository = E4Repository(database)
            service = MediaIngestionService(
                cache,
                repository,
                resolver=public_resolver,
                opener=FakeOpener(
                    [FakeResponse(PNG, headers={"Content-Type": "image/png"})]
                ),
                allowed_remote_hosts=("example.invalid",),
            )

            with mock.patch.object(
                database,
                "harden_files",
                side_effect=OSError("chmod failed"),
            ):
                with self.assertRaisesRegex(OSError, "chmod failed"):
                    service.fetch(image_ref(), 1, "m-harden")

            with database.connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(cache_payload_entries(cache), [])

    def test_declared_size_overflow_is_rejected_before_opening(self):
        from qq_openclaw_bridge.domain.inbound import AttachmentRef
        from qq_openclaw_bridge.domain.media import MediaLimits, MediaRejected

        ref = AttachmentRef(
            kind="image",
            url="https://example.invalid/a.png",
            declared_size=13,
            declared_mime="image/png",
        )
        opener = FakeOpener([])
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache",
                opener,
                limits=MediaLimits(image_bytes=12, audio_bytes=20, file_bytes=20),
            )
            with self.assertRaisesRegex(MediaRejected, "size limit"):
                service.fetch(ref, 1, "m1")
        self.assertEqual(opener.urls, [])

    def test_cache_expiry_is_one_hour(self):
        now = datetime(2026, 7, 19, 8, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache",
                FakeOpener([FakeResponse(PNG, headers={"Content-Type": "image/png"})]),
                now_provider=lambda: now,
            )
            media = service.fetch(image_ref(), 1, "m1")
        self.assertEqual(media.expires_at, now + timedelta(hours=1))

    def test_napcat_source_rejects_escape_and_symlinks(self):
        from qq_openclaw_bridge.application.media_ingestion import (
            NapCatLocalMediaSource,
        )
        from qq_openclaw_bridge.domain.inbound import AttachmentRef
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "napcat"
            root.mkdir()
            outside = Path(temp_dir) / "outside.png"
            outside.write_bytes(PNG)
            link = root / "linked.png"
            link.symlink_to(outside)
            source = NapCatLocalMediaSource((root,))
            service = self._service(
                Path(temp_dir) / "cache",
                FakeOpener([]),
                local_source=source,
            )

            for path in (outside, link, root / ".." / "outside.png"):
                with self.subTest(path=path):
                    ref = AttachmentRef(
                        kind="image",
                        file_id=str(path),
                        name="local.png",
                        declared_mime="image/png",
                    )
                    with self.assertRaisesRegex(
                        MediaRejected, "outside allowed roots|symbolic link"
                    ):
                        service.fetch(ref, 1, "m-local")

            hardlink = root / "hardlink.png"
            os.link(str(outside), str(hardlink))
            with self.assertRaisesRegex(MediaRejected, "hard link"):
                source.open(str(hardlink))

    def test_napcat_source_reads_regular_file_without_network(self):
        from qq_openclaw_bridge.application.media_ingestion import (
            NapCatLocalMediaSource,
        )
        from qq_openclaw_bridge.domain.inbound import AttachmentRef

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "napcat"
            root.mkdir()
            source_path = root / "local.png"
            source_path.write_bytes(PNG)
            opener = FakeOpener([])
            service = self._service(
                Path(temp_dir) / "cache",
                opener,
                local_source=NapCatLocalMediaSource((root,)),
            )
            media = service.fetch(
                AttachmentRef(
                    kind="image",
                    file_id=str(source_path),
                    name="local.png",
                    declared_mime="image/png",
                ),
                1,
                "m-local",
            )

            self.assertEqual(media.path.read_bytes(), PNG)
            self.assertEqual(opener.urls, [])

    def test_dual_field_local_failure_falls_back_only_to_allowlisted_remote(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        class MissingLocal:
            def __init__(self):
                self.calls = []

            def open(self, file_id):
                self.calls.append(file_id)
                raise MediaRejected("NapCat local file is unavailable")

        local = MissingLocal()
        opener = FakeOpener([
            FakeResponse(PNG, headers={"Content-Type": "image/png"})
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache", opener, local_source=local
            )
            media = service.fetch(image_ref(), 1, "m-fallback")

        self.assertEqual(local.calls, ["image-id"])
        self.assertEqual(opener.urls, ["https://example.invalid/a.png"])
        self.assertEqual(media.mime, "image/png")

    def test_cleanup_reconciles_missing_and_unsafe_paths_without_external_delete(self):
        repository = FakeRepository()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            root.mkdir()
            safe = root / "safe.bin"
            safe.write_bytes(b"safe")
            outside = Path(temp_dir) / "outside.bin"
            outside.write_bytes(b"outside")
            link = root / "link.bin"
            link.symlink_to(outside)
            hardlink = root / "hardlink.bin"
            os.link(str(outside), str(hardlink))
            repository.to_cleanup = [
                {"id": "safe", "cache_path": str(safe)},
                {"id": "outside", "cache_path": str(outside)},
                {"id": "link", "cache_path": str(link)},
                {"id": "hardlink", "cache_path": str(hardlink)},
                {"id": "missing", "cache_path": str(root / "missing.bin")},
                {"id": "relative", "cache_path": "relative.bin"},
            ]
            service = self._service(root, FakeOpener([]), repository=repository)

            removed = service.cleanup_expired(
                datetime(2026, 7, 19, tzinfo=timezone.utc)
            )

            self.assertEqual(removed, 1)
            self.assertFalse(safe.exists())
            self.assertTrue(outside.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(hardlink.exists())
            self.assertEqual(
                repository.expired,
                [
                    ("safe", "cache file removed"),
                    ("outside", "invalid cache path"),
                    ("link", "unsafe cache entry"),
                    ("hardlink", "unsafe cache entry"),
                    ("missing", "cache file already missing"),
                    ("relative", "invalid cache path"),
                ],
            )

    def test_cleanup_keeps_transient_unlink_failure_for_retry_and_records_detail(self):
        repository = FakeRepository()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            root.mkdir()
            pending = root / "pending.bin"
            pending.write_bytes(b"pending")
            repository.to_cleanup = [
                {"id": "pending", "cache_path": str(pending)},
            ]
            service = self._service(root, FakeOpener([]), repository=repository)

            with mock.patch.object(
                os,
                "unlink",
                side_effect=OSError(errno.EBUSY, "temporarily busy"),
            ):
                removed = service.cleanup_expired(
                    datetime(2026, 7, 19, tzinfo=timezone.utc)
                )

            self.assertEqual(removed, 0)
            self.assertIn(
                b"pending",
                [item.read_bytes() for item in cache_payload_entries(root)],
            )
            self.assertEqual(repository.expired, [])
            self.assertEqual(len(repository.cleanup_errors), 1)
            asset_id, detail = repository.cleanup_errors[0]
            self.assertEqual(asset_id, "pending")
            self.assertIn("cache unlink failed (EBUSY)", detail)
            retained_path = Path(repository.cleanup_error_calls[0][2]["cache_path"])
            self.assertEqual(detail, "cache unlink failed (EBUSY) at %s" % retained_path)
            self.assertTrue(retained_path.name.startswith(".delete-"))
            self.assertTrue(retained_path.exists())
            self.assertEqual(retained_path.read_bytes(), b"pending")

    def test_response_close_error_does_not_undo_success_or_mask_failure(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = FakeRepository()
            successful = FakeResponse(
                PNG,
                headers={"Content-Type": "image/png"},
                close_error=OSError("close failed"),
            )
            service = self._service(
                root / "success",
                FakeOpener([successful]),
                repository=repository,
            )

            media = service.fetch(image_ref(), 1, "m-close-success")

            self.assertTrue(successful.closed)
            self.assertEqual(len(repository.created), 1)
            self.assertEqual(media.sha256, hashlib.sha256(PNG).hexdigest())

            mismatched = FakeResponse(
                PNG,
                headers={"Content-Type": "image/jpeg"},
                close_error=OSError("close failed"),
            )
            failing = self._service(
                root / "failure",
                FakeOpener([mismatched]),
            )
            with self.assertRaisesRegex(MediaRejected, "MIME mismatch"):
                failing.fetch(image_ref(), 1, "m-close-failure")
            self.assertTrue(mismatched.closed)

    def test_close_property_error_does_not_mask_primary_media_rejection(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        class PropertyFailingClose(FakeResponse):
            @property
            def close(self):
                raise RuntimeError("close property failed")

        response = PropertyFailingClose(
            PNG,
            headers={"Content-Type": "image/jpeg"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache",
                FakeOpener([response]),
            )
            with self.assertRaisesRegex(MediaRejected, "MIME mismatch"):
                service.fetch(image_ref(), 1, "m-close-property")

    def test_malformed_response_status_is_rejected_and_closed(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        response = FakeResponse(PNG, status="not-a-status")
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache"
            service = self._service(cache, FakeOpener([response]))

            with self.assertRaisesRegex(MediaRejected, "response status"):
                service.fetch(image_ref(), 1, "m-status")

            self.assertTrue(response.closed)
            self.assertEqual(cache_payload_entries(cache), [])

    def test_response_metadata_errors_and_non_integer_statuses_always_close(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        class MetadataResponse:
            def __init__(self, status=200, failing=None):
                self._status = status
                self._failing = failing
                self.closed = False

            @property
            def status(self):
                if self._failing == "status":
                    raise RuntimeError("status failed")
                return self._status

            def getcode(self):
                if self._failing == "getcode":
                    raise RuntimeError("getcode failed")
                return 200

            @property
            def headers(self):
                if self._failing == "headers":
                    raise RuntimeError("headers failed")
                return {"Content-Type": "image/png"}

            def close(self):
                self.closed = True

        cases = [
            MetadataResponse(failing="status"),
            MetadataResponse(status=None, failing="getcode"),
            MetadataResponse(failing="headers"),
        ]
        cases.extend(MetadataResponse(status=value) for value in (True, 200.0, "200", 99, 600))

        with tempfile.TemporaryDirectory() as temp_dir:
            for index, response in enumerate(cases):
                with self.subTest(index=index, status=response._status):
                    service = self._service(
                        Path(temp_dir) / ("cache-%d" % index),
                        FakeOpener([response]),
                    )
                    with self.assertRaisesRegex(MediaRejected, "response metadata|response status"):
                        service.fetch(image_ref(), 1, "m-metadata")
                    self.assertTrue(response.closed)

    def test_cleanup_reconciles_entry_that_disappears_before_quarantine(self):
        from qq_openclaw_bridge.application import media_ingestion

        repository = FakeRepository()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            root.mkdir()
            pending = root / "pending.bin"
            pending.write_bytes(b"pending")
            repository.to_cleanup = [
                {"id": "pending", "cache_path": str(pending)},
            ]
            service = self._service(root, FakeOpener([]), repository=repository)

            def disappear(
                source,
                destination,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
            ):
                os.unlink(source, dir_fd=src_dir_fd)
                raise FileNotFoundError(source)

            with mock.patch.object(media_ingestion.os, "rename", side_effect=disappear):
                removed = service.cleanup_expired(
                    datetime(2026, 7, 19, tzinfo=timezone.utc)
                )

            self.assertEqual(removed, 0)
            self.assertFalse(pending.exists())
            self.assertEqual(
                repository.expired,
                [("pending", "cache file already missing")],
            )

    def test_cleanup_quarantine_never_unlinks_replacement_inode(self):
        from qq_openclaw_bridge.application import media_ingestion

        for phase in ("before", "after"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "cache"
                root.mkdir()
                pending = root / "pending.bin"
                pending.write_bytes(b"registered")
                repository = FakeRepository()
                repository.to_cleanup = [
                    {"id": "pending", "cache_path": str(pending)},
                ]
                service = self._service(root, FakeOpener([]), repository=repository)
                real_rename = os.rename

                def replace_around_quarantine(
                    source,
                    destination,
                    *,
                    src_dir_fd=None,
                    dst_dir_fd=None,
                ):
                    if phase == "before":
                        os.unlink(source, dir_fd=src_dir_fd)
                        descriptor = os.open(
                            source,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=src_dir_fd,
                        )
                        with os.fdopen(descriptor, "wb") as replacement_file:
                            replacement_file.write(b"replacement")
                    real_rename(
                        source,
                        destination,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )
                    if phase == "after":
                        os.unlink(destination, dir_fd=dst_dir_fd)
                        descriptor = os.open(
                            destination,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=dst_dir_fd,
                        )
                        with os.fdopen(descriptor, "wb") as replacement_file:
                            replacement_file.write(b"replacement")

                with mock.patch.object(
                    media_ingestion.os,
                    "rename",
                    side_effect=replace_around_quarantine,
                ):
                    removed = service.cleanup_expired(
                        datetime(2026, 7, 19, tzinfo=timezone.utc)
                    )

                self.assertEqual(removed, 0)
                self.assertIn(
                    b"replacement",
                    [item.read_bytes() for item in cache_payload_entries(root)],
                )
                self.assertEqual(repository.cleanup_errors, [])
                self.assertEqual(repository.expired[0][0], "pending")
                self.assertIn("cleanup abandoned", repository.expired[0][1])

    def test_cleanup_rechecks_identity_after_quarantine_stat(self):
        from qq_openclaw_bridge.application import media_ingestion

        repository = FakeRepository()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            root.mkdir()
            pending = root / "pending.bin"
            pending.write_bytes(b"registered")
            repository.to_cleanup = [
                {"id": "pending", "cache_path": str(pending)},
            ]
            service = self._service(root, FakeOpener([]), repository=repository)
            real_stat = os.stat
            replaced = []

            def replace_after_stat(path, *, dir_fd=None, follow_symlinks=True):
                result = real_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )
                if str(path).startswith(".quarantine-") and not replaced:
                    replaced.append(path)
                    os.unlink(path, dir_fd=dir_fd)
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    with os.fdopen(descriptor, "wb") as replacement_file:
                        replacement_file.write(b"replacement")
                return result

            with mock.patch.object(
                media_ingestion.os,
                "stat",
                side_effect=replace_after_stat,
            ):
                removed = service.cleanup_expired(
                    datetime(2026, 7, 19, tzinfo=timezone.utc)
                )

            self.assertEqual(removed, 0)
            self.assertIn(
                b"replacement",
                [item.read_bytes() for item in cache_payload_entries(root)],
            )
            self.assertEqual(repository.cleanup_errors, [])
            self.assertEqual(repository.expired[0][0], "pending")
            self.assertIn("cleanup abandoned", repository.expired[0][1])

    def test_cleanup_eacces_at_each_rename_stage_persists_retained_path_for_restart(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        cases = (
            ("quarantine", "pending.bin"),
            ("delete", ".quarantine-"),
        )
        for phase, retained_prefix in cases:
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory() as temp_dir:
                    base = Path(temp_dir)
                    root = base / "cache"
                    root.mkdir()
                    pending = root / "pending.bin"
                    pending.write_bytes(b"registered")
                    now = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
                    database = SqliteDatabase(base / "state.sqlite3")
                    repository = E4Repository(database)
                    asset = repository.create_media_asset(
                        user_id=1,
                        message_id="m-retained",
                        kind="file",
                        display_name="pending.bin",
                        status="ready",
                        expires_at=now,
                        cache_path=str(pending),
                    )
                    service = self._service(
                        root,
                        FakeOpener([]),
                        repository=repository,
                    )
                    real_rename = os.rename
                    failed = []

                    def deny_rename(source, destination, *args, **kwargs):
                        if (
                            isinstance(destination, str)
                            and destination.startswith(
                                ".quarantine-" if phase == "quarantine" else ".delete-"
                            )
                            and not failed
                        ):
                            failed.append(destination)
                            raise OSError(errno.EACCES, "simulated rename denial")
                        return real_rename(source, destination, *args, **kwargs)

                    with mock.patch.object(
                        media_ingestion.os,
                        "rename",
                        side_effect=deny_rename,
                    ):
                        removed = service.cleanup_expired(now)

                    self.assertEqual(removed, 0)
                    self.assertEqual(len(failed), 1)
                    with database.connect() as connection:
                        stored = connection.execute(
                            "SELECT status, cache_path, expires_at "
                            "FROM media_assets WHERE id=?",
                            (asset["id"],),
                        ).fetchone()
                    self.assertEqual(stored[0], "ready")
                    retained_path = Path(stored[1])
                    self.assertTrue(retained_path.name.startswith(retained_prefix))
                    self.assertTrue(retained_path.exists())

                    service.close()
                    restarted = self._service(
                        root,
                        FakeOpener([]),
                        repository=E4Repository(database),
                    )
                    next_attempt = now + timedelta(minutes=5)
                    self.assertEqual(restarted.cleanup_expired(next_attempt), 1)
                    self.assertEqual(cache_payload_entries(root), [])
                    with database.connect() as connection:
                        status = connection.execute(
                            "SELECT status FROM media_assets WHERE id=?",
                            (asset["id"],),
                        ).fetchone()[0]
                    self.assertEqual(status, "expired")

    def test_cleanup_restores_registered_name_when_retry_persistence_fails(self):
        from qq_openclaw_bridge.application import media_ingestion

        class RejectingRepository(FakeRepository):
            def record_media_cleanup_error(self, *args, **kwargs):
                raise OSError("database unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            root.mkdir()
            pending = root / "pending.bin"
            pending.write_bytes(b"registered")
            repository = RejectingRepository()
            repository.to_cleanup = [{"id": "pending", "cache_path": str(pending)}]
            service = self._service(root, FakeOpener([]), repository=repository)

            with mock.patch.object(
                media_ingestion.os,
                "unlink",
                side_effect=OSError(errno.EIO, "simulated unlink failure"),
            ):
                with self.assertRaisesRegex(OSError, "database unavailable"):
                    service.cleanup_expired(datetime(2026, 7, 23, tzinfo=timezone.utc))

            self.assertTrue(pending.exists())
            self.assertEqual(cache_payload_entries(root), [pending])

    def test_cleanup_restores_registered_name_when_repository_returns_none(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.domain.media import MediaRejected

        class MissingUpdateRepository(FakeRepository):
            def record_media_cleanup_error(self, *args, **kwargs):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            root.mkdir()
            pending = root / "pending.bin"
            pending.write_bytes(b"registered")
            repository = MissingUpdateRepository()
            repository.to_cleanup = [{"id": "pending", "cache_path": str(pending)}]
            service = self._service(root, FakeOpener([]), repository=repository)

            with mock.patch.object(
                media_ingestion.os,
                "unlink",
                side_effect=OSError(errno.EIO, "simulated unlink failure"),
            ):
                with self.assertRaisesRegex(
                    MediaRejected,
                    "cleanup state was not persisted",
                ):
                    service.cleanup_expired(
                        datetime(2026, 7, 23, tzinfo=timezone.utc)
                    )

            self.assertTrue(pending.exists())
            self.assertEqual(cache_payload_entries(root), [pending])

    def test_cleanup_surfaces_retained_path_when_compensation_rename_fails(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.domain.media import MediaRejected

        class RejectingRepository(FakeRepository):
            def record_media_cleanup_error(self, *args, **kwargs):
                raise OSError("database unavailable")

        class MissingUpdateRepository(FakeRepository):
            def record_media_cleanup_error(self, *args, **kwargs):
                return None

        for repository_type in (RejectingRepository, MissingUpdateRepository):
            with self.subTest(repository=repository_type.__name__):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir) / "cache"
                    root.mkdir()
                    pending = root / "pending.bin"
                    pending.write_bytes(b"registered")
                    repository = repository_type()
                    repository.to_cleanup = [
                        {"id": "pending", "cache_path": str(pending)}
                    ]
                    service = self._service(root, FakeOpener([]), repository=repository)
                    real_rename = os.rename

                    def fail_compensation_rename(source, destination, *args, **kwargs):
                        if (
                            str(source).startswith(".delete-")
                            and str(destination) == "pending.bin"
                        ):
                            raise OSError(errno.EIO, "compensation rename failed")
                        return real_rename(source, destination, *args, **kwargs)

                    with mock.patch.object(
                        media_ingestion.os,
                        "unlink",
                        side_effect=OSError(errno.EIO, "simulated unlink failure"),
                    ):
                        with mock.patch.object(
                            media_ingestion.os,
                            "rename",
                            side_effect=fail_compensation_rename,
                        ):
                            with self.assertRaises(MediaRejected) as caught:
                                service.cleanup_expired(
                                    datetime(2026, 7, 23, tzinfo=timezone.utc)
                                )

                    self.assertIn(str(root / ".delete-"), str(caught.exception))
                    self.assertIsInstance(caught.exception.__cause__, OSError)
                    self.assertEqual(caught.exception.__cause__.errno, errno.EIO)

    def test_cleanup_harden_failure_rolls_back_database_and_restores_delete_name(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.application.media_ingestion import MediaIngestionService
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "cache"
            root.mkdir()
            registered = root / "registered.bin"
            registered.write_bytes(b"registered")
            now = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
            database = SqliteDatabase(base / "state.sqlite3")
            repository = E4Repository(database)
            asset = repository.create_media_asset(
                user_id=1,
                message_id="m-cleanup-harden",
                kind="file",
                display_name="registered.bin",
                status="ready",
                expires_at=now,
                cache_path=str(registered),
            )
            service = MediaIngestionService(
                root,
                repository,
                resolver=public_resolver,
                opener=FakeOpener([]),
                pdf_sandbox=FakePdfSandbox(),
                allowed_remote_hosts=("example.invalid",),
            )
            real_unlink = os.unlink

            def fail_delete_unlink(path, *, dir_fd=None):
                if str(path).startswith(".delete-"):
                    raise OSError(errno.EIO, "simulated unlink failure")
                return real_unlink(path, dir_fd=dir_fd)

            with mock.patch.object(
                media_ingestion.os,
                "unlink",
                side_effect=fail_delete_unlink,
            ):
                with mock.patch.object(
                    database,
                    "harden_files",
                    side_effect=OSError("chmod failed"),
                ):
                    with self.assertRaisesRegex(OSError, "chmod failed"):
                        service.cleanup_expired(now)

            with database.connect() as connection:
                stored = connection.execute(
                    "SELECT status, error_detail, cache_path, expires_at "
                    "FROM media_assets WHERE id=?",
                    (asset["id"],),
                ).fetchone()
            self.assertEqual(
                stored,
                (
                    "ready",
                    None,
                    str(registered),
                    asset["expires_at"],
                ),
            )
            self.assertTrue(registered.exists())
            self.assertEqual(registered.read_bytes(), b"registered")
            self.assertEqual(cache_payload_entries(root), [registered])

    def test_download_total_deadline_stops_slow_drip_closes_and_releases_lock(self):
        from qq_openclaw_bridge.application import media_ingestion
        from qq_openclaw_bridge.domain.media import MediaRejected

        clock = [0.0]

        class SlowDripResponse(FakeResponse):
            def read(self, size=-1):
                self.read_sizes.append(size)
                if self._offset >= len(self._body):
                    return b""
                chunk = self._body[self._offset : self._offset + 2]
                self._offset += len(chunk)
                clock[0] += 1.0
                return chunk

        response = SlowDripResponse(PNG, headers={"Content-Type": "image/png"})
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            service = self._service(
                root,
                FakeOpener([response]),
                download_timeout_seconds=2,
                monotonic_clock=lambda: clock[0],
            )

            with self.assertRaisesRegex(MediaRejected, "media download timed out"):
                service.fetch(image_ref(), 1, "m-slow")

            self.assertTrue(response.closed)
            self.assertEqual(cache_payload_entries(root), [])
            lock_fd = os.open(
                media_ingestion.CACHE_LOCK_NAME,
                os.O_RDWR,
                dir_fd=service._cache_dir_fd,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def test_cleanup_eacces_enters_retry_and_retains_registered_file(self):
        from qq_openclaw_bridge.application import media_ingestion

        repository = FakeRepository()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "cache"
            root.mkdir()
            pending = root / "pending.bin"
            pending.write_bytes(b"registered")
            repository.to_cleanup = [
                {"id": "pending", "cache_path": str(pending)},
            ]
            service = self._service(root, FakeOpener([]), repository=repository)
            real_unlink = os.unlink

            def deny_quarantine_unlink(path, *, dir_fd=None):
                if str(path).startswith((".quarantine-", ".delete-")):
                    raise OSError(errno.EACCES, "denied")
                return real_unlink(path, dir_fd=dir_fd)

            with mock.patch.object(
                media_ingestion.os,
                "unlink",
                side_effect=deny_quarantine_unlink,
            ):
                removed = service.cleanup_expired(
                    datetime(2026, 7, 19, tzinfo=timezone.utc)
                )

            self.assertEqual(removed, 0)
            self.assertIn(
                b"registered",
                [item.read_bytes() for item in cache_payload_entries(root)],
            )
            self.assertEqual(repository.expired, [])
            self.assertEqual(len(repository.cleanup_errors), 1)
            asset_id, detail = repository.cleanup_errors[0]
            self.assertEqual(asset_id, "pending")
            self.assertIn("cache unlink failed (EACCES)", detail)
            retained_path = Path(repository.cleanup_error_calls[0][2]["cache_path"])
            self.assertEqual(detail, "cache unlink failed (EACCES) at %s" % retained_path)
            self.assertTrue(retained_path.name.startswith(".delete-"))
            self.assertTrue(retained_path.exists())
            self.assertEqual(retained_path.read_bytes(), b"registered")

    def test_fetch_times_out_if_resolver_returns_after_deadline_without_opening(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        clock = [0.0]
        resolver_calls = []

        def resolver(host):
            resolver_calls.append(host)
            clock[0] = 2.0
            return ("93.184.216.34",)

        opener = FakeOpener([])
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                Path(temp_dir) / "cache",
                opener,
                resolver=resolver,
                download_timeout_seconds=1,
                monotonic_clock=lambda: clock[0],
            )
            with self.assertRaisesRegex(MediaRejected, "media download timed out"):
                service.fetch(image_ref(), 1, "m-resolver-timeout")

        self.assertEqual(resolver_calls, ["example.invalid"])
        self.assertEqual(opener.urls, [])


if __name__ == "__main__":
    unittest.main()
