import copy
import json
from pathlib import Path
import sys
import tempfile
import traceback
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class CommunityStyleConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _valid_value(self):
        return {
            "enabled": True,
            "ollama": {
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen3:8b",
                "allow_remote": False,
            },
            "sources": [
                {
                    "id": "reddit-example",
                    "platform": "reddit",
                    "community": "example",
                    "profile": "reddit-example",
                    "schedule_seconds": 21600,
                    "authorizations": [
                        {
                            "subject_type": "author",
                            "subject_key": "authorized_user",
                            "scope": "all_posts_until_revoke",
                            "evidence_ref": "local-consent-record-001",
                            "granted_at": "2026-09-19T00:00:00Z",
                        }
                    ],
                }
            ],
            "groups": [
                {
                    "group_id": 123456789,
                    "profile": "reddit-example",
                    "cooldown_seconds": 5,
                    "max_reply_chars": 800,
                }
            ],
        }

    def _write_config(self, value=None, **overrides):
        value = self._valid_value() if value is None else copy.deepcopy(value)
        value.update(overrides)
        path = Path(self.temp_dir.name) / "community-style-agent.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _write_valid_config(self):
        return self._write_config()

    def test_loader_decodes_immutable_settings_and_fingerprint(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        settings = load_community_style_settings(self._write_valid_config())

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.ollama.model, "qwen3:8b")
        self.assertEqual(settings.sources[0].authorizations[0].scope, "all_posts_until_revoke")
        self.assertEqual(settings.groups[0].group_id, 123456789)
        self.assertEqual(len(settings.fingerprint), 64)
        with self.assertRaises(AttributeError):
            settings.enabled = False

    def test_loader_rejects_remote_ollama_without_opt_in(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        path = self._write_config(ollama={
            "base_url": "http://192.0.2.1:11434", "model": "qwen3:8b",
            "allow_remote": False,
        })

        with self.assertRaisesRegex(ValueError, "remote Ollama"):
            load_community_style_settings(path)

    def test_loader_rejects_unknown_keys_and_sensitive_key_names(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for key in ("unknown", "api_token"):
            with self.subTest(key=key):
                value = self._valid_value()
                value["ollama"][key] = "not-a-secret"
                with self.assertRaises(ValueError):
                    load_community_style_settings(self._write_config(value))

    def test_loader_rejects_duplicate_ids_invalid_schedule_and_missing_authorization(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        duplicate = self._valid_value()
        duplicate["sources"].append(copy.deepcopy(duplicate["sources"][0]))
        invalid_schedule = self._valid_value()
        invalid_schedule["sources"][0]["schedule_seconds"] = 299
        missing_authorization = self._valid_value()
        missing_authorization["sources"][0]["authorizations"] = []
        for name, value in (
            ("duplicate", duplicate),
            ("schedule", invalid_schedule),
            ("authorization", missing_authorization),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_community_style_settings(self._write_config(value))

    def test_loader_rejects_group_without_known_profile_and_nonpositive_group_id(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        unknown_profile = self._valid_value()
        unknown_profile["groups"][0]["profile"] = "missing"
        invalid_group_id = self._valid_value()
        invalid_group_id["groups"][0]["group_id"] = 0
        for name, value in (("profile", unknown_profile), ("group_id", invalid_group_id)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_community_style_settings(self._write_config(value))

    def test_invalid_reload_keeps_last_valid_snapshot(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleConfigReloader,
        )

        reloader = CommunityStyleConfigReloader(self._write_valid_config())
        previous = reloader.current()
        reloader.path.write_text('{"enabled":', encoding="utf-8")

        self.assertFalse(reloader.reload_if_changed())
        self.assertIs(reloader.current(), previous)

    def test_valid_changed_config_replaces_snapshot_once(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleConfigReloader,
        )

        path = self._write_valid_config()
        reloader = CommunityStyleConfigReloader(path)
        changed = self._valid_value()
        changed["groups"][0]["cooldown_seconds"] = 6
        path.write_text(json.dumps(changed), encoding="utf-8")

        self.assertTrue(reloader.reload_if_changed())
        self.assertEqual(reloader.current().groups[0].cooldown_seconds, 6)
        self.assertFalse(reloader.reload_if_changed())

    def test_loader_rejects_duplicate_json_fields(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        path = self._write_valid_config()
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace('"enabled": true', '"enabled": false, "enabled": true'), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_community_style_settings(path)

    def test_excessive_nesting_reload_keeps_last_valid_snapshot(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleConfigReloader,
        )

        reloader = CommunityStyleConfigReloader(self._write_valid_config())
        previous = reloader.current()
        reloader.path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")

        self.assertFalse(reloader.reload_if_changed())
        self.assertIs(reloader.current(), previous)

    def test_loader_accepts_loopback_and_explicit_remote_opt_in(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for url, allow_remote in (
            ("http://localhost:11434", False),
            ("http://[::1]:11434", False),
            ("https://192.0.2.1:11434", True),
        ):
            with self.subTest(url=url):
                value = self._valid_value()
                value["ollama"].update(base_url=url, allow_remote=allow_remote)
                settings = load_community_style_settings(self._write_config(value))
                self.assertEqual(settings.ollama.base_url, url)

    def test_loader_rejects_unsafe_urls_even_with_remote_opt_in(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for url in (
            "file:///tmp/ollama", "http://user:pass@localhost:11434",
            "http://localhost:11434/?key=value", "http://localhost:11434/#fragment",
            "http://localhost:11434/api", "http://localhost:99999", "http://[::1",
        ):
            with self.subTest(url=url):
                value = self._valid_value()
                value["ollama"].update(base_url=url, allow_remote=True)
                with self.assertRaises(ValueError):
                    load_community_style_settings(self._write_config(value))

    def test_loader_rejects_each_sensitive_key_in_nested_lists(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for key in ("API_SECRET", "access_token", "password_hint", "cookieJar"):
            with self.subTest(key=key):
                value = self._valid_value()
                value["sources"][0]["authorizations"][0][key] = "private"
                with self.assertRaisesRegex(ValueError, "sensitive"):
                    load_community_style_settings(self._write_config(value))

    def test_loader_rejects_duplicate_profiles_and_groups(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        profiles = self._valid_value()
        second_source = copy.deepcopy(profiles["sources"][0])
        second_source["id"] = "another-source"
        profiles["sources"].append(second_source)
        groups = self._valid_value()
        groups["groups"].append(copy.deepcopy(groups["groups"][0]))
        for name, value in (("profiles", profiles), ("groups", groups)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_community_style_settings(self._write_config(value))

    def test_loader_requires_authorization_evidence_and_supported_scope(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for field, invalid in (
            ("evidence_ref", ""), ("evidence_ref", " " * 10),
            ("scope", "all_public_posts"), ("scope", "single_post"),
            ("subject_key", "x" * 513),
        ):
            with self.subTest(field=field, invalid=invalid):
                value = self._valid_value()
                value["sources"][0]["authorizations"][0][field] = invalid
                with self.assertRaises(ValueError):
                    load_community_style_settings(self._write_config(value))
        value = self._valid_value()
        del value["sources"][0]["authorizations"][0]["evidence_ref"]
        with self.assertRaises(ValueError):
            load_community_style_settings(self._write_config(value))

    def test_loader_enforces_numeric_group_ids_and_schedule_boundary(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for group_id in (True, "123456789", -1, 1.5):
            with self.subTest(group_id=group_id):
                value = self._valid_value()
                value["groups"][0]["group_id"] = group_id
                with self.assertRaises(ValueError):
                    load_community_style_settings(self._write_config(value))
        value = self._valid_value()
        value["sources"][0]["schedule_seconds"] = 300
        settings = load_community_style_settings(self._write_config(value))
        self.assertEqual(settings.sources[0].schedule_seconds, 300)

    def test_invalid_file_reloads_preserve_snapshot_and_allow_recovery(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleConfigReloader,
        )

        reloader = CommunityStyleConfigReloader(self._write_valid_config())
        previous = reloader.current()
        invalid_settings = self._valid_value()
        invalid_settings["sources"][0]["schedule_seconds"] = 299
        for raw in (b"\xff", b" " * (1024 * 1024 + 1), json.dumps(invalid_settings).encode()):
            with self.subTest(length=len(raw)):
                reloader.path.write_bytes(raw)
                self.assertFalse(reloader.reload_if_changed())
                self.assertIs(reloader.current(), previous)
        reloader.path.unlink()
        self.assertFalse(reloader.reload_if_changed())
        self.assertIs(reloader.current(), previous)
        value = self._valid_value()
        value["enabled"] = False
        self._write_config(value)
        self.assertTrue(reloader.reload_if_changed())
        self.assertFalse(reloader.current().enabled)

    def test_invalid_ollama_url_tracebacks_do_not_expose_config_values(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        sentinel = "unique-private-url-74a691"
        for url in (
            "http://" + sentinel + "\uff0fhost:11434",
            "http://localhost:" + sentinel,
        ):
            with self.subTest(url_kind="nfkc" if "\uff0f" in url else "port"):
                value = self._valid_value()
                value["ollama"]["base_url"] = url
                path = self._write_config(value)
                try:
                    load_community_style_settings(path)
                except ValueError as exc:
                    rendered = "".join(traceback.format_exception(
                        type(exc), exc, exc.__traceback__,
                    ))
                    self.assertNotIn(sentinel, rendered)
                else:
                    self.fail("malformed Ollama URL was accepted")

    def test_loader_enforces_collection_count_limits(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for field, maximum in (("sources", 100), ("groups", 1000), ("authorizations", 1000)):
            with self.subTest(field=field):
                value = self._valid_value()
                owner = value["sources"][0] if field == "authorizations" else value
                sample = owner[field][0]
                items = [copy.deepcopy(sample) for _ in range(maximum)]
                if field == "sources":
                    for index, item in enumerate(items[1:], start=1):
                        item["id"] = "source-%d" % index
                        item["profile"] = "profile-%d" % index
                elif field == "groups":
                    for index, item in enumerate(items, start=1):
                        item["group_id"] = index
                owner[field] = items
                settings = load_community_style_settings(self._write_config(value))
                decoded = (
                    settings.sources[0].authorizations
                    if field == "authorizations" else getattr(settings, field)
                )
                self.assertEqual(len(decoded), maximum)
                items.append(copy.deepcopy(sample))
                with self.assertRaisesRegex(ValueError, field + " must be a bounded list"):
                    load_community_style_settings(self._write_config(value))

    def test_nested_settings_are_immutable(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        settings = load_community_style_settings(self._write_valid_config())
        for nested, field in (
            (settings.ollama, "model"),
            (settings.sources[0], "profile"),
            (settings.groups[0], "profile"),
            (settings.sources[0].authorizations[0], "scope"),
        ):
            with self.subTest(type=type(nested).__name__), self.assertRaises(AttributeError):
                setattr(nested, field, "changed")

    def test_allow_remote_requires_a_strict_boolean(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        for invalid in (0, 1, "true", "false", None):
            with self.subTest(invalid=invalid):
                value = self._valid_value()
                value["ollama"]["allow_remote"] = invalid
                with self.assertRaisesRegex(ValueError, "allow_remote must be a boolean"):
                    load_community_style_settings(self._write_config(value))
