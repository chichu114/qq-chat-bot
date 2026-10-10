import unittest

from tests import test_bridge as bridge_fakes


class E4AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = bridge_fakes.BridgeTests()

    async def test_disabled_e4_preserves_existing_text_flow(self):
        from qq_openclaw_bridge.bridge import Bridge

        pipeline = bridge_fakes._FakePipeline(
            error=AssertionError("media must stay disabled")
        )
        adapter = bridge_fakes._FakeAdapter(text="原有文本回复")
        websocket = bridge_fakes._FakeWebSocket()
        bridge = Bridge(
            self.harness._config(
                enable_media_pipeline=False,
                enable_long_tasks=False,
            ),
            adapter,
            media_runtime=self.harness._media_runtime(pipeline=pipeline),
        )

        accepted = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 9901,
                "message": "你好",
                "time": 10,
            },
            websocket,
        )

        self.assertTrue(accepted)
        self.assertEqual([request.text for request in adapter.requests], ["你好"])
        self.assertEqual(pipeline.calls, [])
        self.assertEqual(
            [item["params"]["message"] for item in websocket.sent],
            ["原有文本回复"],
        )

    async def test_disabled_e4_media_notice_is_equivalent_in_both_f0_modes(self):
        from tests.test_f0_acceptance import F0AcceptanceHarness

        harness = F0AcceptanceHarness()
        scenario = harness.scenario("e4_disabled_media")
        legacy = await harness.run_mode("legacy_direct", scenario)
        direct = await harness.run_mode("direct", scenario)

        self.assertEqual(direct, legacy)
        self.assertEqual(direct.reply, "第一版只支持文本消息。")
        self.assertEqual(direct.agent_calls, 0)

    async def test_image_long_task_reports_progress_and_reaches_final_reply(self):
        from qq_openclaw_bridge.application.media_pipeline import MediaArtifact
        from qq_openclaw_bridge.bridge import Bridge

        pipeline = bridge_fakes._FakePipeline(
            (
                MediaArtifact(
                    "image",
                    "图片描述",
                    "一张白板架构图",
                    "ready",
                    "asset-acceptance",
                ),
            )
        )
        tasks = bridge_fakes._FakeTaskService(
            notice="任务 #task-44 已接单，正在处理。"
        )
        adapter = bridge_fakes._FakeAdapter(text="最终回答：架构图已整理。")
        websocket = bridge_fakes._FakeWebSocket()
        bridge = Bridge(
            self.harness._config(
                enable_media_pipeline=True,
                enable_long_tasks=True,
            ),
            adapter,
            media_runtime=self.harness._media_runtime(
                pipeline=pipeline,
                task_service=tasks,
                task_commands=bridge_fakes._FakeTaskCommands(),
            ),
        )

        accepted = await bridge.handle_event(
            self.harness._image_event(message_id=9902, text="整理这张图"),
            websocket,
        )

        self.assertTrue(accepted)
        self.assertEqual(len(pipeline.calls), 1)
        self.assertIn("一张白板架构图", adapter.requests[0].media_context)
        self.assertIn(
            ("task-44", "downloading", "attachment=1 kind=image"),
            tasks.reports,
        )
        self.assertEqual(
            [item["params"]["message"] for item in websocket.sent],
            ["任务 #task-44 已接单，正在处理。", "最终回答：架构图已整理。"],
        )


if __name__ == "__main__":
    unittest.main()
