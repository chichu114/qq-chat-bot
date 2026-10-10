# F0 编排控制平面收口设计

## 1. 背景

当前 QQ 私聊智能体已经具备 E1 安全与数据底座、E2 常驻 Gateway 与可靠消息链路、E2.5 模块化接口、E3 长期记忆 2.0 和默认关闭的 E4-A 媒体/长任务能力。

E2.5 已定义 `PlanningStrategy`、`ExecutionEngine`、`VerificationStrategy`、`BudgetPolicy`、`CapabilityRegistry`、`BrainstormGate` 和 `OrchestrationTraceSink` 等接口，但生产运行仍由 `Bridge` 直接创建 `DirectPlanningStrategy + SingleAgentExecutionEngine + CompatibilityVerificationStrategy`。双模型议会、四级路由、DAG、能力档案选模、分级验收和预算保险丝尚未进入真实执行链。

F0 是 A++ 上层建筑的第一条纵向切片。它不增加模型调用、不改变 QQ 回复，而是先统一编排装配权、状态权、审批权、可观测性和回滚权，使后续 F1-F5 能通过替换策略渐进上线，不再反复拆改 OneBot、Outbox 和 `Bridge`。

## 2. 目标

- 将编排组件的创建从 `Bridge` 移到 `runtime/composition.py`。
- 建立单一 `OrchestrationRuntime` 应用入口和可替换策略槽位。
- 在新 Runtime 中保持现有 Direct 单模型行为和调用次数完全不变。
- 为每次编排建立可审计的运行记录、事件记录和配置版本快照。
- 将高风险操作从原始文本确认升级为不可变 `ActionIntent`。
- 建立正式 LaunchAgent、结构化日志轮转和加密备份。
- 提供明确的旧 Direct 硬回退路径，并完成真实回滚演练。
- 在管理台显化运行模式、配置版本、近期编排和回退状态。

## 3. 非目标

- F0 不实现四级智能路由。
- F0 不启用架构师 A、审计师 B 或仲裁官模型。
- F0 不自动拆分 DAG，也不并行调用廉价 Worker。
- F0 不新增模型、API 凭据或 Token 消耗。
- F0 不增加 QQ Agent 的工具权限，不接入日历、邮件或项目管理。
- F0 不启用 E4-A 媒体或长任务功能。
- F0 不删除现有兼容外观和旧 Direct 回退路径。

## 4. 核心原则

### 4.1 确定性控制平面拥有最终决策权

白名单、消息去重、用户会话锁、审批、预算、状态迁移、重试、熔断和 Outbox 投递均由 Python 控制平面决定。模型只能生成文本或结构化提议，不能修改控制策略、审批状态或执行结果。

### 4.2 影子和未来智能策略不得影响当前正式回复

F0 只运行 Direct 策略。后续影子策略的异常、超时、格式错误或持久化失败必须被隔离，正式回复仍走 Direct 基线。

### 4.3 安全失败关闭，可观测性失败降级

- `ActionIntent`、审批所有权、参数哈希和执行前校验失败时严格禁止执行。
- 普通编排 trace 写入失败时记录受控日志，不阻断当轮普通文本回复。
- Outbox、白名单、去重和现有 SQLite 正式状态继续沿用已有失败语义。

### 4.4 每一阶段都可回滚

新 schema 只追加表和索引。旧 Direct 模式忽略新表。运行模式由单一受控配置选择，回滚不需要数据库降级。

## 5. 总体架构

```text
QQ / NapCat OneBot
  -> Inbound parser / whitelist / dedupe
  -> EventDispatcher / per-user lock
  -> CommandChain
  -> MessageCoordinator
  -> OrchestrationRuntime
       -> PlanningStrategy slot
       -> ExecutionEngine slot
       -> VerificationStrategy slot
       -> BudgetPolicy slot
       -> CapabilityRegistry slot
       -> BrainstormGate slot
       -> OrchestrationTraceSink
  -> ReliableDeliveryService
  -> SQLite Outbox
  -> QQ
```

F0 的默认插槽实现为：

- `DirectPlanningStrategy`
- `SingleAgentExecutionEngine`
- `CompatibilityVerificationStrategy`
- `ExistingLimitBudgetPolicy`
- `StaticCapabilityRegistry`
- `DisabledBrainstormGate`
- `SqliteOrchestrationTraceSink`

## 6. 模块设计

### 6.1 `OrchestrationRuntime`

新增应用级运行入口，接收 `TaskContract`，依次完成预算预检、规划、执行、验证和 trace 记录，并返回 `ExecutionResult` 或受控异常。

Runtime 不知道 OneBot、WebSocket、Dashboard、文件路径或 OpenClaw CLI 命令。模型调用仍由注入的 `ExecutionEngine` 使用现有 `AgentAdapter` 完成。

Runtime 的依赖全部通过构造函数注入，不读取全局环境变量，不创建 SQLite 连接，不创建模型适配器。

### 6.2 Composition Root

`runtime/composition.py` 成为唯一生产装配入口，负责：

1. 创建共享 SQLite 状态库。
2. 创建主 Agent、记忆、媒体和领域服务。
3. 根据 `ORCHESTRATION_MODE` 创建编排 Runtime。
4. 将 Runtime 注入 `MessageCoordinator` 或 `Bridge` 兼容外观。
5. 在配置无效时拒绝激活新模式，并保留上一份有效配置。

`Bridge` 不再直接实例化 planner、executor 或 verifier，只保留接入、兼容和委托职责。

### 6.3 运行模式

`ORCHESTRATION_MODE` 只允许以下值：

- `legacy_direct`：绕过 `OrchestrationRuntime`，使用当前兼容 Direct 路径；作为硬回退。
- `direct`：使用新 Runtime，内部仍是一次单 Agent Direct 调用；F0 正式模式。
- `shadow`：为 F1 预留；F0 中配置该值必须拒绝启动，避免伪装已实现。
- `active`：为后续阶段预留；F0 中配置该值必须拒绝启动。

首次部署先使用 `legacy_direct` 完成基线烟测，再切换到 `direct`。任何异常可改回 `legacy_direct` 并重启，目标恢复时间不超过 60 秒。

### 6.4 Dashboard

管理台新增“编排控制”页面，F0 只提供以下能力：

- 显示当前运行模式和 active config version。
- 显示最近编排运行、阶段、耗时、是否 fallback 和安全错误摘要。
- 显示 Direct 调用次数与基线差异。
- 显示 LaunchAgent、日志轮转和备份加密状态。
- 通过现有 Dashboard Token、Host 和 Origin 校验修改 `legacy_direct/direct`。

管理台不得在 F0 配置尚未实现的模型角色，也不得返回 ActionIntent 完整 payload、密钥或原始模型内容。

## 7. 编排数据模型

SQLite schema 从 v4 追加迁移到 v5，不删除或修改旧表语义。

### 7.1 `orchestration_runs`

保存：

- `run_id`
- `user_id`
- `source_message_id`
- `session_key`
- `mode`
- `risk_level`
- `status`
- `config_version`
- `started_at`
- `finished_at`
- `fallback_reason`
- `safe_error_class`

状态为：

```text
received -> planned -> executing -> verifying -> accepted -> delivered
         -> fallback
         -> rejected
         -> failed
         -> cancelled
```

终态不可重新进入活动态。状态更新使用 compare-and-swap，并与对应事件追加处于同一 SQLite 事务。

### 7.2 `orchestration_events`

追加保存 run 的阶段变化、策略选择、预算决定、验证结果、fallback 和安全事件。事件不可原地修改，不保存完整 Prompt、回复正文、媒体正文或凭据。

### 7.3 `orchestration_config_versions`

保存规范化后的非敏感配置 JSON、SHA-256、创建时间、激活时间和状态。配置内容使用固定 schema；未知字段、未知模式和未实现能力必须拒绝。

敏感值只允许保存 SecretRef，不进入配置 JSON。激活操作采用单事务切换，任意时刻只能有一个 active version。

## 8. 结构化 `ActionIntent`

### 8.1 作用范围

F0 先迁移已有的高风险本地命令，例如删除记忆、清空记忆、删除待办、清空已完成和删除风格。F0 不新增外部动作适配器，也不扩大 QQ Agent 工具权限。

未来外部工具必须先注册固定 `action_type + adapter_id + payload schema`，不能传入任意命令、任意 URL 或任意可执行文件。

### 8.2 不可变字段

- `intent_id`
- `approval_id`
- `user_id`
- `source_message_id`
- `session_key`
- `action_type`
- `adapter_id`
- `schema_version`
- `payload_json`
- `payload_sha256`
- `config_version`
- `risk_tier`
- `impact_summary`
- `status`
- `created_at`
- `expires_at`
- `approved_at`
- `attempt`
- `result_class`
- `safe_result_summary`
- `finished_at`

`payload_json` 必须先按固定 JSON 规范化规则序列化，再计算 SHA-256。审批绑定 `intent_id + approval_id + user_id + payload_sha256 + config_version`。

### 8.3 状态机

```text
proposed -> awaiting_approval -> approved -> executing -> succeeded
                                           -> failed
         -> rejected
         -> expired
         -> cancelled
```

规则：

- 任一终态不可重放。
- 修改 payload、适配器、风险等级或配置版本必须创建新 intent。
- 执行前重新验证用户、审批码、有效期、状态、哈希、配置版本和适配器白名单。
- 执行结果只保存受控摘要，不保存密钥或完整第三方响应。
- 数据库写入、哈希校验或所有权校验失败时禁止执行。

## 9. 失败处理

| 失败点 | 控制策略 | 用户侧行为 |
| --- | --- | --- |
| 新编排配置无效 | 拒绝激活，保留上一有效版本 | Direct 链路不受影响 |
| 新 Runtime 初始化失败 | 服务拒绝以 `direct` 启动，可切回 `legacy_direct` | 不进入部分初始化 |
| 普通 trace 写入失败 | 记录受控日志并继续当前 Direct 回复 | 回复照常投递 |
| planner/executor/verifier 异常 | 沿用现有错误映射与熔断 | 返回现有稳定兜底 |
| ActionIntent 或审批持久化失败 | fail closed | 返回“暂不可执行” |
| intent 哈希、所有者、状态或版本不匹配 | 拒绝并追加安全事件 | 要求重新发起 |
| Outbox 发送失败 | 沿用持久重试和去重 | 不直接重复调用模型 |

## 10. 运维设计

### 10.1 LaunchAgent

项目提供三份受管 LaunchAgent：

- Bridge
- Dashboard
- Watchdog

OpenClaw Gateway 继续由 OpenClaw 原生服务管理，F0 不建立第二个 Gateway 所有者。

每份 plist 使用固定 executable、固定工作目录和最小环境变量；服务脚本成为 `launchctl bootstrap/bootout/kickstart/print` 的兼容外观。禁止同时使用 `nohup` 和 LaunchAgent 拉起同一组件。

### 10.2 日志轮转

- Bridge、Dashboard、Watchdog 使用独立日志。
- 单文件最大 10 MiB，保留 5 代。
- 日志、轮转文件和目录按最小权限创建；日志文件为 `0600`。
- 结构化字段包含 component、level、trace_id、run_id、intent_id、safe_error_class 和时间。
- 禁止记录 API Key、SecretRef 解析结果、完整媒体 URL、媒体正文、完整 ActionIntent payload 和完整模型 Prompt。

### 10.3 加密备份

新备份采用 AES-256-GCM：

- 32 字节随机主密钥保存到固定 macOS Keychain service/account。
- 每份备份生成随机 nonce，使用版本化 envelope header。
- 明文 ZIP 只存在于受限临时文件中，权限 `0600`，加密完成后立即删除。
- 恢复时先完成 AEAD 验证，再校验内部 manifest 和文件 SHA-256。
- 新自动备份只产生加密格式。
- 旧普通 ZIP 标记为 `legacy-unencrypted`，允许显式只读恢复，但不能显示为“已加密”。
- 管理台不显示、导出或替换备份主密钥。

实现使用受版本约束的 `cryptography` AESGCM API，不调用 shell 拼接加密命令。

## 11. 测试策略

### 11.1 回归测试

生产基线 596 项测试必须全部通过。不得删除旧断言来适配新结构。

### 11.2 Direct 双路径等价测试

固定语料同时运行 `legacy_direct` 和 `direct`：

- 本地身份、管家、风格、记忆和任务命令保持短路。
- 普通文本只调用一次主 Agent。
- 相同模拟 Agent 输出得到相同最终回复。
- 截断、错误映射、会话键、记忆上下文和媒体上下文保持一致。
- Inbound、Outbox、观察记录和提醒状态变化一致。

### 11.3 状态机与故障注入

- orchestration run 合法状态转换与终态不可逆。
- 状态变更和事件追加事务一致。
- 配置版本原子激活和无效配置拒绝。
- SQLite 锁、trace 写失败和进程取消下的受控降级。
- ActionIntent 篡改、跨用户审批、过期、重放和配置版本变化全部拒绝。
- 加密备份篡改、错密钥、截断和 legacy 恢复行为。
- LaunchAgent 重复拉起、异常退出、登录启动和停止行为。

### 11.4 架构测试

- `Bridge` 不得实例化具体 planner、executor 或 verifier。
- `domain` 和 orchestration contracts 不得导入 SQLite、WebSocket、subprocess、Dashboard 或 watchdog。
- 生产对象图只创建一个共享状态库和一个编排 Runtime。
- 未实现的 `shadow/active` 模式在 F0 必须拒绝。

## 12. 验收门

| Gate | 标准 |
| --- | --- |
| G0 回归 | 596 项生产基线与所有新增测试全绿 |
| G1 等价 | Direct 双路径固定语料行为差异为 0 |
| G2 故障 | 配置、SQLite、trace、进程和审批故障无越权、无重复副作用 |
| G3 重启 | Bridge/Dashboard/Watchdog 各一个进程，连接与队列恢复正常 |
| G4 回滚 | 切到 `legacy_direct` 后 60 秒内恢复原链路，新表不参与执行 |
| G5 安全 | ActionIntent 任一绑定字段变化均拒绝；密钥和备份明文扫描为 0 |

## 13. 部署顺序

1. 创建现有普通备份并验证，作为迁移前最后一份 legacy 备份。
2. 在开发环境完成 schema v5、Direct 双路径和全量测试。
3. 安装备份加密 Keychain 主密钥并创建第一份加密生产备份。
4. 同步代码和受管 LaunchAgent，先保持 `ORCHESTRATION_MODE=legacy_direct`。
5. 重启并完成 OneBot、Gateway、QQ 文本、提醒、Outbox、Dashboard 和 watchdog 烟测。
6. 切换到 `ORCHESTRATION_MODE=direct`，再次完成同一组烟测。
7. 执行一次故障注入和一次真实 `legacy_direct` 回滚演练。
8. 所有 Gate 通过后标记 F0 完成，进入 F1 四级影子路由设计与实现。

## 14. 回滚

将 `ORCHESTRATION_MODE` 改为 `legacy_direct` 并通过受管 LaunchAgent 重启 Bridge。schema v5 新表、加密备份和新日志可以保留，旧路径不得读取这些表来决定回复。

若 LaunchAgent 部署本身失败，先 bootout 新 job，再使用迁移前受管服务脚本拉起旧 Bridge。若加密备份组件失败，停止自动备份并保留最近一份已验证备份，不能降级为静默创建未加密自动备份。

## 15. F0 完成后的下一阶段

F1 只新增四级确定性/轻量影子路由：记录 L0-L3 判断、置信度、升级原因和人工标注，不改变真实 QQ 回复。只有影子准确率、误判率、延迟和成本门槛通过后，才允许低风险层级开始受控接管。
