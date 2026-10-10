from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def make_pdf(text):
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


def make_docx(text, embedded_macro=False):
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>"
        % text
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", document)
        if embedded_macro:
            archive.writestr("word/vbaProject.bin", b"macro secret text")
            archive.writestr("word/embeddings/oleObject1.bin", b"embedded secret text")
    return output.getvalue()


def make_docx_payload(document_xml, extra_entries=0, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("word/document.xml", document_xml)
        for index in range(extra_entries):
            archive.writestr("word/extra-%04d.xml" % index, b"<x/>")
    return output.getvalue()


class DocumentExtractionTests(unittest.TestCase):
    def _extract(
        self,
        body,
        mime,
        max_chars=1000,
        suffix=".bin",
        pdf_sandbox=None,
    ):
        from qq_openclaw_bridge.application.documents import DocumentTextExtractor
        from qq_openclaw_bridge.domain.media import ValidatedMedia

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cache_root = Path(temp_dir.name).resolve() / "cache"
        cache_root.mkdir()
        path = cache_root / ("document" + suffix)
        path.write_bytes(body)
        media = ValidatedMedia(
            asset_id="asset-doc",
            path=path,
            kind="file",
            mime=mime,
            size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        extractor = DocumentTextExtractor(cache_root, pdf_sandbox=pdf_sandbox)
        self.addCleanup(extractor.close)
        return extractor.extract(media, max_chars)

    def _media(self, path, body, mime="text/plain", **overrides):
        from qq_openclaw_bridge.domain.media import ValidatedMedia

        values = {
            "asset_id": "asset-doc",
            "path": path,
            "kind": "file",
            "mime": mime,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        values.update(overrides)
        return ValidatedMedia(**values)

    def test_txt_and_markdown_decode_utf8_with_replacement(self):
        txt = self._extract(b"hello \xff world", "text/plain", suffix=".txt")
        markdown = self._extract(
            "# 标题\n正文".encode("utf-8"), "text/markdown", suffix=".md"
        )

        self.assertEqual(txt.text, "hello \ufffd world")
        self.assertEqual(markdown.text, "# 标题\n正文")

    def test_json_is_parsed_and_serialized_without_ascii_escaping(self):
        artifact = self._extract(
            b'{"name":"\\u6587\\u6863","items":[1,2]}',
            "application/json",
            suffix=".json",
        )

        self.assertEqual(
            json.loads(artifact.text), {"name": "文档", "items": [1, 2]}
        )
        self.assertIn("文档", artifact.text)
        self.assertNotIn("\\u6587", artifact.text)

    def test_csv_is_normalized_with_standard_csv_parser(self):
        artifact = self._extract(
            'name,note\r\nAlice,"hello, world"\r\n'.encode("utf-8"),
            "text/csv",
            suffix=".csv",
        )

        self.assertEqual(artifact.text, 'name,note\nAlice,"hello, world"\n')

    def test_pdf_extracts_text_through_sandbox(self):
        class Sandbox:
            def __init__(self):
                self.calls = []

            def extract(self, snapshot, max_chars):
                self.calls.append((snapshot, max_chars))
                return "Hello PDF"

        sandbox = Sandbox()
        artifact = self._extract(
            make_pdf("Hello PDF"),
            "application/pdf",
            suffix=".pdf",
            pdf_sandbox=sandbox,
        )

        self.assertEqual(artifact.text, "Hello PDF")
        self.assertEqual(sandbox.calls, [(make_pdf("Hello PDF"), 1000)])

    def test_docx_only_reads_word_document_xml(self):
        artifact = self._extract(
            make_docx("第一段", embedded_macro=True),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            suffix=".docx",
        )

        self.assertEqual(artifact.text, "第一段")
        self.assertNotIn("macro", artifact.text.casefold())
        self.assertNotIn("embedded", artifact.text.casefold())

    def test_text_is_truncated_by_characters(self):
        artifact = self._extract("甲乙丙丁".encode("utf-8"), "text/plain", max_chars=3)

        self.assertEqual(artifact.text, "甲乙丙")
        self.assertTrue(artifact.truncated)

    def test_invalid_json_and_unsupported_mime_are_rejected(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        with self.assertRaisesRegex(MediaRejected, "invalid JSON"):
            self._extract(b"{not-json", "application/json", suffix=".json")
        with self.assertRaisesRegex(MediaRejected, "unsupported document"):
            self._extract(b"data", "application/octet-stream")

    def test_extractor_rejects_outside_symlink_and_hardlink_paths(self):
        from qq_openclaw_bridge.application.documents import DocumentTextExtractor
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            cache_root = root / "cache"
            cache_root.mkdir()
            outside = root / "outside.txt"
            body = b"outside"
            outside.write_bytes(body)
            symlink = cache_root / "symlink.txt"
            symlink.symlink_to(outside)
            hardlink = cache_root / "hardlink.txt"
            os.link(str(outside), str(hardlink))
            extractor = DocumentTextExtractor(cache_root)

            cases = (
                (outside, "trusted cache root"),
                (symlink, "symbolic link"),
                (hardlink, "hard link"),
            )
            for path, message in cases:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(MediaRejected, message):
                        extractor.extract(self._media(path, body), 100)

    def test_extractor_revalidates_expiry_size_and_sha256(self):
        from qq_openclaw_bridge.application.documents import DocumentTextExtractor
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir).resolve() / "cache"
            cache_root.mkdir()
            path = cache_root / "document.txt"
            body = b"trusted body"
            path.write_bytes(body)
            extractor = DocumentTextExtractor(cache_root)
            cases = (
                (
                    self._media(
                        path,
                        body,
                        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    ),
                    "expired",
                ),
                (self._media(path, body, size=len(body) + 1), "size"),
                (self._media(path, body, sha256="0" * 64), "SHA-256"),
            )

            for media, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(MediaRejected, message):
                        extractor.extract(media, 100)

    def test_extractor_parses_immutable_bytes_collected_during_hash_validation(self):
        from qq_openclaw_bridge.application import documents
        from qq_openclaw_bridge.application.documents import DocumentTextExtractor

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir).resolve() / "cache"
            cache_root.mkdir()
            path = cache_root / "document.txt"
            trusted = b"trusted body"
            tampered = b"forged  body"
            path.write_bytes(trusted)
            media = self._media(path, trusted)
            real_sha256 = hashlib.sha256

            class MutatingHasher:
                def __init__(self):
                    self._delegate = real_sha256()

                def update(self, value):
                    self._delegate.update(value)

                def hexdigest(self):
                    path.write_bytes(tampered)
                    return self._delegate.hexdigest()

            with mock.patch.object(documents.hashlib, "sha256", MutatingHasher):
                artifact = DocumentTextExtractor(cache_root).extract(media, 100)

            self.assertEqual(path.read_bytes(), tampered)
            self.assertEqual(artifact.text, trusted.decode("ascii"))

    def test_extractor_rejects_forged_pdf_and_docx_validated_media(self):
        from qq_openclaw_bridge.application.documents import (
            DOCX_MIME,
            DocumentTextExtractor,
        )
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir).resolve() / "cache"
            cache_root.mkdir()
            extractor = DocumentTextExtractor(cache_root)
            cases = (
                ("forged.pdf", b"not a pdf", "application/pdf", "PDF signature"),
                ("forged.docx", b"not a zip", DOCX_MIME, "DOCX"),
            )
            for name, body, mime, message in cases:
                with self.subTest(name=name):
                    path = cache_root / name
                    path.write_bytes(body)
                    with self.assertRaisesRegex(MediaRejected, message):
                        extractor.extract(self._media(path, body, mime=mime), 100)

    def test_document_input_over_20_mib_is_rejected_before_reading(self):
        from qq_openclaw_bridge.application.documents import DocumentTextExtractor
        from qq_openclaw_bridge.domain.media import MediaRejected

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir).resolve() / "cache"
            cache_root.mkdir()
            path = cache_root / "document.txt"
            body = b"small"
            path.write_bytes(body)
            media = self._media(path, body, size=20 * 1024 * 1024 + 1)

            with self.assertRaisesRegex(MediaRejected, "20 MiB"):
                DocumentTextExtractor(cache_root).extract(media, 100)

    def test_pdf_sandbox_controls_character_budget_and_truncated_flag(self):
        class Sandbox:
            def __init__(self):
                self.calls = []

            def extract(self, snapshot, max_chars):
                self.calls.append((snapshot, max_chars))
                return "abcd"

        sandbox = Sandbox()
        body = make_pdf("budget")
        artifact = self._extract(
            body,
            "application/pdf",
            max_chars=3,
            suffix=".pdf",
            pdf_sandbox=sandbox,
        )

        self.assertEqual(sandbox.calls, [(body, 3)])
        self.assertEqual(artifact.text, "abc")
        self.assertTrue(artifact.truncated)

    def test_pdf_truncation_uses_sandbox_effective_character_limit(self):
        from qq_openclaw_bridge.application.pdf_sandbox import MAX_EXTRACT_CHARS

        class Sandbox:
            def extract(self, snapshot, max_chars):
                return "x" * (MAX_EXTRACT_CHARS + 1)

        artifact = self._extract(
            make_pdf("budget"),
            "application/pdf",
            max_chars=MAX_EXTRACT_CHARS + 1000,
            suffix=".pdf",
            pdf_sandbox=Sandbox(),
        )

        self.assertEqual(len(artifact.text), MAX_EXTRACT_CHARS)
        self.assertTrue(artifact.truncated)

    def test_pdf_sandbox_rejection_is_propagated_without_in_process_fallback(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        class Sandbox:
            def extract(self, snapshot, max_chars):
                raise MediaRejected("PDF sandbox rejected document")

        with self.assertRaisesRegex(MediaRejected, "PDF sandbox rejected"):
            self._extract(
                make_pdf("rejected"),
                "application/pdf",
                suffix=".pdf",
                pdf_sandbox=Sandbox(),
            )

    def test_docx_rejects_entry_flood_size_and_compression_ratio(self):
        from qq_openclaw_bridge.application.documents import DOCX_MIME
        from qq_openclaw_bridge.domain.media import MediaRejected

        envelope = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        )
        ending = b"</w:t></w:r></w:p></w:body></w:document>"
        cases = (
            (
                make_docx_payload(envelope + b"ok" + ending, extra_entries=256),
                "entry limit",
            ),
            (
                make_docx_payload(envelope + b"x" * (4 * 1024 * 1024) + ending),
                "too large",
            ),
            (
                make_docx_payload(
                    envelope + b"x" * (256 * 1024) + ending,
                    compression=zipfile.ZIP_DEFLATED,
                ),
                "compression ratio",
            ),
        )

        for index, (body, message) in enumerate(cases):
            with self.subTest(message=message):
                with self.assertRaisesRegex(MediaRejected, message):
                    self._extract(
                        body,
                        DOCX_MIME,
                        max_chars=10,
                        suffix="-%d.docx" % index,
                    )

    def test_docx_rejects_dtd_and_entity_declarations(self):
        from qq_openclaw_bridge.application.documents import DOCX_MIME
        from qq_openclaw_bridge.domain.media import MediaRejected

        document_xml = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE w:document [<!ENTITY payload "expanded">]>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            b"&payload;</w:t></w:r></w:p></w:body></w:document>"
        )

        utf16_xml = document_xml.decode("utf-8").replace(
            'encoding="UTF-8"', 'encoding="UTF-16"'
        ).encode("utf-16")
        utf32_xml = document_xml.decode("utf-8").replace(
            'encoding="UTF-8"', 'encoding="UTF-32"'
        ).encode("utf-32")
        for index, value in enumerate((document_xml, utf16_xml, utf32_xml)):
            with self.subTest(encoding=index):
                with self.assertRaisesRegex(MediaRejected, "DTD or ENTITY"):
                    self._extract(
                        make_docx_payload(value),
                        DOCX_MIME,
                        suffix="-%d.docx" % index,
                    )

    def test_csv_row_limit_and_incremental_json_output_are_enforced(self):
        from qq_openclaw_bridge.application import documents
        from qq_openclaw_bridge.domain.media import MediaRejected

        csv_body = ("value\n" * 10002).encode("utf-8")
        with self.assertRaisesRegex(MediaRejected, "CSV row limit"):
            self._extract(csv_body, "text/csv", max_chars=100000, suffix=".csv")

        with mock.patch.object(
            documents.json,
            "dumps",
            side_effect=AssertionError("monolithic JSON output is forbidden"),
        ):
            artifact = self._extract(
                b'{"items":[1,2,3,4]}',
                "application/json",
                max_chars=5,
                suffix=".json",
            )
        self.assertEqual(len(artifact.text), 5)
        self.assertTrue(artifact.truncated)

    def test_json_has_dedicated_input_and_linear_prescan_budgets(self):
        from qq_openclaw_bridge.domain.media import MediaRejected

        cases = (
            (
                b'"' + b"x" * (2 * 1024 * 1024) + b'"',
                "JSON input limit",
                "input",
            ),
            (
                ("[" * 65 + "0" + "]" * 65).encode("ascii"),
                "nesting depth",
                "depth",
            ),
            (
                ("[" + ",".join("0" for _ in range(100001)) + "]").encode("ascii"),
                "token limit",
                "tokens",
            ),
            (b"1" * 1001, "numeric literal", "number"),
            (b'"' + b"x" * (1024 * 1024 + 1) + b'"', "string length", "string"),
        )
        for body, message, label in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(MediaRejected, message):
                    self._extract(
                        body,
                        "application/json",
                        max_chars=1,
                        suffix="-%s.json" % label,
                    )


if __name__ == "__main__":
    unittest.main()
