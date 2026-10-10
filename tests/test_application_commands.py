from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ApplicationCommandTests(unittest.TestCase):
    def _request(self):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(1, "m1", "测试", 1.0)

    def test_command_chain_stops_at_first_matching_handler(self):
        from qq_openclaw_bridge.application.commands import (
            CallableCommandHandler,
            CommandChain,
        )

        calls = []

        def first(request):
            calls.append("first")
            return None

        def second(request):
            calls.append("second")
            return "本地回复"

        def third(request):
            calls.append("third")
            return "不应执行"

        chain = CommandChain(
            tuple(CallableCommandHandler(item) for item in (first, second, third))
        )

        self.assertEqual(chain.handle(self._request()), "本地回复")
        self.assertEqual(calls, ["first", "second"])

    def test_command_chain_returns_none_when_no_handler_matches(self):
        from qq_openclaw_bridge.application.commands import (
            CallableCommandHandler,
            CommandChain,
        )

        chain = CommandChain((CallableCommandHandler(lambda request: None),))

        self.assertIsNone(chain.handle(self._request()))

    def test_command_chain_does_not_swallow_handler_errors(self):
        from qq_openclaw_bridge.application.commands import (
            CallableCommandHandler,
            CommandChain,
        )

        def broken(request):
            raise ValueError("broken handler")

        chain = CommandChain((CallableCommandHandler(broken),))

        with self.assertRaisesRegex(ValueError, "broken handler"):
            chain.handle(self._request())


if __name__ == "__main__":
    unittest.main()
