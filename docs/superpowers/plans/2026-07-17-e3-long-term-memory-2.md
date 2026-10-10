# E3 长期记忆 2.0 实施计划

## 任务 1：schema v3 与结构化仓库

- 先写迁移、事实状态、轮次、摘要和统计测试。
- 新增 v3 表和 `MemoryRepository`。
- 给 `WorkspaceStateStore` 增加兼容门面，不触碰 v2 公开方法。

## 任务 2：混合检索与分层上下文

- 先写中文关键词、特征哈希向量、余弦排序和预算裁剪测试。
- 新增纯领域检索模块。
- 新增上下文组装服务，通过 `AgentRequest.context` 交给适配器。

## 任务 3：确认式长期记忆

- 先写 pending / confirm / reject / explicit-confirmed 流程测试。
- 新增 E3 记忆命令处理器。
- 让现有 `记住：...` 与结构化事实双写，Markdown 回复保持兼容。

## 任务 4：自动摘要与轻量模型

- 先写批次阈值、后台隔离、JSON 容错和本地降级测试。
- 新增轻量分析器与异步观察器。
- 使用独立 OpenClaw session 和 `MEMORY_LIGHT_MODEL`，不占用主对话 session。

## 任务 5：人格压缩与风格回归

- 先写规则去重、字符预算、身份锚点和禁止项测试。
- 新增人格上下文编译器与幂等安装脚本。
- 部署前备份工作区，再压缩 `AGENTS.md`、`SOUL.md`，保留 `STYLE.md` 反馈。

## 任务 6：面板、文档与可观测

- 扩展面板 state API 和确认/拒绝 mutation API。
- 显示分层统计、待确认事实、最近摘要和轻量模型。
- 更新 `.env.example`、README 和 watchdog 指标说明。

## 任务 7：验证与部署

- 运行 E3 定向测试和全量测试。
- 检查数据库 quick check、schema、文件权限和备份。
- 同步到运行目录，重启 bridge / watchdog / dashboard。
- 用真实 Gateway 做一轮上下文、回复和持久化验收。

