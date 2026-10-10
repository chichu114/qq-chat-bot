# QQ 个人账号 OpenClaw 私聊桥接

这个项目是在 NapCatQQ 与 OneBot v11 之上增加智能体能力的本地桥接服务。

## 架构定位：在 NapCat + OneBot 之上的升级

NapCatQQ 提供 QQ 登录与会话层，OneBot v11 提供标准事件与动作协议；本项目在它们
上方增加消息路由、权限策略、模型调用、可靠投递和健康恢复。这是对现有架构的能力扩展，
不是对 NapCat 或 OneBot 源码的修改。

```text
QQ -> NapCatQQ -> OneBot v11 WebSocket -> 本项目桥接与策略层
   -> OpenClaw / Ollama -> 可靠发件箱 -> OneBot v11 -> NapCatQQ -> QQ
```

主要升级包括：

- 私聊与群聊分流：私聊可走 OpenClaw，授权群聊可绑定独立 Ollama 风格模型；
- KanagawaWave 风格模型：支持 `@`/回复触发、模型判断后的主动插话和按群绑定；
- 本地策略层：白名单、限流、风格学习、敏感词谐音替换和高风险操作确认；
- 可靠发件箱：模型完成和 QQ 投递分离，支持去重、重试和失败审计；
- 受限自愈监控：独立 watchdog 检查 Bridge、OneBot 与 NapCat 状态，并在阈值内恢复。

项目各阶段、已完成能力、默认关闭模块和后续路线见
[总体设计与完成状态](docs/overall-design-status-zh.md)。

## 重要提醒

普通个人 QQ 号方案依赖非官方 QQ 协议工具，可能因为 QQ 行为变化而失效，也可能带来账号风控风险。建议使用备用 QQ 号，并保持第一版只允许自己或少数白名单账号私聊。

当前默认支持：

- 私聊文本消息、长期记忆、待办提醒和受限长任务
- 白名单 QQ 用户
- echo 测试适配器、本地 OpenClaw CLI 适配器或 HTTP 形式的 OpenClaw 适配器
- 基础限流、兜底回复和长回复截断
- 风格学习：你在私聊里反馈语气，桥接服务会写入 OpenClaw 工作区的 `STYLE.md`
- 可选 E4-A 图片、语音、文件和引用消息；默认关闭，需独立配置媒体 API
- 可选的独立群聊风格 Agent：定时从已获作者授权的 Reddit/贴吧内容提取表达风格并按群绑定；可另行开启本地 Ollama 判断后的主动插话，默认关闭

群聊 Agent 不支持群发、命令/工具链、长期群聊记忆或把来源正文当作知识库。项目也不支持任意本地 shell 执行、支付能力，以及 E4-B/E4-C 的日历、邮件、搜索、多 Agent 自动协作。

## 本地准备

建议使用 Python 3.9 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
cp .env.example .env
```

编辑 `.env`：

- `ONEBOT_WS_URL`：NapCatQQ 的 OneBot WebSocket 地址。
- `BOT_QQ`：由 NapCatQQ 登录的机器人 QQ 号。
- `ALLOWED_QQ_USERS`：允许触发智能体的 QQ 号，用英文逗号分隔。
- `AGENT_ADAPTER`：先用 `echo` 跑通链路；接本地 OpenClaw CLI 时改成 `openclaw-cli`。
- `OPENCLAW_COMMAND`：本项目的 OpenClaw 启动脚本，默认 `scripts/openclaw.sh`。
- `OPENCLAW_AGENT_ID`：桥接明确调用的 OpenClaw Agent，默认 `main`；E1 专用 Agent 使用 `qq`。
- `OPENCLAW_MODEL`：可选的模型覆盖，例如 `openai/gpt-5.5`。
- `ENABLE_STYLE_MEMORY`：是否启用风格学习，默认 `true`。
- `STYLE_MEMORY_PATH`：风格反馈写入的文件，默认 `~/.openclaw/workspace/STYLE.md`。
- `ENABLE_HOUSEKEEPER`：是否启用本地管家命令，默认 `true`。
- `HOUSEKEEPER_MEMORY_PATH`：长期记忆写入的文件，默认 `~/.openclaw/workspace/MEMORY.md`。
- `HOUSEKEEPER_TODO_PATH`：待办写入的文件，默认 `~/.openclaw/workspace/TODO.md`。
- `ENABLE_REMINDER_PUSH`：是否启用到点主动推送，默认 `true`。
- `REMINDER_CHECK_INTERVAL_SECONDS`：提醒扫描间隔，默认 `30` 秒。
- `REMINDER_STATE_PATH`：提醒暂停/恢复状态文件，默认 `~/.openclaw/workspace/REMINDER_STATE.md`。
- `ENABLE_DAILY_BRIEFING`：是否启用每日早报，默认 `true`。
- `DAILY_BRIEFING_TIME`：每日早报时间，默认 `09:00`。
- `ENABLE_EVENING_REVIEW`：是否启用晚间复盘提示，默认 `true`。
- `EVENING_REVIEW_TIME`：晚间复盘时间，默认 `23:00`。
- `QUIET_HOURS_START` / `QUIET_HOURS_END`：免打扰时段，默认 `00:00-08:30`。
- `ENABLE_SECURITY_CONFIRMATION`：是否启用 C3 高风险操作二次确认，默认 `true`。
- `CONFIRMATION_TIMEOUT_SECONDS`：确认单有效时间，默认 `120` 秒。
- `ENABLE_AUTO_BACKUP`：是否启用 D2 自动备份，默认 `true`。
- `AUTO_BACKUP_INTERVAL_HOURS`：自动备份间隔，默认 `24` 小时。
- `AUTO_BACKUP_RETENTION_DAYS`：自动备份保留天数，默认 `30` 天。
- `AUTO_BACKUP_MAX_FILES`：自动备份最多保留份数，默认 `30` 份。
- `AUTO_BACKUP_DIR`：备份目录，默认项目内的 `backups`。
- `ENABLE_SQLITE_STORE`：是否使用 E1 SQLite WAL 状态库，默认 `true`。
- `STATE_DATABASE_PATH`：动态状态数据库路径，默认 `~/.openclaw/workspace/chichu.sqlite3`。
- `ENABLE_MEDIA_PIPELINE`：启用 E4-A 图片、语音和文件处理，默认 `false`。
- `ENABLE_LONG_TASKS`：启用长任务接单、进度和取消，默认 `false`，可独立开启。
- `NAPCAT_MEDIA_ROOTS` / `MEDIA_REMOTE_ALLOWED_HOSTS`：允许读取的 NapCat 本地根目录或精确远端主机白名单；媒体模式至少配置一项。
- `MEDIA_API_BASE_URL`、`MEDIA_IMAGE_MODEL`、`MEDIA_AUDIO_MODEL`：独立媒体 API 与模型，不复用主对话模型配置。
- `MEDIA_API_SECRET_REF`：推荐填写 `keychain:服务名:账户名`；`MEDIA_API_KEY` 只保留兼容用途。
- `MEDIA_API_DAILY_BUDGET`：媒体 API 每日费用保险丝；`0` 表示禁止付费媒体调用。
- `MEDIA_CACHE_DIR` / `MEDIA_CACHE_TTL_SECONDS`：受限缓存目录与过期时间，默认 1 小时。
- `ENABLE_WATCHDOG`：是否启用 D3 独立自愈监控，默认 `true`。
- `WATCHDOG_CHECK_INTERVAL_SECONDS`：健康检查间隔；实时监测建议设为 `10` 秒。
- `WATCHDOG_FAILURE_THRESHOLD`：桥接连续失败多少次后尝试重启，默认 `3` 次。
- `WATCHDOG_ONEBOT_FAILURE_THRESHOLD`：OneBot 连续报告离线多少次后自动恢复，建议 `2` 次。
- `WATCHDOG_ONEBOT_AUTO_RECOVERY`：是否允许 watchdog 通过 NapCat WebUI 自动重启 QQ/NapCat。
- `NAPCAT_WEBUI_URL` / `NAPCAT_WEBUI_CONFIG_PATH`：NapCat 本机管理地址及包含 WebUI Token 的配置文件；Token 不复制进 `.env`。
- `WATCHDOG_RESTART_COOLDOWN_SECONDS`：两次自动重启间的冷却时间，默认 `300` 秒。
- `WATCHDOG_MAX_RESTARTS` / `WATCHDOG_RESTART_WINDOW_SECONDS`：默认 30 分钟内最多自动重启 3 次。
- `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`：如果你的 OpenAI API 需要走 Clash 代理，把端口写到这里，例如 `http://127.0.0.1:7897`。

## 可选：独立群聊风格 Agent

这条链路与现有私聊 OpenClaw 链路隔离，默认由
`ENABLE_GROUP_AGENT=false` 关闭。它只学习已授权贴文的抽象表达风格，
不学习事实，也不把来源原文带进群聊提示词。默认只响应 `@机器人` 或回复机器人；
主动插话必须再显式启用。

### 现有 KanagawaWave v2 CUDA 训练包

仓库已经提交了之前实际使用的训练方案，位于
[`training/kanagawawave_v2`](training/kanagawawave_v2/README_zh.md)：它固定使用
LLaMA-Factory、`Qwen/Qwen3-4B-Instruct-2507`、`qwen3_nothink` 与 4-bit NF4 QLoRA，
并提供 RTX 4060 8GB 和低显存两份配置。它是可复现的 KanagawaWave v2 固定配方，
不是全模型通用训练器。

把已经生成并通过审核的 v2 数据集打成训练包：

```bash
python scripts/package_kanagawave_v2_training.py \
  --dataset-dir datasets/kanagawawave_v2 \
  --output-zip KanagawaWave-v2-training.zip
```

在 CUDA 训练机上解压并校验后即可开始训练：

```bash
python scripts/validate_kanagawawave_v2_bundle.py .
llamafactory-cli train configs/train_4060_8gb.yaml
```

数据集必须保持现有 `instruction` / `input` / `output` 契约、固定提示词和清单哈希；
打包器不会接受任意未经校验的原始文本。完整的继续训练、合并和 Ollama 导入说明见
[训练包说明](training/kanagawawave_v2/README_zh.md)。

### 1. 安装本地 Ollama

从 [Ollama 官方 macOS 下载页](https://ollama.com/download/mac) 安装并启动
Ollama。模型下载必须由操作者在自己的终端明确执行；本项目的安装、启动和
readiness 命令都不会替你下载模型：

```bash
ollama pull qwen3:8b
```

`ollama pull` 的含义和其他命令可查 [Ollama 官方 CLI 文档](https://docs.ollama.com/cli)。
默认只连接 `http://127.0.0.1:11434`。只有把 JSON 的 `allow_remote` 明确设为
`true` 后，Task 1 配置契约才允许远端 Ollama；readiness 会对这个已授权地址执行健康
探测。远端部署应使用可信 HTTPS 端点，并由运营者承担传输、访问控制和数据离开本机的
风险。无论本地或远端，readiness 都只用五秒超时调用 `/api/tags`，不会生成回复或
拉取模型。

### 2. 准备 Reddit OAuth bearer

先阅读 Reddit 当前的 [Data API 指引](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)、
[API 文档](https://www.reddit.com/dev/api/) 与
[Developer Terms](https://redditinc.com/policies/developer-terms) / [Data API Terms](https://redditinc.com/policies/data-api-terms)，
再按 Reddit 的申请流程在[应用管理页](https://www.reddit.com/prefs/apps)注册应用并取得
只有读取所需范围的 OAuth bearer。Reddit 来源只访问 OAuth API，不会退回网页抓取。

当前版本是“单 Keychain bearer”MVP：它只解析
`REDDIT_OAUTH_TOKEN_REF=keychain:服务名:账户名`，不会接收 bearer 明文，也不在项目内
完成令牌续期。请用 macOS“钥匙串访问”创建“通用密码”，把 bearer 作为密码保存，
并由外部流程在到期前更新同一服务名/账户名的条目。`.env` 和 JSON 中只能保存引用，
不能保存 bearer。若没有可靠的外部轮换，Reddit 定时学习会在 bearer 到期后停止，
这不是长期无人值守的生产认证方案。

贴吧来源只读取无需登录的公开页面并遵守 robots 决策；遇到验证码、登录墙、
`403`、`429` 或无法识别的页面结构会停止/退避，不会尝试绕过。

### 3. 记录授权并配置按群绑定

先复制完全合成、没有真实账号或群号的例子：

```bash
cp config/community-style-agent.example.json config/community-style-agent.json
```

编辑新文件时：

- `author` + `all_posts_until_revoke` 只允许该作者在对应来源中的贴文；
- `post` + `single_post` 只允许一个明确贴文；
- `evidence_ref` 只写本地同意记录编号，不写聊天原文、姓名或联系方式；
- 每个 `groups[].group_id` 只绑定一个 `profile`。示例的六小时周期、五秒冷却和
  800 字回复上限可按需收紧；
- 只有在作者明确同意、授权范围与平台主体完全匹配且未过期时，正文才会短暂进入
  本地 Ollama 风格分析。

撤权时，从 `authorizations` 删除对应的精确授权项并原子保存配置。学习进程下次热加载
会撤销贡献并使相关画像失效/重建；确认日志完成后再继续使用该画像。若需立即停用整条
群聊链路，先把 `.env` 的 `ENABLE_GROUP_AGENT` 改回 `false`，停止 learner 并重启
Bridge，再处理配置记录。不要仅删除同意记录却保留 JSON 授权项。

### 4. 启用、检查和启动

在 `.env` 中设置以下非秘密值，并保持文件权限为 `0600`：

```bash
ENABLE_GROUP_AGENT=true
STYLE_AGENT_CONFIG_PATH=config/community-style-agent.json
KANAGAWAVE_PRIVATE_USERS=
ENABLE_GROUP_INTERJECTIONS=true
GROUP_INTERJECTION_COOLDOWN_SECONDS=10
ENABLE_GROUP_HOMOPHONE_FILTER=true
GROUP_HOMOPHONE_MAP_PATH=config/group-homophones.json
REDDIT_OAUTH_TOKEN_REF=keychain:qq-style-agent:reddit-oauth
```

先启动独立 learner，让它初始化状态库并发布足量样本的画像；确认 readiness 后再启动
或重启 Bridge：

```bash
chmod 600 .env
scripts/style_learner_service.sh start
set -a
source .env
set +a
.venv/bin/python scripts/check_style_agent.py
.venv/bin/python scripts/check_style_agent.py --json
scripts/bridge_service.sh restart
```

readiness 是只读检查：它验证 Task 1 bootstrap/JSON，把源 SQLite 主库和复制期间保持
不变的 WAL 只读复制到系统临时目录，仅在临时副本上以 `mode=ro + query_only` 汇总
Task 3 状态，再调用 Task 4 的 Ollama `/api/tags` 健康接口。它不会让 SQLite 打开源库，
也不在源目录创建或修改数据库、WAL、SHM、锁或表；不会迁移、同步、推进游标、发 QQ、
生成回复或下载模型。只有复制前后印章变化、sidecar 增删/替换等可证实的源库并发变化，
或存在热 journal 时才返回 `database=busy`；无法证明源库变化的复制 I/O 或临时资源
故障返回 `database=snapshot_unavailable`，不会拿忽略 WAL 的旧主库冒充 ready。退出码为 `0`（ready 或明确
disabled）、`1`（not ready）、`2`（invalid config）；输出只包含固定状态和计数，
不包含群号、路径、授权主体、同意证据、Keychain 引用或 bearer。首次启动 learner 前
数据库尚不存在时，`database=missing` 和退出码 `1` 是预期结果。

快照复制还有独立的 DB/WAL/SHM 与总字节上限，以及十秒绝对时限；按固定小块复制时会
持续检查增长和超时。当前内部保险丝分别是主库 512 MiB、WAL 256 MiB、SHM 64 MiB、
合计 640 MiB。它们是防稀疏展开、异常增长和慢文件系统拖挂的安全阈值，不是受支持状态库
容量承诺；触发时固定返回 `database=snapshot_unavailable`，临时部分副本会被清理。

只启用 `ENABLE_GROUP_AGENT` 时，白名单群里只有两类消息触发：明确 `@` 机器人，或回复
机器人已经成功发出的消息。再启用 `ENABLE_GROUP_INTERJECTIONS` 后，普通文本会先交给同一
本地 Ollama 模型做严格布尔判断；适合接话的闲聊、玩梗、吐槽、观点或群体提问才会进入
现有 KanagawaWave 风格回复链路。`GROUP_INTERJECTION_COOLDOWN_SECONDS=10` 表示每个群的
主动回复至少相隔十秒，不设置滚动十分钟次数上限；`@`/回复触发仍走群配置里的原有冷却，
不经过主动插话判断器。纯链接、命令、纯表情/标点、过长或异常消息、其他群及机器人自身
消息不会触发；模型判断失败时保持静默。

群聊敏感词谐音替换由 `ENABLE_GROUP_HOMOPHONE_FILTER` 单独启用，只处理机器人最终发出的
群消息，不修改私聊、用户输入、训练数据或日志。词表位于
`config/group-homophones.json`，在 `replacements` 中填写明确的
`"原词": "谐音词"` 映射；保存后下一条群消息自动加载，无需重启。最长词优先且每段文本
只替换一次，因此替换结果不会再次触发另一条规则。词表更新无效时继续使用上一份有效版本；
首次启动时文件缺失或无效则拒绝启动群聊运行时，避免无过滤发送。词表文件最多 64 KiB、
最多 512 条映射，原词和谐音词各不超过 64 个字符；被拒绝的热更新只记录一条不含路径和
词条内容的告警。

若要让特定白名单 QQ 的普通私聊固定使用同一个 KanagawaWave 模型，把这些 QQ 同时加入
`ALLOWED_QQ_USERS` 和 `KANAGAWAVE_PRIVATE_USERS`（英文逗号分隔）。待办、记忆和安全命令
仍由原命令链先处理；需要生成对话时才切到本地模型。未列入该配置的私聊用户继续使用
原 OpenClaw 模型；本地模型失败时返回固定故障提示，不回退到其他模型。

### 5. 隐私、日志、故障与回滚

来源正文只在单次分析的内存边界内使用，但 SQLite 并非“只存派生数据”。为支持调度、
授权、版本和投递去重，它会明文保存来源 ID、平台、community/profile/model/config
标识、游标、状态和时间戳；授权的 `subject_type`、`subject_key`、scope、有效期及撤权
元数据；样本的 external content ID；群 ID、画像绑定和版本；以及成功投递后的 outbound
receipt ID。`evidence_ref` 原值存在操作者维护的 JSON 中，SQLite 只保存它的摘要；
样本还保存派生 features/counts 及内容、作者、canonical URL、配置等不可逆指纹。

SQLite 不保存来源正文、canonical URL 原文、群消息正文、OAuth bearer 或 Keychain
secret；入站群消息只用于计算去重摘要。来源正文不会成为运行时检索上下文，群消息也不
进入长期群聊记忆。日志只应出现固定错误类别；不要把真实贴文、bearer、同意材料或群聊
正文粘进 issue/日志。JSON 配置本身含 community、授权 subject、授权记录引用和群 ID，
也必须按敏感运维文件保护。

```bash
tail -f logs/style-learner.log
tail -f logs/bridge.log
scripts/style_learner_service.sh status
scripts/bridge_service.sh status
```

常见故障：

- `ollama=model_missing`：由操作者运行 `ollama pull qwen3:8b`，再重跑 readiness；
- Reddit `429`：停止加速抓取，等待平台窗口恢复并检查当前 API 限制；
- 贴吧 CAPTCHA/登录墙：保持暂停，人工确认公开访问与条款，不要绕过；
- `profile-not-ready`：先检查授权未撤销/过期、learner 日志和有效样本数；旧画像不会被
  半成品覆盖；
- `database=missing/invalid/busy/snapshot_unavailable`：确认 learner 已启动以及
  `STATE_DATABASE_PATH` 指向现有状态库；`busy` 时等待当前写入结束后重试；
  `snapshot_unavailable` 时检查状态库/WAL 异常增长或慢文件系统，readiness 本身不会
  修复、迁移或扩大保险丝；
- `ollama=connection/timeout`：确认本机 Ollama 已启动且监听 loopback。

回滚时将 `ENABLE_GROUP_AGENT=false`，然后执行：

```bash
scripts/style_learner_service.sh stop
scripts/bridge_service.sh restart
```

这会恢复为原有私聊路径；不要删除 SQLite 以“回滚”，保留它便于审计和后续安全恢复。
无论内容公开与否，平台条款、robots、API 访问资格、作者授权、撤权响应、隐私与当地法律
责任都由部署和运营者承担。作者同意不替代 Reddit、贴吧或 QQ 的平台许可。

## 日常启动

日常使用时，先确认：

1. Clash Verge 已启动，并且代理端口与 `.env` 里的端口一致。
2. QQ/NapCat 已启动并登录机器人 QQ。
3. NapCat 的 OneBot WebSocket 地址与 `.env` 里的 `ONEBOT_WS_URL` 一致。

### 双击启动

打开项目里的 `apps` 文件夹：

- 双击 `启动QQ智能体.app`：后台启动桥接服务。
- 双击 `停止QQ智能体.app`：停止后台桥接服务。
- 双击 `查看QQ智能体日志.app`：打开最近运行日志。
- 双击 `打开QQ智能体面板.app`：启动并打开本地管理台。

启动成功后，macOS 会弹出通知。然后就可以给机器人 QQ 发私聊。

运行日志在：

```bash
logs/bridge.log
```

如果双击启动失败，先看这个日志文件。

也可以直接双击 `查看QQ智能体日志.app`，省得你再翻文件夹。是的，少折腾一点，chichu 才像管家，不像让你打杂的老板。

### 命令启动

命令行管理同样走 LaunchAgent：

```bash
scripts/bridge_service.sh start
scripts/bridge_service.sh status
scripts/bridge_service.sh stop
```

`scripts/start_bridge.sh` 只用于前台调试。使用前先执行
`scripts/bridge_service.sh stop`，不要让前台进程和 LaunchAgent 同时连接 OneBot。

如果只想检查配置，不启动桥接服务：

```bash
scripts/start_bridge.sh --check-only
```

如果想额外做一次真实 OpenClaw API 测试：

```bash
scripts/check_ready.sh --smoke-openclaw
```

这个命令会真实调用一次模型，可能消耗 API 用量。

切到 F0 `direct` 后，可显式做一次编排探测：

```bash
scripts/check_ready.sh --smoke-orchestration
```

它要求 `ORCHESTRATION_MODE=direct`，只调用一次模型，并拒绝 OpenClaw
embedded fallback。普通 `scripts/check_ready.sh` 不调用模型。

媒体能力启用并配置完成后，可显式做一次独立媒体 API 语义探测：

```bash
scripts/check_ready.sh --smoke-media
```

它会发送一张极小测试图片并产生少量媒体 API 用量；普通 `check_ready.sh` 不会调用媒体 API。

### 常见故障

- 启动检查提示 `OneBot/NapCat is not reachable`：QQ/NapCat 还没开，或者 OneBot WebSocket 端口和 `.env` 不一致。
- QQ 收到 OpenClaw 卡住的提示：模型或网络超时，先检查 Clash 和 OpenAI API 状态。
- QQ 收到代理/地区提示：Clash 没开、端口变了，或当前节点不适合 OpenAI。
- QQ 收到 API Key 提示：OpenClaw 的 OpenAI API Key 或认证状态需要重新检查。

## 启动 NapCatQQ

1. 使用备用 QQ 号登录 NapCatQQ。
2. 开启 OneBot v11 WebSocket 服务。
3. 确认 WebSocket 地址与 `.env` 里的 `ONEBOT_WS_URL` 一致，例如 `ws://127.0.0.1:3001`。
4. 确认该账号能接收你的私聊消息。

## 启动桥接服务

推荐日常直接使用：

```bash
scripts/bridge_service.sh start
```

前台调试时才手动启动：

```bash
set -a
source .env
set +a
python3 -m qq_openclaw_bridge
```

如果 `AGENT_ADAPTER=echo`，白名单 QQ 用户发送私聊文本后，会收到 `echo: 原消息`。这一步用于确认 QQ -> OneBot -> 桥接服务 -> QQ 的链路正常。

## 安装并初始化 OpenClaw

本项目使用项目本地 OpenClaw CLI，不要求系统安装 Node。确认版本：

```bash
scripts/openclaw.sh --version
```

如果还没安装本地 OpenClaw 依赖：

```bash
${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/pnpm add openclaw@latest
${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/pnpm approve-builds
```

初始化 OpenClaw 配置和工作区：

```bash
scripts/openclaw.sh setup --non-interactive --accept-risk --workspace "$HOME/.openclaw/workspace"
```

E2 默认通过常驻 Gateway 调用 OpenClaw。若提示 Gateway 未启动，请先运行 `bash scripts/openclaw.sh gateway start`，再重新启动 QQ 智能体。只有显式设置 `OPENCLAW_USE_GATEWAY=false` 时才会退回本地嵌入模式。

配置模型：

```bash
scripts/openclaw.sh configure --section model
```

不要把 API Key 发到聊天里；在你自己的终端提示里输入。

配置完成后，先做一次本地 smoke test：

```bash
scripts/openclaw.sh agent --local --json --message "只回复两个字：你好" --session-key agent:main:qq-smoke --timeout 60
```

## 接入 OpenClaw CLI

echo 链路成功、OpenClaw 模型也配置成功后，把 `.env` 改成：

```bash
AGENT_ADAPTER=openclaw-cli
OPENCLAW_COMMAND=scripts/openclaw.sh
OPENCLAW_MODEL=
OPENCLAW_TIMEOUT_SECONDS=120
HTTPS_PROXY=http://127.0.0.1:7897
HTTP_PROXY=http://127.0.0.1:7897
ALL_PROXY=http://127.0.0.1:7897
NODE_USE_ENV_PROXY=1
```

重启桥接服务：

```bash
scripts/bridge_service.sh restart
```

然后用白名单 QQ 给机器人 QQ 发私聊，回复就会来自 OpenClaw。

如果 QQ 收到的是 `智能体暂时不可用，请稍后再试。`，先看桥接服务终端里的错误。常见原因是 Clash 代理没开、代理端口变了、OpenAI API 超时或 `.env` 没有重新加载。

## 第二阶段：让 chichu 学你的语气

桥接服务现在会先识别一小类“风格反馈”。这些消息不会发给模型消耗 API，而是直接写入：

```bash
~/.openclaw/workspace/STYLE.md
```

### B1：固化 chichu 人设内核

如果 OpenClaw 工作区被重置，或者 chichu 又开始像“刚初始化的代理”，可以重新安装 B1 人设内核：

```bash
python3 scripts/install_persona_b1.py ~/.openclaw/workspace
```

这个脚本会更新：

- `~/.openclaw/workspace/SOUL.md`
- `~/.openclaw/workspace/STYLE.md`

它只替换 `B1 人设内核` 和 `B1 风格内核` 这两个受控小节，不会清掉 `STYLE.md` 里的用户反馈沉淀。也就是说，它负责把 chichu 的身份证钉牢，不会顺手把她的成长记录擦了，没那么粗暴。

### B2：本地身份问答兜底

下面这些身份问题会由桥接服务本地直接回复，不调用模型：

- `你是谁`
- `介绍一下你自己`
- `chichu 是谁`

这样 chichu 的名字、身份和管家感不会因为模型上下文波动而乱飘。她至少得知道自己是谁，不然还管什么家，先管管自己吧。

### B3：风格反馈升级

可以直接给机器人 QQ 发：

- `太客服了，少废话`
- `更御姐一点`
- `毒舌一点`
- `太凶了`
- `太油了`
- `太冷了`
- `更毒舌一点`
- `少自我介绍`
- `更像管家`
- `这个味对了，以后都这样`
- `记住这句`

收到这类消息后，chichu 会短回复一句确认，并把反馈沉淀到 `STYLE.md` 的 `用户反馈沉淀` 小节。之后正常聊天时，OpenClaw 会继续读取工作区里的风格文件。

### B4：风格账本管理

可以直接给机器人 QQ 发：

- `查看风格`
- `风格沉淀`
- `风格自检`
- `删除风格 1`
- `清空风格反馈`

这些口令也不会调用模型。`查看风格` 和 `风格沉淀` 会列出当前沉淀；`风格自检` 会统计数量并显示最近几条；`删除风格 1` 会删掉指定编号；`清空风格反馈` 只清空 `用户反馈沉淀` 小节里的列表，不会动 B1 人设内核。账本终于能自己翻了，不用你亲自当文件管理员。

如果想暂停这个能力，把 `.env` 改成：

```bash
ENABLE_STYLE_MEMORY=false
```

然后重启 `启动QQ智能体.app`。

## 第三阶段：长期记忆和待办

桥接服务现在会先处理一批本地管家命令。这些命令不会调用模型，也不会消耗 API；chichu 会直接写入 OpenClaw 工作区：

```bash
~/.openclaw/workspace/MEMORY.md
~/.openclaw/workspace/TODO.md
```

### 长期记忆

给机器人 QQ 发：

- `记住：我不喜欢长篇解释`
- `记住：我喜欢先给结论`
- `查看记忆`
- `搜索记忆 结论`
- `删除记忆 1`
- `清空记忆`

`记住：...` 会写入长期记忆；`查看记忆`（也支持 `我的记忆`）会按编号列出当前内容；`搜索记忆 关键词` 会返回匹配项及其原编号；`删除记忆 1` 会删除指定编号；`清空记忆` 只清空 `MEMORY.md` 的“长期记忆”小节，不会删除文件中的其他内容。这些命令都在本地执行，不调用模型，也不消耗 API。

### 待办和提醒

给机器人 QQ 发：

- `待办：检查 API 额度`
- `工作待办：写周报`
- `待办：项目：修启动按钮`
- `待办：背单词 #学习`
- `明天晚上提醒我检查 API 额度`
- `2026-07-11 21:30提醒我检查 API 额度`
- `每天9点提醒我喝水`
- `每周一 9:30提醒我开周会`
- `每月1号 9:00提醒我交房租`
- `待办列表`
- `待办列表 工作`
- `完成待办 1`
- `未来七天提醒`
- `把提醒 1 改到明天晚上`
- `改期提醒 1 到每周一 9:30`
- `取消提醒 1`

提醒会写入 `TODO.md`，并保存到期时间、QQ 用户和已提醒状态。桥接服务运行并连接 OneBot 时，会按 `REMINDER_CHECK_INTERVAL_SECONDS` 周期扫描，到点后主动给对应 QQ 私聊推送，并把该提醒标记为已提醒，避免重复轰炸。

待办支持五个分类：`工作`、`生活`、`学习`、`项目`、`其他`。可以用 `工作待办：...`、`待办：工作：...` 或末尾 `#工作` 记录分类；用 `待办列表 工作` 查看单个分类。

重复提醒支持：

- `每天9点提醒我喝水`
- `每周一 9:30提醒我开周会`
- `每月1号 9:00提醒我交房租`

重复提醒发出后会自动把下一次到期时间写回 `TODO.md`，不会每天都新建一条，账本不至于膨胀成灾。

当前支持的时间表达先走实用路线：

- `今晚`、`明天`、`明天晚上`、`后天早上`
- `明天上午10点`、`明天下午3点`
- `2026-07-11 21:30`
- `7月12日 9:30`
- `每天9点`
- `每周一 9:30`
- `每月1号 9:00`

### C2：智能待办管理

给机器人 QQ 发：

- `未来七天提醒`：按时间顺序查看接下来七天的提醒。
- `把提醒 1 改到明天晚上`：给普通待办设置时间，或修改已有提醒时间。
- `改期提醒 1 到2026-07-20 09:00`：改成指定日期时间。
- `改期提醒 1 到每周一 9:30`：把提醒改成周期提醒。
- `取消提醒 1`：取消到点推送，但保留原待办和分类。
- `删除待办 1`：连同待办本身一起删除。

改期会清掉旧的已提醒状态，保留事项内容和分类。无法识别或不合法的时间不会写入文件。这些管理口令由本地桥接服务处理，不调用模型，也不消耗 API。

### C3：高风险操作二次确认

下面这些本地操作不会立即执行：

- `删除记忆 1`
- `清空记忆`
- `删除待办 1`
- `清空已完成`
- `删除风格 1`
- `清空风格反馈`

chichu 会先说明待执行内容并生成六位审批码。默认在 120 秒内回复 `确认执行 A1B2C3` 这种“命令 + 对应审批码”才会继续；回复 `取消操作` 会作废；超时、审批码不匹配或由另一个 QQ 回复确认都不能放行。待确认记录保存在 SQLite 中，重启智能体后仍可正确恢复。每个 QQ 同时只保留一条待确认操作，新风险操作会替换旧确认单。

明确要求 OpenClaw 外发消息或文件、删除本地文件、执行命令或脚本、修改系统配置、付款或购买时，也会先进入相同确认流程。二次确认只允许请求继续交给 OpenClaw，不会启用项目原本不支持的能力。

普通咨询不会被拦截，例如 `如何删除文件` 会正常交给模型；`取消提醒 1` 只移除推送并保留待办，也不需要二次确认。

可以在 `.env` 中调整：

```bash
ENABLE_SECURITY_CONFIRMATION=true
CONFIRMATION_TIMEOUT_SECONDS=120
```

## D1：本地可视化管理面板

双击桌面的 `打开QQ智能体面板.app`，浏览器会打开：

```text
http://127.0.0.1:18790
```

管理面板是独立后台进程。即使在页面里停止 QQ 智能体，面板本身仍会运行，因此可以继续查看文件并重新启动智能体。它只监听本机 `127.0.0.1`，不会向局域网或公网开放。

当前面板支持：

- 查看桥接进程、OneBot、OpenClaw 和 C3 二次确认状态。
- 启动、停止和重启 QQ 智能体。
- 新增、删除和清空长期记忆。
- 新增、完成、删除、改期待办，以及取消到点提醒。
- 查看、删除和清空风格反馈沉淀。
- 查看经过脱敏的运行事件摘要。
- 一键备份和恢复 `.env`、记忆、待办、风格和提醒状态。
- 查看长任务状态、阶段和耗时，并请求取消自己的活动任务。
- 查看模型调用量、Token、失败数、P50/P95 延迟和每日预算。
- 查看审批记录与组件健康时间线。
- 修改非敏感媒体配置，并把新密钥直接写入 macOS Keychain；页面不会读取或回显密钥。

危险按钮会在浏览器里再次弹出确认框。所有写请求还必须携带面板启动时生成的随机令牌；令牌只存在当前本地面板页面中。备份保存在：

```text
项目目录/backups/
```

恢复备份前，面板会自动创建一份 `pre-restore` 备份。恢复 `.env` 后需要在总览中重启智能体，让配置重新加载。

面板后台服务也可以用脚本管理：

```bash
scripts/dashboard_service.sh start
scripts/dashboard_service.sh status
scripts/dashboard_service.sh stop
```

## D2：自动备份与恢复点

QQ 智能体启动后会立即检查自动备份；没有可用自动备份时会创建第一份，之后默认每 24 小时创建一次。备份包含：

- 项目 `.env`
- `MEMORY.md`
- `TODO.md`
- `STYLE.md`
- `REMINDER_STATE.md`
- `chichu.sqlite3`（SQLite 启用时使用一致性快照）
- `AGENTS.md`、`SOUL.md`、`IDENTITY.md`、`USER.md`
- `TOOLS.md`、`HEARTBEAT.md`

每份新格式备份都包含 `manifest.json`，记录创建时间、备份类型、文件大小和 SHA-256。恢复前会重新计算校验值；校验失败、包含未授权路径或 ZIP 损坏时会拒绝恢复，当前文件不会被覆盖。

备份中的 `.env` 会保留模型、功能开关和 `SecretRef`，但所有 `*_API_KEY` 原值会被置空；媒体缓存目录不进入备份。恢复后如需密钥，应从 Keychain 解析或重新写入，别把裸密钥塞回压缩包里。

默认自动备份保留 30 天且最多 30 份。清理规则只作用于 `auto` 自动备份，不会自动删除 `manual` 手动备份或 `pre-restore` 恢复前备份。每次恢复前都会先生成一份恢复前备份，并使用临时文件完成原子替换。

可以在 `.env` 调整：

```bash
ENABLE_AUTO_BACKUP=true
AUTO_BACKUP_INTERVAL_HOURS=24
AUTO_BACKUP_RETENTION_DAYS=30
AUTO_BACKUP_MAX_FILES=30
AUTO_BACKUP_DIR=backups
```

修改后重启 QQ 智能体。打开 D1 面板的“备份恢复”页面，可以查看备份类型和完整性、创建手动备份、单独校验、执行保留策略或恢复指定版本。

## D3：健康检查与受限自愈

双击“启动QQ智能体.app”时会同时启动独立 watchdog；双击“停止QQ智能体.app”或在面板中停止时，会先关闭 watchdog，避免你主动停机后又被自动拉起。管理面板顶部会显示自愈状态，总览中可以看到最近检查、当前重启次数和异常原因。

配置为实时模式后每 10 秒检查桥接进程、OneBot、代理端口和 OpenClaw 命令。OneBot 检查会实际调用 `get_status`，只有 `online=true` 且 `good=true` 才算健康；连续两次离线会通过 NapCat WebUI 完整重启 QQ/NapCat。两次重启至少间隔 5 分钟，30 分钟内最多重启 3 次；仍未上线时停止重启并提示扫码。通知显示失败只记录诊断，不会覆盖健康快照。

健康快照写入 `logs/health.json`，watchdog 日志写入 `logs/watchdog.log`。需要手动检查时可以运行：

```bash
scripts/watchdog_service.sh status
scripts/watchdog_service.sh restart
scripts/watchdog_service.sh stop
```

可以在 `.env` 调整：

```bash
ENABLE_WATCHDOG=true
WATCHDOG_CHECK_INTERVAL_SECONDS=10
WATCHDOG_FAILURE_THRESHOLD=3
WATCHDOG_ONEBOT_FAILURE_THRESHOLD=2
WATCHDOG_ONEBOT_AUTO_RECOVERY=true
WATCHDOG_RESTART_COOLDOWN_SECONDS=300
WATCHDOG_MAX_RESTARTS=3
WATCHDOG_RESTART_WINDOW_SECONDS=1800
WATCHDOG_NOTIFICATIONS=true
NAPCAT_WEBUI_URL=http://127.0.0.1:6099
NAPCAT_WEBUI_CONFIG_PATH="~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/NapCat/config/webui.json"
```

## E1：安全与数据底座

QQ 桥接明确路由到独立的 OpenClaw `qq` Agent。该 Agent 使用 Docker 沙箱，宿主工作区只读挂载，关闭 elevated，并采用双层工具白名单。允许的能力只有读取人格工作区、长期记忆检索、网页检索和会话状态；执行命令、写文件、外发消息、浏览器控制和创建子 Agent 均被硬性拒绝。

动态状态以 `chichu.sqlite3` 为主存储，开启 WAL、完整同步和跨进程文件锁；`MEMORY.md`、`TODO.md`、`STYLE.md`、`REMINDER_STATE.md` 继续作为可读导出。启用 SQLite 后不要直接编辑这些导出文件，否则下一次读取会恢复数据库里的正式版本；请通过 QQ 指令或管理面板修改。

E1 同时包含：六位审批码、审批记录持久化、SQLite 一致性备份、完整人格文件备份、正数配置校验，以及 `.env`、日志、数据库和锁文件的最小权限。

### SQLite ownership 来源边界

Task 7 的受控 Python 写入接口只接受 `1..10^20-1` 范围内的真正整数作为
`user_id`；`bool`、字符串、浮点数、零、负数和超界值都会在写入前被拒绝。
直接修改 SQLite 属于可信管理边界。SQLite 的 INTEGER affinity 会把 SQL
`TRUE` 存成整数 `1`，落库后它与调用方直接写入的合法整数 `1` 无法区分，
因此系统不会伪造 provenance 检测或仅因猜测来源语法而拒绝该值。腐败加固
保证畸形或相互冲突的持久 ownership 不会越权或阻塞队列，但不承诺从已规范
化的整数反推出原始 SQL 表达式。

## E2：常驻 Gateway 与可靠消息链路

E2 默认使用 `OPENCLAW_USE_GATEWAY=true`，桥接 CLI 不再添加 `--local`，而是把 Agent 回合交给本机 `127.0.0.1:18789` 上的常驻 OpenClaw Gateway。模型调用在线程工作器中执行，OneBot WebSocket、提醒和备份不会再被单次模型请求阻塞；同一 QQ 的会话严格串行，不同 QQ 可在并发上限内同时处理。

SQLite schema v2 新增入站消息去重、可靠发件箱和运行指标。回复与提醒先落库再发送，临时断线会指数退避重试，重启后继续；同一个 `message_id` 或提醒通知不会重复投递。连续模型故障达到阈值后会短暂熔断，避免故障线路被反复调用。

Outbox 的无回调 lease 过期属于外部效果不确定的 crash 场景：原始 generation
只允许一个 replay generation，因此最多产生一个重复外部效果；该 replay 再次
无回调过期后会按消息种类终态化。收到明确失败回调的 sender retry 不属于这
个模糊 crash allowance，仍遵循配置的重试次数。

关键参数：

- `OPENCLAW_GATEWAY_PROBE_URL=ws://127.0.0.1:18789`
- `MAX_CONCURRENT_AGENT_CALLS=2`
- `MAX_PENDING_EVENTS=50`
- `AGENT_RETRY_ATTEMPTS=2`
- `AGENT_CIRCUIT_FAILURE_THRESHOLD=3`
- `AGENT_CIRCUIT_COOLDOWN_SECONDS=60`
- `OUTBOX_RETRY_BASE_SECONDS=2`
- `OUTBOX_MAX_ATTEMPTS=8`

管理台会显示待发送、发送中、失败、处理中数量，以及最近一次模型和 QQ 投递成功时间。看门狗会探测真正的 Gateway 端口，并对 SQLite 执行完整性与失败队列检查。

```bash
OPENCLAW_AGENT_ID=qq
OPENCLAW_SESSION_PREFIX=agent:qq:qq
ENABLE_SQLITE_STORE=true
STATE_DATABASE_PATH=~/.openclaw/workspace/chichu.sqlite3
```

### 每日早报、晚间复盘和免打扰

桥接服务连接 OneBot 后，会按提醒扫描间隔检查主动推送：

- 每日早报：默认 `09:00`，推送今天提醒和当前待办。
- 晚间复盘：默认 `23:00`，提醒你回顾未完成待办。
- 免打扰：默认 `00:00-08:30`，这段时间不主动推送提醒、早报或复盘；过了免打扰时间，已到期提醒会继续推送。

可以在 `.env` 里调整：

```bash
ENABLE_DAILY_BRIEFING=true
DAILY_BRIEFING_TIME=09:00
ENABLE_EVENING_REVIEW=true
EVENING_REVIEW_TIME=23:00
QUIET_HOURS_START=00:00
QUIET_HOURS_END=08:30
```

如果想暂停本地管家命令，把 `.env` 改成：

```bash
ENABLE_HOUSEKEEPER=false
```

然后重启 `启动QQ智能体.app`。

如果只想暂停到点主动推送，但保留记忆和待办，把 `.env` 改成：

```bash
ENABLE_REMINDER_PUSH=false
```

然后重启 `启动QQ智能体.app`。

### 管家控制口令

给机器人 QQ 发：

- `删除待办 1`
- `修改待办 1：明天晚上提醒我检查 API 额度`
- `清空已完成`
- `查看今天提醒`
- `暂停提醒`
- `恢复提醒`

`暂停提醒` 会写入 `~/.openclaw/workspace/REMINDER_STATE.md`，后台扫描仍在，但不会主动推送。`恢复提醒` 后，到点推送继续工作。提醒真正发出后，日志里会出现 `reminder pushed`，方便排查是哪条待办推给了哪个 QQ。

## 接入自定义 HTTP 服务

如果你后续自己写了 OpenClaw HTTP wrapper，也可以把 `.env` 改成：

```bash
AGENT_ADAPTER=http
OPENCLAW_ENDPOINT=http://127.0.0.1:8000/agent/reply
```

桥接服务会向该地址发送 JSON：

```json
{
  "user_id": 987654321,
  "message_id": "123",
  "text": "你好",
  "timestamp": 1720000000.0
}
```

OpenClaw 入口需要返回以下任意一种字段：

```json
{"reply": "回复文本"}
```

也可以返回 `text` 或 `message` 字段。桥接服务会读取第一个非空文本字段并发回 QQ。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前测试覆盖配置加载、OneBot 文本解析、私聊过滤、白名单、限流、Gateway 与本地适配器、异步并发、单用户会话顺序、消息去重、可靠发件箱、重试与熔断、风格学习、长期记忆、SQLite WAL 与 Markdown 导出、持久审批码、待办分类、重复提醒、提醒暂停/恢复、免打扰、每日早报、晚间复盘、到点提醒推送、一致性备份、兜底回复、长回复截断和不支持消息类型提示。

## E3：长期记忆 2.0

E3 在 E2.5 模块化底座上增加四层记忆：人格核心、已确认用户档案、动态风格和已确认长期记忆。普通对话会保存到 SQLite；达到阈值后，独立轻量模型在后台生成摘要并提取候选事实，不会阻塞 QQ 回复。候选事实只有经过用户确认才会进入主模型上下文。

QQ 可用的新指令：

```text
待确认记忆
确认记忆 12
拒绝记忆 12
记忆状态
查看用户档案
```

原有 `记住：...` 视为用户主动确认，仍会写入 `MEMORY.md` 可读导出，同时进入 SQLite 已确认层。原有查看、搜索、删除和清空指令保持兼容。

检索采用关键词和本地特征向量混合排序，只注入当前问题相关的已确认事实与摘要。`AgentRequest.context` 与用户原始消息分离，因此安全确认和本地指令不会被记忆上下文改写。

E3 配置：

```dotenv
ENABLE_MEMORY_2=true
MEMORY_LIGHT_MODEL=openai/gpt-5.4-nano
MEMORY_LIGHT_TIMEOUT_SECONDS=180
MEMORY_LIGHT_SESSION_PREFIX=agent:qq:memory
MEMORY_SUMMARY_TRIGGER_TURNS=8
MEMORY_SUMMARY_BATCH_TURNS=24
MEMORY_CONTEXT_MAX_CHARS=2400
MEMORY_FACT_LIMIT=6
MEMORY_SUMMARY_LIMIT=3
MEMORY_CANDIDATE_MIN_CONFIDENCE_PERCENT=75
```

`MEMORY_LIGHT_MODEL` 只负责摘要和事实分类；复杂对话继续使用 `OPENCLAW_MODEL` 或 OpenClaw 默认主模型。每个摘要批次使用隔离 session，避免轻量模型历史无限增长。轻量任务在后台执行，180 秒上限用于容纳首次冷启动，不会阻塞 QQ 回复。把 `MEMORY_LIGHT_MODEL` 留空会退回本地保守摘要，本地模式不会自动提取候选事实。

如果 OpenClaw 提示轻量模型不在 Agent 白名单，运行下面的脚本。它只向 `agents.defaults.models` 增加指定模型，并在写入前后校验 `qq` Agent 的工具 deny 和 Docker 沙箱配置完全不变：

```bash
.venv/bin/python scripts/allow_memory_model.py
```

管理面板的“长期记忆”页会显示分层统计、待确认事实、最近摘要、轻量模型和分析状态，并允许直接确认或拒绝候选。

部署前可运行下面的脚本压缩重复人格规则。脚本只重写 `AGENTS.md` 和 `SOUL.md`，保留 `STYLE.md` 动态反馈，并先保存原文件：

```bash
.venv/bin/python scripts/install_persona_e3.py
```

详细设计见 `docs/superpowers/specs/2026-07-17-e3-long-term-memory-2-design.md`。

## E4-A：媒体、引用与长任务

E4-A 默认关闭。推荐先保持 `ENABLE_MEDIA_PIPELINE=false`、`ENABLE_LONG_TASKS=false` 完成纯文本回归，再分两步启用。

### 支持范围与限制

- 图片：JPEG、PNG、WebP、GIF，单个最多 10 MiB，每条消息最多 4 张。
- 语音：WAV、OGG、FLAC、AMR、MP3、M4A/MP4，单个最多 20 MiB。
- 文件：TXT、Markdown、CSV、JSON、PDF、DOCX，单个最多 20 MiB；JSON 额外限制为 2 MiB，CSV 最多读取 10000 行，PDF 最多 100 页。
- 引用消息：优先使用消息内嵌文本，必要时通过 OneBot `get_msg` 获取；不可用时只给模型受控失败标记。
- 所有附件内容都以“不可信媒体上下文”注入，不允许覆盖系统、安全或工具规则。

媒体只从可信来源进入：`NAPCAT_MEDIA_ROOTS` 是逗号分隔的本地绝对目录，`MEDIA_REMOTE_ALLOWED_HOSTS` 是逗号分隔的精确主机名。启用媒体时至少配置一项；只启用长任务则不需要媒体来源。

### 独立 API 与 Keychain

媒体 API 使用独立的 OpenAI 兼容端点，不复用 OpenClaw 主对话密钥。推荐在管理面板“媒体设置”中写入密钥，或手动执行：

```bash
security add-generic-password -U \
  -s qq-openclaw-media -a media-api -w '你的媒体 API Key'
```

然后配置：

```dotenv
MEDIA_API_BASE_URL=https://你的媒体服务/v1
MEDIA_API_SECRET_REF=keychain:qq-openclaw-media:media-api
MEDIA_IMAGE_MODEL=你的图片理解模型
MEDIA_AUDIO_MODEL=你的语音转写模型
MEDIA_API_DAILY_BUDGET=1.00
```

不要同时把 Key 写进 `MEDIA_API_KEY`。预算为 `0` 时所有付费媒体调用都会被保险丝拒绝；请求频率、并发和超时分别由 `MEDIA_API_REQUESTS_PER_MINUTE`、`MEDIA_MAX_CONCURRENT_CALLS`、`MEDIA_API_TIMEOUT_SECONDS` 控制。

### 长任务与控制台

启用 `ENABLE_LONG_TASKS=true` 后，超过接单阈值的任务会发送接单与真实阶段进度，最终结果走独立 Outbox 记录。QQ 私聊支持：

```text
查看任务
任务状态 <任务ID>
取消任务 <任务ID>
```

任务按 QQ 用户隔离，不能查看或取消别人的任务。管理面板 `http://127.0.0.1:18790` 可查看任务、模型统计、审批、健康时间线和媒体设置；任务取消与配置修改仍受本机 Host/Origin/随机 Token 校验。

常见受控失败包括附件超限、格式不支持、可信来源不匹配、缓存不安全、预算耗尽、API 超时或响应格式错误。回复只给稳定提示，原始 URL、路径、响应正文和密钥不会发到 QQ 或控制台。

### 启用、清理与回滚

1. 先配置 Keychain、可信来源、API 地址、模型和正数预算。
2. 运行 `scripts/check_ready.sh --smoke-media`，确认语义探测成功。
3. 先设 `ENABLE_MEDIA_PIPELINE=true` 并重启，验证图片、短语音、TXT、PDF 和引用消息。
4. 再设 `ENABLE_LONG_TASKS=true` 并重启，验证接单、进度和取消。

缓存默认位于 `~/.openclaw/workspace/media-cache`，运行时按 `MEDIA_CACHE_TTL_SECONDS` 清理，目录权限必须为 `0700`。缓存不进入备份。

遇到异常时先把对应开关改回 `false` 并重启。关闭媒体后纯文本、记忆、提醒、审批和 Outbox 继续工作；数据库 schema v5 和历史统计可以保留，无需降级数据库。E4-B（日历、邮件、搜索等外部能力）与 E4-C（管家/记忆/调度/编码多 Agent）不在本阶段。

## E2.5 模块化控制平面

E2.5 在不删除既有功能的前提下完成模块化重组。当前仍使用 `DirectPlanningStrategy + SingleAgentExecutionEngine`：每条需要模型处理的 QQ 消息只调用一次原有 OpenClaw Agent，不会因为重构额外消耗 API Token。

新增的 A++ 契约包括 `TaskContract`、规划策略、执行引擎、验收策略、预算策略、能力档案、头脑风暴开关和追踪端口。双模型议会、四级路由、DAG 工作包和非对称审计尚未启用；后续可通过实现这些端口接入，而无需改动 QQ 收发、Outbox、提醒或 SQLite Repository。

模块边界如下：

- `application/`：命令链、消息协调、并发调度、可靠投递和后台服务。
- `orchestration/`：A++ 稳定契约与当前单模型兼容实现。
- `domain/`：记忆、待办、时间规则和管家命令服务，不依赖 WebSocket 或 SQLite。
- `persistence/`：schema v5、文档、审批、消息、Outbox、分层记忆、媒体、长任务、编排追踪和指标 Repository。
- `runtime/`：集中装配和 WebSocket 生命周期。
- Dashboard 查询与命令分离；Watchdog 探针可独立测试。

扩展新能力时应优先新增策略、Repository 或领域服务，并在 `runtime/composition.py` 集中装配。不要让领域模块直接导入 CLI、HTTP Server、SQLite 或 OneBot。

## F0 编排上线与回滚

F0 只有两种可用模式：

- `legacy_direct`：兼容路径，保留原来的单模型调用流程，是部署和回滚默认值。
- `direct`：通过新的 Runtime、追踪和策略接口调用同一个 Agent；普通文本仍只调用一次模型，本地命令调用零次。

F1 影子路由现已实现但默认关闭：它只记录 L0-L3 决策，不接管 Direct 回复。启用、分类器安装、标注、验收和回滚见[中文 F1 影子路由运维手册](docs/f1-shadow-routing-runbook-zh.md)；双模型议会、DAG 工作包和非对称审计仍未实现。

分类器默认使用兼容的 OpenClaw transport。设置
`ROUTING_CLASSIFIER_TRANSPORT=responses_api` 可改用直连 Responses API；它从
`~/.openclaw/agents/<ROUTING_AGENT_ID>/agent/codex-home/auth.json` 读取 API key，
该文件必须由当前用户拥有且权限严格为 `0600`。`scripts/check_ready.sh --probe-routing`
会按所选 transport 执行一次真实分类探测。

### 审批与 ActionIntent

高风险本地操作先生成结构化 `ActionIntent`，其中包含所有者、受控 payload、配置版本和完整性绑定；系统返回 `approval_id` 后，只有同一 QQ 用户在有效期内确认才会执行。任何所有者、payload、哈希、版本、状态或过期时间不匹配都会 fail closed。模型不能直接执行宿主机命令，也不能绕过本地审批仓库。

管理台 `http://127.0.0.1:18790` 的“编排”页可以查看当前模式、配置版本、最近运行和安全运维状态，并且只允许切换 `legacy_direct` 与 `direct`。

### 加密备份

新备份使用 AES-256-GCM，扩展名为 `.chichu-backup`。32 字节主密钥保存在 macOS Keychain 的 service/account：

```text
qq-openclaw-backup / backup-master-v1
```

管理台不会显示或导出这个密钥。旧 ZIP 会标记为 `legacy-unencrypted`；它不会被伪装成已加密备份。媒体缓存、原始 API Key 和临时文件不进入备份。

### 安装与卸载 LaunchAgent

先生成三份权限为 `0600` 的 plist，再通过兼容服务命令 bootstrap：

```bash
.venv/bin/python scripts/install_launch_agents.py --project-dir "$PWD" --home-dir "$HOME"
scripts/dashboard_service.sh start
scripts/watchdog_service.sh start
scripts/bridge_service.sh start
```

停止并卸载运行中的 job：

```bash
scripts/bridge_service.sh stop
scripts/watchdog_service.sh stop
scripts/dashboard_service.sh stop
rm "$HOME/Library/LaunchAgents/com.chichu.qq-bridge.plist"
rm "$HOME/Library/LaunchAgents/com.chichu.qq-watchdog.plist"
rm "$HOME/Library/LaunchAgents/com.chichu.qq-dashboard.plist"
```

Bridge、Dashboard 和 Watchdog 的进程所有权只属于当前用户的 LaunchAgent。服务脚本不再使用 `nohup`、PID 文件或 `kill -9`。

### 部署顺序

1. 保持 `ORCHESTRATION_MODE=legacy_direct`，保持 `ENABLE_MEDIA_PIPELINE=false` 和 `ENABLE_LONG_TASKS=false`。
2. 安装依赖和 LaunchAgent，运行普通 `scripts/check_ready.sh`。
3. 确认 OneBot、Gateway、SQLite、Outbox、管理台和 watchdog 健康。
4. 用白名单 QQ 发送一条普通文本和一条 `你是谁`，记录回复与模型调用数。
5. 在管理台切到 `direct`，执行 `scripts/bridge_service.sh restart`，重复两条消息并比较证据。
6. 只有验收通过后才保持 `direct`；F1 和 E4 仍维持关闭。

回滚时，在管理台把模式改回 `legacy_direct`，然后执行：

```bash
scripts/bridge_service.sh restart
scripts/check_ready.sh
```

目标是在 60 秒内恢复一条 QQ 文本回复。数据库使用同一 schema v5，不需要降级或删除历史追踪。

运行完整开发验收：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

结构化日志位于 `logs/bridge.log`、`logs/dashboard.log` 和 `logs/watchdog.log`，单文件上限 10 MiB，保留 5 代，文件权限为 `0600`。

## 项目结构

- `src/qq_openclaw_bridge/config.py`：环境变量配置。
- `src/qq_openclaw_bridge/onebot.py`：OneBot 消息解析和发送动作构造。
- `src/qq_openclaw_bridge/filters.py`：私聊、文本、白名单和限流判定。
- `src/qq_openclaw_bridge/agent.py`：echo、HTTP 与本地 OpenClaw CLI 适配器。
- `src/qq_openclaw_bridge/domain/`：长期记忆、待办、时间规则和管家领域服务。
- `src/qq_openclaw_bridge/orchestration/`：A++ 编排契约和单模型直接策略。
- `src/qq_openclaw_bridge/application/`：消息用例、调度、投递和后台服务。
- `src/qq_openclaw_bridge/persistence/`：SQLite schema v5 与分职责 Repository。
- `src/qq_openclaw_bridge/runtime/`：依赖装配与 WebSocket 监督器。
- `src/qq_openclaw_bridge/housekeeper.py`：保持旧 API 的管家兼容入口。
- `src/qq_openclaw_bridge/reminders.py`：到点提醒扫描、重复提醒推进、免打扰、每日早报和晚间复盘。
- `src/qq_openclaw_bridge/style_memory.py`：风格反馈记录。
- `src/qq_openclaw_bridge/backups.py`：D2 自动备份、完整性校验、保留策略和安全恢复。
- `src/qq_openclaw_bridge/storage.py`：保持旧 API 的 Repository 兼容外观。
- `src/qq_openclaw_bridge/bridge.py`：保持旧 API 的消息桥接兼容外观。
- `scripts/start_bridge.sh`：前台调试桥接服务。
- `scripts/check_ready.sh`：启动前检查配置、虚拟环境、OpenClaw 和代理变量。
- `scripts/bridge_service.sh`：通过 LaunchAgent 启动、停止和查看桥接服务状态。
- `scripts/show_logs.sh`：查看或打开最近运行日志。
- `scripts/dashboard_service.sh`：启动、停止和检查本地管理面板。
- `scripts/watchdog_service.sh`：启动、停止和检查 D3 独立自愈监控。
- `apps/启动QQ智能体.app`：双击后台启动桥接服务。
- `apps/停止QQ智能体.app`：双击停止后台桥接服务。
- `apps/查看QQ智能体日志.app`：双击打开运行日志。
- `apps/打开QQ智能体面板.app`：双击启动并打开本地管理台。
- `src/qq_openclaw_dashboard/`：面板控制器、HTTP 服务和静态界面。
- `src/qq_openclaw_watchdog/`：健康检查、受限重启、状态快照和 macOS 异常通知。
- `tests/`：自动化测试。
