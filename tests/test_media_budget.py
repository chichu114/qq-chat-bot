from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class MediaBudgetTests(unittest.TestCase):
    def test_zero_budget_blocks_reservation(self):
        from qq_openclaw_bridge.application.media_budget import (
            BudgetExceeded,
            MediaBudgetPolicy,
        )

        policy = MediaBudgetPolicy(0)

        with self.assertRaises(BudgetExceeded):
            policy.reserve("image", 0.01)

    def test_reservation_reconciles_actual_cost_and_releases_failures(self):
        from qq_openclaw_bridge.application.media_budget import (
            BudgetExceeded,
            MediaBudgetPolicy,
        )

        policy = MediaBudgetPolicy("1.00")
        first = policy.reserve("image", "0.60")
        first.reconcile("0.40")
        failed = policy.reserve("audio", "0.60")
        failed.release()
        final = policy.reserve("image", "0.60")

        self.assertEqual(policy.consumed_cost, Decimal("0.4"))
        self.assertEqual(policy.reserved_cost, Decimal("0.6"))
        with self.assertRaises(BudgetExceeded):
            policy.reserve("audio", "0.01")
        final.release()

    def test_same_instance_reservations_cannot_oversubscribe_budget(self):
        from qq_openclaw_bridge.application.media_budget import (
            BudgetExceeded,
            MediaBudgetPolicy,
        )

        policy = MediaBudgetPolicy("1.00")
        policy.reserve("image", "0.60")

        with self.assertRaises(BudgetExceeded):
            policy.reserve("audio", "0.60")

    def test_costs_reject_nonfinite_values_and_preserve_decimal_precision(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy

        for value in ("NaN", "Infinity", "-Infinity", float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non-negative"):
                    MediaBudgetPolicy(value)

        policy = MediaBudgetPolicy("0.3000000000000000003")
        first = policy.reserve("image", "0.1000000000000000001")
        first.reconcile("0.1000000000000000001")
        second = policy.reserve("image", "0.2000000000000000002")
        self.assertEqual(policy.consumed_cost, Decimal("0.1000000000000000001"))
        self.assertEqual(policy.reserved_cost, Decimal("0.2000000000000000002"))
        second.release()

    def test_daily_budget_refreshes_from_persisted_cost_provider(self):
        from qq_openclaw_bridge.application.media_budget import (
            BudgetExceeded,
            MediaBudgetPolicy,
        )

        current_day = [date(2026, 7, 23)]
        provider_calls = []

        def consumed_cost_provider(day):
            provider_calls.append(day)
            return {
                date(2026, 7, 23): Decimal("0.80"),
                date(2026, 7, 24): Decimal("0.10"),
            }[day]

        policy = MediaBudgetPolicy(
            "1.00",
            day_provider=lambda: current_day[0],
            consumed_cost_provider=consumed_cost_provider,
        )
        with self.assertRaises(BudgetExceeded):
            policy.reserve("image", "0.21")

        current_day[0] = date(2026, 7, 24)
        reservation = policy.reserve("image", "0.50")

        self.assertEqual(provider_calls, [date(2026, 7, 23), date(2026, 7, 24)])
        self.assertEqual(policy.consumed_cost, Decimal("0.10"))
        self.assertEqual(policy.reserved_cost, Decimal("0.50"))
        reservation.release()

    def test_failed_daily_refresh_is_atomic_and_retries_provider(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy

        old_day = date(2026, 7, 23)
        new_day = date(2026, 7, 24)
        current_day = [old_day]
        provider_calls = []

        def consumed_cost_provider(day):
            provider_calls.append(day)
            if day == old_day:
                return Decimal("0.20")
            if provider_calls.count(new_day) == 1:
                raise RuntimeError("temporary persistence failure")
            return Decimal("0.10")

        policy = MediaBudgetPolicy(
            "1.00",
            day_provider=lambda: current_day[0],
            consumed_cost_provider=consumed_cost_provider,
        )
        reservation = policy.reserve("image", "0.30")
        current_day[0] = new_day

        with self.assertRaisesRegex(RuntimeError, "temporary persistence failure"):
            policy.consumed_cost

        self.assertEqual(policy._day, old_day)
        self.assertEqual(policy._consumed, Decimal("0.20"))
        self.assertEqual(policy._reserved, Decimal("0.30"))
        self.assertFalse(reservation._finished)

        self.assertEqual(policy.consumed_cost, Decimal("0.10"))
        self.assertEqual(policy._day, new_day)
        self.assertEqual(policy.reserved_cost, Decimal("0"))
        self.assertEqual(provider_calls, [old_day, new_day, new_day])
        self.assertFalse(reservation._finished)

    def test_actual_overage_is_committed_and_reservation_completion_is_idempotent(self):
        from qq_openclaw_bridge.application.media_budget import (
            BudgetExceeded,
            MediaBudgetPolicy,
        )

        policy = MediaBudgetPolicy("1.00")
        reservation = policy.reserve("image", "0.40")

        within_budget = reservation.reconcile("1.20")
        repeated = reservation.reconcile("9.99")
        reservation.release()

        self.assertFalse(within_budget)
        self.assertFalse(repeated)
        self.assertTrue(policy.over_budget)
        self.assertEqual(policy.consumed_cost, Decimal("1.20"))
        self.assertEqual(policy.reserved_cost, Decimal("0"))
        with self.assertRaises(BudgetExceeded):
            policy.reserve("audio", "0")


if __name__ == "__main__":
    unittest.main()
