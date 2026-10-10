# F1 影子路由运维手册

## 边界与健康

F1 只记录 L0-L3 影子决策，不改变 Direct 回复、Outbox 或 QQ 投递。默认关闭；关闭时 routing 健康为正常。启用后，watchdog 只读 SQLite：必须恰有一个 active config、不能有超过 `classifier_timeout_seconds + 60` 秒的 `pending` 决策；纯规则模式到此为止。启用分类器时，持久化 circuit 必须为 `closed`，或为未超过当前 config 一个 `cooldown_seconds` 的 `cooling`。已超过 cooldown 的 `cooling` 在 readiness 中按 effective `closed` 处理，下一次 observer 观察会用 CAS 落盘为 `closed`。watchdog 的 30 秒循环不会调用模型、外部 API 或写入 circuit。

routing 不健康只会让总状态为 `degraded` 并写入健康时间线；当 Direct、OneBot、Gateway 和 Outbox 正常时，它不会重启 Bridge。

## 启用纯规则观察

1. 保持 `ORCHESTRATION_MODE=direct` 或 `legacy_direct` 的现有值不变。在 `.env` 中将 `ENABLE_SHADOW_ROUTING=true`，并保持 `ENABLE_SQLITE_STORE=true`。
2. 启动本地管理台并重启 Bridge：

```bash
scripts/dashboard_service.sh start
scripts/bridge_service.sh restart
scripts/check_ready.sh
```

3. 在 Dashboard 的“影子路由”页确认当前 active config 的“启用分类器”为关闭。此时是 pure-rule，未安装 routing Agent 也可以启动；不要运行 `--probe-routing`，因为该探测会实际发起一次分类请求。
4. 在白名单 QQ 中发送一条普通文本，确认 Direct 回复仍然正常。F1 只增加影子记录，不应增加主 Agent 调用或改变回复内容。

## 安装并开启分类器

分类器使用专用、无工具的 `routing` Agent。先在受控终端提供已批准的 `ROUTING_MODEL` 环境变量；不要将任何密钥放入命令行或终端输出。以下命令必须在项目根目录执行，且只加载受控的本项目 `.env`；变量检查只显示变量名，不回显路径、模型或密钥：

```bash
set -a
source ./.env
set +a
: "${OPENCLAW_CONFIG_PATH:?OPENCLAW_CONFIG_PATH must be set in .env}"
: "${ROUTING_MODEL:?ROUTING_MODEL must be set in the controlled shell}"
.venv/bin/python scripts/install_routing_agent.py \
  --config "$OPENCLAW_CONFIG_PATH" \
  --model "$ROUTING_MODEL" \
  --agent routing
```

在 Dashboard 的“影子路由”页启用“分类器”，保存新版本并确认其模型在允许列表中。然后执行：

```bash
scripts/check_ready.sh
scripts/check_ready.sh --probe-routing
scripts/bridge_service.sh restart
```

`--probe-routing` 是一次有成本的 routing-only 分类探测；只在准备启用或排查分类器时执行。分类器已开启但 routing Agent 缺失时，`scripts/check_ready.sh` 必须失败并指出 Agent 校验问题；先修复 Agent，再继续，不要让 Bridge 反复重启。

## 标注与验收证据

在 Dashboard 的“影子路由”页查看脱敏决策，逐条选择“正确、应升级或应降级”以及正确的 L0-L3。备注只写必要的、无敏感信息的简短说明。

导出验收证据时仅查询聚合 counts/statuses。下面命令不选择 `message_preview`、任何哈希、模型响应、密钥或异常正文：

```bash
set -a
source ./.env
set +a
: "${STATE_DATABASE_PATH:?STATE_DATABASE_PATH must be set in .env}"
sqlite3 -readonly "$STATE_DATABASE_PATH" <<'SQL'
SELECT status, COUNT(*) AS decision_count
FROM route_decisions
GROUP BY status
ORDER BY status;

SELECT verdict, correct_level, COUNT(*) AS label_count
FROM route_labels
GROUP BY verdict, correct_level
ORDER BY verdict, correct_level;
SQL
```

保留 Dashboard 的准入计数与状态证据即可。F2 门槛仍是至少 7 天、100 条决策、30 条标注、至少 90% 标注准确率、0 个强制高风险漏判，以及 P95 Direct 额外延迟不超过 1.5 秒；F1 不会自动接管正式执行。

## 故障处理与回滚

若 routing 健康 degraded，先确认 active config 唯一、处理 stale `pending`（Bridge 启动恢复会将其标为 `interrupted`），并检查 circuit 状态。冷却超过当前 config 一个 cooldown 时，readiness 会按 effective `closed` 报告；observer 在下一次观察时才会 CAS 落盘为 `closed`。不要在 watchdog 中补发模型请求或写入 circuit。

要关闭 F1，在 `.env` 中将 `ENABLE_SHADOW_ROUTING=false`，然后：

```bash
scripts/bridge_service.sh restart
scripts/check_ready.sh
```

历史路由表保留用于审计，不需要删除 SQLite 表或回退 schema。

若只需回退分类配置，在 Dashboard 的“影子路由”页选择要恢复的配置版本并执行“回滚”；系统会创建一个新的 active version。随后执行：

```bash
scripts/check_ready.sh --probe-routing
scripts/bridge_service.sh restart
```

最后验证 F0 Direct E2E：在白名单 QQ 发送一条无风险普通文本，确认收到一次正常私聊回复；再检查 Dashboard 或安全聚合证据，确认 Outbox 没有 `failed` 状态。若需要硬回滚 F0，在 Dashboard 将编排模式切回 `legacy_direct`，重启 Bridge，再重复这条 QQ 文本验证。
