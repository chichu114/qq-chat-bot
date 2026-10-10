# E4-A 多媒体与长任务可观测设计

## 目标

在不删除 E1、E2、E2.5、E3 既有能力的前提下，让 chichu 支持 QQ 私聊中的图片、语音、文件和引用消息，并为耗时任务增加持久任务队列、真实阶段通知、取消能力和面板可观测性。

E4-A 只扩展输入理解、任务执行体验和运维面板，不在本阶段接入真实日历、邮件或项目管理账号，也不启用管家、记忆、调度、编码四专职 Agent。后两部分分别留给 E4-B 和 E4-C。

## 不变边界

- 只处理 `ALLOWED_QQ_USERS` 白名单中的 QQ 私聊，群聊继续忽略。
- 纯文本消息继续使用现有 E2.5 `DirectPlanningStrategy -> SingleAgentExecutionEngine -> CompatibilityVerificationStrategy` 链路，不增加媒体 API 调用。
- 最终回复继续通过 E2 Outbox 可靠投递，消息 ID 去重、单用户会话锁、重试和熔断继续生效。
- E1 受限 `qq` Agent 的 Docker 沙箱、工具白名单和审批边界不得扩大。
- E3 记忆上下文仍通过独立 `AgentRequest.context` 注入，媒体派生文本不得改写用户原始文本。
- 媒体识别使用独立 API 凭据，不复用 OpenClaw 或主模型凭据。
- 媒体、文件和 API 失败不得阻塞提醒、OneBot 收发或其他 QQ 用户的普通对话。

## 方案选择

采用“独立媒体管线 + OpenAI 兼容媒体 API + OpenClaw 最终推理”。

未采用媒体直接进入 OpenClaw Gateway，因为当前 `openclaw agent` CLI 没有稳定的媒体附件参数，依赖内部 Gateway 协议会增加升级风险。未采用把所有逻辑写回 `Bridge`，因为下载、解析、识别、任务状态和回复会重新耦合到同一个运行时类，违背 E2.5 的模块化边界。

## 总体架构

```text
QQ / NapCat OneBot v11
  -> InboundMessageParser
  -> InboundEnvelope
  -> MediaPipeline
       -> MediaIngestionService
       -> ImageUnderstandingProcessor
       -> AudioTranscriptionProcessor
       -> DocumentTextProcessor
       -> QuoteResolver
  -> StructuredContextCompiler
  -> E3 MemoryContextService
  -> E2.5 orchestration
  -> OpenClaw final reasoning
  -> E2 ReliableDeliveryService / Outbox
```

### `InboundMessageParser`

负责把 OneBot v11 的字符串消息或消息段数组解析为统一 `InboundEnvelope`，保留消息段原始顺序。它只做结构化解析，不下载附件、不调用模型、不访问 SQLite。

`InboundEnvelope` 包含：

- `user_id`、`message_id`、`timestamp`。
- 用户输入的文本片段。
- 有序 `AttachmentRef` 列表。
- 可选 `QuoteRef`。
- 原始消息类型摘要，不保存完整 OneBot 事件。

`AttachmentRef` 只保存处理所需字段：附件种类、OneBot 文件标识、临时 URL、显示名称、声明大小和 MIME。日志中不得输出完整临时 URL。

### `MediaPipeline`

负责附件下载、验证、处理和派生上下文。处理器通过注册表按附件种类选择，新增处理器不需要修改 `Bridge`。

每个处理器输出 `MediaArtifact`：

- 图片：模型生成的客观图片描述。
- 语音：带语言和时长信息的转写文本。
- 文件：文件名、类型、页数或行数，以及受预算限制的提取文本。
- 引用：被引用消息的发送者、文本和附件摘要。

派生内容统一标记为“不可信外部输入”，防止文件正文或图片文字中的指令被当作系统指令。

### `MediaApiAdapter`

媒体 API 使用 OpenAI 兼容接口，但与 OpenClaw 完全分离。配置项包括：

- `MEDIA_API_BASE_URL`
- `MEDIA_API_SECRET_REF` 或 `MEDIA_API_KEY`
- `MEDIA_IMAGE_MODEL`
- `MEDIA_AUDIO_MODEL`
- `MEDIA_API_TIMEOUT_SECONDS`
- `MEDIA_API_REQUESTS_PER_MINUTE`
- `MEDIA_API_DAILY_BUDGET`

生产环境优先使用 SecretRef；`.env` 兼容字段必须保持 `0600`。管理面板只能更新密钥，不能读取或回显密钥。媒体 API 客户端采用独立限流、重试、熔断和用量记录，失败时绝不自动切换到主模型。

### `LongTaskService`

长任务服务负责持久任务状态、阶段事件、取消请求和进度通知。它不理解具体媒体格式，通过任务执行器报告的阶段事件工作。

超过 15 秒仍未结束的任务发送一次“已接单”。后续只在真实阶段变化时通知，同一任务两次进度通知至少间隔 60 秒，不生成虚假百分比。

支持本地命令：

- `查看任务`
- `任务状态 <id>`
- `取消任务 <id>`

取消为协作式取消：排队或未开始的步骤立即停止；可取消的 HTTP 请求主动中断；无法安全中断的调用允许在后台结束，但丢弃结果、不进入下一阶段、不发送最终回复。

## 支持范围

### 图片

- MIME：JPEG、PNG、WebP、GIF。
- 单张不超过 10MB。
- 每条消息最多 4 张。
- GIF 默认只分析首帧，避免隐式放大成本。
- 图片描述必须区分“可见内容”和“模型不确定推断”。

### 语音

- 单条不超过 20MB。
- 语音先转写，再把转写文本交给 OpenClaw。
- 转写失败时保留用户同时发送的文字，并明确指出失败的附件。
- 原始语音不进入普通日志和长期记忆。

### 文件

- 单文件不超过 20MB。
- 支持 TXT、Markdown、JSON、CSV、PDF、DOCX。
- 只读取正文，不执行宏、脚本、嵌入对象或压缩包内容。
- PDF 和 DOCX 提取设字符预算；超出预算时生成截断说明，而不是静默丢失。
- 文件正文只有在用户明确确认长期事实时才进入 E3 记忆。

### 引用消息

- 优先使用当前 OneBot 事件中已携带的引用内容。
- 引用内容缺失时，使用带唯一 `echo` 的 OneBot RPC `get_msg` 获取。
- OneBot RPC 响应与普通事件由响应复用器分流，不能进入消息处理队列。
- 引用解析失败不阻塞当前文字消息，只插入“引用内容不可用”的结构化标记。

## 安全与隐私

- 只有白名单私聊通过媒体管线；未授权用户的媒体不下载。
- 下载 URL 只允许 HTTPS、NapCat 提供的受限本机资源和明确域名白名单。
- DNS 解析后拒绝未授权的环回、链路本地、私网和保留地址，防止 SSRF；NapCat 本机资源走独立受限适配器，不经过通用 URL 下载器。
- 下载使用随机文件名，拒绝路径穿越、符号链接和声明类型与实际类型不符。
- 下载过程中流式计算大小和 SHA-256，超过上限立即停止。
- 缓存目录权限为 `0700`，媒体文件权限为 `0600`，默认保留 1 小时后清理。
- 日志只记录媒体 ID、种类、字节数、状态和耗时，不记录完整 URL、API 密钥、原始语音或文件正文。
- 从媒体提取的所有文本都作为用户提供的不可信上下文，不得覆盖人格、安全策略或工具授权。

## 数据模型

SQLite schema 从 v3 升级到 v4，只新增表和索引，不删除历史数据。

### `media_assets`

保存用户、消息 ID、附件种类、显示名称、MIME、字节数、SHA-256、处理状态、错误摘要、创建时间和过期时间。默认不保存完整外部 URL。

### `long_tasks`

保存任务 ID、用户、来源消息、状态、当前阶段、取消请求、创建、开始、结束和最后通知时间。

状态只能按以下方向转换：

```text
queued -> running -> succeeded
                  -> failed
                  -> cancelled
queued ----------------> cancelled
```

终态不可再次进入运行态。

### `task_events`

追加保存任务阶段变化、进度通知、重试、取消和错误事件。事件不可原地修改，便于审计和面板时间线展示。

### `model_calls`

保存调用种类、供应商、模型、输入和输出 Token、耗时、成功状态、错误分类和估算成本。无法从供应商取得 Token 时保存为空，不使用字符数伪装为精确 Token。

### `health_events`

只在 Bridge、OneBot、Gateway、媒体 API、任务队列的健康状态发生变化时追加记录，避免按固定频率制造重复数据。

## 任务阶段与通知

标准阶段为：

1. `validating`：校验消息段和附件限制。
2. `downloading`：获取受限媒体。
3. `understanding_image`、`transcribing_audio` 或 `extracting_document`。
4. `resolving_quote`：按需获取引用消息。
5. `reasoning`：调用 OpenClaw 完成最终回答。
6. `delivering`：通过 Outbox 投递回复。

并行附件允许并发处理，但受媒体并发上限控制。所有附件处理完毕后才进入最终推理；单个附件失败会形成错误工件，其他附件继续执行。

## 面板扩展

`http://127.0.0.1:18790` 保持只监听回环地址，并沿用现有 Dashboard Token 和 Origin 校验。

### 任务队列

展示任务 ID、用户、状态、阶段、耗时、最近事件和取消按钮。取消属于 mutation API，必须携带 Dashboard Token。

### 模型统计

分别展示 OpenClaw 主模型和媒体模型的调用次数、Token、P50/P95 延迟、错误率、当日预算和熔断状态。统计只来自结构化 `model_calls`，不解析日志字符串。

### 审批中心

复用 E1 持久审批记录，显示待审批、已批准、已拒绝和过期记录。面板批准动作仍执行现有审批状态机，不直接执行高风险工具。

### 健康时间线

展示组件离线、恢复、重试、熔断和队列积压事件。健康状态变化写入 `health_events`，页面轮询现有 state API，不引入公网服务。

### 媒体设置

允许修改 API 地址、图片模型、语音模型、超时、限流和预算。密钥字段只支持“替换”操作，后端和页面都不能返回原值。

## 功能开关与降级

新增开关：

- `ENABLE_MEDIA_PIPELINE=false`
- `ENABLE_LONG_TASKS=false`

部署时先完成 schema v4 迁移和代码同步，但默认保持关闭。配置并验证独立媒体 API 后开启媒体管线，再开启长任务通知。

降级规则：

- 媒体管线关闭：纯文本照常处理，媒体返回明确提示。
- 媒体 API 不可用：文件本地提取和引用解析仍可工作；图片、语音只报告不可用。
- 长任务服务关闭：任务仍执行，但不发送中间通知；最终回复和 Outbox 不变。
- SQLite 写入失败：拒绝启动 E4 功能，纯文本 Bridge 按现有就绪检查策略处理，不在内存中伪造持久任务。

## 测试策略

### PDF 解析隔离

PDF 结构校验和文本提取不在 Bridge 主进程内执行。父进程只向固定的本地 worker
入口传入最多 20 MiB 的不可变 PDF 字节，并限制返回 JSON 大小；worker 在导入和
调用 `pypdf>=4,<7` 前设置 CPU、地址空间、文件大小和文件描述符上限。父进程还
负责墙钟超时和异常退出回收，因此复杂页树、Form XObject 展开、压缩炸弹和旧版
`pypdf` 缺少私有 limit hook 都不会拖垮 Bridge。合法 PDF 的对外接口保持不变。

缓存写入、发布、补偿删除和过期清理由同一缓存根内的 advisory `flock` 串行化，
覆盖应用的多进程实例。能以当前 macOS 用户身份无视锁并任意修改代码、数据库和
密钥的恶意进程属于宿主机信任边界之外；需要更强边界时应把 Bridge 放入独立系统
用户或容器，而不是依赖 pathname 检查模拟内核隔离。

### 远程下载硬超时隔离

通用 HTTPS 媒体下载使用固定入口的独立子进程，不在 Bridge 主进程内执行可能阻塞
的 DNS、TCP、TLS、响应头或正文读取。父进程通过当前 `sys.executable` 和
`remote_download_sandbox.py` 的绝对路径启动 worker，`shell=False`，并在 60 秒
墙钟期限到达后 kill/reap。生产默认路径只能使用该 worker；测试可注入固定 runner，
不得从 QQ 消息控制 executable、模块、参数或环境。

父进程向 stdin 发送不超过 64 KiB 的严格 JSON 请求，只包含 URL、精确主机白名单、
redirect 上限和本次字节上限。worker 的 stdout 协议为 4 字节大端 JSON 头长度、
严格 JSON 头以及原始媒体字节；成功头只允许 `ok`、`content_type` 和
`byte_size`，失败头只允许 `ok` 与固定 `error`。父进程把 stdout 写入匿名临时文件，
总输出不得超过本次媒体上限加 64 KiB；长度、字段、类型、UTF-8、额外字节或退出码
异常全部 fail closed。

worker 对初始 URL 和每次 redirect 都先执行精确域名白名单，再解析 DNS 并拒绝非
global 地址；TCP 连接固定到已验证 IP，同时保留原 hostname 的 TLS SNI 和 HTTP
Host。正文按 64 KiB 流式读取并在超过 kind 限制时立即停止。worker 在网络访问前
设置文件大小和文件描述符上限；父进程墙钟终止是 DNS、连接、响应头和 slow-drip
正文的最终硬边界。下载结果返回主进程后才获取缓存根 `RLock + flock` 并发布，
因此网络等待不占用缓存锁。

### 清理事务与主机规范化

`record_media_cleanup_error()` 必须在 SQLite 事务提交前调用 `harden_files()`；
若 harden 失败，事务回滚，调用方才可以在仍持有 `flock` 时把 retained 文件改回
已登记名称。repository 返回 `None` 也视为持久化失败，不能静默留下文件名分裂。

精确主机白名单先执行 IDNA 转换，再移除 ASCII 尾点并重新验证标签。因此 ASCII
`.` 与 IDNA 等价分隔符 `U+3002`、`U+FF0E`、`U+FF61` 的尾点形式具有相同结果，
但不支持通配符或隐式子域匹配。

### 单元测试

- OneBot 字符串和消息段的文字、图片、语音、文件、引用及混合顺序解析。
- MIME、大小、数量、路径和 URL 安全规则。
- 任务状态转换、通知节流、取消和终态不可逆。
- 媒体 API 限流、日预算、错误分类和熔断。
- 文档字符预算、媒体派生上下文的不可信标记。

### 集成测试

- 使用本机模拟 HTTP 服务验证安全下载，不访问真实供应商。
- 使用伪媒体 API 验证图片描述、语音转写和用量记录。
- 使用模拟 OneBot WebSocket 验证 `get_msg` 的 `echo` 响应与普通事件分流。
- 验证长任务阶段经 Outbox 投递且不会重复通知。

### 回归测试

- 纯文本私聊、身份、人设和风格命令。
- E1 审批与受限 Agent 权限。
- E2 去重、Outbox、重试、熔断、提醒和 watchdog。
- E2.5 单 Agent 兼容编排。
- E3 记忆确认、混合检索、摘要和风格漂移检查。

### 生产烟测

配置独立媒体 API 后，使用一张测试图片、一段短语音、一个 TXT、一个 PDF 和一条引用消息完成端到端验收。检查 QQ 回复、任务状态、`model_calls`、面板统计和缓存清理，不使用用户私人文件作为测试样本。

## 部署与回滚

1. 创建包含数据库、人格文件和必要配置的一致性备份。
2. 运行全量测试和 schema v3 到 v4 迁移测试。
3. 同步代码和配置示例，保持两个 E4 开关关闭。
4. 重启 Bridge、Dashboard 和 Watchdog，验证纯文本回归。
5. 在面板配置独立媒体 API，运行供应商连通性测试。
6. 开启媒体管线并完成媒体烟测。
7. 开启长任务通知并验证任务查询、取消和通知节流。

回滚时关闭两个功能开关即可恢复 E3 行为。schema v4 新表保留，不做破坏性降级；旧代码忽略新表。

## 验收标准

- 白名单 QQ 的图片、语音、支持文件和引用消息可以进入最终回答。
- 超限、危险 URL、类型伪装和路径穿越在调用媒体 API 前被拒绝。
- 媒体 API 使用独立凭据、模型、限流、预算和熔断。
- 超过 15 秒的任务发送一次接单通知，阶段通知真实且至少间隔 60 秒。
- `查看任务`、`任务状态 <id>`、`取消任务 <id>` 可用，取消后不发送被丢弃的最终结果。
- 面板可查看任务、模型统计、审批和健康时间线，密钥不可回显。
- 纯文本、审批、Outbox、提醒、E3 记忆和人格回归测试全部通过。
- 功能开关关闭时，运行行为与 E3 保持兼容。
