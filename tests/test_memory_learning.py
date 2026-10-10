import tempfile
import unittest
from pathlib import Path


class _Analyzer:
    model_name = "test-light"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def analyze(self, user_id, turns):
        self.calls.append((user_id, turns))
        if self.error is not None:
            raise self.error
        return self.result


class MemoryAnalysisTests(unittest.TestCase):
    def test_parse_json_analysis_filters_transient_and_low_confidence_facts(self):
        from qq_openclaw_bridge.application.memory_learning import parse_analysis_response

        response = """```json
        {
          "summary": "用户在开发 QQ 管家，并说明自己不吃香菜。",
          "facts": [
            {"content": "用户不吃香菜", "layer": "profile", "category": "preference", "confidence": 0.95, "important": true},
            {"content": "用户现在有点困", "layer": "profile", "category": "temporary", "confidence": 0.99, "important": false},
            {"content": "项目也许用 Rust", "layer": "long_term", "category": "project", "confidence": 0.4, "important": true}
          ]
        }
        ```"""

        result = parse_analysis_response(response, min_confidence=0.75)

        self.assertEqual(result.summary, "用户在开发 QQ 管家，并说明自己不吃香菜。")
        self.assertEqual(len(result.facts), 1)
        self.assertEqual(result.facts[0].content, "用户不吃香菜")

    def test_openclaw_analyzer_uses_an_isolated_session_for_each_batch(self):
        from qq_openclaw_bridge.application.memory_learning import OpenClawMemoryAnalyzer
        from qq_openclaw_bridge.models import AgentResponse

        class Adapter:
            def __init__(self):
                self.request = None

            def reply(self, request):
                self.request = request
                return AgentResponse(text='{"summary":"摘要","facts":[]}')

        adapter = Adapter()
        analyzer = OpenClawMemoryAnalyzer(
            adapter,
            "openai/gpt-5.4-nano",
            session_prefix="agent:memory:test",
        )

        analyzer.analyze(
            42,
            (
                {"id": 10, "role": "user", "content": "你好"},
                {"id": 11, "role": "assistant", "content": "又见面了"},
            ),
        )

        self.assertEqual(adapter.request.session_key, "agent:memory:test-42-10-11")


class MemoryLearningServiceTests(unittest.IsolatedAsyncioTestCase):
    def _repository(self, root):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.memory import MemoryRepository

        return MemoryRepository(SqliteDatabase(Path(root) / "state.sqlite3"))

    @staticmethod
    def _request():
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(42, "m-10", "我不吃香菜", 1.0)

    async def test_observe_records_turns_and_runs_light_analysis_in_background(self):
        from qq_openclaw_bridge.application.memory_learning import (
            AnalysisResult,
            CandidateFact,
            MemoryLearningService,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = self._repository(temp_dir)
            analyzer = _Analyzer(
                AnalysisResult(
                    summary="用户说明自己不吃香菜。",
                    facts=(
                        CandidateFact(
                            content="用户不吃香菜",
                            layer="profile",
                            category="preference",
                            confidence=0.96,
                        ),
                    ),
                )
            )
            service = MemoryLearningService(
                repository,
                analyzer,
                summary_trigger_turns=2,
                summary_batch_turns=10,
            )

            await service.observe(self._request(), "行，点餐时我盯着。")
            await service.wait()

            self.assertEqual(len(analyzer.calls), 1)
            self.assertEqual(repository.stats(42)["summaries"], 1)
            pending = repository.list_facts(42, status="pending")
            self.assertEqual(pending[0]["content"], "用户不吃香菜")

    async def test_analyzer_failure_uses_local_summary_without_creating_facts(self):
        from qq_openclaw_bridge.application.memory_learning import MemoryLearningService

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = self._repository(temp_dir)
            service = MemoryLearningService(
                repository,
                _Analyzer(error=RuntimeError("model down")),
                summary_trigger_turns=2,
                summary_batch_turns=10,
            )

            await service.observe(self._request(), "知道了。")
            await service.wait()

            summaries = repository.recent_summaries(42)
            self.assertEqual(summaries[0]["model"], "local-fallback")
            self.assertEqual(repository.list_facts(42, status="pending"), [])
            run = repository.latest_analysis_run(42)
            self.assertEqual(run["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
