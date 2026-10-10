# F1 四级影子路由设计

## 1. 背景

当前 QQ 私聊智能体已经完成 F0 编排控制平面，并在生产环境使用
`ORCHESTRATION_MODE=direct`。正式消息仍由
`DirectPlanningStrategy + SingleAgentExecutionEngine +
CompatibilityVerificationStrategy` 处理，每条需要模型处理的消息只调用一个
OpenClaw QQ Agent。

F1 是 A++ 上层建筑的下一条纵向切片。它只观察并记录 L0-L3 路由判断，不改变
当前回复、主模型、审批、Outbox 或 QQ 收发行为。先通过真实数据校准路由，再在后续
阶段讨论受控接管，避免未经验证的分类器直接决定成本和安全等级。

## 2. 目标

- 建立 L0-L3 四级任务分类标准。
- 使用“确定性规则优先、轻量模型补判”的混合影子路由。
- 强制高风险任务进入 L3，模型不能降级。
- 让轻量分类与 Direct 推理并行，影子故障不阻断正式回复。
- 持久保存路由决策、配置版本和人工标注。
- 在本地管理台提供路由统计、标注和受控配置。
- 建立明确的 F2 准入门槛，但 F1 不自动切换正式执行路径。

## 3. 非目标

- 不让 L0-L3 结果决定本轮使用哪个正式模型。
- 不启用架构师 A、审计师 B、仲裁模型或双模型议会。
- 不拆分或执行 DAG 工作包。
- 不实现廉价 Worker 池、能力档案选模或非对称双模型审计。
- 不在 QQ 中暂停对话并发起真实头脑风暴。
- 不开放新的 OpenClaw 工具权限。
- 不接入日历、邮件、项目管理或外部执行能力。
- 不修改 F0 的 `legacy_direct` / `direct` 语义和硬回退能力。

## 4. 路由等级

### 4.1 L0：确定性本地能力

适用于无需主模型即可稳定完成的受控命令，例如：

- 身份回答和固定自我介绍。
- 查看或修改风格反馈。
- 待办、提醒和记忆确认命令。
- 查看任务、取消任务和状态查询。
- 审批确认、拒绝和其他本地状态操作。

L0 由当前 `CommandChain` 的真实命中结果确定，不重复执行命令，也不调用轻量分类
模型。

### 4.2 L1：轻量任务

适用于低风险、低复杂度、结果容易检查的语言任务，例如闲聊、短改写、简单提取、
分类和短摘要。F1 只记录该判断，正式回复仍走 Direct 主模型。

### 4.3 L2：标准任务

适用于普通分析、代码解释、方案整理、多步骤但边界清晰的任务。无法可靠判断的普通
消息默认保守落入 L2。

### 4.4 L3：重大或高风险任务

适用于复杂架构、重大方案与难以验收的任务。以下操作无论语言复杂度如何都强制进入
L3：

- 删除或覆盖数据。
- 对外发送消息、公开发布或代表用户作出承诺。
- 付款、购买、财务和账号权限操作。
- 密钥、Token、登录凭据和代理订阅处理。
- 系统配置、服务安装、进程控制和宿主机执行。

高风险规则具有最终升级权。轻量模型不得把规则判定的 L3 降为其他等级。

## 5. 总体架构

```text
QQ / NapCat
  -> 过滤、白名单、去重、每用户锁
  -> MessageCoordinator.prepare
       -> 本地命令已命中：记录 L0
       -> 未命中：启动 ShadowRoutingObserver
            -> DeterministicRiskRouter
            -> 明确 L3：直接持久化
            -> 其余模糊消息：LightweightRouteClassifier
            -> ConservativeRouteResolver
            -> RouteDecisionRepository
  -> 当前 Direct OrchestrationRuntime
  -> 当前 OpenClaw QQ Agent
  -> 当前 Outbox
  -> QQ
```

`ShadowRoutingObserver` 不拥有正式执行权。它只能生成影子决策、统计和审计记录。

## 6. 组件设计

### 6.1 `RoutingContract`

包含当前路由判断所需的最小数据：用户、来源消息标识、文本、是否命中本地命令、
配置版本和创建时间。它不包含 OpenClaw 凭据、完整环境变量或工具权限。

### 6.2 `DeterministicRiskRouter`

负责：

- 根据真实命令命中结果确定 L0。
- 根据结构化操作类型和受控规则确定强制 L3。
- 为未确定消息生成可解释的风险特征和升级原因。
- 对输入做长度限制，不使用正则处理无界文本。

规则版本写入每条决策，便于后续比较不同规则集。

### 6.3 `LightweightRouteClassifier`

仅处理规则无法确定的消息，使用专用的只读 OpenClaw `routing` Agent、独立 session
和配置的轻量模型。该 Agent 不加载 QQ 人格记忆，不拥有任何工具，只接收分类提示并
输出严格 JSON：

```json
{
  "level": "L1",
  "confidence": 0.92,
  "task_kind": "smalltalk",
  "reasons": ["low_risk", "single_step"],
  "brainstorm_required": false
}
```

只允许 L1、L2、L3。字段缺失、未知枚举、非有限置信度、超长原因或额外敏感字段均
视为无效响应。

### 6.4 `ConservativeRouteResolver`

合并规则和模型结果：

1. 本地命令命中时输出 L0。
2. 强制风险命中时输出 L3。
3. 有效模型结果且置信度达到阈值时采用模型等级。
4. 低置信度、冲突、超时或无效响应时输出 L2。
5. 架构设计或重大方案的 L3 记录 `brainstorm_required=true` 和
   `brainstorm_mode=ask_user`，但不改变本轮 QQ 流程。

### 6.5 `ShadowRoutingObserver`

观察器与当前 Direct 推理并行运行。它负责创建 pending 记录、执行补判、保存终态和
更新指标。观察器异常被隔离，不传播到 `MessageCoordinator.resolve_prepared`。

同一消息只允许一条有效决策。重复 OneBot 事件、进程恢复或观察器重复调度均依赖唯一
键幂等收敛。

## 7. 数据模型

F1 新增独立表，不扩展 F0 严格限定的编排配置 JSON。

### 7.1 `route_decisions`

主要字段：

- `id`：内部决策 ID。
- `user_id`：所有者。
- `source_message_sha256`：不可逆来源消息标识。
- `message_sha256`：规范化消息内容哈希。
- `message_preview`：最多 160 字的脱敏预览。
- `rule_level`、`model_level`、`final_level`。
- `confidence`、`task_kind`、`risk_flags_json`、`reasons_json`。
- `brainstorm_required`、`brainstorm_mode`。
- `classifier_provider`、`classifier_model`、`classifier_duration_ms`。
- `status`：`pending`、`classified`、`rule_only`、`error`。
- `safe_error_class`：受控错误类型，不保存响应正文。
- `routing_config_version`、`rule_version`、`created_at`、`finished_at`。

唯一约束为 `(user_id, source_message_sha256, routing_config_version)`。

### 7.2 `route_labels`

主要字段：

- `decision_id`、`user_id`。
- `verdict`：`correct`、`upgrade`、`downgrade`。
- `correct_level`：L0-L3。
- `note`：可选、限长、纯文本备注。
- `created_at`、`updated_at`。

每个决策只有一条有效标签。更新采用当前版本条件比较，避免并发覆盖。

### 7.3 `routing_config_versions`

保存轻量模型、规则版本、置信度阈值、分类超时、每日最大调用数、熔断参数、状态和
激活时间。任何配置变更创建新版本，旧版本退役但不删除。

## 8. 隐私和安全

- 消息预览先遮蔽密钥样式、Bearer Token、URL 查询参数、长数字和疑似账号。
- 预览最多 160 字；完整模型响应、异常正文和凭据不进入路由表。
- Dashboard 只绑定 loopback，写操作继续使用 Host、Origin 和随机 Token 校验。
- 轻量分类器固定使用独立的只读 `routing` Agent。该 Agent 的工具集为空，并显式拒绝
  exec/process、写文件、消息发送、Gateway 管理和子 Agent 权限。
- 路由模型只提出分类，不拥有预算、审批、执行或配置修改权。

## 9. 并发、预算与熔断

- 规则判定在当前事件协程中完成，时间预算应为毫秒级。
- 模型补判作为受监管后台任务与 Direct 推理并行。
- 使用独立并发上限，不占满主 Agent 调用信号量。
- 每日最大分类调用数是硬保险丝；达到上限后只记录规则结果和
  `classifier_budget_exhausted`。
- 连续分类失败达到阈值后打开独立熔断器；冷却期内不调用分类模型。
- 进程退出时未完成记录在启动恢复中转为 `error/interrupted`，不自动补发模型请求。

## 10. 管理台

新增“影子路由”页面，提供：

- 运行天数、决策总数、人工标注数、准确率、高风险漏判数。
- 分类错误率、P50/P95 分类耗时和 P95 Direct 额外延迟。
- 脱敏决策列表、规则等级、模型等级、最终等级、置信度和升级原因。
- “正确、应升级、应降级”及正确等级标注。
- 轻量模型、置信度、超时、每日调用上限和熔断参数配置。
- 激活配置版本、规则版本和回滚按钮。
- F2 准入状态；F1 页面不得提供正式接管开关。

API Key 不通过 Dashboard 读取或展示。分类模型必须来自 OpenClaw 已配置模型。

## 11. 指标与准入门槛

F2 准入必须同时满足：

- 影子运行不少于 7 天。
- 路由决策不少于 100 条。
- 人工标注不少于 30 条。
- 已标注样本整体准确率不低于 90%。
- 强制高风险样本漏判数为 0。
- P95 Direct 额外延迟不超过 1.5 秒。

页面只显示是否达到门槛，不自动激活 F2。样本类别分布和混淆矩阵必须同时展示，避免
大量 L1 样本掩盖 L3 误判。

## 12. 故障语义

- 规则异常：记录 `status=error`、`safe_error_class=rule_error` 和保守 L2，不启动模型
  补判，Direct 继续。
- 分类超时：记录 `classifier_timeout`，最终影子等级 L2。
- 无效 JSON：记录 `invalid_classifier_response`，最终影子等级 L2。
- 预算耗尽：记录 `classifier_budget_exhausted`，使用规则可得结果，否则 L2。
- SQLite 写入失败：记录结构化降级日志，不影响 Direct 和 Outbox。
- 配置版本不匹配：拒绝激活新版本，保留上一份 active 配置。
- 高风险规则与模型冲突：最终等级固定 L3，并记录 `model_downgrade_blocked`。

## 13. 测试与验收

### 13.1 单元测试

- L0 命令命中映射。
- L1-L3 规则边界和高风险强制升级。
- 严格 JSON 解析、置信度和枚举校验。
- 消息预览脱敏和长度限制。
- 保守合并规则。
- 每日调用上限和熔断状态迁移。

### 13.2 Repository 测试

- schema 创建和迁移幂等。
- 决策唯一键、状态迁移和重启恢复。
- 配置激活、退役和回滚。
- 人工标注 CAS、所有者隔离和统计口径。

### 13.3 集成测试

- 影子分类与 Direct 并行，分类超时不延迟或改变回复。
- 本地命令只记录 L0 且不调用分类模型或主模型。
- 同一 QQ 消息只生成一条路由决策。
- 路由数据库、分类器和统计故障不影响 Outbox 投递。
- `legacy_direct` 回滚后 QQ 文本仍可正常回复。

### 13.4 回归门禁

- 普通文本的正式回复内容保持基线兼容。
- 主模型调用次数不得因 F1 增加。
- QQ Agent 工具权限不得扩大。
- 完整测试套件和 F0 端到端烟测必须通过。

## 14. 部署与回滚

1. 追加 F1 schema 和管理台页面，影子路由默认关闭。
2. 创建首个 routing config，先启用纯规则观察。
3. 验证记录、脱敏、Dashboard 和 Direct 基线。
4. 启用轻量模型补判，并观察独立预算与熔断。
5. 运行至少 7 天并完成人工标注。
6. 达到门槛后只生成 F2 准入报告，不自动接管。

紧急回滚只需关闭 F1 影子开关并重启 Bridge。F0 `direct`、Outbox、SQLite 原有表、
提醒、记忆和 QQ 收发继续工作；F1 历史表保留供审计，无需数据库降级。

## 15. 后续边界

F1 完成后才能进入 F2：模型能力档案、分层模型配置和全局预算账本。真实用户头脑风暴
Gate、双模型议会、DAG Worker、非对称审计和第三模型仲裁仍分别留在后续阶段，不在
F1 中提前实现。
