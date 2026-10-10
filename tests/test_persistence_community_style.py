from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import traceback
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_openclaw_bridge.domain.community_style import (
    AuthorizedContent, SourceCandidate, DerivedSample, StyleFeatures, StyleProfile, StyleRule,
    aggregate_style_profile,
)
from qq_openclaw_bridge.runtime.community_style_config import (
    AuthorizationSettings, CommunityStyleSettings, GroupBindingSettings,
    OllamaSettings, SourceSettings,
)
from qq_openclaw_bridge.storage import WorkspaceStateStore

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
FEATURES = StyleFeatures("short", "short", ("comma",), "low", "zh", .5,
                         "low", "low", (), ("首先",), ("privacy",))


def settings():
    authorization = AuthorizationSettings("author", "Alice", "all_posts_until_revoke",
                                        "local-consent-001", NOW.isoformat())
    return CommunityStyleSettings(True, OllamaSettings("http://127.0.0.1:11434", "qwen3:8b", False),
        (SourceSettings("s", "reddit", "example", "p", 300, (authorization,)),),
        (GroupBindingSettings(123, "p", 5, 800),))


class CommunityStylePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = WorkspaceStateStore(self.path)

    def configured(self):
        self.assertTrue(hasattr(self.store, "community_style_repository"))
        self.repo = self.store.community_style_repository
        self.config = settings()
        self.repo.sync_configuration(self.config, NOW)
        authorization = self.repo.active_authorizations("s", NOW)[0]
        self.sample = self.sample_for(authorization)

    def sample_for(self, authorization, post="post-1", author="Alice"):
        candidate = SourceCandidate(authorization.source_id, authorization.platform,
            authorization.community_key, post, author, "", "https://example.test/post")
        return DerivedSample.from_content(AuthorizedContent(candidate, "fixture content"), authorization, FEATURES)

    def profile(self, fingerprint="d"):
        for index in range(5):
            self.repo.save_sample(replace(self.sample, external_content_id="profile-%d" % index,
                sample_id="profile-%d" % index, content_fingerprint=fingerprint * 64))
        return aggregate_style_profile("p", self.repo.active_samples("p"))

    def test_v7_migration_is_idempotent_and_preserves_old_data(self):
        self.store.save_approval("approval", 1, "keep", "keep", LATER.isoformat(), NOW.isoformat())
        self.configured()
        self.repo.save_sample(self.sample)
        restarted = WorkspaceStateStore(self.path)
        self.assertEqual(len(restarted.community_style_repository.active_samples("p")), 1)
        self.assertEqual(restarted.get_pending_approval(1)["command"], "keep")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "7")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_configuration_authorization_has_source_binding_and_stable_identity(self):
        self.configured()
        first = self.repo.active_authorizations("s", NOW)[0]
        self.repo.sync_configuration(self.config, LATER)
        self.assertEqual(self.repo.active_authorizations("s", LATER)[0], first)
        self.assertEqual((first.source_id, first.platform, first.community_key, first.subject_key),
                         ("s", "reddit", "example", "alice"))

    def test_replacement_revokes_samples_and_retires_profile(self):
        self.configured()
        self.repo.save_sample(self.sample)
        first = self.repo.publish_profile(self.profile(), NOW)
        source = self.config.sources[0]
        changed = replace(source.authorizations[0], evidence_ref="new-consent")
        affected = self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=(changed,)),)), LATER)
        self.assertIn("p", affected)
        self.assertNotEqual(self.repo.active_authorizations("s", LATER)[0].authorization_id, self.sample.authorization_id)
        self.assertEqual(self.repo.active_samples("p"), ())
        self.assertIsNone(self.repo.active_profile("p"))
        self.assertEqual(self.repo.profile(first["id"])["status"], "retired")
        with self.assertRaises(ValueError):
            self.repo.commit_batch("s", {}, [self.sample], LATER)

    def test_removed_source_revokes_authorization_and_disables_binding(self):
        self.configured()
        self.repo.save_sample(self.sample)
        self.repo.publish_profile(self.profile(), NOW)
        self.repo.sync_configuration(replace(self.config, sources=(), groups=()), LATER)
        self.assertEqual(self.repo.active_authorizations("s", LATER), ())
        self.assertEqual(self.repo.active_samples("p"), ())
        self.assertIsNone(self.repo.active_profile("p"))
        self.assertIsNone(self.repo.group_binding(123))
        self.assertEqual(self.repo.due_sources(LATER, 10), ())

    def test_expiry_and_naive_times_fail_closed(self):
        self.configured()
        source = self.config.sources[0]
        authorization = replace(source.authorizations[0], expires_at=LATER.isoformat())
        self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=(authorization,)),)), NOW)
        self.assertEqual(len(self.repo.active_authorizations("s", NOW)), 1)
        self.assertEqual(self.repo.active_authorizations("s", LATER), ())
        with self.assertRaises(ValueError):
            self.repo.sync_configuration(self.config, NOW.replace(tzinfo=None))

    def test_samples_are_derived_only_and_duplicate_content_is_idempotent(self):
        self.configured()
        self.assertTrue(self.repo.save_sample(self.sample))
        self.assertFalse(self.repo.save_sample(self.sample))
        self.assertEqual(self.repo.active_samples("p"), (self.sample,))
        with sqlite3.connect(self.path) as connection:
            stored = connection.execute("SELECT features_json FROM community_style_samples").fetchone()[0]
            self.assertEqual(stored, json.dumps(json.loads(stored), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(community_style_samples)")}
            self.assertFalse(columns & {"text", "body", "response", "author_key", "canonical_url"})

    def test_real_sqlite_failure_rolls_back_all_samples_and_cursor(self):
        self.configured()
        self.repo.update_source_cursor("s", {"after": "old"}, NOW)
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TRIGGER reject_second BEFORE INSERT ON community_style_samples "
                               "WHEN NEW.external_content_id='post-2' BEGIN SELECT RAISE(ABORT, 'test_failure'); END")
        second = replace(self.sample, sample_id="sample-2", external_content_id="post-2")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.commit_batch("s", {"after": "new"}, [self.sample, second], LATER)
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "old"})
        self.assertEqual(self.repo.active_samples("p"), ())

    def test_successful_batch_advances_cursor_and_schedules_next_attempt(self):
        self.configured()
        self.assertEqual(self.repo.commit_batch("s", {"after": "next"}, [self.sample], NOW), 1)
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "next"})
        self.assertEqual(self.repo.due_sources(NOW, 10), ())
        self.assertEqual(self.repo.due_sources(LATER, 1)[0]["id"], "s")

    def test_claim_source_is_due_enabled_and_cross_instance_atomic(self):
        self.configured()
        second = WorkspaceStateStore(self.path).community_style_repository
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda repo: repo.claim_source("s", NOW), (self.repo, second)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        claimed = next(claim for claim in claims if claim is not None)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(self.repo.source("s")["status"], "running")
        self.assertIsNone(self.repo.claim_source("s", LATER))

        self.repo.commit_batch("s", {}, (), NOW)
        self.assertIsNone(self.repo.claim_source("s", NOW))
        self.assertIsNotNone(self.repo.claim_source("s", LATER))

    def test_retry_and_pause_require_running_source_and_bounded_safe_state(self):
        self.configured()
        unused_token = NOW.isoformat(timespec="microseconds")
        self.assertFalse(self.repo.retry_source("s", unused_token, "temporary", NOW + timedelta(seconds=10), NOW))
        first = self.repo.claim_source("s", NOW)
        self.assertIsNotNone(first)
        self.assertTrue(self.repo.retry_source("s", first["claim_token"], "temporary", NOW + timedelta(seconds=10), NOW))
        state = self.repo.source("s")
        self.assertEqual((state["status"], state["safe_error_class"]), ("retry", "temporary"))
        self.assertEqual(datetime.fromisoformat(state["next_attempt_at"]), NOW + timedelta(seconds=10))

        self.assertIsNone(self.repo.claim_source("s", NOW))
        second = self.repo.claim_source("s", NOW + timedelta(seconds=10))
        self.assertIsNotNone(second)
        self.assertTrue(self.repo.pause_source("s", second["claim_token"], "reddit_auth", NOW + timedelta(seconds=10)))
        state = self.repo.source("s")
        self.assertEqual((state["status"], state["safe_error_class"]), ("paused", "reddit_auth"))
        self.assertIsNone(self.repo.claim_source("s", LATER))

        for error in ("", "secret: payload", "A", "x" * 65, "invented_but_safe"):
            with self.subTest(error=error), self.assertRaises(ValueError):
                self.repo.retry_source("s", second["claim_token"], error, NOW, NOW)
        with self.assertRaises(ValueError):
            self.repo.retry_source("s", second["claim_token"], "temporary", NOW + timedelta(days=2), NOW)
        with self.assertRaises(ValueError):
            self.repo.pause_source("s", second["claim_token"], "secret: payload", NOW)

    def test_retry_and_pause_fail_closed_if_running_source_becomes_corrupt(self):
        self.configured()
        claim = self.repo.claim_source("s", NOW)
        self.assertIsNotNone(claim)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_sources SET cursor_json='not-json' WHERE id='s'")
        self.assertFalse(self.repo.retry_source("s", claim["claim_token"], "temporary", NOW, NOW))
        self.assertFalse(self.repo.pause_source("s", claim["claim_token"], "reddit_schema", NOW))
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT status,safe_error_class FROM community_style_sources WHERE id='s'"
            ).fetchone()
        self.assertEqual(row, ("running", None))

    def test_claim_unknown_disabled_and_corrupt_source_fails_closed(self):
        self.configured()
        self.assertIsNone(self.repo.claim_source("unknown", NOW))
        disabled = replace(self.config, enabled=False)
        self.repo.sync_configuration(disabled, LATER)
        self.assertIsNone(self.repo.claim_source("s", LATER))

        self.repo.sync_configuration(self.config, LATER)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_sources SET cursor_json='not-json' WHERE id='s'")
        self.assertIsNone(self.repo.claim_source("s", LATER))
        with sqlite3.connect(self.path) as connection:
            self.assertNotEqual(connection.execute(
                "SELECT status FROM community_style_sources WHERE id='s'"
            ).fetchone()[0], "running")

    def test_recover_interrupted_sources_only_recovers_stale_running(self):
        self.configured()
        second_settings = replace(self.config, sources=(
            self.config.sources[0],
            replace(self.config.sources[0], id="second", profile="second-profile"),
        ))
        self.repo.sync_configuration(second_settings, NOW)
        self.assertIsNotNone(self.repo.claim_source("s", NOW))
        self.assertIsNotNone(self.repo.claim_source("second", NOW))
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE community_style_sources SET last_attempt_at=? WHERE id='s'",
                ((NOW - timedelta(minutes=10)).isoformat(),),
            )
        recovered = self.repo.recover_interrupted_sources(
            NOW, NOW - timedelta(minutes=5)
        )
        self.assertEqual(recovered, 1)
        self.assertEqual(self.repo.source("s")["status"], "retry")
        self.assertEqual(self.repo.source("s")["safe_error_class"], "interrupted")
        self.assertEqual(self.repo.source("second")["status"], "running")

    def test_claim_generation_rejects_stale_commit_retry_and_pause(self):
        self.configured()
        claim_a = self.repo.claim_source("s", NOW)
        token_a = claim_a["claim_token"]
        recovered_at = NOW + timedelta(minutes=10)
        self.assertEqual(
            self.repo.recover_interrupted_sources(
                recovered_at, recovered_at - timedelta(minutes=5)
            ),
            1,
        )
        claim_b = self.repo.claim_source("s", recovered_at)
        token_b = claim_b["claim_token"]
        self.assertNotEqual(token_a, token_b)

        self.assertIsNone(
            self.repo.commit_claimed_batch(
                "s", token_a, {"after": "stale"}, (self.sample,), recovered_at
            )
        )
        self.assertFalse(
            self.repo.retry_source(
                "s", token_a, "temporary", recovered_at + timedelta(seconds=30), recovered_at
            )
        )
        self.assertFalse(self.repo.pause_source("s", token_a, "reddit_auth", recovered_at))
        state = self.repo.source("s")
        self.assertEqual((state["status"], state["cursor"]), ("running", {}))
        self.assertEqual(state["last_attempt_at"], token_b)
        self.assertEqual(self.repo.active_samples("p"), ())

        self.assertEqual(
            self.repo.commit_claimed_batch(
                "s", token_b, {"after": "fresh"}, (self.sample,), recovered_at
            ),
            1,
        )
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "fresh"})
        self.assertEqual(self.repo.active_samples("p"), (self.sample,))

    def test_claimed_authorizations_require_exact_live_generation_atomically(self):
        self.configured()
        second = WorkspaceStateStore(self.path).community_style_repository
        claim_a = self.repo.claim_source("s", NOW)
        token_a = claim_a["claim_token"]
        self.assertEqual(
            self.repo.claimed_authorizations("s", token_a, NOW),
            self.repo.active_authorizations("s", NOW),
        )

        recovered_at = NOW + timedelta(minutes=10)
        self.assertEqual(
            second.recover_interrupted_sources(
                recovered_at, recovered_at - timedelta(minutes=5)
            ),
            1,
        )
        claim_b = second.claim_source("s", recovered_at)
        token_b = claim_b["claim_token"]
        self.assertIsNone(self.repo.claimed_authorizations("s", token_a, recovered_at))
        self.assertEqual(
            len(second.claimed_authorizations("s", token_b, recovered_at)), 1
        )

        class TextSubclass(str):
            pass

        with self.assertRaises(ValueError):
            self.repo.claimed_authorizations("s", TextSubclass(token_b), recovered_at)

    def test_claim_token_is_strict_builtin_canonical_and_required(self):
        self.configured()
        claim = self.repo.claim_source("s", NOW)
        token = claim["claim_token"]
        self.assertEqual(token, claim["last_attempt_at"])

        class TextSubclass(str):
            pass

        for invalid in (TextSubclass(token), "not-a-time", token + " ", "x" * 1000):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                self.repo.commit_claimed_batch("s", invalid, {}, (), NOW)

    def test_reclaim_at_same_clock_tick_strictly_increments_generation(self):
        self.configured()
        first = self.repo.claim_source("s", NOW)
        self.assertEqual(self.repo.recover_interrupted_sources(NOW, NOW), 1)
        second = self.repo.claim_source("s", NOW)
        self.assertGreater(
            datetime.fromisoformat(second["claim_token"]),
            datetime.fromisoformat(first["claim_token"]),
        )

    def test_publish_is_atomic_and_same_fingerprint_is_idempotent(self):
        self.configured()
        first = self.repo.publish_profile(self.profile(), NOW)
        self.assertEqual(self.repo.publish_profile(self.profile(), LATER)["id"], first["id"])
        next_profile = self.profile("e")
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TRIGGER reject_profile BEFORE INSERT ON community_style_profiles "
                               "WHEN NEW.source_fingerprint='" + next_profile.source_fingerprint + "' BEGIN SELECT RAISE(ABORT, 'test_failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.publish_profile(next_profile, LATER)
        self.assertEqual(self.repo.active_profile("p")["id"], first["id"])
        second = self.repo.publish_profile(self.profile("f"), LATER)
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.repo.profile(first["id"])["status"], "retired")

    def test_dynamic_and_pinned_group_binding(self):
        self.configured()
        first = self.repo.publish_profile(self.profile(), NOW)
        second = self.repo.publish_profile(self.profile("e"), LATER)
        binding = self.repo.group_binding(123)
        self.assertIsNone(binding["profile_version"])
        self.assertEqual(self.repo.bound_profile(binding)["id"], second["id"])
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE group_style_bindings SET profile_version=1 WHERE group_id=123")
        pinned = self.repo.group_binding(123)
        self.assertEqual(self.repo.bound_profile(pinned)["id"], first["id"])
        self.assertIsNone(self.repo.bound_profile(dict(binding, enabled=False)))
        self.assertIsNone(self.repo.bound_profile(dict(binding, profile_version=99)))

    def test_receipts_are_idempotent_terminal_and_group_isolated(self):
        self.configured()
        token = self.repo.claim_group_message(123, "raw-inbound-id", NOW)
        self.assertIs(type(token), str)
        self.assertIsNone(self.repo.claim_group_message(123, "raw-inbound-id", NOW))
        self.assertTrue(self.repo.complete_group_message(
            123, "raw-inbound-id", token, "101", LATER
        ))
        self.assertFalse(self.repo.fail_group_message(
            123, "raw-inbound-id", token, "timeout", LATER
        ))
        self.assertTrue(self.repo.is_bot_message(123, "101"))
        self.assertFalse(self.repo.is_bot_message(124, "101"))
        other = self.repo.claim_group_message(124, "raw-inbound-id", LATER)
        self.assertIs(type(other), str)
        self.assertTrue(self.repo.fail_group_message(
            124, "raw-inbound-id", other, "timeout", LATER
        ))
        self.assertIsNone(self.repo.claim_group_message(124, "raw-inbound-id", LATER))
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT inbound_message_sha256 FROM group_message_receipts").fetchall()
            self.assertTrue(all(len(row[0]) == 64 and row[0] != "raw-inbound-id" for row in rows))
        with self.assertRaises(ValueError):
            self.repo.fail_group_message(
                124, "raw-inbound-id", other, "secret: full exception", LATER
            )

    def test_failed_receipt_reclaim_rejects_stale_worker_generation(self):
        self.configured()
        repository_b = WorkspaceStateStore(
            self.path
        ).community_style_repository
        first = self.repo.claim_group_message(123, "lease-race", NOW)
        self.assertIs(type(first), str)
        self.assertTrue(self.repo.fail_group_message(
            123, "lease-race", first, "timeout", NOW
        ))

        reclaimed_at = NOW + timedelta(minutes=6)
        second = repository_b.claim_group_message(
            123, "lease-race", reclaimed_at, lease_seconds=300
        )
        self.assertIs(type(second), str)
        self.assertNotEqual(second, first)
        self.assertFalse(self.repo.complete_group_message(
            123, "lease-race", first, "901", reclaimed_at
        ))
        self.assertFalse(self.repo.fail_group_message(
            123, "lease-race", first, "timeout", reclaimed_at
        ))
        self.assertTrue(repository_b.complete_group_message(
            123, "lease-race", second, "902", reclaimed_at
        ))
        self.assertFalse(self.repo.is_bot_message(123, "901"))
        self.assertTrue(self.repo.is_bot_message(123, "902"))

    def test_processing_receipt_is_a_permanent_tombstone_even_after_lease(self):
        self.configured()
        first = self.repo.claim_group_message(123, "unknown-send", NOW)
        self.assertIs(type(first), str)

        self.assertIsNone(self.repo.claim_group_message(
            123, "unknown-send", NOW + timedelta(days=30), lease_seconds=1
        ))

    def test_failed_receipt_retries_once_and_sent_receipt_is_terminal(self):
        self.configured()
        first = self.repo.claim_group_message(123, "retry-once", NOW)
        self.assertTrue(self.repo.fail_group_message(
            123, "retry-once", first, "timeout", NOW
        ))
        retry_at = NOW + timedelta(minutes=6)
        second = self.repo.claim_group_message(
            123, "retry-once", retry_at, lease_seconds=300
        )
        self.assertIs(type(second), str)
        self.assertNotEqual(second, first)
        self.assertTrue(self.repo.complete_group_message(
            123, "retry-once", second, "903", retry_at
        ))
        self.assertIsNone(self.repo.claim_group_message(
            123, "retry-once", retry_at + timedelta(days=1), lease_seconds=1
        ))

    def test_receipt_claim_tokens_are_exact_canonical_and_bounded(self):
        self.configured()
        token = self.repo.claim_group_message(123, "token-contract", NOW)
        self.assertEqual(token, NOW.isoformat(timespec="microseconds"))

        class TextSubclass(str):
            pass

        for invalid in (TextSubclass(token), token + " ", "not-a-time", "x" * 1000):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                self.repo.complete_group_message(
                    123, "token-contract", invalid, "904", LATER
                )

    def test_receipt_failure_class_uses_fixed_delivery_allowlist(self):
        self.configured()
        token = self.repo.claim_group_message(123, "safe-error", NOW)

        for invalid in ("secret_api_key", "persistence_failed", "Timeout"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.repo.fail_group_message(
                    123, "safe-error", token, invalid, NOW
                )

    def test_cursor_and_malformed_persisted_json_fail_closed(self):
        self.configured()
        for cursor in ({"cookie": "secret"}, {"after": "x" * 513}, {"after": 1}):
            with self.assertRaises(ValueError):
                self.repo.update_source_cursor("s", cursor, NOW)
        self.repo.save_sample(self.sample)
        first = self.repo.publish_profile(self.profile(), NOW)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_sources SET cursor_json=?", ('{"after":"x","after":"y"}',))
            connection.execute("UPDATE community_style_samples SET features_json=?", ('{"text":"secret"}',))
            connection.execute("UPDATE community_style_profiles SET rules_json=?", ('{"body":"secret"}',))
        self.assertEqual(self.repo.active_samples("p"), ())
        self.assertIsNone(self.repo.active_profile("p"))
        self.assertIsNone(self.repo.profile(first["id"]))
        self.assertIsNone(self.repo.source("s"))
        self.assertEqual(self.repo.due_sources(NOW, 10), ())

    def test_status_redacts_subject_evidence_cursor_hashes_and_model(self):
        self.configured()
        self.repo.save_sample(self.sample)
        self.repo.update_source_cursor("s", {"after": "opaque-cursor"}, NOW)
        snapshot = self.repo.status_snapshot()
        encoded = json.dumps(snapshot)
        for sensitive in ("alice", "local-consent-001", "opaque-cursor", "a" * 64, "qwen3", "_json", "fingerprint"):
            self.assertNotIn(sensitive, encoded)
        self.assertEqual(snapshot["samples"]["active"], 1)

    def test_disabled_configuration_is_inert(self):
        self.configured()
        self.repo.sync_configuration(replace(self.config, enabled=False), LATER)
        self.assertEqual(self.repo.due_sources(LATER, 10), ())
        self.assertEqual(self.repo.active_authorizations("s", LATER), ())
        self.assertIsNone(self.repo.group_binding(123))

    def test_new_content_version_replaces_old_active_sample(self):
        self.configured()
        self.repo.save_sample(self.sample)
        updated = replace(self.sample, content_fingerprint="e" * 64)
        self.assertTrue(self.repo.save_sample(updated))
        self.assertEqual(self.repo.active_samples("p"), (updated,))

    def test_regrant_requires_fresh_derived_write_and_can_replace_removed_sample(self):
        self.configured()
        self.repo.save_sample(self.sample)
        source = self.config.sources[0]
        self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=()),)), LATER)
        self.repo.sync_configuration(self.config, LATER)
        self.assertEqual(self.repo.active_samples("p"), ())
        self.assertTrue(self.repo.save_sample(self.sample))
        self.assertEqual(self.repo.active_samples("p"), (self.sample,))

    def test_revocation_invalidates_pinned_history_and_rejects_republication(self):
        self.configured()
        self.repo.save_sample(self.sample)
        candidate = self.profile()
        first = self.repo.publish_profile(candidate, NOW)
        self.repo.publish_profile(self.profile("e"), LATER)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE group_style_bindings SET profile_version=? WHERE group_id=123", (first["version"],))
        binding = self.repo.group_binding(123)
        source = self.config.sources[0]
        self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=()),)), LATER)
        self.assertIsNone(self.repo.bound_profile(binding))
        with self.assertRaises(ValueError):
            self.repo.publish_profile(candidate, LATER)

    def test_concurrent_identical_publish_uses_one_active_version(self):
        self.configured()
        second = WorkspaceStateStore(self.path).community_style_repository
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(repo.publish_profile, self.profile(), NOW) for repo in (self.repo, second)]
            results = [future.result() for future in futures]
        self.assertEqual(results[0]["id"], results[1]["id"])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM community_style_profiles WHERE status='active'").fetchone()[0], 1)

    def test_same_fingerprint_with_different_rules_rejected(self):
        self.configured()
        first = self.repo.publish_profile(self.profile(), NOW)
        with self.assertRaises(ValueError):
            self.repo.publish_profile(replace(self.profile(), rules=(StyleRule("directness", "0.10", 3, 1),)), LATER)
        self.assertEqual(self.repo.active_profile("p")["id"], first["id"])

    def test_due_sources_are_deterministic_bounded_and_no_evidence_or_secrets(self):
        self.configured()
        source = replace(self.config.sources[0], id="a", profile="a-profile")
        self.repo.sync_configuration(replace(self.config, sources=self.config.sources + (source,)), NOW)
        self.assertEqual([row["id"] for row in self.repo.due_sources(NOW, 1)], ["a"])
        for limit in (0, -1, True, 101):
            with self.assertRaises(ValueError):
                self.repo.due_sources(NOW, limit)
        encoded = json.dumps(self.repo.due_sources(NOW, 100))
        self.assertNotIn("local-consent", encoded)
        self.assertNotIn("subject", encoded)
        self.assertNotIn("qwen3", encoded)

    def test_time_offsets_normalize_and_future_grants_are_not_active(self):
        self.configured()
        offset_now = NOW.astimezone(timezone(timedelta(hours=8)))
        self.repo.update_source_cursor("s", {}, offset_now)
        self.assertEqual(datetime.fromisoformat(self.repo.source("s")["updated_at"]), NOW)
        source = self.config.sources[0]
        authorization = replace(source.authorizations[0], granted_at=LATER.isoformat())
        self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=(authorization,)),)), NOW)
        self.assertEqual(self.repo.active_authorizations("s", NOW), ())
        for invalid in ("not-a-date", "2026-09-19T00:00:00"):
            with self.assertRaises(ValueError):
                self.repo.sync_configuration(replace(self.config, sources=(replace(source,
                    authorizations=(replace(authorization, granted_at=invalid),)),)), NOW)
        ambiguous = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        with self.assertRaises(ValueError):
            self.repo.due_sources(ambiguous, 10)

    def test_source_identity_change_resets_cursor_and_retires_old_profile(self):
        self.configured()
        self.repo.save_sample(self.sample)
        self.repo.publish_profile(self.profile(), NOW)
        self.repo.update_source_cursor("s", {"after": "old"}, NOW)
        source = replace(self.config.sources[0], community="different", profile="new-profile")
        self.repo.sync_configuration(replace(self.config, sources=(source,), groups=()), LATER)
        self.assertEqual(self.repo.source("s")["cursor"], {})
        self.assertEqual(self.repo.active_samples("p"), ())
        self.assertIsNone(self.repo.active_profile("p"))
        self.assertNotEqual(self.repo.active_authorizations("s", LATER)[0].authorization_id, self.sample.authorization_id)

    def test_invalid_sample_source_rolls_back_batch(self):
        self.configured()
        wrong = replace(self.sample, source_id="other")
        with self.assertRaises(ValueError):
            self.repo.commit_batch("s", {"after": "next"}, [self.sample, wrong], NOW)
        self.assertEqual(self.repo.active_samples("p"), ())
        self.assertEqual(self.repo.source("s")["cursor"], {})

    def test_oversized_and_nonfinite_json_fail_closed(self):
        self.configured()
        self.repo.save_sample(self.sample)
        profile = self.repo.publish_profile(self.profile(), NOW)
        for raw in ('[NaN]', '[' * 1000 + ']' * 1000, ' ' * 65537, '{"unknown":1}'):
            with self.subTest(raw=raw[:20]), sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE community_style_profiles SET rules_json=?", (raw,))
                connection.execute("UPDATE community_style_samples SET features_json=?", (raw,))
            self.assertIsNone(self.repo.profile(profile["id"]))
            self.assertEqual(self.repo.active_samples("p"), ())

    def test_schema_migration_failure_rolls_back_ddl(self):
        from qq_openclaw_bridge.persistence.schema import migrate_to_v7
        with sqlite3.connect(Path(self.temp.name) / "migration.sqlite3") as connection:
            connection.execute("CREATE TABLE legacy (value TEXT)")
            connection.execute("INSERT INTO legacy VALUES ('preserved')")
            connection.commit()
            def deny_last_table(action, name, *unused):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == "group_message_receipts" else sqlite3.SQLITE_OK
            connection.set_authorizer(deny_last_table)
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_to_v7(connection)
            connection.rollback()
            connection.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
            self.assertEqual(connection.execute("SELECT value FROM legacy").fetchone()[0], "preserved")
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE name='community_style_sources'").fetchall(), [])

    def test_fresh_reauthorized_samples_allow_new_version_of_same_derived_profile(self):
        self.configured()
        profile = self.profile()
        samples = self.repo.active_samples("p")
        first = self.repo.publish_profile(profile, NOW)
        self.repo.sync_configuration(replace(self.config, sources=(), groups=()), LATER)
        self.repo.sync_configuration(self.config, LATER)
        with self.assertRaises(ValueError):
            self.repo.publish_profile(profile, LATER)
        for sample in samples:
            self.repo.save_sample(sample)
        second = self.repo.publish_profile(profile, LATER)
        self.assertEqual(second["version"], first["version"] + 1)
        self.assertEqual(self.repo.profile(first["id"])["status"], "retired")

    def test_unknown_feature_schema_fails_entire_profile_closed(self):
        self.configured()
        self.repo.save_sample(self.sample)
        self.repo.save_sample(replace(self.sample, sample_id="sample-2", external_content_id="post-2"))
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE community_style_samples SET feature_schema_version=2 WHERE external_content_id='post-2'")
        self.assertEqual(self.repo.active_samples("p"), ())

    def test_sample_records_configured_model_across_repository_restart(self):
        self.configured()
        restarted = WorkspaceStateStore(self.path).community_style_repository
        restarted.save_sample(self.sample)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT model FROM community_style_samples").fetchone()[0], "qwen3:8b")

    def test_unpublished_candidate_cannot_race_authorization_withdrawal(self):
        self.configured()
        candidate = self.profile()
        self.assertEqual(candidate.sample_count, 5)
        # A separate real database proves this exact candidate is publishable before withdrawal.
        control = WorkspaceStateStore(Path(self.temp.name) / "control.sqlite3").community_style_repository
        control.sync_configuration(self.config, NOW)
        control.commit_batch("s", {}, self.repo.active_samples("p"), NOW)
        self.assertEqual(control.publish_profile(candidate, NOW)["sample_count"], 5)
        self.repo.sync_configuration(replace(self.config, sources=(), groups=()), LATER)
        with self.assertRaises(ValueError):
            self.repo.publish_profile(candidate, LATER)
        self.assertIsNone(self.repo.active_profile("p"))

    def test_review_historical_grant_revocation_invalidates_old_pin(self):
        self.configured()
        source = self.config.sources[0]
        post_grant = AuthorizationSettings("post", "profile-0", "single_post", "post-consent", NOW.isoformat())
        both = replace(self.config, sources=(replace(source, authorizations=source.authorizations + (post_grant,)),))
        self.repo.sync_configuration(both, NOW)
        candidate = self.profile()
        old = self.repo.publish_profile(candidate, NOW)
        post_auth = next(a for a in self.repo.active_authorizations("s", NOW) if a.subject_type == "post")
        self.repo.save_sample(replace(self.sample_for(post_auth, "profile-0"), content_fingerprint="f" * 64))
        # Only an already-removed author contribution remains for this old profile.
        for index in range(1, 5):
            grant = replace(post_grant, subject_key="profile-%d" % index)
            both = replace(both, sources=(replace(both.sources[0], authorizations=both.sources[0].authorizations + (grant,)),))
            self.repo.sync_configuration(both, NOW)
            auth = next(a for a in self.repo.active_authorizations("s", NOW) if a.subject_key == grant.subject_key)
            self.repo.save_sample(replace(self.sample_for(auth, grant.subject_key), content_fingerprint="f" * 64))
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE group_style_bindings SET profile_version=? WHERE group_id=123", (old["version"],))
        pin = self.repo.group_binding(123)
        posts_only = replace(both.sources[0], authorizations=tuple(g for g in both.sources[0].authorizations if g.subject_type == "post"))
        self.repo.sync_configuration(replace(both, sources=(posts_only,)), LATER)
        self.assertIsNone(self.repo.bound_profile(pin))
        self.assertIsNotNone(self.repo.profile(old["id"])["invalidated_at"])

    def test_review_every_publish_requires_five_current_samples_and_exact_candidate(self):
        self.configured()
        fabricated = StyleProfile("p", 0, "candidate", (), 0, 0, "a" * 64)
        for candidate in (fabricated, replace(fabricated, profile_key="unknown")):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.repo.publish_profile(candidate, NOW)
        self.repo.save_sample(self.sample)
        with self.assertRaises(ValueError):
            self.repo.publish_profile(aggregate_style_profile("p", [self.sample], minimum_samples=1), NOW)
        valid = self.profile()
        for forged in (replace(valid, source_fingerprint="a" * 64), replace(valid, rules=()),
                       replace(valid, author_count=10), replace(valid, sample_count=99)):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                self.repo.publish_profile(forged, NOW)
        self.repo.publish_profile(valid, NOW)

    def test_review_cursor_snapshots_actual_items_and_sanitizes_mapping_exceptions(self):
        self.configured()
        class Misleading(Mapping):
            def __iter__(self): return iter(("after",))
            def __len__(self): return 1
            def __getitem__(self, key): return "safe"
            def items(self): return iter((("cookie", "secret"),))
        class Duplicate(Misleading):
            def items(self): return iter((("after", "first"), ("after", "second")))
        class Broken(Misleading):
            def items(self): raise RuntimeError(SENTINEL)
        for cursor in (Misleading(), Duplicate(), Broken()):
            try:
                self.repo.update_source_cursor("s", cursor, NOW)
            except ValueError:
                self.assertNotIn(SENTINEL, traceback.format_exc())
            else:
                self.fail("unsafe cursor accepted")
        self.assertEqual(self.repo.source("s")["cursor"], {})

    def test_review_bound_profile_rechecks_current_binding_and_rejects_forgery(self):
        self.configured()
        self.repo.publish_profile(self.profile(), NOW)
        binding = self.repo.group_binding(123)
        for forged in ({"enabled": True}, dict(binding, group_id=999), dict(binding, profile_version=1),
                       dict(binding, group_id=True), dict(binding, unknown="value")):
            with self.subTest(binding=forged):
                self.assertIsNone(self.repo.bound_profile(forged))
        self.repo.sync_configuration(replace(self.config, groups=()), LATER)
        self.assertIsNone(self.repo.bound_profile(binding))

    def test_review_removed_source_resync_does_not_invalidate_replacement(self):
        self.configured()
        self.repo.publish_profile(self.profile(), NOW)
        replacement = replace(self.config.sources[0], id="new-source")
        changed = replace(self.config, sources=(replacement,))
        self.repo.sync_configuration(changed, LATER)
        auth = self.repo.active_authorizations("new-source", LATER)[0]
        self.sample = self.sample_for(auth)
        new = self.repo.publish_profile(self.profile(), LATER)
        self.repo.sync_configuration(changed, LATER)
        self.assertEqual(self.repo.active_profile("p")["id"], new["id"])

    def test_review_nfc_settings_match_domain_samples_and_normalized_duplicates_rejected(self):
        self.configured()
        decomposed, normalized = "cafe\u0301", "café"
        source = replace(self.config.sources[0], id=decomposed, community=decomposed, profile=decomposed)
        config = replace(self.config, sources=(source,), groups=(replace(self.config.groups[0], profile=decomposed),))
        self.repo.sync_configuration(config, NOW)
        auth = self.repo.active_authorizations(normalized, NOW)[0]
        self.assertTrue(self.repo.save_sample(self.sample_for(auth)))
        self.assertEqual(len(self.repo.active_samples(normalized)), 1)
        duplicate = replace(source, id=normalized, profile="other")
        with self.assertRaises(ValueError):
            self.repo.sync_configuration(replace(config, sources=(source, duplicate)), NOW)
        for bad in ("source\u200b", " source"):
            with self.assertRaises(ValueError):
                self.repo.sync_configuration(replace(config, sources=(replace(source, id=bad),)), NOW)

    def test_review_batch_actual_iteration_is_bounded_and_exception_is_safe(self):
        self.configured()
        sample = self.sample
        class Dishonest(Sequence):
            def __len__(self): return 1
            def __getitem__(self, index):
                if index >= 101: raise IndexError
                return replace(sample, external_content_id="post-%d" % index)
        class Broken(Dishonest):
            def __getitem__(self, index):
                if index: raise RuntimeError(SENTINEL)
                return sample
        for batch in (Dishonest(), Broken()):
            try:
                self.repo.commit_batch("s", {"after": "unsafe"}, batch, NOW)
            except ValueError:
                self.assertNotIn(SENTINEL, traceback.format_exc())
            else:
                self.fail("unsafe batch accepted")
        self.assertEqual(self.repo.source("s")["cursor"], {})
        self.assertEqual(self.repo.active_samples("p"), ())

    def test_review_authorization_subject_enforced_and_corrupt_links_fail_closed(self):
        self.configured()
        with self.assertRaises(ValueError):
            self.repo.save_sample(replace(self.sample, author_fingerprint="a" * 64))
        self.repo.save_sample(self.sample)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE style_authorizations SET status='revoked'")
        self.assertEqual(self.repo.active_samples("p"), ())
        grant = AuthorizationSettings("post", "allowed-post", "single_post", "consent", NOW.isoformat())
        source = replace(self.config.sources[0], authorizations=(grant,))
        self.repo.sync_configuration(replace(self.config, sources=(source,)), NOW)
        auth = self.repo.active_authorizations("s", NOW)[0]
        with self.assertRaises(ValueError):
            self.repo.save_sample(self.sample_for(auth, "other-post"))
        self.assertTrue(self.repo.save_sample(self.sample_for(auth, "allowed-post")))

    def test_review_corrupt_source_metadata_and_overflow_features_fail_closed(self):
        self.configured()
        for field, value in (("safe_error_class", SENTINEL), ("community_key", "x" * 10000),
                             ("last_attempt_at", "not-time"), ("schedule_seconds", "not-int")):
            with self.subTest(field=field):
                self.repo.sync_configuration(self.config, NOW)
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE community_style_sources SET " + field + "=?", (value,))
                self.assertIsNone(self.repo.source("s"))
                self.assertEqual(self.repo.due_sources(NOW, 10), ())
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE community_style_sources SET safe_error_class=NULL,last_attempt_at=NULL,schedule_seconds=300,community_key='example'")
        self.repo.save_sample(self.sample)
        with sqlite3.connect(self.path) as connection:
            payload = json.loads(connection.execute("SELECT features_json FROM community_style_samples").fetchone()[0])
            payload["directness"] = 10**1000
            connection.execute("UPDATE community_style_samples SET features_json=?", (json.dumps(payload),))
        self.assertEqual(self.repo.active_samples("p"), ())

    def test_review_authorization_and_schedule_preserve_microsecond_boundaries(self):
        self.configured()
        boundary = NOW + timedelta(microseconds=900000)
        source = self.config.sources[0]
        grant = replace(source.authorizations[0], granted_at=boundary.isoformat(), expires_at=(boundary + timedelta(seconds=1)).isoformat())
        self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=(grant,)),)), NOW)
        self.assertEqual(self.repo.active_authorizations("s", boundary - timedelta(microseconds=1)), ())
        self.assertEqual(len(self.repo.active_authorizations("s", boundary)), 1)
        auth = self.repo.active_authorizations("s", boundary)[0]
        self.repo.commit_batch("s", {}, [self.sample_for(auth)], boundary)
        due_at = boundary + timedelta(seconds=300)
        self.assertEqual(self.repo.due_sources(due_at - timedelta(microseconds=1), 1), ())
        self.assertEqual(len(self.repo.due_sources(due_at, 1)), 1)

    def test_review_revocation_below_minimum_cannot_publish_remaining_four(self):
        self.configured()
        source = self.config.sources[0]
        bob = replace(source.authorizations[0], subject_key="Bob", evidence_ref="bob-consent")
        self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=source.authorizations + (bob,)),)), NOW)
        auths = {a.subject_key: a for a in self.repo.active_authorizations("s", NOW)}
        for index in range(5):
            author = "alice" if index < 4 else "bob"
            self.repo.save_sample(self.sample_for(auths[author], "post-%d" % index, author))
        self.repo.publish_profile(aggregate_style_profile("p", self.repo.active_samples("p")), NOW)
        self.repo.sync_configuration(self.config, LATER)
        four = self.repo.active_samples("p")
        self.assertEqual(len(four), 4)
        with self.assertRaises(ValueError):
            self.repo.publish_profile(aggregate_style_profile("p", four, minimum_samples=1), LATER)
        self.assertIsNone(self.repo.active_profile("p"))

    def test_review_any_invalid_sample_link_invalidates_entire_read(self):
        self.configured()
        self.repo.save_sample(self.sample)
        self.repo.save_sample(replace(self.sample, external_content_id="post-2"))
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_samples SET authorization_id='missing' WHERE external_content_id='post-2'")
        self.assertEqual(self.repo.active_samples("p"), ())
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_samples SET authorization_id=?", (self.sample.authorization_id,))
            connection.execute("UPDATE community_style_sources SET profile_key='other-profile'")
        self.assertEqual(self.repo.active_samples("p"), ())

    def test_review_one_corrupt_due_source_suppresses_entire_due_result(self):
        self.configured()
        other = replace(self.config.sources[0], id="other", profile="other-profile")
        self.repo.sync_configuration(replace(self.config, sources=self.config.sources + (other,)), NOW)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_sources SET safe_error_class=? WHERE id='s'", (SENTINEL,))
        self.assertEqual(self.repo.due_sources(NOW, 10), ())

    def test_review_infinite_batch_and_cursor_iterators_stop_at_hard_bounds(self):
        self.configured()
        sample = self.sample
        class Endless(Sequence):
            calls = 0
            def __len__(self): raise RuntimeError(SENTINEL)
            def __getitem__(self, index):
                self.calls += 1
                if self.calls > 101: raise AssertionError("unbounded iteration")
                return sample
        batch = Endless()
        with self.assertRaises(ValueError):
            self.repo.commit_batch("s", {}, batch, NOW)
        self.assertEqual(batch.calls, 101)
        class EndlessMapping(Mapping):
            calls = 0
            def __len__(self): raise RuntimeError(SENTINEL)
            def __iter__(self): raise RuntimeError(SENTINEL)
            def __getitem__(self, key): raise RuntimeError(SENTINEL)
            def items(self):
                while True:
                    self.calls += 1
                    if self.calls > 3: raise AssertionError("unbounded iteration")
                    yield ("after", "value")
        cursor = EndlessMapping()
        with self.assertRaises(ValueError):
            self.repo.update_source_cursor("s", cursor, NOW)
        self.assertEqual(cursor.calls, 3)
        self.assertEqual(self.repo.active_samples("p"), ())

    def test_review_microsecond_expiry_before_and_at_boundary(self):
        self.configured()
        source = self.config.sources[0]
        expires = NOW + timedelta(microseconds=900000)
        grant = replace(source.authorizations[0], expires_at=expires.isoformat())
        self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=(grant,)),)), NOW)
        self.assertEqual(len(self.repo.active_authorizations("s", expires - timedelta(microseconds=1))), 1)
        self.assertEqual(self.repo.active_authorizations("s", expires), ())

    def test_review_nfc_post_and_group_identifiers_are_shared_with_domain(self):
        self.configured()
        grant = AuthorizationSettings("post", "cafe\u0301", "single_post", "evidence", NOW.isoformat())
        source = replace(self.config.sources[0], profile="cafe\u0301", authorizations=(grant,))
        group = replace(self.config.groups[0], profile="café")
        self.repo.sync_configuration(replace(self.config, sources=(source,), groups=(group,)), NOW)
        auth = self.repo.active_authorizations("s", NOW)[0]
        self.assertEqual(auth.subject_key, "café")
        self.assertTrue(self.repo.save_sample(self.sample_for(auth, "cafe\u0301")))
        self.assertEqual(self.repo.group_binding(123)["profile_key"], "café")

    def test_review2_profile_metadata_is_validated_and_never_echoed(self):
        self.configured()
        published = self.repo.publish_profile(self.profile(), NOW)
        binding = self.repo.group_binding(123)
        oversized = SENTINEL * 10000
        fields = ("profile_key", "version", "status", "sample_count", "author_count",
                  "source_fingerprint", "created_at", "activated_at", "invalidated_at")
        with sqlite3.connect(self.path) as connection:
            original = connection.execute("SELECT " + ",".join(fields) + " FROM community_style_profiles").fetchone()
        for field, old in zip(fields, original):
            with self.subTest(field=field):
                with sqlite3.connect(self.path) as connection:
                    connection.execute("PRAGMA ignore_check_constraints=ON")
                    connection.execute("UPDATE community_style_profiles SET " + field + "=?", (oversized,))
                try:
                    self.assertTrue(self.repo.profile(published["id"]) is None, "corrupt profile metadata returned")
                    self.assertTrue(self.repo.active_profile("p") is None, "corrupt active metadata returned")
                    self.assertTrue(self.repo.bound_profile(binding) is None, "corrupt bound metadata returned")
                finally:
                    with sqlite3.connect(self.path) as connection:
                        connection.execute("UPDATE community_style_profiles SET " + field + "=?", (old,))
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_profiles SET id=-1")
        self.assertIsNone(self.repo.active_profile("p"))
        self.assertIsNone(self.repo.bound_profile(binding))

    def test_review2_nul_suffix_never_turns_bad_json_into_valid_prefix(self):
        self.configured()
        published = self.repo.publish_profile(self.profile(), NOW)
        targets = (("community_style_sources", "cursor_json"),
                   ("community_style_profiles", "rules_json"),
                   ("community_style_samples", "features_json"))
        for table, field in targets:
            with self.subTest(table=table):
                with sqlite3.connect(self.path) as connection:
                    raw = connection.execute("SELECT " + field + " FROM " + table + " LIMIT 1").fetchone()[0]
                    connection.execute("UPDATE " + table + " SET " + field + "=?", (raw + "\x00" + SENTINEL,))
                if table == "community_style_sources":
                    self.assertIsNone(self.repo.source("s"))
                    self.assertEqual(self.repo.due_sources(NOW, 10), ())
                elif table == "community_style_profiles":
                    self.assertIsNone(self.repo.profile(published["id"]))
                    self.assertIsNone(self.repo.active_profile("p"))
                    self.assertIsNone(self.repo.bound_profile(self.repo.group_binding(123)))
                else:
                    self.assertEqual(self.repo.active_samples("p"), ())
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE " + table + " SET " + field + "=?", (raw,))

    def test_review2_optional_source_and_grant_fields_reject_nul_corruption(self):
        self.configured()
        self.repo.save_sample(self.sample)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_sources SET safe_error_class=?", ("timeout\x00" + SENTINEL,))
        self.assertIsNone(self.repo.source("s"))
        self.assertEqual(self.repo.due_sources(NOW, 10), ())
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_sources SET safe_error_class=NULL")
            connection.execute("UPDATE style_authorizations SET expires_at=?", ("2099-01-01T00:00:00+00:00\x00" + SENTINEL,))
        self.assertEqual(self.repo.active_authorizations("s", NOW), ())
        self.assertEqual(self.repo.active_samples("p"), ())

    def test_review2_legacy_second_precision_grant_survives_identical_config_sync(self):
        self.configured()
        expires = "2099-01-01T00:00:00+00:00"
        source = self.config.sources[0]
        grant = replace(source.authorizations[0], expires_at=expires)
        self.config = replace(self.config, sources=(replace(source, authorizations=(grant,)),))
        self.repo.sync_configuration(self.config, NOW)
        current_id = self.repo.active_authorizations("s", NOW)[0].authorization_id
        def legacy_hash(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        evidence = legacy_hash(["evidence-ref-v1", grant.evidence_ref])
        legacy_id = legacy_hash(["authorization-v1", "s", "reddit", "example", "author", "alice",
                                 "all_posts_until_revoke", evidence, NOW.isoformat(), expires])
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE style_authorizations SET id=?,granted_at=?,expires_at=? WHERE id=?",
                               (legacy_id, NOW.isoformat(), expires, current_id))
        authorization = self.repo.active_authorizations("s", NOW)[0]
        self.assertEqual(authorization.authorization_id, legacy_id)
        self.sample = self.sample_for(authorization)
        published = self.repo.publish_profile(self.profile(), NOW)
        samples = self.repo.active_samples("p")
        self.repo.update_source_cursor("s", {"after": "legacy-cursor"}, NOW)
        self.assertEqual(self.repo.sync_configuration(self.config, LATER), ())
        self.assertEqual(self.repo.active_authorizations("s", LATER)[0].authorization_id, legacy_id)
        self.assertEqual(self.repo.active_samples("p"), samples)
        self.assertEqual(self.repo.active_profile("p")["id"], published["id"])
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "legacy-cursor"})

    def test_review3_corrupt_grants_invalidate_dynamic_and_pinned_profiles(self):
        for case in ("clean", "nul-id", "utf8-id", "blob-expiry", "bad-created", "bad-updated", "bad-revoked"):
            with self.subTest(case=case):
                self.path = Path(self.temp.name) / (case + ".sqlite3")
                self.store = WorkspaceStateStore(self.path)
                self.configured()
                self.config = replace(self.config, groups=self.config.groups + (GroupBindingSettings(456, "p", 5, 800),))
                self.repo.sync_configuration(self.config, NOW)
                published = self.repo.publish_profile(self.profile(), NOW)
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE group_style_bindings SET profile_version=1 WHERE group_id=456")
                    if case == "nul-id":
                        bad_id = self.sample.authorization_id + "\x00" + SENTINEL
                        connection.execute("UPDATE style_authorizations SET id=?", (bad_id,))
                        connection.execute("UPDATE community_style_samples SET authorization_id=?", (bad_id,))
                    elif case == "utf8-id":
                        bad_id = b"bad\xff" + SENTINEL.encode()
                        connection.execute("UPDATE style_authorizations SET id=CAST(? AS TEXT)", (bad_id,))
                        connection.execute("UPDATE community_style_samples SET authorization_id=CAST(? AS TEXT)", (bad_id,))
                    elif case == "blob-expiry":
                        connection.execute("UPDATE style_authorizations SET expires_at=?", (sqlite3.Binary(b"2099-01-01T00:00:00+00:00"),))
                    elif case.startswith("bad-"):
                        field = {"bad-created": "created_at", "bad-updated": "updated_at", "bad-revoked": "revoked_at"}[case]
                        connection.execute("UPDATE style_authorizations SET " + field + "=?", (SENTINEL,))
                dynamic, pinned = self.repo.group_binding(123), self.repo.group_binding(456)
                values = (self.repo.active_profile("p"), self.repo.bound_profile(dynamic), self.repo.bound_profile(pinned))
                if case == "clean":
                    self.assertTrue(all(value["id"] == published["id"] for value in values))
                else:
                    self.assertTrue(all(value is None for value in values), "corrupt grant retained a consumable profile")
                    self.assertEqual(self.repo.active_samples("p"), ())
                    with sqlite3.connect(self.path) as connection:
                        self.assertNotEqual(connection.execute("SELECT status FROM style_authorizations").fetchone()[0], "active")

    def test_review3_offset_expiry_is_compared_as_an_exact_utc_instant(self):
        self.configured()
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        source = self.config.sources[0]
        grant = replace(source.authorizations[0], expires_at=expires.isoformat())
        self.config = replace(self.config, sources=(replace(source, authorizations=(grant,)),))
        self.repo.sync_configuration(self.config, NOW)
        self.sample = self.sample_for(self.repo.active_authorizations("s", LATER)[0])
        published = self.repo.publish_profile(self.profile(), LATER)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE style_authorizations SET expires_at=?", (expires.astimezone(timezone(timedelta(hours=8))).isoformat(),))
        self.assertEqual(len(self.repo.active_authorizations("s", expires - timedelta(microseconds=1))), 1)
        self.assertEqual(self.repo.active_profile("p")["id"], published["id"])
        self.assertEqual(self.repo.active_authorizations("s", expires), ())
        self.assertIsNone(self.repo.active_profile("p"))

    def test_review3_invalid_utf8_fails_closed_without_exception_or_byte_leak(self):
        cases = (("community_style_sources", "community_key", "source"),
                 ("community_style_sources", "safe_error_class", "source"),
                 ("style_authorizations", "subject_key", "authorization"),
                 ("style_authorizations", "expires_at", "authorization"),
                 ("community_style_samples", "features_json", "sample"),
                 ("community_style_samples", "model", "sample"),
                 ("community_style_samples", "analyzed_at", "sample"),
                 ("community_style_samples", "removed_at", "sample"),
                 ("community_style_profiles", "created_at", "profile"),
                 ("community_style_profiles", "activated_at", "profile"),
                 ("group_style_bindings", "profile_key", "binding"))
        for table, field, kind in cases:
            with self.subTest(kind=kind):
                self.path = Path(self.temp.name) / (kind + field + ".sqlite3")
                self.store = WorkspaceStateStore(self.path)
                self.configured()
                published = self.repo.publish_profile(self.profile(), NOW)
                binding = self.repo.group_binding(123)
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE " + table + " SET " + field + "=CAST(? AS TEXT)", (b"bad\xff" + SENTINEL.encode(),))
                try:
                    if kind == "source":
                        result = (self.repo.source("s"), self.repo.due_sources(NOW, 10))
                        expected = (None, ())
                    elif kind == "authorization":
                        result = (self.repo.active_authorizations("s", NOW), self.repo.active_profile("p"), self.repo.bound_profile(binding))
                        expected = ((), None, None)
                    elif kind == "sample":
                        result, expected = self.repo.active_samples("p"), ()
                    elif kind == "profile":
                        result = (self.repo.profile(published["id"]), self.repo.active_profile("p"), self.repo.bound_profile(binding))
                        expected = (None, None, None)
                    else:
                        result = (self.repo.group_binding(123), self.repo.bound_profile(binding))
                        expected = (None, None)
                except Exception:
                    self.assertNotIn(SENTINEL, traceback.format_exc())
                    self.fail("invalid UTF-8 raised instead of failing closed")
                self.assertEqual(result, expected)
                self.assertNotIn(SENTINEL, str(result))

    def test_review3_legacy_fractional_grant_keeps_identity_and_gains_exact_boundaries(self):
        self.configured()
        granted = NOW + timedelta(microseconds=123456)
        expires = datetime(2099, 1, 1, microsecond=654321, tzinfo=timezone.utc)
        source = self.config.sources[0]
        grant = replace(source.authorizations[0], granted_at=granted.isoformat(), expires_at=expires.isoformat())
        self.config = replace(self.config, sources=(replace(source, authorizations=(grant,)),))
        self.repo.sync_configuration(self.config, LATER)
        current_id = self.repo.active_authorizations("s", LATER)[0].authorization_id
        def legacy_hash(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        legacy_id = legacy_hash(["authorization-v1", "s", "reddit", "example", "author", "alice", "all_posts_until_revoke",
            legacy_hash(["evidence-ref-v1", grant.evidence_ref]), granted.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")])
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE style_authorizations SET id=?,granted_at=?,expires_at=? WHERE id=?",
                (legacy_id, granted.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds"), current_id))
        self.sample = self.sample_for(self.repo.active_authorizations("s", LATER)[0])
        published = self.repo.publish_profile(self.profile(), LATER)
        samples = self.repo.active_samples("p")
        self.repo.update_source_cursor("s", {"after": "fractional-legacy-cursor"}, LATER)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE group_style_bindings SET profile_version=1")
        # Restore exact times before expiration scanning: legacy seconds alone
        # would incorrectly expire this grant within its final fractional second.
        self.assertEqual(self.repo.sync_configuration(self.config, expires - timedelta(microseconds=1)), ())
        self.assertEqual(self.repo.active_authorizations("s", LATER)[0].authorization_id, legacy_id)
        self.assertEqual(self.repo.active_samples("p"), samples)
        self.assertEqual(self.repo.active_profile("p")["id"], published["id"])
        self.assertEqual(self.repo.bound_profile(self.repo.group_binding(123))["id"], published["id"])
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "fractional-legacy-cursor"})
        self.assertEqual(self.repo.active_authorizations("s", granted - timedelta(microseconds=1)), ())
        self.assertEqual(len(self.repo.active_authorizations("s", granted)), 1)
        self.assertEqual(len(self.repo.active_authorizations("s", expires - timedelta(microseconds=1))), 1)
        self.assertEqual(self.repo.active_authorizations("s", expires), ())

    def test_review3_existing_current_identity_precedes_legacy_without_unique_conflict(self):
        self.repo = self.store.community_style_repository
        config = settings()
        source = config.sources[0]
        precise_grant = replace(source.authorizations[0], granted_at=(NOW + timedelta(microseconds=123456)).isoformat())
        precise_config = replace(config, sources=(replace(source, authorizations=(precise_grant,)),))
        self.repo.sync_configuration(precise_config, LATER)
        current_id = self.repo.active_authorizations("s", LATER)[0].authorization_id
        self.repo.sync_configuration(config, LATER)
        legacy_id = self.repo.active_authorizations("s", LATER)[0].authorization_id
        self.assertNotEqual(current_id, legacy_id)
        self.repo.sync_configuration(precise_config, LATER)
        grants = self.repo.active_authorizations("s", LATER)
        self.assertEqual([grant.authorization_id for grant in grants], [current_id])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM style_authorizations WHERE id=?", (legacy_id,)).fetchone()[0], "revoked")

    def test_review3_new_fractional_grant_id_distinguishes_one_microsecond(self):
        identifiers = []
        for microsecond in (123456, 123457):
            self.path = Path(self.temp.name) / (str(microsecond) + ".sqlite3")
            self.store = WorkspaceStateStore(self.path)
            self.repo = self.store.community_style_repository
            self.config = settings()
            source = self.config.sources[0]
            grant = replace(source.authorizations[0], granted_at=(NOW + timedelta(microseconds=microsecond)).isoformat())
            self.repo.sync_configuration(replace(self.config, sources=(replace(source, authorizations=(grant,)),)), LATER)
            identifiers.append(self.repo.active_authorizations("s", LATER)[0].authorization_id)
        self.assertNotEqual(*identifiers)

    def test_review4_withdrawal_uses_source_when_all_sample_profile_keys_are_corrupt(self):
        for case in ("clean", "nul", "utf8", "blob"):
            with self.subTest(case=case):
                self.path = Path(self.temp.name) / ("sample-key-" + case + ".sqlite3")
                self.store = WorkspaceStateStore(self.path)
                self.configured()
                self.config = replace(self.config, groups=self.config.groups + (GroupBindingSettings(456, "p", 5, 800),))
                self.repo.sync_configuration(self.config, NOW)
                published = self.repo.publish_profile(self.profile(), NOW)
                self.assertEqual(published["sample_count"], 5)
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE group_style_bindings SET profile_version=1 WHERE group_id=456")
                for group_id in (123, 456):
                    self.assertEqual(self.repo.bound_profile(self.repo.group_binding(group_id))["id"], published["id"])
                with sqlite3.connect(self.path) as connection:
                    if case == "nul":
                        connection.execute("UPDATE community_style_samples SET profile_key=?", ("p\x00" + SENTINEL,))
                    elif case == "utf8":
                        connection.execute("UPDATE community_style_samples SET profile_key=CAST(? AS TEXT)", (b"p\xff" + SENTINEL.encode(),))
                    elif case == "blob":
                        connection.execute("UPDATE community_style_samples SET profile_key=?", (sqlite3.Binary(b"p"),))
                source = replace(self.config.sources[0], authorizations=())
                affected = self.repo.sync_configuration(replace(self.config, sources=(source,)), LATER)
                self.assertEqual(affected, ("p",))
                self.assertIsNone(self.repo.active_profile("p"))
                for group_id in (123, 456):
                    binding = self.repo.group_binding(group_id)
                    self.assertIsNotNone(binding)
                    self.assertIsNone(self.repo.bound_profile(binding))
                self.assertEqual(self.repo.group_binding(456)["profile_version"], 1)
                self.assertEqual(self.repo.source("s")["profile_key"], "p")
                self.assertEqual(self.repo.active_authorizations("s", LATER), ())
                self.assertEqual(self.repo.active_samples("p"), ())
                with sqlite3.connect(self.path) as connection:
                    self.assertEqual(connection.execute("SELECT status FROM style_authorizations").fetchall(), [("revoked",)])
                    self.assertEqual(connection.execute("SELECT status FROM community_style_samples").fetchall(), [("removed",)] * 5)

    def test_review4_withdrawal_includes_safe_historical_sample_profile_keys(self):
        self.configured()
        old = self.repo.publish_profile(self.profile(), NOW)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_profiles SET profile_key='history',status='retired'")
            connection.execute("UPDATE community_style_samples SET profile_key='history',status='removed',removed_at=?", (NOW.isoformat(),))
        current = self.repo.publish_profile(self.profile("e"), LATER)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_samples SET profile_key=? WHERE status='active'", (sqlite3.Binary(b"p"),))
        source = replace(self.config.sources[0], authorizations=())
        self.assertEqual(self.repo.sync_configuration(replace(self.config, sources=(source,)), LATER), ("history", "p"))
        for profile in (old, current):
            self.assertIsNotNone(self.repo.profile(profile["id"])["invalidated_at"])

    def test_review4_untrusted_source_binding_invalidates_all_current_and_pinnable_history(self):
        for case in ("missing-link", "missing-source", "nul-key", "utf8-key", "blob-key"):
            with self.subTest(case=case):
                self.path = Path(self.temp.name) / ("source-binding-" + case + ".sqlite3")
                self.store = WorkspaceStateStore(self.path)
                self.configured()
                other = replace(self.config.sources[0], id="other", profile="other-profile")
                self.config = replace(self.config, sources=self.config.sources + (other,), groups=self.config.groups + (
                    GroupBindingSettings(456, "p", 5, 800), GroupBindingSettings(789, "other-profile", 5, 800),
                    GroupBindingSettings(987, "other-profile", 5, 800)))
                self.repo.sync_configuration(self.config, NOW)
                published = []
                for source_id, profile_key in (("s", "p"), ("other", "other-profile")):
                    sample = self.sample_for(self.repo.active_authorizations(source_id, NOW)[0])
                    for fingerprint in ("d", "e"):
                        samples = [replace(sample, sample_id="profile-%d" % index,
                            external_content_id="profile-%d" % index, content_fingerprint=fingerprint * 64) for index in range(5)]
                        self.repo.commit_batch(source_id, {}, samples, NOW)
                        published.append(self.repo.publish_profile(aggregate_style_profile(profile_key, self.repo.active_samples(profile_key)), NOW))
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE group_style_bindings SET profile_version=1 WHERE group_id IN (456,987)")
                for group_id in (123, 456, 789, 987):
                    self.assertIsNotNone(self.repo.bound_profile(self.repo.group_binding(group_id)))
                with sqlite3.connect(self.path) as connection:
                    # An undecodable profile key must also be retired without being
                    # returned as an affected identifier, even if already invalidated.
                    connection.execute("INSERT INTO community_style_profiles(profile_key,version,status,rules_json,sample_count,author_count,"
                        "source_fingerprint,created_at,activated_at,invalidated_at) SELECT CAST(? AS TEXT),version,'active',rules_json,"
                        "sample_count,author_count,source_fingerprint,created_at,activated_at,? FROM community_style_profiles WHERE id=?",
                        (b"bad\xff" + SENTINEL.encode(), NOW.isoformat(), published[-1]["id"]))
                    connection.execute("UPDATE community_style_samples SET profile_key=? WHERE source_id='s'", (sqlite3.Binary(b"p"),))
                    if case == "missing-link":
                        connection.execute("UPDATE style_authorizations SET source_id='missing' WHERE source_id='s'")
                    elif case == "missing-source":
                        connection.execute("DELETE FROM community_style_sources WHERE id='s'")
                    elif case == "nul-key":
                        connection.execute("UPDATE community_style_sources SET profile_key=? WHERE id='s'", ("p\x00" + SENTINEL,))
                    elif case == "utf8-key":
                        connection.execute("UPDATE community_style_sources SET profile_key=CAST(? AS TEXT) WHERE id='s'", (b"p\xff" + SENTINEL.encode(),))
                    else:
                        connection.execute("UPDATE community_style_sources SET profile_key=? WHERE id='s'", (sqlite3.Binary(b"p"),))
                if case.startswith("missing-"):
                    source = replace(self.config.sources[0], authorizations=())
                    affected = self.repo.sync_configuration(replace(self.config, sources=(source, other)), LATER)
                    self.assertEqual(affected, ("other-profile", "p"))
                    self.assertNotIn(SENTINEL, repr(affected))
                else:
                    self.assertEqual(self.repo.active_authorizations("s", LATER), ())
                for profile_key in ("p", "other-profile"):
                    self.assertIsNone(self.repo.active_profile(profile_key))
                for group_id in (123, 456, 789, 987):
                    self.assertIsNone(self.repo.bound_profile(self.repo.group_binding(group_id)))
                for profile in published:
                    self.assertIsNotNone(self.repo.profile(profile["id"])["invalidated_at"])
                self.assertEqual(self.repo.active_samples("p"), ())
                self.assertEqual(len(self.repo.active_samples("other-profile")), 5)
                with sqlite3.connect(self.path) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM community_style_profiles WHERE status='active' OR invalidated_at IS NULL").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM community_style_samples WHERE source_id='s' AND status='active'").fetchone()[0], 0)

    def published_profile_pair(self, name, expires_at=None):
        self.path = Path(self.temp.name) / (name + ".sqlite3")
        self.store = WorkspaceStateStore(self.path)
        self.configured()
        source = self.config.sources[0]
        other = replace(source, id="other", profile="q")
        grant = replace(source.authorizations[0], expires_at=expires_at)
        self.config = replace(self.config, sources=(replace(source, authorizations=(grant,)), other),
            groups=self.config.groups + (GroupBindingSettings(456, "p", 5, 800),
                GroupBindingSettings(789, "q", 5, 800), GroupBindingSettings(987, "q", 5, 800)))
        self.repo.sync_configuration(self.config, NOW)
        published = {}
        for source_id, profile_key in (("other", "q"), ("s", "p")):
            sample = self.sample_for(self.repo.active_authorizations(source_id, NOW)[0])
            published[profile_key] = []
            for fingerprint in ("d", "e", "f"):
                samples = [replace(sample, sample_id="profile-%d" % index,
                    external_content_id="profile-%d" % index, content_fingerprint=fingerprint * 64) for index in range(5)]
                self.repo.commit_batch(source_id, {}, samples, NOW)
                candidate = aggregate_style_profile(profile_key, self.repo.active_samples(profile_key))
                published[profile_key].append(self.repo.publish_profile(candidate, NOW))
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE group_style_bindings SET profile_version=1 WHERE group_id IN (456,987)")
        for group_id, key, index in ((123, "p", 2), (456, "p", 0), (789, "q", 2), (987, "q", 0)):
            self.assertEqual(self.repo.bound_profile(self.repo.group_binding(group_id))["id"], published[key][index]["id"])
        return published

    def assert_profile_pair_invalidated(self, published):
        for key in ("p", "q"):
            self.assertIsNone(self.repo.active_profile(key))
            for profile in published[key]:
                stored = self.repo.profile(profile["id"])
                self.assertEqual(stored["status"], "retired")
                self.assertIsNotNone(stored["invalidated_at"])
        for group_id in (123, 456, 789, 987):
            binding = self.repo.group_binding(group_id)
            self.assertIsNotNone(binding)
            self.assertIsNone(self.repo.bound_profile(binding))
        self.assertEqual(self.repo.group_binding(456)["profile_version"], 1)
        self.assertEqual(self.repo.group_binding(987)["profile_version"], 1)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM community_style_profiles "
                "WHERE status='active' OR invalidated_at IS NULL").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM community_style_samples "
                "WHERE source_id='s' AND status='active'").fetchone()[0], 0)
        self.assertEqual(len(self.repo.active_samples("q")), 5)

    def test_review5_ambiguous_sample_keys_override_valid_source_profile_key(self):
        expires = datetime(2099, 1, 1, tzinfo=timezone.utc)
        cases = (("nul", "?", "p\x00" + SENTINEL),
                 ("utf8", "CAST(? AS TEXT)", b"p\xff" + SENTINEL.encode()),
                 ("blob", "?", sqlite3.Binary(b"p")),
                 ("invalid-token", "?", " p"),
                 ("noncanonical", "?", "cafe\u0301"))
        for case, expression, value in cases:
            with self.subTest(case=case):
                published = self.published_profile_pair("ambiguous-" + case, expires.isoformat())
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE community_style_samples SET profile_key=" + expression + " WHERE source_id='s'", (value,))
                    connection.execute("UPDATE community_style_sources SET profile_key='q' WHERE id='s'")
                    # Also cover an undecodable active row already marked invalid.
                    connection.execute("INSERT INTO community_style_profiles(profile_key,version,status,rules_json,sample_count,author_count,"
                        "source_fingerprint,created_at,activated_at,invalidated_at) SELECT CAST(? AS TEXT),version,'active',rules_json,"
                        "sample_count,author_count,source_fingerprint,created_at,activated_at,? FROM community_style_profiles WHERE id=?",
                        (b"bad\xff" + SENTINEL.encode(), NOW.isoformat(), published["q"][-1]["id"]))
                self.assertEqual(self.repo.source("s")["profile_key"], "q")
                affected = self.repo.sync_configuration(self.config, expires)
                self.assertEqual(affected, ("p", "q"))
                self.assertNotIn(SENTINEL, repr(affected))
                self.assertNotIn("invalid_sqlite_utf8", repr(affected))
                self.assert_profile_pair_invalidated(published)
                self.assertEqual(self.repo.active_authorizations("s", expires), ())
                with sqlite3.connect(self.path) as connection:
                    self.assertEqual(connection.execute("SELECT status FROM style_authorizations WHERE source_id='s' AND expires_at IS NOT NULL").fetchall(), [("expired",)])

    def test_review5_one_corrupt_historical_sample_invalidates_unrelated_profiles(self):
        published = self.published_profile_pair("one-corrupt-history")
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE community_style_samples SET profile_key=? WHERE id="
                "(SELECT MIN(id) FROM community_style_samples WHERE source_id='s' AND status='removed')", (sqlite3.Binary(b"p"),))
        withdrawn = replace(self.config.sources[0], authorizations=())
        affected = self.repo.sync_configuration(replace(self.config, sources=(withdrawn, self.config.sources[1])), LATER)
        self.assertEqual(affected, ("p", "q"))
        self.assert_profile_pair_invalidated(published)

    def test_review5_provable_source_scope_preserves_unrelated_profile(self):
        for history in ("clean", "empty"):
            with self.subTest(history=history):
                published = self.published_profile_pair("provable-" + history)
                if history == "empty":
                    with sqlite3.connect(self.path) as connection:
                        connection.execute("DELETE FROM community_style_samples WHERE source_id='s'")
                withdrawn = replace(self.config.sources[0], authorizations=())
                affected = self.repo.sync_configuration(replace(self.config, sources=(withdrawn, self.config.sources[1])), LATER)
                self.assertEqual(affected, ("p",))
                self.assertIsNone(self.repo.active_profile("p"))
                for group_id in (123, 456):
                    self.assertIsNone(self.repo.bound_profile(self.repo.group_binding(group_id)))
                for profile in published["p"]:
                    self.assertIsNotNone(self.repo.profile(profile["id"])["invalidated_at"])
                self.assertEqual(self.repo.active_profile("q")["id"], published["q"][-1]["id"])
                for group_id, index in ((789, 2), (987, 0)):
                    self.assertEqual(self.repo.bound_profile(self.repo.group_binding(group_id))["id"], published["q"][index]["id"])
                for profile in published["q"]:
                    self.assertIsNone(self.repo.profile(profile["id"])["invalidated_at"])
                self.assertEqual(len(self.repo.active_samples("q")), 5)

    def test_review5_no_sample_history_with_corrupt_source_invalidates_all_profiles(self):
        published = self.published_profile_pair("no-history-corrupt-source")
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM community_style_samples WHERE source_id='s'")
            connection.execute("UPDATE community_style_sources SET profile_key=? WHERE id='s'", (sqlite3.Binary(b"p"),))
        self.assertEqual(self.repo.active_authorizations("s", LATER), ())
        self.assert_profile_pair_invalidated(published)


SENTINEL = "private-body-cookie-sentinel"


if __name__ == "__main__":
    unittest.main()
