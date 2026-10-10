import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from qq_openclaw_bridge.application.style_sync import StyleSyncService
from qq_openclaw_bridge.bridge import Bridge
from qq_openclaw_bridge.config import Config
from qq_openclaw_bridge.domain.community_style import (
    AuthorizedContent,
    SourceCandidate,
    SourcePage,
    StyleFeatures,
)
from qq_openclaw_bridge.models import AgentResponse
from qq_openclaw_bridge.runtime.community_style import create_group_runtime
from qq_openclaw_bridge.runtime.community_style_config import (
    AuthorizationSettings,
    CommunityStyleSettings,
    GroupBindingSettings,
    OllamaSettings,
    SourceSettings,
)
from qq_openclaw_bridge.runtime.ollama import OllamaReply
from qq_openclaw_bridge.storage import WorkspaceStateStore


NOW = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
SOURCE_FIXTURE_SENTENCE = "SOURCE_FIXTURE_SENTENCE"
AUTH_SUBJECT = "authorized-post-"
EVIDENCE = "private-consent-evidence"
PRIVATE_TOOLS = ("/todo", "/memory", "private_tool_secret")


def _features(*, length="short", marker="不过", directness=0.8):
    return StyleFeatures(
        length,
        "short",
        ("，",),
        "low",
        "mostly_chinese",
        directness,
        "low",
        "low",
        (),
        (marker,),
        ("personal_attack",),
    )


class _Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds=301):
        self.value += timedelta(seconds=seconds)


class _Source:
    def __init__(self, source_id, candidates, phase):
        self.source_id = source_id
        self.candidates = tuple(candidates)
        self.phase = phase
        self.fetches = []

    def list_candidates(self, cursor, limit):
        del cursor
        return SourcePage(self.candidates[:limit], (("pn", str(self.phase[0])),))

    def fetch_content(self, candidate):
        self.fetches.append(candidate.external_content_id)
        return AuthorizedContent(
            candidate,
            "%s %s %s phase-%s" % (
                SOURCE_FIXTURE_SENTENCE,
                self.source_id,
                candidate.external_content_id,
                self.phase[0],
            ),
        )


class _Analyzer:
    def __init__(self, phase):
        self.phase = phase
        self.inputs = []

    def analyze(self, text):
        self.inputs.append(text)
        if "source-two" in text:
            return _features(length="long", marker="总之", directness=0.2)
        if self.phase[0] == 1:
            return _features(length="short", marker="不过", directness=0.8)
        return _features(length="long", marker="所以", directness=0.4)


class _Reloader:
    def __init__(self, settings):
        self.settings = settings

    def current(self):
        return self.settings

    def reload_if_changed(self):
        return False


class _GroupOllama:
    def __init__(self):
        self.calls = []

    def chat(self, messages, *, response_schema, options):
        self.calls.append((messages, response_schema, options))
        return OllamaReply("这是隔离后的群聊回复")


class _Rpc:
    def __init__(self, bot_qq):
        self.bot_qq = bot_qq
        self.calls = []
        self.group_sends = []
        self.next_message_id = 9000

    async def request(self, websocket, action, params, timeout=5):
        del websocket, timeout
        copied = json.loads(json.dumps(params, ensure_ascii=False))
        self.calls.append((action, copied))
        if action == "get_msg":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"sender": {"user_id": self.bot_qq}},
            }
        if action == "send_group_msg":
            self.next_message_id += 1
            self.group_sends.append(copied)
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": self.next_message_id},
            }
        raise AssertionError("unexpected RPC action")

    def consume(self, payload):
        del payload
        return False

    def fail_pending(self, error=None):
        del error


class _OpenClaw:
    def __init__(self):
        self.calls = []

    def reply(self, request):
        self.calls.append(request)
        return AgentResponse("私聊回复")


class _WebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class CommunityStyleAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = WorkspaceStateStore(
            Path(self.temporary.name) / "acceptance.sqlite3"
        )
        self.repository = self.store.community_style_repository
        self.clock = _Clock()
        self.phase = [1]
        self.bot_qq = 999001
        self.group_one = 77001
        self.group_two = 77002
        self.source_one = self._source_settings("source-one", "profile-one", 1)
        self.source_two = self._source_settings("source-two", "profile-two", 101)
        self.settings = self._settings(self.source_one, self.source_two)
        self.repository.sync_configuration(self.settings, self.clock())
        self.sources = {
            "source-one": _Source(
                "source-one", self._candidates("source-one", 1), self.phase
            ),
            "source-two": _Source(
                "source-two", self._candidates("source-two", 101), self.phase
            ),
        }
        self.analyzer = _Analyzer(self.phase)
        self.learner = StyleSyncService(
            self.repository,
            self.sources,
            self.analyzer,
            jitter=lambda: 0.0,
            clock=self.clock,
        )

    def _source_settings(self, source_id, profile, first_post):
        grants = tuple(
            AuthorizationSettings(
                "post",
                "%s%d" % (AUTH_SUBJECT, index),
                "single_post",
                "%s-%d" % (EVIDENCE, index),
                NOW.isoformat(),
            )
            for index in range(first_post, first_post + 5)
        )
        return SourceSettings(
            source_id, "tieba", "approved-board", profile, 300, grants
        )

    def _settings(self, source_one, source_two=None):
        sources = (source_one,) if source_two is None else (source_one, source_two)
        groups = [GroupBindingSettings(self.group_one, source_one.profile, 1, 800)]
        if source_two is not None:
            groups.append(
                GroupBindingSettings(self.group_two, source_two.profile, 1, 800)
            )
        return CommunityStyleSettings(
            True,
            OllamaSettings("http://127.0.0.1:11434", "qwen3:8b", False),
            sources,
            tuple(groups),
            "acceptance-config-v1",
        )

    def _candidates(self, source_id, first_post, *, include_unauthorized=False):
        values = [
            SourceCandidate(
                source_id,
                "tieba",
                "approved-board",
                "%s%d" % (AUTH_SUBJECT, index),
                "author-%d" % index,
                NOW.isoformat(),
                "https://tieba.baidu.com/p/%d" % index,
            )
            for index in range(first_post, first_post + 5)
        ]
        if include_unauthorized:
            values.append(
                SourceCandidate(
                    source_id,
                    "tieba",
                    "approved-board",
                    "unauthorized-post",
                    "unauthorized-author",
                    NOW.isoformat(),
                    "https://tieba.baidu.com/p/999999",
                )
            )
        return tuple(values)

    def _config(self):
        return Config(
            onebot_ws_url="ws://127.0.0.1:3001",
            allowed_qq_users={88001},
            bot_qq=self.bot_qq,
            enable_group_agent=True,
            style_agent_config_path="unused-by-injected-reloader.json",
            enable_style_memory=False,
            enable_housekeeper=False,
            enable_reminder_push=False,
            enable_security_confirmation=False,
            agent_retry_attempts=1,
            rate_limit_messages_per_minute=100,
        )

    def _group_event(
        self, message_id, *, group_id=None, mention=False, reply_id=None, text="怎么看？"
    ):
        segments = []
        if mention:
            segments.append({"type": "at", "data": {"qq": str(self.bot_qq)}})
        if reply_id is not None:
            segments.append({"type": "reply", "data": {"id": str(reply_id)}})
        segments.append({"type": "text", "data": {"text": text}})
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": self.group_one if group_id is None else group_id,
            "user_id": 88001,
            "message_id": message_id,
            "message": segments,
            "time": self.clock().timestamp(),
        }

    def _private_event(self, message_id=5001):
        return {
            "post_type": "message",
            "message_type": "private",
            "user_id": 88001,
            "message_id": message_id,
            "message": [{"type": "text", "data": {"text": "私聊问题"}}],
            "time": self.clock().timestamp(),
        }

    def _bridge(self):
        group_ollama = _GroupOllama()
        rpc = _Rpc(self.bot_qq)
        reloader = _Reloader(self.settings)
        runtime = create_group_runtime(
            self._config(),
            object(),
            repository_factory=lambda state_store: self.repository,
            config_reloader_factory=lambda path: reloader,
            rpc_factory=lambda: rpc,
            ollama_factory=lambda settings, **kwargs: group_ollama,
            clock_factory=lambda: self.clock,
        )
        openclaw = _OpenClaw()
        bridge = Bridge(
            self._config(),
            openclaw,
            group_runtime=runtime,
            now_provider=self.clock,
        )
        return bridge, group_ollama, rpc, openclaw

    async def test_authorized_sync_to_explicit_group_reply_is_private_and_isolated(self):
        self.sources["source-one"].candidates = self._candidates(
            "source-one", 1, include_unauthorized=True
        )
        first = await self.learner.sync_source("source-one", self.clock())
        second = await self.learner.sync_source("source-two", self.clock())

        self.assertEqual((first.derived_count, first.unauthorized_count), (5, 1))
        self.assertEqual((second.derived_count, second.unauthorized_count), (5, 0))
        self.assertNotIn(
            "unauthorized-post", self.sources["source-one"].fetches
        )
        self.assertEqual(self.repository.active_profile("profile-one")["sample_count"], 5)
        self.assertEqual(self.repository.active_profile("profile-two")["sample_count"], 5)

        bridge, group_ollama, rpc, openclaw = self._bridge()
        websocket = _WebSocket()

        ordinary = self._group_event(6001)
        mentioned = self._group_event(6002, mention=True)
        self.assertFalse(await bridge.handle_event(ordinary, websocket))
        self.assertTrue(await bridge.handle_event(mentioned, websocket))
        outbound_id = rpc.next_message_id
        self.assertTrue(
            self.repository.is_bot_message(self.group_one, str(outbound_id))
        )

        # Replay after the cooldown, so receipt idempotency -- not the limiter --
        # is what rejects this duplicate message ID.
        self.clock.advance(2)
        self.assertFalse(await bridge.handle_event(mentioned, websocket))
        self.assertEqual(len(group_ollama.calls), 1)
        self.assertEqual(len(rpc.group_sends), 1)

        self.clock.advance(2)
        reply = self._group_event(6003, reply_id=outbound_id, text="再说说")
        self.assertTrue(await bridge.handle_event(reply, websocket))
        self.assertEqual(len(group_ollama.calls), 2)
        self.assertEqual(len(rpc.group_sends), 2)

        self.clock.advance(2)
        other_group = self._group_event(
            6004, group_id=self.group_two, mention=True, text="另一个群的问题"
        )
        self.assertTrue(await bridge.handle_event(other_group, websocket))
        self.assertEqual(len(group_ollama.calls), 3)
        one_style = json.loads(group_ollama.calls[0][0][1]["content"])
        two_style = json.loads(group_ollama.calls[2][0][1]["content"])
        one_values = {rule["value"] for rule in one_style["style_card"]["rules"]}
        two_values = {rule["value"] for rule in two_style["style_card"]["rules"]}
        self.assertIn("不过", one_values)
        self.assertNotIn("总之", one_values)
        self.assertIn("总之", two_values)
        self.assertNotIn("不过", two_values)

        self.assertTrue(await bridge.handle_event(self._private_event(), websocket))
        self.assertEqual(len(openclaw.calls), 1)
        self.assertEqual(len(group_ollama.calls), 3)

        rendered = json.dumps(
            {
                "prompts": [call[0] for call in group_ollama.calls],
                "rpc": rpc.calls,
                "websocket": websocket.sent,
            },
            ensure_ascii=False,
        )
        for forbidden in (
            SOURCE_FIXTURE_SENTENCE,
            AUTH_SUBJECT,
            EVIDENCE,
            *PRIVATE_TOOLS,
        ):
            self.assertNotIn(forbidden, rendered)

    async def test_profile_hot_switch_and_withdrawal_rebuild_remove_contribution(self):
        await self.learner.sync_source("source-one", self.clock())
        first = self.repository.active_profile("profile-one")
        self.assertEqual(first["version"], 1)

        bridge, group_ollama, rpc, unused = self._bridge()
        self.assertTrue(
            await bridge.handle_event(self._group_event(7001, mention=True), _WebSocket())
        )
        first_style = json.loads(group_ollama.calls[-1][0][1]["content"])
        self.assertIn(
            "short",
            {rule["value"] for rule in first_style["style_card"]["rules"]},
        )

        self.phase[0] = 2
        self.clock.advance()
        switched = await self.learner.sync_source("source-one", self.clock())
        active = self.repository.active_profile("profile-one")
        self.assertEqual((switched.profile_version, active["version"]), (2, 2))
        self.assertTrue(
            await bridge.handle_event(self._group_event(7002, mention=True), _WebSocket())
        )
        latest_style = json.loads(group_ollama.calls[-1][0][1]["content"])
        latest_values = {
            rule["value"] for rule in latest_style["style_card"]["rules"]
        }
        self.assertIn("long", latest_values)
        self.assertIn("所以", latest_values)
        self.assertNotIn("不过", latest_values)

        remaining = self.source_one.authorizations[1:]
        withdrawn_source = replace(self.source_one, authorizations=remaining)
        withdrawn_settings = self._settings(withdrawn_source, self.source_two)
        affected = self.repository.sync_configuration(withdrawn_settings, self.clock())
        self.assertEqual(affected, ("profile-one",))
        self.assertIsNone(self.repository.active_profile("profile-one"))
        remaining_samples = self.repository.active_samples("profile-one")
        self.assertEqual(len(remaining_samples), 4)
        self.assertNotIn(
            "%s1" % AUTH_SUBJECT,
            {sample.external_content_id for sample in remaining_samples},
        )

        replacement = AuthorizationSettings(
            "post",
            "%s6" % AUTH_SUBJECT,
            "single_post",
            "%s-6" % EVIDENCE,
            NOW.isoformat(),
        )
        rebuilt_source = replace(
            withdrawn_source, authorizations=remaining + (replacement,)
        )
        self.settings = self._settings(rebuilt_source, self.source_two)
        self.repository.sync_configuration(self.settings, self.clock())
        self.sources["source-one"].candidates = self._candidates("source-one", 2)
        self.clock.advance()
        rebuilt = await self.learner.sync_source("source-one", self.clock())
        rebuilt_profile = self.repository.active_profile("profile-one")
        self.assertEqual(rebuilt.derived_count, 5)
        self.assertEqual(rebuilt_profile["sample_count"], 5)
        self.assertGreater(rebuilt_profile["version"], active["version"])
        active_ids = {
            sample.external_content_id
            for sample in self.repository.active_samples("profile-one")
        }
        self.assertEqual(
            active_ids,
            {"%s%d" % (AUTH_SUBJECT, index) for index in range(2, 7)},
        )


class CheckReadyStyleAggregationTests(unittest.TestCase):
    def _run(self, enabled, *, style_exit=0):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            scripts = project / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "check_ready.sh", scripts / "check_ready.sh")
            scripts.joinpath("check_style_agent.py").write_text(
                "raise SystemExit(%d)\n" % style_exit, encoding="utf-8"
            )
            venv = project / ".venv" / "bin"
            venv.mkdir(parents=True)
            call_log = project / "python-calls.log"
            fake_python = venv / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$PYTHON_CALL_LOG\"\n"
                "if [[ \"${1:-}\" == '-' ]]; then /bin/cat >/dev/null; exit 0; fi\n"
                "if [[ \"${1:-}\" == *check_style_agent.py ]]; then exit \"$STYLE_EXIT\"; fi\n"
                "exit 99\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            group_flag = (
                "" if enabled is None else "ENABLE_GROUP_AGENT=%s\n" % (
                    "true" if enabled else "false"
                )
            )
            (project / ".env").write_text(
                "ONEBOT_WS_URL=not-a-websocket\n"
                "AGENT_ADAPTER=echo\n"
                "%s"
                "STYLE_AGENT_CONFIG_PATH=%s\n"
                "STATE_DATABASE_PATH=%s\n"
                % (
                    group_flag,
                    project / "must-not-open.json",
                    project / "must-not-open.sqlite3",
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {"PYTHON_CALL_LOG": str(call_log), "STYLE_EXIT": str(style_exit)}
            )
            result = subprocess.run(
                [str(scripts / "check_ready.sh")],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            calls = call_log.read_text(encoding="utf-8").splitlines()
            return result, calls

    def test_disabled_style_check_is_one_fixed_line_and_never_invokes_helper(self):
        for configured in (False, None):
            with self.subTest(configured=configured):
                result, calls = self._run(configured)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    [
                        line
                        for line in result.stdout.splitlines()
                        if "community style agent" in line
                    ],
                    ["[SKIP] community style agent: disabled"],
                )
                self.assertEqual(calls, ["-"])

    def test_enabled_style_check_is_read_only_helper_and_failure_is_aggregated(self):
        result, calls = self._run(True, style_exit=7)

        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], "-")
        self.assertIn("check_style_agent.py --project-dir", calls[1])


if __name__ == "__main__":
    unittest.main()
