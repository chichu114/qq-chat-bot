from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class RateLimiterPairTests(unittest.TestCase):
    def test_pair_accepts_and_charges_both_limiters_atomically(self):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        first = RateLimiter(1, window_seconds=60)
        second = RateLimiter(1, window_seconds=60)

        self.assertEqual(first.allow_pair("g", second, "u", now=10), "allowed")
        self.assertEqual(first.allow_pair("g", second, "u", now=11), "first_limited")
        self.assertFalse(second.allow("u", now=11))

    def test_first_rejection_does_not_touch_second_limiter(self):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        first = RateLimiter(1, window_seconds=60)
        second = RateLimiter(1, window_seconds=60)
        self.assertTrue(first.allow("g", now=10))

        self.assertEqual(first.allow_pair("g", second, "u", now=11), "first_limited")
        self.assertTrue(second.allow("u", now=11))

    def test_second_rejection_does_not_consume_first_limiter(self):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        first = RateLimiter(1, window_seconds=60)
        second = RateLimiter(1, window_seconds=60)
        self.assertTrue(second.allow("u", now=10))

        self.assertEqual(first.allow_pair("g", second, "u", now=11), "second_limited")
        self.assertTrue(first.allow("g", now=11))

    def test_invalid_second_limiter_does_not_consume_first(self):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        first = RateLimiter(1, window_seconds=60)

        with self.assertRaises(TypeError):
            first.allow_pair("g", object(), "u", now=10)
        self.assertTrue(first.allow("g", now=10))

    def test_pair_is_thread_safe_and_never_partially_charges(self):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        first = RateLimiter(5, window_seconds=60)
        second = RateLimiter(5, window_seconds=60)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(
                pool.map(
                    lambda _: first.allow_pair("g", second, "u", now=10),
                    range(100),
                )
            )

        self.assertEqual(results.count("allowed"), 5)
        self.assertEqual(results.count("first_limited"), 95)
        self.assertFalse(first.allow("g", now=11))
        self.assertFalse(second.allow("u", now=11))

    def test_single_allow_remains_thread_safe_and_compatible(self):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        limiter = RateLimiter(3, window_seconds=60)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: limiter.allow("key", now=10), range(20)))

        self.assertEqual(results.count(True), 3)
        self.assertEqual(results.count(False), 17)


if __name__ == "__main__":
    unittest.main()
