from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class MessagingRepositoryTests(unittest.TestCase):
    def test_generic_enqueue_rejects_all_reserved_outbox_kinds(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.messaging import OutboxRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OutboxRepository(
                SqliteDatabase(Path(temp_dir) / "state.sqlite3")
            )
            for kind in ("task_final", "task_notice", "e3_observation"):
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(ValueError, "reserved"):
                        repository.enqueue(
                            kind, 1, "internal", "%s:key" % kind
                        )

    def test_inbound_outbox_retry_and_metrics_share_database(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.messaging import (
            InboundRepository,
            OutboxRepository,
            RuntimeMetricRepository,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
            database = SqliteDatabase(Path(temp_dir) / "state.sqlite3")
            inbound = InboundRepository(database)
            outbox = OutboxRepository(database)
            metrics = RuntimeMetricRepository(database)

            self.assertTrue(inbound.claim("m1", 1, now=now))
            self.assertFalse(inbound.claim("m1", 1, now=now))
            row = outbox.enqueue("reply", 1, "回复", "reply:m1", now=now)
            claimed = outbox.claim_due(now=now)
            outbox.mark_retry(
                claimed["id"], claimed["claim_token"],
                "closed", 2, 8, now=now,
            )
            self.assertIsNone(
                outbox.claim_due(now=now + timedelta(seconds=1))
            )
            metrics.set("circuit_state", "closed")

            self.assertEqual(row["dedupe_key"], "reply:m1")
            self.assertEqual(metrics.transport_stats()["outbox_pending"], 1)

    def test_claim_due_recovers_only_expired_sending_lease(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.messaging import OutboxRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OutboxRepository(
                SqliteDatabase(Path(temp_dir) / "state.sqlite3")
            )
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            repository.enqueue("reply", 1, "reply", "reply:m1", now=now)
            first = repository.claim_due(now=now, lease_seconds=300)
            self.assertEqual(first["status"], "sending")

            self.assertIsNone(
                repository.claim_due(
                    now=now + timedelta(seconds=299), lease_seconds=300
                )
            )
            recovered = repository.claim_due(
                now=now + timedelta(seconds=300), lease_seconds=300
            )

            self.assertEqual(recovered["id"], first["id"])
            self.assertEqual(recovered["status"], "sending")
            self.assertEqual(recovered["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
