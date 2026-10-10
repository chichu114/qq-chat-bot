from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ActionIntentDomainTests(unittest.TestCase):
    def test_payload_hash_is_stable_across_mapping_order(self):
        from qq_openclaw_bridge.domain.actions import payload_sha256

        left = payload_sha256({"command": "删除记忆 1", "index": 1})
        right = payload_sha256({"index": 1, "command": "删除记忆 1"})

        self.assertEqual(left, right)

    def test_canonical_payload_is_compact_utf8_json(self):
        from qq_openclaw_bridge.domain.actions import canonical_payload

        canonical = canonical_payload(
            {"z": [True, None, 2], "a": {"text": "中文"}}
        )

        self.assertEqual(canonical, '{"a":{"text":"中文"},"z":[true,null,2]}')

    def test_payload_rejects_ambiguous_or_unknown_types_recursively(self):
        from qq_openclaw_bridge.domain.actions import canonical_payload

        class IntSubclass(int):
            pass

        invalid = (
            {"x": 1.2},
            {"x": b"raw"},
            {"x": object()},
            {"x": (1, 2)},
            {1: "non-string key"},
            {"x": [IntSubclass(1)]},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                canonical_payload(payload)

    def test_payload_rejects_invalid_utf8_cycles_and_excessive_depth(self):
        from qq_openclaw_bridge.domain.actions import canonical_payload

        invalid_utf8 = chr(0xD800)
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            canonical_payload({"value": invalid_utf8})
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            canonical_payload({invalid_utf8: "value"})

        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaisesRegex(ValueError, "cyclic"):
            canonical_payload({"value": cyclic})

        nested = []
        for _ in range(40):
            nested = [nested]
        with self.assertRaisesRegex(ValueError, "depth"):
            canonical_payload({"value": nested})

    def test_payload_byte_budget_fails_before_serializing_the_root(self):
        import qq_openclaw_bridge.domain.actions as actions

        original_dumps = actions.json.dumps

        def guarded_dumps(value, *args, **kwargs):
            if isinstance(value, (dict, list)):
                raise AssertionError("oversized root must not be serialized")
            return original_dumps(value, *args, **kwargs)

        with patch.object(actions.json, "dumps", side_effect=guarded_dumps):
            with self.assertRaisesRegex(ValueError, "byte size"):
                actions.canonical_payload({"value": "x" * 70000})

    def test_identity_versions_are_bounded_to_sqlite_integer_range(self):
        from qq_openclaw_bridge.domain.actions import ActionIntent

        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        common = {
            "user_id": 7,
            "source_message_id": "m1",
            "session_key": "agent:main:qq-7",
            "action_type": "memory.delete",
            "adapter_id": "housekeeper",
            "payload": {"index": 1},
            "config_version": 1,
            "risk_tier": "high",
            "impact_summary": "删除记忆",
            "created_at": now,
            "expires_at": now + timedelta(minutes=5),
        }
        for field in ("user_id", "schema_version", "config_version"):
            with self.subTest(field=field):
                invalid = dict(common)
                invalid[field] = 2**63
                with self.assertRaisesRegex(ValueError, "SQLite"):
                    ActionIntent.create(**invalid)

    def test_datetime_requires_a_real_utc_offset(self):
        from qq_openclaw_bridge.domain.actions import ActionIntent

        class MissingOffset(tzinfo):
            def utcoffset(self, value):
                return None

            def dst(self, value):
                return None

        created = datetime(2026, 8, 5, 12, 0, tzinfo=MissingOffset())
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ActionIntent.create(
                user_id=7,
                source_message_id="m1",
                session_key="agent:main:qq-7",
                action_type="memory.delete",
                adapter_id="housekeeper",
                payload={"index": 1},
                config_version=1,
                risk_tier="high",
                impact_summary="删除记忆",
                created_at=created,
                expires_at=created + timedelta(minutes=5),
            )

    def test_create_is_immutable_and_binds_canonical_payload(self):
        from qq_openclaw_bridge.domain.actions import ActionIntent, payload_sha256

        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        payload = {"command": "删除记忆 1", "index": 1}
        intent = ActionIntent.create(
            user_id=7,
            source_message_id="m1",
            session_key="agent:main:qq-7",
            action_type="memory.delete",
            adapter_id="housekeeper",
            payload=payload,
            config_version=3,
            risk_tier="high",
            impact_summary="删除一条长期记忆",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        self.assertEqual(intent.payload_sha256, payload_sha256(payload))
        self.assertEqual(intent.schema_version, 1)
        self.assertTrue(intent.intent_id)
        self.assertTrue(intent.approval_id)
        with self.assertRaises(FrozenInstanceError):
            intent.status = "approved"

    def test_create_rejects_invalid_identity_versions_and_expiry(self):
        from qq_openclaw_bridge.domain.actions import ActionIntent

        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        common = {
            "user_id": 7,
            "source_message_id": "m1",
            "session_key": "agent:main:qq-7",
            "action_type": "memory.delete",
            "adapter_id": "housekeeper",
            "payload": {"index": 1},
            "config_version": 1,
            "risk_tier": "high",
            "impact_summary": "删除记忆",
            "created_at": now,
            "expires_at": now + timedelta(minutes=5),
        }
        for field, value in (
            ("user_id", True),
            ("user_id", 0),
            ("config_version", True),
            ("config_version", 0),
            ("source_message_id", ""),
            ("expires_at", now),
        ):
            values = dict(common)
            values[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                ActionIntent.create(**values)

    def test_direct_construction_cannot_bypass_payload_hash_binding(self):
        from qq_openclaw_bridge.domain.actions import ActionIntent

        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            ActionIntent(
                intent_id="intent-1",
                approval_id="approval-1",
                user_id=7,
                source_message_id="m1",
                session_key="agent:main:qq-7",
                action_type="memory.delete",
                adapter_id="housekeeper",
                schema_version=1,
                payload_json='{"index":1}',
                payload_sha256="0" * 64,
                config_version=1,
                risk_tier="high",
                impact_summary="删除记忆",
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )


if __name__ == "__main__":
    unittest.main()
