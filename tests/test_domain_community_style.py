from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from qq_openclaw_bridge.domain.community_style import (
    Authorization,
    AuthorizedContent,
    DerivedSample,
    SourceCandidate,
    SourcePage,
    StyleFeatures,
    aggregate_style_profile,
    authorization_matches,
    normalize_subject,
)


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class CommunityStyleDomainTests(unittest.TestCase):
    def _candidate(self, *, platform="reddit", author="Alice", content_id="p1"):
        return SourceCandidate(
            "source-1", platform, "community", content_id, author,
            "2026-09-19T12:00:00Z", "https://example.invalid/post", "source body",
        )

    def _features(self, *, markers=(), directness=0.5, address_terms=()):
        return StyleFeatures(
            "medium", "medium", ("。",), "low", "mixed", directness,
            "low", "low", address_terms, markers, ("personal_attack",),
        )

    def _authorization(self, authorization_id, subject_type, subject_key, scope, expires_at=None, **extra):
        context = {
            "source_id": "source-1",
            "platform": "reddit",
            "community_key": "community",
        }
        context.update(extra)
        return Authorization(authorization_id, subject_type, subject_key, scope, expires_at, **context)

    def _sample(self, sample_id, author, *, markers=(), directness=0.5, status="active", body="body"):
        candidate = self._candidate(author=author, content_id=sample_id)
        content = AuthorizedContent(candidate, body)
        authorization = self._authorization(
            "authorization-1", "author", author, "all_posts_until_revoke",
        )
        return DerivedSample.from_content(
            content, authorization, self._features(markers=markers, directness=directness),
            status=status,
        )

    def test_reddit_author_match_is_case_insensitive_but_not_confusable(self):
        candidate = self._candidate(author="u/Alice")
        allowed = self._authorization("a", "author", "alice", "all_posts_until_revoke")
        confusable = self._authorization("b", "author", "aӏice", "all_posts_until_revoke")

        self.assertTrue(authorization_matches(candidate, allowed, NOW))
        self.assertFalse(authorization_matches(candidate, confusable, NOW))

    def test_tieba_author_match_is_nfc_normalized_but_case_exact(self):
        candidate = self._candidate(platform="tieba", author="Cafe\u0301")
        allowed = Authorization(
            "a", "author", "Café", "all_posts_until_revoke", None,
            source_id="source-1", platform="tieba", community_key="community",
        )
        wrong_case = Authorization(
            "b", "author", "CAFÉ", "all_posts_until_revoke", None,
            source_id="source-1", platform="tieba", community_key="community",
        )

        self.assertTrue(authorization_matches(candidate, allowed, NOW))
        self.assertFalse(authorization_matches(candidate, wrong_case, NOW))

    def test_authorization_is_fail_closed_for_scope_status_expiry_platform_and_naive_clock(self):
        candidate = self._candidate()
        cases = (
            self._authorization("a", "author", "alice", "single_post"),
            self._authorization("b", "post", "p1", "all_posts_until_revoke"),
            self._authorization("c", "author", "alice", "all_posts_until_revoke", status="revoked"),
            self._authorization("d", "author", "alice", "all_posts_until_revoke", NOW - timedelta(seconds=1)),
        )
        for authorization in cases:
            with self.subTest(authorization=authorization.authorization_id):
                self.assertFalse(authorization_matches(candidate, authorization, NOW))
        self.assertFalse(authorization_matches(self._candidate(platform="unknown"), cases[0], NOW))
        active = self._authorization("e", "author", "alice", "all_posts_until_revoke")
        self.assertFalse(authorization_matches(candidate, active, NOW.replace(tzinfo=None)))

    def test_post_authorization_is_scoped_to_the_exact_candidate(self):
        allowed = self._authorization("a", "post", "p1", "single_post")

        self.assertTrue(authorization_matches(self._candidate(content_id="p1"), allowed, NOW))
        self.assertFalse(authorization_matches(self._candidate(content_id="p2"), allowed, NOW))

    def test_authorization_requires_complete_matching_source_platform_and_community_context(self):
        candidate = self._candidate()
        allowed = self._authorization("a", "author", "alice", "all_posts_until_revoke")
        missing_constraints = (
            Authorization("missing-source", "author", "alice", "all_posts_until_revoke", None,
                          platform="reddit", community_key="community"),
            Authorization("missing-platform", "author", "alice", "all_posts_until_revoke", None,
                          source_id="source-1", community_key="community"),
            Authorization("missing-community", "author", "alice", "all_posts_until_revoke", None,
                          source_id="source-1", platform="reddit"),
            self._authorization("wrong-source", "author", "alice", "all_posts_until_revoke", source_id="source-2"),
            self._authorization("wrong-platform", "author", "alice", "all_posts_until_revoke", platform="tieba"),
            self._authorization("wrong-community", "author", "alice", "all_posts_until_revoke", community_key="other"),
        )

        self.assertTrue(authorization_matches(candidate, allowed, NOW))
        for authorization in missing_constraints:
            with self.subTest(authorization=authorization.authorization_id):
                self.assertFalse(authorization_matches(candidate, authorization, NOW))

    def test_unknown_platform_is_rejected_with_an_otherwise_valid_authorization(self):
        candidate = self._candidate(platform="unknown", author="Alice")
        authorization = Authorization(
            "a", "author", "alice", "all_posts_until_revoke", None,
            source_id="source-1", platform="unknown", community_key="community",
        )

        self.assertFalse(authorization_matches(candidate, authorization, NOW))

    def test_normalize_subject_rejects_unknown_platform_without_homoglyph_folding(self):
        self.assertEqual(normalize_subject("reddit", "u/ALICE"), "alice")
        self.assertEqual(normalize_subject("tieba", "Aӏice"), "Aӏice")
        with self.assertRaises(ValueError):
            normalize_subject("unknown", "Alice")

    def test_constructors_normalize_nfc_and_reject_unbounded_or_nonfinite_values_without_echoing(self):
        candidate = self._candidate(author="Cafe\u0301")
        self.assertEqual(candidate.author_key, "Café")
        self.assertIsInstance(SourcePage((candidate,), (("after", "token"),)).items, tuple)
        for directness in (math.nan, math.inf, -math.inf):
            with self.subTest(directness=directness):
                with self.assertRaises(ValueError):
                    self._features(directness=directness)
        secret = "not-for-error-output" + ("x" * 200)
        with self.assertRaises(ValueError) as error:
            SourceCandidate(secret, "reddit", "c", "p", "a", "", "u")
        self.assertNotIn(secret, str(error.exception))
        with self.assertRaises(ValueError):
            StyleFeatures("medium", "medium", tuple("x" for _ in range(65)), "low", "mixed", 0.5,
                          "low", "low", (), (), ())
        with self.assertRaises(ValueError):
            SourceCandidate("source\x00one", "reddit", "c", "p", "a", "", "u")
        with self.assertRaises(ValueError):
            SourceCandidate("source\x1fone", "reddit", "c", "p", "a", "", "u")
        preserved_text = SourceCandidate("source", "reddit", "c", "p", "a", "", "u", "line one\nline two")
        self.assertEqual(preserved_text.text, "line one\nline two")

    def test_aggregate_requires_minimum_active_samples_and_cross_author_marker_support(self):
        only_one_author = tuple(
            self._sample("post-%d" % number, "alice", markers=("讲真",))
            for number in range(5)
        )
        self.assertIsNone(aggregate_style_profile("profile", only_one_author[:4]))
        no_marker_profile = aggregate_style_profile("profile", only_one_author)
        self.assertNotIn("discourse_marker:讲真", tuple(rule.key for rule in no_marker_profile.rules))

        supported = (
            self._sample("post-1", "alice", markers=("讲真",)),
            self._sample("post-2", "bob", markers=("讲真",)),
            self._sample("post-3", "alice", markers=("讲真",)),
            self._sample("post-4", "carol"),
            self._sample("post-5", "dave", status="removed"),
            self._sample("post-6", "erin"),
        )
        profile = aggregate_style_profile("profile", supported, minimum_samples=5)

        self.assertEqual(profile.sample_count, 5)
        self.assertEqual(profile.author_count, 4)
        self.assertIn("discourse_marker:讲真", tuple(rule.key for rule in profile.rules))

    def test_aggregate_uses_median_and_deterministic_rules_without_source_content(self):
        samples = (
            self._sample("post-3", "carol", markers=("讲真",), directness=1.0),
            self._sample("post-1", "alice", markers=("讲真",), directness=0.0),
            self._sample("post-2", "bob", markers=("讲真",), directness=0.5),
            self._sample("post-4", "dave", directness=1.0),
            self._sample("post-5", "erin", directness=0.0),
        )

        forward = aggregate_style_profile("profile", samples)
        reverse = aggregate_style_profile("profile", tuple(reversed(samples)))
        directness = next(rule for rule in forward.rules if rule.key == "directness")

        self.assertEqual(directness.value, "0.50")
        self.assertEqual(forward.rules, reverse.rules)
        self.assertEqual(forward.source_fingerprint, reverse.source_fingerprint)
        rendered = repr(forward)
        self.assertNotIn("source body", rendered)
        self.assertNotIn("example.invalid", rendered)
        self.assertNotIn("alice", rendered.casefold())

    def test_aggregate_deduplicates_identical_content_identities_before_counting_support_and_median(self):
        unique = (
            self._sample("post-1", "alice", markers=("讲真",), directness=0.0),
            self._sample("post-2", "bob", markers=("讲真",), directness=0.0),
            self._sample("post-3", "carol", markers=("讲真",), directness=0.5),
            self._sample("post-4", "dave", directness=1.0),
            self._sample("post-5", "erin", directness=1.0),
        )
        baseline = aggregate_style_profile("profile", unique)
        augmented = aggregate_style_profile("profile", unique + (unique[0], unique[0]))
        only_duplicate = aggregate_style_profile("profile", (unique[0],) * 5)

        self.assertEqual(augmented, baseline)
        self.assertIsNone(only_duplicate)

    def test_aggregate_rejects_conflicting_versions_for_one_content_identity(self):
        conflicting = (
            self._sample("same", "alice", directness=0.0, body="first body"),
            self._sample("same", "alice", directness=1.0, body="second body"),
            self._sample("post-2", "bob"),
            self._sample("post-3", "carol"),
            self._sample("post-4", "dave"),
            self._sample("post-5", "erin"),
        )

        with self.assertRaises(ValueError):
            aggregate_style_profile("profile", conflicting)

    def test_profile_fingerprint_changes_for_content_or_feature_changes_without_exposing_raw_values(self):
        originals = tuple(self._sample("post-%d" % number, "author-%d" % number, body="body-a")
                          for number in range(5))
        changed_content = tuple(self._sample("post-%d" % number, "author-%d" % number, body="body-b")
                                for number in range(5))
        changed_feature = tuple(self._sample("post-%d" % number, "author-%d" % number, body="body-a", directness=0.9)
                                for number in range(5))

        original_profile = aggregate_style_profile("profile", originals)
        content_profile = aggregate_style_profile("profile", changed_content)
        feature_profile = aggregate_style_profile("profile", changed_feature)

        self.assertNotEqual(original_profile.source_fingerprint, content_profile.source_fingerprint)
        self.assertNotEqual(original_profile.source_fingerprint, feature_profile.source_fingerprint)
        self.assertNotIn("body-a", repr(original_profile))
        self.assertNotIn("author-0", repr(original_profile))

    def test_equivalent_signed_zero_duplicate_versions_have_order_independent_profiles(self):
        shared = (
            self._sample("post-1", "alice", directness=0.25),
            self._sample("post-2", "bob", directness=0.5),
            self._sample("post-3", "carol", directness=0.75),
            self._sample("post-4", "dave", directness=1.0),
        )
        positive_zero = self._sample("post-0", "erin", directness=0.0, body="same body")
        negative_zero = self._sample("post-0", "erin", directness=-0.0, body="same body")

        forward = aggregate_style_profile("profile", shared + (positive_zero, negative_zero))
        reverse = aggregate_style_profile("profile", shared + (negative_zero, positive_zero))

        self.assertEqual(forward.rules, reverse.rules)
        self.assertEqual(forward.source_fingerprint, reverse.source_fingerprint)

    def test_aggregate_caps_rules_deterministically_and_reserves_directness_for_maximum_markers(self):
        markers = tuple("marker-%02d" % number for number in range(64))
        samples = tuple(
            self._sample("post-%d" % number, "author-%d" % number, markers=markers)
            for number in range(5)
        )

        profile = aggregate_style_profile("profile", samples)
        reversed_profile = aggregate_style_profile("profile", tuple(reversed(samples)))

        self.assertLessEqual(len(profile.rules), 64)
        self.assertEqual(profile.rules, reversed_profile.rules)
        self.assertEqual(profile.rules[-1].key, "directness")

    def test_style_feature_tokens_fit_the_rule_key_budget(self):
        boundary_marker = "m" * 96
        features = self._features(markers=(boundary_marker,))
        samples = tuple(
            DerivedSample.from_content(
                AuthorizedContent(self._candidate(author="author-%d" % number, content_id="post-%d" % number), "body"),
                self._authorization("authorization-%d" % number, "author", "author-%d" % number, "all_posts_until_revoke"),
                features,
            )
            for number in range(5)
        )

        self.assertIn("discourse_marker:" + boundary_marker,
                      tuple(rule.key for rule in aggregate_style_profile("profile", samples).rules))
        with self.assertRaises(ValueError):
            self._features(markers=("m" * 97,))


if __name__ == "__main__":
    unittest.main()
