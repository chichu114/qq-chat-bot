# E2.5 模块化控制平面设计

## 1. 目标

在不删除、不弱化现有功能的前提下，重组 QQ 私聊智能体内部架构，使消息接入、业务命令、模型调用、可靠投递、持久化、后台任务和运维界面具备清晰边界，并为 E3 的 A++ 多模型编排预留稳定接口。

E2.5 完成后，运行时仍使用当前单 OpenClaw Agent。双模型议会、四级智能路由、DAG 并行执行和模型能力评分只建立领域模型与扩展接口，不在本阶段启用，也不产生额外 API 调用。

## 2. 不变条件

- 保留私聊白名单、限流、文本过滤和 OneBot 行为。
- 保留身份回答、审批、管家命令、风格学习及其现有优先级。
- 保留长期记忆、待办、提醒、早报、晚间复盘和免打扰。
- 保留 OpenClaw Gateway、每用户串行、跨用户并发、重试和熔断。
- 保留 SQLite WAL、消息去重、可靠 Outbox、备份和权限加固。
- 保留管理面板、watchdog、双击启动器和现有环境变量。
- 保留现有公开导入入口；旧测试和外部脚本不需要一次性迁移。
- 保留当前回复文案、截断规则、失败兜底及安全确认语义。

## 3. 非目标

- 不在 E2.5 启用架构师 A、审计师 B 或仲裁官 C。
- 不在 E2.5 自动拆分或并行执行 DAG 任务。
- 不在 E2.5 修改长期记忆格式或启用向量检索。
- 不在 E2.5 增加新的 QQ 消息类型或外部工具权限。
- 不以文件数量或代码行数为目的进行机械拆分。
- 不同时更换框架、数据库或 OneBot/OpenClaw 接入方式。

## 4. 架构原则

### 4.1 确定性控制平面

Python 控制平面拥有状态迁移、权限、预算、重试、去重和投递决策。模型适配器只能处理分配给它的任务，不能修改控制策略或绕过安全边界。

### 4.2 依赖方向

依赖只能按以下方向流动：

```text
Transport / Runtime
        -> Application
        -> Orchestration Ports + Domain
        <- Infrastructure Adapters
```

领域模型和编排接口不能导入 WebSocket、SQLite、OpenClaw CLI、HTTP Server 或 macOS 服务脚本。基础设施通过接口接入应用层。

### 4.3 兼容外观

`Bridge`、`Housekeeper`、`WorkspaceStateStore`、`DashboardController` 等现有类保留为薄外观。它们委托新模块工作，并维持旧构造参数和常用方法，避免一次性修改全部调用方。

### 4.4 提取而非重写

每次迁移只移动一个已被表征测试覆盖的职责。先复制行为测试，再提取实现，最后删除原位置的重复逻辑。不得在同一步中顺便改变业务语义。

## 5. 模块边界

### 5.1 接入与运行时

- `transport.onebot`：OneBot 事件解析、动作构造和 WebSocket 连接适配。
- `runtime.supervisor`：连接重试、后台任务生命周期和优雅停止。
- `application.dispatcher`：容量限制、事件任务跟踪和每用户会话锁。
- `runtime.composition`：集中创建配置、仓储、服务、适配器和应用用例。

### 5.2 应用流水线

- `application.coordinator`：处理一条已接收消息的完整用例。
- `application.command_chain`：按固定优先级调用安全、身份、管家和风格处理器。
- `application.delivery`：Outbox 入队、发送、重试、去重和入站完成状态。
- `application.background`：提醒扫描、自动备份和运行指标更新。

`MessageCoordinator` 不直接操作 SQLite 或拼接 OneBot JSON，只依赖仓储和投递接口。

### 5.3 A++ 控制平面骨架

E2.5 定义以下稳定契约：

- `TaskContract`：目标、原始输入、约束、验收标准、风险和上下文信息。
- `PlanningStrategy`：把 `TaskContract` 转换为 `ExecutionPlan`。
- `ExecutionPlan` / `WorkItem` / `WorkGraph`：描述单任务或未来 DAG。
- `ExecutionEngine`：执行计划并返回结构化 `ExecutionResult`。
- `VerificationStrategy`：返回 `VerificationReport`，不直接执行副作用。
- `BudgetPolicy`：判断调用是否在预算和次数限制内。
- `CapabilityRegistry`：未来按任务类别选择模型实现。
- `BrainstormGate`：未来决定用户参与、托管或暂停。
- `OrchestrationTraceSink`：未来记录规划、执行、验证和仲裁事件。

E2.5 提供以下兼容实现：

- `DirectPlanningStrategy`：生成仅含一个工作项的计划。
- `SingleAgentExecutionEngine`：委托当前 `AgentAdapter`。
- `CompatibilityVerificationStrategy`：只执行当前已有的结构与空回复检查，不新增语义拦截。
- `ExistingLimitBudgetPolicy`：复用当前超时、并发和重试限制，不增加 Token 预算。
- `StaticCapabilityRegistry`：只注册当前 OpenClaw Agent。
- `DisabledBrainstormGate`：直接进入现有单模型流程，不向用户提问。

未来 E3 的 `CouncilPlanningStrategy` 与 `DagExecutionEngine` 通过相同接口接入，不修改 QQ 接入、命令链或可靠投递模块。

### 5.4 领域模块

将当前 `housekeeper.py` 按职责提取为：

- `domain.memory`：记忆命令及内容规则。
- `domain.todos`：待办的增删改查、完成和分类。
- `domain.scheduling`：提醒、重复规则和状态转换。
- `domain.time_parser`：中文日期时间与周期表达解析。
- `domain.housekeeper`：仅负责命令识别与委托。
- `domain.persona`：身份、本地回复和风格行为。
- `domain.security`：审批语义和 ActionIntent 边界。

领域模块不得知道 Markdown 文件、SQLite 连接或 OpenClaw 工作区路径。存取通过 Repository 接口完成。

### 5.5 持久化

将 `storage.py` 提取为：

- `persistence.database`：连接、WAL、事务、锁和权限加固。
- `persistence.schema`：版本检查和迁移入口。
- `persistence.documents`：记忆、待办、风格和提醒文档仓储。
- `persistence.approvals`：审批仓储。
- `persistence.inbound`：入站消息幂等仓储。
- `persistence.outbox`：可靠发件箱仓储。
- `persistence.metrics`：运行指标查询与写入。

`WorkspaceStateStore` 继续存在，并把旧方法委托到这些仓储。E2.5 不改变 schema v2，也不迁移现有数据。

### 5.6 OpenClaw 适配器

`AgentAdapter` 继续作为模型边界。CLI/Gateway 命令构造、结果解析和 embedded fallback 检查归入基础设施适配器；应用层只接收结构化请求与回复。

### 5.7 Dashboard 与 Watchdog

Dashboard 拆分为只读查询服务、受限命令服务、备份服务和 HTTP 适配器。`DashboardController` 保留为兼容外观。前端功能和 API 路径保持不变。

Watchdog 将进程、OneBot、代理、Gateway 和队列检查提取为独立 probe，通过统一结果模型汇总；重启策略继续由确定性 watchdog 控制。

## 6. 消息数据流

```text
OneBot WebSocket
  -> EventDispatcher
  -> InboundRepository.claim
  -> MessageCoordinator
  -> ConfirmationGuard
  -> CommandChain
  -> TaskContractFactory
  -> DirectPlanningStrategy
  -> SingleAgentExecutionEngine
  -> CompatibilityVerificationStrategy
  -> ReliableDeliveryService.enqueue_and_try_send
  -> Outbox / OneBot
```

本地命令在 `CommandChain` 命中后直接生成回复，不进入模型规划；其执行顺序与当前实现一致。

## 7. 错误处理

- 适配器异常继续使用现有分类和用户兜底文案。
- 暂时性模型错误继续按当前次数重试并参与熔断计数。
- Outbox 继续先落库后发送，断线与重启后继续投递。
- 后台提醒只有在 Outbox 标记成功后才更新提醒状态。
- 模块化迁移不得吞掉任务异常；后台任务异常必须被日志消费。
- 组合根创建失败时服务拒绝启动，不进入部分初始化状态。

## 8. 配置策略

现有 `Config` 保留。内部可以把配置整理为运行时、可靠性、管家、提醒、备份和 watchdog 子设置，但旧属性与环境变量继续可用。

E2.5 只新增默认关闭或内部使用的架构标识，不要求用户修改 `.env`。未来模型角色、议会和预算配置由 E3 设计负责。

## 9. 测试策略

### 9.1 表征测试

现有 178 项测试作为行为基线。重构前补齐以下容易漂移的场景：

- 安全、身份、管家、风格和模型调用优先级。
- 本地命令不调用模型。
- 模型失败文案和回复截断。
- 同用户串行、跨用户并发和消息去重。
- Outbox 重试、提醒去重与重启恢复。

### 9.2 契约测试

- `DirectPlanningStrategy` 只能生成一个保持原始输入的工作项。
- `SingleAgentExecutionEngine` 与现有 `AgentAdapter` 行为一致。
- 兼容外观与新模块对同一输入返回相同结果。
- Repository 接口对 SQLite 实现执行统一测试。
- Dashboard 和 watchdog 拆分前后 API/健康快照一致。

### 9.3 架构测试

增加导入依赖检查，阻止领域层和编排契约导入基础设施模块。新模块必须通过组合根注入依赖，禁止重新创建全局单例。

## 10. 迁移顺序

1. 冻结行为基线并补表征测试。
2. 建立 A++ 结构化模型、接口和兼容实现。
3. 提取消息协调器、命令链和模型执行路径。
4. 提取可靠投递、事件调度和后台服务。
5. 提取 SQLite 基础设施与分表 Repository。
6. 拆分 Housekeeper 领域模块，保留兼容外观。
7. 拆分 Dashboard 查询/命令服务和 watchdog probes。
8. 将对象创建集中到 Composition Root，缩减旧类为薄外观。
9. 运行全量测试、静态检查和架构检查。
10. 创建部署前备份，部署到运行目录，重启并执行真实 Gateway、QQ、提醒、面板和 watchdog 验收。

每一步必须单独保持测试通过；若某一步需要改变业务行为，应停止并作为独立功能设计处理。

## 11. 完成标准

- 所有 E2 及之前功能保持可用，现有配置无需修改。
- 完整测试套件与新增契约/架构测试通过。
- `Bridge` 只负责兼容和委托，不再同时实现业务、持久化和后台循环。
- `Housekeeper` 与 `WorkspaceStateStore` 成为兼容外观，核心职责进入独立模块。
- Dashboard 与 watchdog 的查询、命令和探针可独立测试。
- `DirectPlanningStrategy` 是唯一启用的规划实现，真实调用次数与当前一致。
- 双模型议会、DAG 执行、验证链、预算和能力档案都有稳定接入接口，但默认不启用、不消耗 API。
- 运行目录部署后，Bridge、Dashboard、Watchdog、Gateway、OneBot 和 SQLite 队列全部健康。

## 12. 回滚

部署前创建包含 `.env`、SQLite、人格与动态状态的手动备份。源代码部署保留上一运行版本；若实机验收失败，恢复上一版本代码和备份，重启原 E2 服务。SQLite schema 在 E2.5 不升级，因此代码回滚不需要数据降级。
