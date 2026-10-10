# Task 1 实施报告

## 状态

实现完成，等待规格审查与代码质量审查。

## TDD 证据

### RED

命令：

```bash
.venv/bin/python -m unittest tests.test_persistence_e4 -v
```

结果：6 项测试均按预期失败，直接原因是
`ModuleNotFoundError: No module named 'qq_openclaw_bridge.persistence.e4'`。

### GREEN

命令：

```bash
.venv/bin/python -m unittest tests.test_persistence_e4 tests.test_persistence_memory tests.test_storage -v
```

结果：19 项测试全部通过。

## 变更

- SQLite schema 从 v3 升级到 v4，仅新增 `media_assets`、`long_tasks`、
  `task_events`、`model_calls`、`health_events` 及索引。
- 新增 `E4Repository`，提供媒体、长任务、事件、模型调用和健康事件的
  写入与查询接口。
- 任务状态转换使用带期望状态的原子更新，转换和事件追加位于同一事务。
- 终态不可逆；取消请求限制为任务所有者和活动任务。
- 健康事件只在状态变化时记录。
- `WorkspaceStateStore.e4_repository` 暴露独立 E4 Repository。
- 将两个旧 schema v3 测试预期更新为 v4；该范围扩展已获用户确认。

## 风险与后续

- Task 1 只提供持久化底座，媒体下载、长任务调度和面板接线由后续任务完成。
- `model_stats` 当前提供聚合计数、Token、平均延迟和估算成本；P50/P95 延迟
  将由后续可观测性任务按面板需求补充。

## Fix Review

### RED

命令：

```bash
.venv/bin/python -m unittest tests.test_persistence_e4 -v
```

结果：新增 11 项 E4 测试中 4 项按预期失败：

- `append_task_event` 接受不存在的任务并写入孤儿事件。
- 重复 `request_cancel` 仍返回任务并重复追加取消事件。
- `health_timeline` 以插入 ID 而非 `created_at DESC, id DESC` 排序。
- v3 升级后缺少显式 `(component, created_at)` 健康事件索引。

跨独立 `E4Repository` 实例的 stale expected CAS、事件插入触发器失败时的
状态回滚，以及 `WorkspaceStateStore.e4_repository` 属性在修复前已通过；保留
为回归覆盖。

### GREEN

命令：

```bash
.venv/bin/python -m unittest tests.test_persistence_e4 tests.test_persistence_memory tests.test_storage -v
PYTHONPYCACHEPREFIX=/tmp/qq-openclaw-pycache .venv/bin/python -m py_compile src/qq_openclaw_bridge/persistence/database.py src/qq_openclaw_bridge/persistence/e4.py tests/test_persistence_e4.py
```

结果：24 项指定单元测试全部通过；`py_compile` 通过。

### Fixes And Concerns

- 健康事件迁移新增
  `health_events_component_created_at(component, created_at)`；时间线按
  `created_at DESC, id DESC` 返回。迁移不删除任何已有表、数据或索引。
- 取消更新限制为尚未请求取消的活动任务，重复调用不写事件。
- 任务事件追加在同一 `BEGIN IMMEDIATE` 事务中确认任务存在，拒绝孤儿事件。
- 未修改 `tests/test_storage.py` 或 `tests/test_persistence_memory.py` 的行为；
  它们仅保留已有的 schema v4 断言。

## Second Review Fix

### RED

命令：

```bash
PYTHONPYCACHEPREFIX=/tmp/qq-openclaw-pycache .venv/bin/python -m unittest tests.test_persistence_e4 -v
```

结果：11 项测试中 2 项按预期失败：

- v3 升级测试未找到 `media_assets_expires_at`，其首列应为 `expires_at`。
- `record_health_change` 将早于当前最新事件的 `unhealthy` 状态写入，未返回
  `None`。

### GREEN

命令：

```bash
PYTHONPYCACHEPREFIX=/tmp/qq-openclaw-pycache .venv/bin/python -m unittest tests.test_persistence_e4 -v
```

结果：11 项 E4 持久化测试全部通过。

- Schema 新增 `media_assets_expires_at(expires_at)` 和
  `model_calls_created_at(created_at)`；真实 v3 升级测试检查了两个索引的列顺序。
- `record_health_change` 现在按 `created_at DESC, id DESC` 获取当前事件；严格早于
  该事件的输入不写入并返回 `None`。`health_timeline` 继续按同一顺序返回。

## Final Quality Fix

### RED

命令：

```bash
PYTHONPYCACHEPREFIX=/tmp/qq-openclaw-pycache .venv/bin/python -m unittest tests.test_persistence_e4 -v
```

结果：新增混合已知/未知模型调用测试按预期失败。相同聚合组的一条完整调用和
一条四项指标均为 `NULL` 的调用，旧 `SUM/AVG` 仍返回已知行的 `input_tokens=100`，
证明 SQLite 聚合静默忽略未知值并产生伪精确统计。

### GREEN

命令：

```bash
PYTHONPYCACHEPREFIX=/tmp/qq-openclaw-pycache .venv/bin/python -m unittest tests.test_persistence_e4 -v
```

结果：12 项 E4 持久化测试全部通过。

- `model_stats` 对 `input_tokens`、`output_tokens`、`average_duration_ms` 和
  `estimated_cost` 使用完整性门控：仅当聚合组内所有调用都具有该值时才返回
  总值或平均值；任一 `NULL` 会使对应结果为 `None`。
- 结果新增 `known_input_token_calls`、`known_output_token_calls`、
  `known_duration_calls` 和 `known_cost_calls`，分别使用 `COUNT(column)` 返回
  已知指标调用数，供面板显示覆盖率。
