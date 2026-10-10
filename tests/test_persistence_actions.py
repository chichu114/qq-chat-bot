from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ActionIntentRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.store = WorkspaceStateStore(
            Path(self.temp_dir.name) / "state.sqlite3"
        )
        self.repository = self.store.action_intent_repository
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _intent(self, **overrides):
        from qq_openclaw_bridge.domain.actions import ActionIntent

        values = {
            "user_id": 7,
            "source_message_id": "m1",
            "session_key": "agent:main:qq-7",
            "action_type": "memory.delete",
            "adapter_id": "housekeeper",
            "payload": {"command": "删除记忆 1", "index": 1},
            "config_version": 2,
            "risk_tier": "high",
            "impact_summary": "删除一条长期记忆",
            "created_at": self.now,
            "expires_at": self.now + timedelta(minutes=5),
        }
        values.update(overrides)
        return ActionIntent.create(**values)

    def _approve(self, intent):
        return self.repository.approve(
            intent.intent_id,
            intent.approval_id,
            intent.user_id,
            intent.payload_sha256,
            intent.config_version,
            now=self.now,
        )

    def test_propose_persists_immutable_contract_and_store_reuses_repository(self):
        intent = self._intent()

        proposed = self.repository.propose(intent)

        self.assertIs(self.repository, self.store.action_intent_repository)
        self.assertEqual(proposed["status"], "proposed")
        self.assertEqual(proposed["payload_json"], intent.payload_json)
        self.assertEqual(proposed["payload_sha256"], intent.payload_sha256)
        with self.assertRaises(ValueError):
            self.repository.propose(intent)

    def test_approved_intent_is_consumed_once_with_exact_owner_hash_and_version(self):
        intent = self._intent()
        self.repository.propose(intent)
        approved = self._approve(intent)

        consumed = self.repository.consume(
            intent.intent_id,
            intent.user_id,
            intent.payload_sha256,
            intent.config_version,
            now=self.now,
        )

        self.assertEqual(approved["status"], "approved")
        self.assertEqual(consumed["status"], "executing")
        self.assertEqual(consumed["attempt"], 1)
        with self.assertRaises(ValueError):
            self.repository.consume(
                intent.intent_id,
                intent.user_id,
                intent.payload_sha256,
                intent.config_version,
                now=self.now,
            )

    def test_approve_and_consume_binds_payload_json_and_replays_once(self):
        intent = self._intent()
        self.repository.propose(intent)

        with self.assertRaises(ValueError):
            self.repository.approve_and_consume(
                intent.intent_id,
                intent.approval_id,
                intent.user_id,
                intent.action_type,
                intent.adapter_id,
                '{"index":2}',
                intent.payload_sha256,
                intent.config_version,
                intent.expires_at,
                now=self.now,
            )
        with self.assertRaises(ValueError):
            self.repository.approve_and_consume(
                intent.intent_id,
                intent.approval_id,
                intent.user_id,
                intent.action_type,
                intent.adapter_id,
                intent.payload_json,
                intent.payload_sha256,
                intent.config_version,
                intent.expires_at + timedelta(seconds=1),
                now=self.now,
            )
        consumed = self.repository.approve_and_consume(
            intent.intent_id,
            intent.approval_id,
            intent.user_id,
            intent.action_type,
            intent.adapter_id,
            intent.payload_json,
            intent.payload_sha256,
            intent.config_version,
            intent.expires_at,
            now=self.now,
        )
        self.assertEqual(consumed["status"], "executing")
        self.assertEqual(consumed["attempt"], 1)
        with self.assertRaises(ValueError):
            self.repository.approve_and_consume(
                intent.intent_id,
                intent.approval_id,
                intent.user_id,
                intent.action_type,
                intent.adapter_id,
                intent.payload_json,
                intent.payload_sha256,
                intent.config_version,
                intent.expires_at,
                now=self.now,
            )

    def test_expired_or_cross_user_intent_cannot_be_approved_or_consumed(self):
        expired = self._intent(
            created_at=self.now - timedelta(minutes=10),
            expires_at=self.now - timedelta(seconds=1),
        )
        self.repository.propose(expired)
        with self.assertRaises(ValueError):
            self._approve(expired)

        active = self._intent(source_message_id="m2")
        self.repository.propose(active)
        with self.assertRaises(ValueError):
            self.repository.approve(
                active.intent_id,
                active.approval_id,
                active.user_id + 1,
                active.payload_sha256,
                active.config_version,
                now=self.now,
            )
        self._approve(active)
        with self.assertRaises(ValueError):
            self.repository.consume(
                active.intent_id,
                active.user_id + 1,
                active.payload_sha256,
                active.config_version,
                now=self.now,
            )

    def test_hash_or_config_drift_fails_closed_without_state_change(self):
        intent = self._intent()
        self.repository.propose(intent)

        for payload_hash, config_version in (
            ("0" * 64, intent.config_version),
            (intent.payload_sha256, intent.config_version + 1),
        ):
            with self.subTest(payload_hash=payload_hash, config=config_version):
                with self.assertRaises(ValueError):
                    self.repository.approve(
                        intent.intent_id,
                        intent.approval_id,
                        intent.user_id,
                        payload_hash,
                        config_version,
                        now=self.now,
                    )
                self.assertEqual(
                    self.repository.get(intent.intent_id)["status"], "proposed"
                )

    def test_mutation_rejects_naive_clock_before_sql(self):
        intent = self._intent()
        self.repository.propose(intent)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.repository.approve(
                intent.intent_id,
                intent.approval_id,
                intent.user_id,
                intent.payload_sha256,
                intent.config_version,
                now=datetime(2026, 8, 5, 12, 0),
            )
        self.assertEqual(
            self.repository.get(intent.intent_id)["status"], "proposed"
        )

    def test_finish_requires_executing_and_terminal_state_is_immutable(self):
        intent = self._intent()
        self.repository.propose(intent)
        with self.assertRaises(ValueError):
            self.repository.finish(
                intent.intent_id,
                intent.user_id,
                intent.payload_sha256,
                intent.config_version,
                "succeeded",
                "Success",
                "已完成",
                now=self.now,
            )
        self._approve(intent)
        self.repository.consume(
            intent.intent_id,
            intent.user_id,
            intent.payload_sha256,
            intent.config_version,
            now=self.now,
        )

        finished = self.repository.finish(
            intent.intent_id,
            intent.user_id,
            intent.payload_sha256,
            intent.config_version,
            "succeeded",
            "Success",
            "已完成",
            now=self.now,
        )

        self.assertEqual(finished["status"], "succeeded")
        with self.assertRaises(ValueError):
            self.repository.cancel(
                intent.intent_id,
                intent.user_id,
                intent.payload_sha256,
                intent.config_version,
                now=self.now,
            )

    def test_cancel_is_owner_bound_and_cannot_be_replayed(self):
        intent = self._intent()
        self.repository.propose(intent)
        with self.assertRaises(ValueError):
            self.repository.cancel(
                intent.intent_id,
                intent.user_id + 1,
                intent.payload_sha256,
                intent.config_version,
                now=self.now,
            )

        cancelled = self.repository.cancel(
            intent.intent_id,
            intent.user_id,
            intent.payload_sha256,
            intent.config_version,
            now=self.now,
        )

        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.repository.cancel(
                intent.intent_id,
                intent.user_id,
                intent.payload_sha256,
                intent.config_version,
                now=self.now,
            )

    def test_restart_recovery_marks_executing_unknown_and_resolve_is_bound_cas(self):
        intent = self._intent()
        self.repository.propose(intent)
        self._approve(intent)
        self.repository.consume(
            intent.intent_id,
            intent.user_id,
            intent.payload_sha256,
            intent.config_version,
            now=self.now,
        )

        recovered = self.repository.recover_executing_as_unknown(now=self.now)

        self.assertEqual(recovered, 1)
        self.assertEqual(self.repository.get(intent.intent_id)["status"], "unknown")
        with self.assertRaises(ValueError):
            self.repository.consume(
                intent.intent_id,
                intent.user_id,
                intent.payload_sha256,
                intent.config_version,
                now=self.now,
            )
        with self.assertRaises(ValueError):
            self.repository.resolve_unknown(
                intent.intent_id,
                intent.user_id + 1,
                intent.payload_sha256,
                intent.config_version,
                "succeeded",
                "ManualVerification",
                "verified by operator",
                now=self.now,
            )

        resolved = self.repository.resolve_unknown(
            intent.intent_id,
            intent.user_id,
            intent.payload_sha256,
            intent.config_version,
            "succeeded",
            "ManualVerification",
            "verified by operator",
            now=self.now,
        )

        self.assertEqual(resolved["status"], "succeeded")
        with self.assertRaises(ValueError):
            self.repository.resolve_unknown(
                intent.intent_id,
                intent.user_id,
                intent.payload_sha256,
                intent.config_version,
                "failed",
                "ManualVerification",
                "cannot replay resolution",
                now=self.now,
            )

if __name__ == "__main__":
    unittest.main()
