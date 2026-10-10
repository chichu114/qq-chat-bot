# Ollama 社区语气学习与 QQ 群聊 Agent 设计

## 1. 背景

当前项目通过 NapCatQQ 和 OneBot v11 接收 QQ 消息，正式支持的入口是白名单私聊，
回复由 OpenClaw Agent 生成。项目已有消息去重、限流、SQLite 状态库、可靠投递和后台
服务基础，但会明确忽略群消息，也没有 Ollama 适配器或外部社区风格采集能力。

本阶段在不改变现有私聊链路的前提下，新增一个本地 Ollama 群聊 Agent。它定时读取
已获得内容作者授权的 Reddit 子版块和百度贴吧贴文，提炼社区的表达风格，并让指定
QQ 群在明确触发机器人时使用相应语气回复。

## 2. 已确认意图和约束

- 数据源必须定时增量采集，而不是仅支持手工导入。
- 群聊仅在机器人被 `@` 或用户回复机器人消息时触发；机器人不得自主插话。
- 采集内容只用于学习表达风格，不作为事实或社区知识库。
- 每个 Reddit 子版块或贴吧分别生成风格档案，QQ 群显式绑定一个档案。
- 内容作者已授权使用相关贴文；系统仍需保存授权范围和撤回所需的审计信息。
- 推理在本地 Ollama 上完成。目标机器是 16 GB Apple Silicon Mac，默认示例模型为
  `qwen3:8b`，但模型名必须可配置。
- 第一版不改变当前 OpenClaw 私聊行为。

## 3. 目标

- 为 Reddit 和贴吧提供独立、可停用、可限速的增量来源适配器。
- 只处理授权作者或授权帖子白名单命中的内容。
- 使用 Ollama 将贴文转换成受约束的结构化风格特征。
- 聚合、版本化和审计每个社区的风格卡。
- 按 QQ 群绑定风格卡，并通过本地 Ollama 生成回复。
- 对群事件实施白名单、显式触发、去重、限流、并发上限和安全降级。
- 让采集、风格学习和群聊回复的故障彼此隔离，也与现有私聊链路隔离。
- 支持作者撤回授权后移除其影响并重建风格卡。

## 4. 非目标

- 不建立 Reddit 或贴吧知识库，不把社区内容用于事实回答。
- 不保留原帖作为运行时 few-shot 示例，不复述或检索原帖正文。
- 不进行 LoRA、全量微调或模型权重训练。
- 不自主插话，不监听关键词后主动参与群聊。
- 不处理群聊图片、语音、文件或其他富媒体。
- 不提供管理台 UI；第一版使用版本化 JSON 配置和只读状态命令。
- 不绕过验证码、登录校验、robots 限制、访问频率限制或平台封禁。
- 不实现贴吧自动登录或 Cookie 抓取。
- 不让群聊入口执行当前私聊的管家命令、审批操作或高风险工具。

## 5. 方案选择

采用“结构化风格卡”方案：每个授权样本先被转换成风格特征，再由确定性聚合器生成
社区风格卡。群聊生成只接收风格卡，不接收原帖。

没有选择运行时示例检索，因为它会增加近似复述、隐私泄露和长上下文风险。没有选择
LoRA，因为 16 GB 机器上的训练、验证和多风格切换成本高，且少量社区样本容易过拟合。

## 6. 总体架构

```text
Reddit OAuth API ----> RedditSource ----\
                                      +--> StyleSyncService
贴吧公开页面 --------> TiebaSource -----/      |
                                             v
                                    AuthorizationFilter
                                             |
                                             v
                                    OllamaStyleAnalyzer
                                             |
                                             v
                                StyleSampleRepository (SQLite)
                                             |
                                      StyleAggregator
                                             |
                                             v
                                  versioned StyleProfile

QQ / NapCat / OneBot
        |
        v
GroupEventFilter -- allowlist + @/reply + rate limit + dedupe
        |
        v
GroupStyleBindingRepository --> GroupPromptBuilder --> OllamaChatAdapter
                                                    |
                                                    v
                                             send_group_msg
```

采集和学习运行在独立的 `style-learner` 进程中。群聊事件仍由 Bridge 的 OneBot 连接
接收，但进入专用 `GroupMessageCoordinator`，不复用私聊命令链或 OpenClaw Agent。
独立进程避免网络采集、HTML 变化或风格分析阻塞 QQ 收发。

## 7. 组件设计

### 7.1 `CommunityStyleConfig`

从 `STYLE_AGENT_CONFIG_PATH` 指向的 JSON 文件读取非敏感配置。配置包含：

- 全局开关、Ollama 地址、模型、超时、并发和默认回复长度。
- 允许的 QQ 群、群级冷却时间和群到风格档案的绑定。
- 来源平台、板块名、同步周期、分页和每轮最大采样量。
- 授权作者和授权帖子 ID，以及授权记录引用和授权范围。

配置必须完整校验后原子生效；未知字段、重复 ID、无效周期、群绑定不存在的档案、
明文凭据或非本地 Ollama 地址均拒绝启动。远程 Ollama 只有在单独显式开关打开时才
允许使用。

Bridge 和 `style-learner` 各自通过文件版本指纹检测配置变化。新文件只有在完整解析、
校验和交叉引用检查全部通过后才替换内存快照；失败时继续使用上一份有效快照并记录
安全错误。这样可以在不重启进程的情况下切换群绑定，同时避免读取只写了一半的文件。

Reddit 客户端 ID、客户端密钥等敏感值通过现有 secret reference 机制或环境变量提供，
不得写入 JSON、日志或 SQLite。

### 7.2 `RedditSource`

Reddit 来源只使用官方允许的 OAuth 访问方式，并发送可识别的 User-Agent。适配器：

- 读取指定子版块的新贴及允许配置的评论范围。
- 使用平台游标和外部内容 ID 做增量同步。
- 遵守服务端限速响应；收到 `429` 时使用 `Retry-After`，不并发重试。
- 只返回采集契约所需的最小字段，不读取私密用户数据。
- API 凭据失效、权限不足或条款要求变化时将来源暂停并报告，不回退到网页抓取。

Reddit Data API 条款要求使用获准的访问信息，并明确限制未经权利人许可把用户内容
用于机器学习或 AI。实现不能把“公开可见”视为已授权。

### 7.3 `TiebaSource`

贴吧没有在本设计中可依赖的公开数据 API，因此第一版只读取无需登录的公开板块页面：

- 每次同步前按缓存周期检查 `robots.txt`；明确禁止时暂停来源。
- robots 状态无法取得时，只有仍在有效期内的最近一次允许结果可继续使用；过期后
  fail closed。
- 使用固定、可识别的 User-Agent，低并发、带抖动的长周期和严格的每轮页数上限。
- 支持 `ETag` / `Last-Modified` 时使用条件请求。
- 遇到验证码、登录墙、`403`、`429` 或页面结构无法验证时停止，不尝试绕过。
- HTML 解析只提取帖子 ID、作者标识、时间、规范 URL 和正文候选；选择器集中版本化，
  不散落在业务代码中。

百度贴吧协议和页面规则可能变化，因此来源需要独立开关和可观察的暂停原因。作者授权
不等于平台允许绕过技术或合同限制。

### 7.4 `AuthorizationFilter`

所有内容必须在进入 Ollama 前通过授权过滤。每项授权记录包含：

- 平台和社区 ID。
- 授权作者的规范化账号，或明确的授权帖子 ID。
- 授权范围：指定帖子，或作者明确授权的持续范围。
- 用户保存的授权记录引用、记录日期和可选到期日。
- 状态：`active`、`revoked`、`expired`。

扫描整个板块只用于发现候选项；未命中 active 授权的内容立即丢弃，不保存正文或摘要。
授权检查默认拒绝，账号大小写和 Unicode 规范化必须由平台适配器确定。

### 7.5 `OllamaStyleAnalyzer`

分析器调用 `POST /api/chat`，使用非流式响应和 JSON Schema 输出。输入是单个已授权
样本的规范化正文，长度有硬上限。输出只允许以下受控特征：

- 句长和段落长度分布。
- 标点、表情和中英文混用倾向。
- 语气强度、直接程度、幽默和反问倾向。
- 通用称呼、连接词和话语标记。
- 应避免的攻击性、歧视性、身份冒充和个人信息模式。

输出不得包含原帖摘要、事实、链接、用户名、长引文或可执行指令。解析器拒绝额外字段、
未知枚举、非有限数值、超长字符串和无效 JSON。帖子中的指令全部视为数据；系统提示
明确禁止遵循帖子命令。

### 7.6 `StyleAggregator`

聚合器是确定性领域服务，不让模型直接覆盖 active 风格卡。它按社区汇总有效样本：

- 至少达到配置的最小样本数才发布首个 active 版本。
- 只有跨多个样本重复出现的短话语标记才能进入风格卡。
- 单一作者、单一帖子独有的表达不得成为社区级规则。
- 每项规则保存支持样本数和归一化权重，但不保存原文。
- 新版本先写为 candidate，通过 schema 和安全约束后原子激活。
- 样本撤回、过期或删除后重新聚合；没有足够样本时保留审计版本但取消 active 状态。

### 7.7 `GroupEventFilter`

群聊路径与私聊过滤分开。事件必须同时满足：

1. `post_type=message` 且 `message_type=group`。
2. `group_id` 在允许列表中，并存在有效风格绑定。
3. 消息含有指向机器人 QQ 的 `at` segment，或 reply 指向机器人发送的消息。
4. 消息包含非空文本，去除 `@` 后仍有实际问题。
5. 消息 ID 未处理，群和用户均未超过限流。

判断 reply 目标时优先读取本地发送记录；需要查询 OneBot `get_msg` 时使用已有 RPC
边界并校验返回发送者。解析失败时不得把普通 reply 当作触发。系统不被动持久化未触发
的群消息。

### 7.8 `GroupMessageCoordinator`

群聊协调器只完成文本回复，不挂接私聊 `CommandChain`、记忆、待办、审批、媒体或工具。
它从群绑定中取得 active 风格卡，构造分层提示词：

1. 固定系统安全规则和机器人身份。
2. 结构化风格卡，声明其只控制表达、不提供事实。
3. 当前触发消息及可选的被引用文本。

系统规则要求模型不声称自己是原作者、社区成员或真实人物，不复述训练样本，不因风格
模仿生成骚扰、歧视、隐私披露或危险内容。风格规则与安全规则冲突时安全规则优先。

### 7.9 `OllamaChatAdapter`

适配器默认访问 `http://127.0.0.1:11434/api/chat`，设置 `stream=false`，并限制：

- 请求体和响应体最大字节数。
- 连接和总请求超时。
- 输出 token、温度和上下文窗口。
- 同时在途请求数和每群串行锁。
- 仅接受 JSON object 中非空的 `message.content`。

适配器不自动拉取模型，不执行 Ollama CLI，也不在运行时修改模型。健康检查分别验证
HTTP 服务和配置模型是否存在。

### 7.10 `GroupDelivery`

新增 `build_send_group_msg(group_id, text, echo)`，并为群回复保存最小发送记录，以支持
去重和后续 reply-to-bot 判断。群 Outbox 使用 `(group_id, inbound_message_id)` 作为
幂等边界，不能与私聊用户 ID 共用命名空间。

## 8. 数据模型

在现有 SQLite 状态库中新增独立表，不改变已有私聊表的语义。

### 8.1 `community_style_sources`

- `id`、`platform`、`community_key`、`profile_key`。
- `enabled`、`schedule_seconds`、`max_items_per_sync`。
- `cursor_json`、`last_attempt_at`、`last_success_at`。
- `status`、`safe_error_class`、`next_attempt_at`。
- `config_fingerprint`、`created_at`、`updated_at`。

`cursor_json` 只允许适配器定义的有界 schema，不能写入 Cookie、Token 或正文。

### 8.2 `style_authorizations`

- `id`、`source_id`、`subject_type`、`subject_key`。
- `scope`、`evidence_ref`、`granted_at`、`expires_at`。
- `status`、`revoked_at`、`created_at`、`updated_at`。

同一来源和授权主体只有一条当前记录。`evidence_ref` 是用户管理的本地引用，不复制
第三方私聊或个人信息进入数据库。

### 8.3 `community_style_samples`

- `id`、`source_id`、`authorization_id`。
- `external_content_id`、`canonical_url_hash`、`author_key_hash`。
- `content_sha256`、`feature_schema_version`、`features_json`。
- `model`、`analyzed_at`、`status`、`removed_at`。

唯一约束为 `(source_id, external_content_id, content_sha256)`。原帖正文只存在于单次
处理的内存或受限临时文件中，分析成功或失败后立即清除，不写入该表和普通日志。

### 8.4 `community_style_profiles`

- `id`、`profile_key`、`version`、`status`。
- `rules_json`、`sample_count`、`author_count`。
- `source_fingerprint`、`created_at`、`activated_at`。

每个 `profile_key` 最多一个 active 版本。撤回重建产生新版本，不原地修改历史版本。

### 8.5 `group_style_bindings`

- `group_id`、`profile_key`、`profile_version`。
- `enabled`、`cooldown_seconds`、`max_reply_chars`。
- `config_fingerprint`、`updated_at`。

默认跟随 profile 的 active 版本；只有显式 pin 时才保存固定版本。

### 8.6 `group_message_receipts`

- 方向、`group_id`、消息 ID 哈希、关联入站消息 ID 哈希。
- 状态、尝试次数、下次重试时间和受控错误类别。
- 创建、发送和终态时间。

记录不保存消息正文，只为幂等投递和 reply-to-bot 判断服务。

## 9. 定时调度与并发

- `style-learner` 使用持久化 `next_attempt_at`，重启后不会同时补跑所有来源。
- 每个来源同一时间只有一个同步任务，不同来源受全局小并发限制。
- 正常周期添加抖动，避免固定时间产生请求尖峰。
- 网络错误指数退避并设上限；平台明确限速优先采用平台时间。
- Ollama 分析并发独立于群聊生成，并为群聊预留容量，避免同步任务占满模型。
- 同一 QQ 群消息严格串行，不同群在全局上限内并行。
- 关闭时停止领取新任务，给当前请求有界宽限时间，并把未完成同步恢复为可重试状态。

## 10. 故障语义

- Ollama 未安装或服务不可达：群聊返回一次简短受控提示；私聊继续工作。
- Ollama 缺少配置模型：健康检查失败，风格同步暂停，群聊使用相同受控提示。
- 群没有 active 风格卡：使用克制的默认语气回复并记录 `profile_not_ready`。
- Reddit 认证失效：暂停该来源，保留上一 active 风格卡，不回退网页采集。
- Reddit 或贴吧限速：延后该来源，不密集重试。
- 贴吧验证码、登录墙或 robots 禁止：暂停来源并报告明确原因。
- HTML 结构变化：丢弃本轮全部候选，不部分写入可能错误的数据。
- 风格分析超时或无效 JSON：样本标记安全错误，原文清除，后续按上限重试。
- SQLite 写入失败：不激活新 profile；旧 profile 继续可读。
- 群回复超时：按受控次数投递故障提示，不向 Ollama 无限重试。
- 进程重启或 WebSocket 重连：依赖消息收据去重，不重复回复。

所有日志使用来源 ID、内容 ID 哈希和安全错误类别，不记录授权正文、OAuth 密钥、完整
模型响应或群消息正文。

## 11. 安全、隐私和合规

- 每个平台来源必须单独启用，授权过滤默认拒绝。
- Reddit 使用 OAuth 和获准的访问身份；不得用匿名 JSON 页面代替失败的 API。
- 贴吧仅做低频公开访问，并持续遵守 robots、平台协议和技术限制。
- 作者撤回时将授权标为 revoked、移除派生样本、重新生成 profile，并清除待处理原文。
- 外部正文、HTML 和模型输出都是不可信输入，不能改变系统提示、配置或执行行为。
- 群 Agent 无工具、无 shell、无文件写权限，也不继承私聊长期记忆。
- 不把 QQ 群消息发送到远程 Ollama，除非管理员显式启用并接受该数据边界。
- 机器人账号资料和固定系统提示应清楚表明其为 AI，避免冒充具体作者或社区官方账号。

参考约束：

- Reddit Data API Terms：<https://redditinc.com/policies/data-api-terms>
- Reddit Developer Terms：<https://redditinc.com/policies/developer-terms>
- 百度贴吧协议：<https://sala.baidu.com/tb/eula.html>
- Ollama 本地 API：<https://docs.ollama.com/api/introduction>
- Ollama Chat API：<https://docs.ollama.com/api/chat>
- Ollama macOS 安装说明：<https://docs.ollama.com/macos>
- Ollama `qwen3:8b` 模型页：<https://ollama.com/library/qwen3:8b>

## 12. 配置示例

以下仅展示结构，真实社区、授权主体和密钥引用由用户填写：

```json
{
  "enabled": true,
  "ollama": {
    "base_url": "http://127.0.0.1:11434",
    "model": "qwen3:8b",
    "allow_remote": false
  },
  "sources": [
    {
      "id": "reddit-example",
      "platform": "reddit",
      "community": "example",
      "profile": "reddit-example",
      "schedule_seconds": 21600,
      "authorizations": [
        {
          "subject_type": "author",
          "subject_key": "authorized_user",
          "scope": "all_posts_until_revoke",
          "evidence_ref": "local-consent-record-001",
          "granted_at": "2026-09-19T00:00:00Z"
        }
      ]
    },
    {
      "id": "tieba-example",
      "platform": "tieba",
      "community": "示例吧",
      "profile": "tieba-example",
      "schedule_seconds": 21600,
      "authorizations": [
        {
          "subject_type": "post",
          "subject_key": "1234567890",
          "scope": "single_post",
          "evidence_ref": "local-consent-record-002",
          "granted_at": "2026-09-19T00:00:00Z"
        }
      ]
    }
  ],
  "groups": [
    {
      "group_id": 123456789,
      "profile": "tieba-example",
      "cooldown_seconds": 5,
      "max_reply_chars": 800
    }
  ]
}
```

环境变量只保存引导项：

- `ENABLE_GROUP_AGENT=false`
- `STYLE_AGENT_CONFIG_PATH=config/community-style-agent.json`
- Reddit OAuth secret references

功能默认关闭，只有配置、数据库迁移和健康检查全部通过后才显式开启。

## 13. 测试与验收

### 13.1 单元测试

- 群 ID、用户 ID、`at`、reply 和文本 segment 的严格解析。
- 非白名单群、普通群消息、对他人的回复和空文本被拒绝。
- 群/用户限流、消息去重和群间隔离。
- JSON 配置 schema、交叉引用、本地 URL 和明文密钥拒绝。
- 授权作者、授权帖子、撤回、到期和默认拒绝。
- Reddit 游标、限速和无网页回退语义。
- 贴吧 robots、验证码、登录墙、限速和解析失败语义。
- Ollama 请求上限、严格 JSON 特征解析和响应体限制。
- 风格聚合的最小样本、跨样本阈值、版本激活和撤回重建。
- 提示词优先级、原帖不进入运行时 prompt、群 Agent 无工具。
- `send_group_msg` 动作和群消息收据幂等键。

### 13.2 Repository 测试

- 新表创建和迁移幂等。
- 同一外部内容版本去重，不同内容哈希产生新样本版本。
- profile candidate 到 active 的原子切换。
- 撤回授权与样本移除、聚合重建在同一事务边界内收敛。
- 来源游标只在整批成功后推进。
- 群绑定、发送收据和 reply-to-bot 查询相互隔离。

### 13.3 集成测试

使用本地假 HTTP 服务，不访问真实 Reddit、贴吧或 Ollama：

- 同步授权样本并生成 active 风格卡。
- 未授权候选不会到达 Ollama，也不会留下正文。
- 同步失败保留旧风格卡和旧游标。
- QQ 群 `@` 触发后加载绑定 profile、调用假 Ollama 并发送一次群消息。
- 普通群消息不会调用 Ollama；私聊仍走原 OpenClaw 链路。
- Ollama 慢、返回超大响应或无效 JSON 时安全降级。
- WebSocket 重连不会重复发送已完成的群回复。

### 13.4 手工冒烟

1. 安装并启动 Ollama，拉取配置模型。
2. 运行只读 readiness，确认 OneBot、SQLite、Ollama 和模型均可用。
3. 用一个测试来源同步至少达到最小样本数，并查看 profile 版本和样本计数。
4. 在测试群发送普通消息，确认机器人不响应。
5. `@` 机器人提问，确认回复使用绑定风格且不复述样本。
6. 回复机器人上一条消息，确认可再次触发。
7. 停止 Ollama，确认群聊安全降级而私聊 OpenClaw 保持可用。
8. 撤回一项授权，确认样本移除并产生新的 profile 版本。

## 14. 部署与回滚

1. 先合入 schema、领域服务和全部假服务测试，功能保持关闭。
2. 安装 Ollama，并在本机拉取适合 16 GB 内存的配置模型。
3. 写入来源授权和测试群绑定，运行配置与 readiness 检查。
4. 单独启动 `style-learner`，观察一个完整同步周期。
5. 确认 active profile 后，只为测试群打开群聊功能。
6. 通过手工冒烟后再逐群加入 allowlist。

紧急回滚只需关闭 `ENABLE_GROUP_AGENT` 并停止 `style-learner`。已有 SQLite 表和历史
profile 保留用于审计；当前私聊 Bridge、OpenClaw、提醒、记忆和 Outbox 不需要降级。

## 15. 第一版完成标准

- 全部新增和现有回归测试通过。
- 自动测试不访问真实外部服务，也不下载模型。
- 至少一个授权测试来源可通过手工同步生成风格卡。
- 测试群只在 `@` 或回复机器人时调用 Ollama 并发送一次回复。
- 群绑定能够在两个独立 profile 之间切换，不需重启或重新训练模型。
- 未授权内容不会进入 Ollama、SQLite、日志或 prompt。
- 原帖正文不在分析完成后持久保存，也不在群聊生成时使用。
- Ollama 或采集器故障不影响现有私聊链路。
- 授权撤回能移除派生样本并重建风格卡。

## 16. 后续边界

第一版稳定后可以单独设计管理台编辑、来源状态可视化、人工风格卡审核和受控的低频
自主参与。知识检索、示例检索、媒体群聊和模型微调仍应作为独立阶段评审，不能因本
设计预留接口而默认启用。
