import unittest


class Memory2DomainTests(unittest.TestCase):
    def test_keywords_and_vector_ranking_favor_related_memory(self):
        from qq_openclaw_bridge.domain.memory2 import (
            build_feature_vector,
            extract_keywords,
            rank_memory_records,
        )

        records = [
            {
                "id": 1,
                "layer": "long_term",
                "content": "用户不吃香菜，点餐时需要去掉香菜",
                "keywords": extract_keywords("用户不吃香菜，点餐时需要去掉香菜"),
                "vector": build_feature_vector("用户不吃香菜，点餐时需要去掉香菜"),
            },
            {
                "id": 2,
                "layer": "long_term",
                "content": "QQ 智能体项目使用 Python",
                "keywords": extract_keywords("QQ 智能体项目使用 Python"),
                "vector": build_feature_vector("QQ 智能体项目使用 Python"),
            },
        ]

        ranked = rank_memory_records("今晚点餐记得我的忌口", records, limit=2)

        self.assertEqual(ranked[0]["id"], 1)
        self.assertGreater(ranked[0]["relevance"], ranked[1]["relevance"])

    def test_context_budget_keeps_profile_before_summary(self):
        from qq_openclaw_bridge.domain.memory2 import build_memory_context

        context = build_memory_context(
            persona_rules=("你叫 chichu", "默认中文"),
            profile=("用户不吃香菜",),
            facts=("QQ 项目使用 Python",),
            summaries=("之前讨论过一个很长很长的项目规划" * 20,),
            max_chars=120,
        )

        self.assertIn("你叫 chichu", context)
        self.assertIn("用户不吃香菜", context)
        self.assertLessEqual(len(context), 120)

    def test_rule_deduplication_normalizes_bullets_and_spaces(self):
        from qq_openclaw_bridge.domain.memory2 import deduplicate_rules

        rules = deduplicate_rules(
            ("- 默认中文。", "默认中文", "  先办事，再吐槽。 ", "先办事，再吐槽")
        )

        self.assertEqual(rules, ("默认中文。", "先办事，再吐槽。"))


if __name__ == "__main__":
    unittest.main()

