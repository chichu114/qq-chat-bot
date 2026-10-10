from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _Reply:
    def __init__(self, content):
        self.content = content


class _Ollama:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error is not None:
            raise self.error
        return _Reply(self.content)


class GroupInterjectionDeciderTests(unittest.TestCase):
    def test_casual_chat_approval_is_read_from_strict_boolean_json(self):
        from qq_openclaw_bridge.application.group_interjection import (
            GroupInterjectionDecider,
        )

        allowed = GroupInterjectionDecider(
            _Ollama('{"should_reply":true}')
        )
        declined = GroupInterjectionDecider(
            _Ollama('{"should_reply":false}')
        )

        self.assertTrue(allowed.should_reply("这也太离谱了吧"))
        self.assertFalse(declined.should_reply("哈哈确实"))

    def test_non_conversational_messages_are_rejected_before_model_call(self):
        from qq_openclaw_bridge.application.group_interjection import (
            GroupInterjectionDecider,
        )

        class ExplodingOllama:
            def chat(self, *args, **kwargs):
                raise AssertionError("prefiltered text must not reach Ollama")

        decider = GroupInterjectionDecider(ExplodingOllama())
        for text in (
            "https://example.com/a",
            "/help",
            "😂😂😂",
            "？！……",
            "a" * 1001,
        ):
            with self.subTest(text=text[:20]):
                self.assertFalse(decider.should_reply(text))

    def test_model_failure_or_invalid_response_fails_closed(self):
        from qq_openclaw_bridge.application.group_interjection import (
            GroupInterjectionDecider,
        )

        cases = (
            _Ollama(error=RuntimeError("secret")),
            _Ollama("not-json"),
            _Ollama("{}"),
            _Ollama('{"should_reply":1}'),
            _Ollama('{"should_reply":true,"extra":1}'),
        )
        for ollama in cases:
            with self.subTest(content=ollama.content):
                self.assertFalse(
                    GroupInterjectionDecider(ollama).should_reply("接个梗试试")
                )


if __name__ == "__main__":
    unittest.main()
