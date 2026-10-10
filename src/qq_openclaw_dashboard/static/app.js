const token = document.querySelector('meta[name="dashboard-token"]').content;
let state = null;
let todoFilter = 'active';
let toastTimer = null;
let pendingConfirm = null;
let rescheduleTodoId = null;
let mediaFormDirty = false;

const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));

async function api(path, payload = {}) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Dashboard-Token': token},
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || '操作失败');
  return result;
}

async function loadState(showNotice = false) {
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '状态读取失败');
    state = result;
    render();
    if (showNotice) toast('状态已刷新');
  } catch (error) {
    toast(error.message, true);
  }
}

function render() {
  renderStatus();
  renderOverview();
  renderMemories();
  renderTodos();
  renderStyles();
  renderTasks();
  renderModels();
  renderApprovals();
  renderHealthTimeline();
  renderOrchestrationView(state.orchestration || {}, {
    escapeHtml, statusBadge, formatDateTime, elapsedTime, emptyTableRow,
  });
  renderRoutingView(state.routing || {}, {
    api, loadState, toast, formatDateTime,
  });
  renderMedia();
  renderLogs();
  renderBackups();
  byId('memoryCount').textContent = state.memory2?.stats?.confirmed ?? state.memories.length;
  byId('todoCount').textContent = state.todos.filter((item) => !item.done).length;
  byId('styleCount').textContent = state.styles.length;
  byId('taskCount').textContent = (state.tasks || []).filter((item) => ['pending', 'running'].includes(item.status)).length;
  byId('approvalCount').textContent = (state.approvals || []).filter((item) => ['pending', 'later'].includes(item.status)).length;
  byId('updatedAt').textContent = `更新于 ${formatDateTime(state.updated_at)}`;
}

function setStatus(dotId, textId, ok, label) {
  byId(dotId).className = `status-dot ${ok ? 'ok' : 'bad'}`;
  byId(textId).textContent = label;
}

function renderStatus() {
  const running = state.service.running;
  const healthLabels = {healthy:'健康', degraded:'异常', recovering:'恢复中', blocked:'已停止重启', stopped:'未运行', unknown:'等待首次检查'};
  const watchdogOk = state.service.watchdog_running && state.health.status === 'healthy';
  setStatus('bridgeDot', 'bridgeStatus', running, running ? `运行中 · PID ${state.service.pid}` : '已停止');
  setStatus('onebotDot', 'onebotStatus', state.connections.onebot, state.connections.onebot ? '已连接' : '未连接');
  setStatus('openclawDot', 'openclawStatus', state.connections.openclaw, state.connections.openclaw ? '已配置' : '未配置');
  setStatus('securityDot', 'securityStatus', state.config.security_confirmation, state.config.security_confirmation ? '已启用' : '已关闭');
  setStatus('watchdogDot', 'watchdogStatus', watchdogOk, healthLabels[state.health.status] || state.health.status);
}

function renderOverview() {
  const running = state.service.running;
  byId('serviceBadge').className = `badge ${running ? 'ok' : 'bad'}`;
  byId('serviceBadge').textContent = running ? '运行中' : '已停止';
  const healthTime = state.health.updated_at ? `，最近检查 ${formatDateTime(state.health.updated_at)}` : '';
  const healthDetail = state.service.watchdog_running
    ? `自愈监控 PID ${state.service.watchdog_pid}：${state.health.message}${healthTime}，当前窗口重启 ${state.health.restart_count} 次。`
    : '自愈监控当前未运行。';
  byId('serviceDetail').textContent = `${running ? `桥接进程 PID ${state.service.pid}，可以正常接收 QQ 私聊。` : '桥接服务当前未运行，面板仍可用。'} ${healthDetail}`;
  byId('metricMemories').textContent = state.memory2?.stats?.confirmed ?? state.memories.length;
  byId('metricTodos').textContent = state.todos.filter((item) => !item.done).length;
  byId('metricStyles').textContent = state.styles.length;
  byId('configAdapter').textContent = state.config.adapter;
  byId('configAgent').textContent = state.config.agent_id;
  byId('configModel').textContent = state.config.model;
  byId('configLightModel').textContent = state.config.memory_light_model;
  byId('configGateway').textContent = state.config.gateway_mode ? `Gateway · ${state.config.gateway_url}` : '本地嵌入模式';
  byId('configOnebot').textContent = state.config.onebot_url;
  byId('configProxy').textContent = state.config.proxy_configured ? '已配置' : '未配置';
  byId('configReminder').textContent = state.service.reminders_paused ? '已暂停' : '正常';
  byId('configBackup').textContent = state.config.auto_backup ? `每 ${state.config.auto_backup_interval_hours} 小时` : '已关闭';
  byId('configWatchdog').textContent = state.config.watchdog ? (state.service.watchdog_running ? '运行中' : '已启用，未运行') : '已关闭';
  byId('configStorage').textContent = state.config.sqlite_store ? 'SQLite WAL' : 'Markdown 兼容模式';
  byId('configQueue').textContent = `待发送 ${state.transport.outbox_pending} · 发送中 ${state.transport.outbox_sending} · 失败 ${state.transport.outbox_failed} · 处理中 ${state.transport.active_inbound}`;
  byId('configAgentSuccess').textContent = state.transport.last_agent_success_at ? formatDateTime(state.transport.last_agent_success_at) : '暂无';
  byId('configDeliverySuccess').textContent = state.transport.last_delivery_success_at ? formatDateTime(state.transport.last_delivery_success_at) : '暂无';
}

function renderMemories() {
  const memory2 = state.memory2 || {enabled:false, light_model:'未配置', stats:{turns:0,summaries:0,pending:0,confirmed:state.memories.length,rejected:0}, pending:[], recent_summaries:[], latest_analysis:null};
  const latest = memory2.latest_analysis;
  const analysisLabel = latest ? `${latest.status} · ${formatDateTime(latest.updated_at)}` : '尚未达到摘要阈值';
  byId('memory2Status').textContent = `${memory2.enabled ? 'E3 已启用' : 'E3 已关闭'} · 轻量模型 ${memory2.light_model} · 已确认 ${memory2.stats.confirmed} · 待确认 ${memory2.stats.pending} · 对话 ${memory2.stats.turns} 轮 · 摘要 ${memory2.stats.summaries} 条 · 最近分析 ${analysisLabel}`;

  const pendingRoot = byId('pendingMemoryList');
  pendingRoot.innerHTML = memory2.pending.length ? memory2.pending.map((item) => `
    <article class="list-item">
      <div><p class="item-title">${escapeHtml(item.content)}</p><div class="item-meta"><span>#${item.id}</span><span>${escapeHtml(item.layer)}</span><span>${escapeHtml(item.category)}</span><span>置信度 ${Math.round(item.confidence * 100)}%</span></div></div>
      <div class="item-actions"><button class="small-button" data-memory-confirm="${item.id}" type="button">确认</button><button class="small-button danger" data-memory-reject="${item.id}" type="button">拒绝</button></div>
    </article>`).join('') : '<div class="empty-state compact">没有待确认事实。未经你拍板的内容不会进入长期记忆。</div>';

  const root = byId('memoryList');
  if (!state.memories.length) {
    root.innerHTML = '<div class="empty-state">还没有长期记忆。账本空着，倒也省心。</div>';
  } else {
    root.innerHTML = state.memories.map((item) => `
      <article class="list-item">
        <div><p class="item-title">${escapeHtml(item.text)}</p><div class="item-meta"><span>#${item.index}</span><span>${escapeHtml(item.date)}</span></div></div>
        <div class="item-actions"><button class="small-button danger" data-memory-delete="${item.index}" type="button">删除</button></div>
      </article>`).join('');
  }

  const summaryRoot = byId('memorySummaryList');
  summaryRoot.innerHTML = memory2.recent_summaries.length ? memory2.recent_summaries.map((item) => `
    <article class="list-item">
      <div><p class="item-title">${escapeHtml(item.content)}</p><div class="item-meta"><span>${escapeHtml(item.model)}</span><span>${formatDateTime(item.created_at)}</span></div></div>
    </article>`).join('') : '<div class="empty-state compact">还没有对话摘要。达到轮次阈值后会在后台生成。</div>';
}

function renderTodos() {
  const root = byId('todoList');
  const items = state.todos.filter((item) => todoFilter === 'all' || (todoFilter === 'done' ? item.done : !item.done));
  if (!items.length) {
    root.innerHTML = '<div class="empty-state">这个筛选下没有待办。难得干净，别急着填满。</div>';
    return;
  }
  root.innerHTML = items.map((item) => {
    const due = item.due ? formatDateTime(item.due) : '未设提醒';
    const repeat = item.repeat ? ` · ${repeatLabel(item.repeat)}` : '';
    const category = item.category ? `<span>${escapeHtml(item.category)}</span>` : '';
    const actions = item.done
      ? `<button class="small-button danger" data-todo-delete="${item.id}" type="button">删除</button>`
      : `<button class="small-button" data-todo-complete="${item.id}" type="button">完成</button>
         <button class="small-button" data-todo-reschedule="${item.id}" type="button">改期</button>
         ${item.due ? `<button class="small-button" data-todo-cancel="${item.id}" type="button">取消提醒</button>` : ''}
         <button class="small-button danger" data-todo-delete="${item.id}" type="button">删除</button>`;
    return `<article class="list-item ${item.done ? 'done' : ''}">
      <div><p class="item-title">${escapeHtml(item.text)}</p><div class="item-meta"><span>#${item.id}</span>${category}<span>${escapeHtml(due + repeat)}</span></div></div>
      <div class="item-actions">${actions}</div>
    </article>`;
  }).join('');
}

function renderStyles() {
  const root = byId('styleList');
  if (!state.styles.length) {
    root.innerHTML = '<div class="empty-state">还没有风格反馈沉淀。chichu 暂时只能靠自觉。</div>';
    return;
  }
  root.innerHTML = state.styles.map((item) => `
    <article class="list-item">
      <div><p class="item-title">${escapeHtml(item.text)}</p><div class="item-meta"><span>#${item.index}</span></div></div>
      <div class="item-actions"><button class="small-button danger" data-style-delete="${item.index}" type="button">删除</button></div>
    </article>`).join('');
}

function renderTasks() {
  const root = byId('taskTableBody');
  const tasks = state.tasks || [];
  if (!tasks.length) {
    root.innerHTML = emptyTableRow(6, '目前没有长任务。');
    return;
  }
  root.innerHTML = tasks.map((item) => {
    const active = ['pending', 'running'].includes(item.status);
    const cancel = active
      ? `<button class="small-button danger" data-task-cancel="${escapeHtml(item.id)}" type="button" ${item.cancel_requested ? 'disabled' : ''}>${item.cancel_requested ? '取消中' : '取消'}</button>`
      : '-';
    return `<tr>
      <td class="mono-cell" title="${escapeHtml(item.id)}">${escapeHtml(shortId(item.id))}</td>
      <td>${escapeHtml(item.kind || '-')}</td>
      <td>${statusBadge(item.status)}</td>
      <td>${escapeHtml(item.stage || '-')}</td>
      <td>${escapeHtml(elapsedTime(item))}</td>
      <td>${cancel}</td>
    </tr>`;
  }).join('');
}

function renderModels() {
  const root = byId('modelTableBody');
  const models = state.model_stats || [];
  if (!models.length) {
    root.innerHTML = emptyTableRow(8, '最近 24 小时没有模型调用记录。');
    return;
  }
  root.innerHTML = models.map((item) => {
    const calls = metric(item.calls);
    const successes = metric(item.successes);
    const failures = calls === null || successes === null ? '-' : Math.max(0, calls - successes);
    const inputTokens = metric(item.input_tokens);
    const outputTokens = metric(item.output_tokens);
    const tokens = inputTokens === null || outputTokens === null ? '-' : formatNumber(inputTokens + outputTokens);
    return `<tr>
      <td><strong>${escapeHtml(item.model || 'unknown')}</strong><span class="table-subtext">${escapeHtml(item.provider || 'unknown')}</span></td>
      <td>${escapeHtml(item.call_kind || '-')}</td>
      <td>${calls === null ? '-' : formatNumber(calls)}</td>
      <td>${failures}</td>
      <td>${tokens}</td>
      <td>${latency(item.p50_latency_ms)}</td>
      <td>${latency(item.p95_latency_ms)}</td>
      <td>${money(item.today_estimated_cost)} / ${item.daily_budget === null ? '未设' : money(item.daily_budget)}</td>
    </tr>`;
  }).join('');
}

function renderApprovals() {
  const root = byId('approvalTableBody');
  const approvals = state.approvals || [];
  if (!approvals.length) {
    root.innerHTML = emptyTableRow(5, '没有近期审批记录。');
    return;
  }
  root.innerHTML = approvals.map((item) => `<tr>
    <td class="mono-cell">${escapeHtml(shortId(item.approval_id))}</td>
    <td>${escapeHtml(item.description || '待确认的敏感操作')}</td>
    <td>${statusBadge(item.status)}</td>
    <td>${formatDateTime(item.created_at)}</td>
    <td>${formatDateTime(item.expires_at)}</td>
  </tr>`).join('');
}

function renderHealthTimeline() {
  const root = byId('healthTableBody');
  const events = state.health_timeline || [];
  if (!events.length) {
    root.innerHTML = emptyTableRow(3, '还没有组件健康变化记录。');
    return;
  }
  root.innerHTML = events.map((item) => `<tr>
    <td>${formatDateTime(item.created_at)}</td>
    <td>${escapeHtml(item.component || 'unknown')}</td>
    <td>${statusBadge(item.status)}</td>
  </tr>`).join('');
}

function renderMedia() {
  const media = state.media || {total: 0, settings: {}};
  const settings = media.settings || {};
  const counts = ['pending', 'processing', 'ready', 'failed'].map((name) => `${statusText(name)} ${media[name] || 0}`).join(' · ');
  byId('mediaStatus').textContent = `媒体记录 ${media.total || 0} · ${counts}`;
  byId('mediaSecretStatus').textContent = settings.secret_configured ? '密钥已配置，页面不会读取或显示原值。' : '尚未配置媒体 API 密钥。';
  if (mediaFormDirty) return;
  byId('mediaEnabledInput').checked = settings.enabled === true;
  byId('mediaBaseUrlInput').value = settings.base_url || '';
  byId('mediaImageModelInput').value = settings.image_model || '';
  byId('mediaAudioModelInput').value = settings.audio_model || '';
  byId('mediaTimeoutInput').value = settings.timeout_seconds ?? 90;
  byId('mediaRateInput').value = settings.requests_per_minute ?? 10;
  byId('mediaBudgetInput').value = settings.daily_budget ?? '';
  byId('mediaConcurrencyInput').value = settings.max_concurrent_calls ?? 2;
}

function renderLogs() {
  byId('logOutput').textContent = state.logs || '还没有运行日志。';
}

function renderBackups() {
  const root = byId('backupList');
  byId('backupPolicy').textContent = state.config.auto_backup
    ? `自动备份每 ${state.config.auto_backup_interval_hours} 小时执行一次，保留 ${state.config.auto_backup_retention_days} 天且最多 ${state.config.auto_backup_max_files} 份；手动和恢复前备份不自动删除。`
    : '自动备份已关闭；仍可手动创建、校验和恢复。';
  if (!state.backups.length) {
    root.innerHTML = '<div class="empty-state">还没有备份。现在点一次创建，给未来的手滑留条后路。</div>';
    return;
  }
  root.innerHTML = state.backups.map((item) => `
    <article class="list-item">
      <div><p class="item-title">${escapeHtml(item.name)}</p><div class="item-meta"><span>${backupKind(item.kind)}</span><span>${item.valid ? '校验正常' : '校验失败'}</span><span>${formatDateTime(item.created_at || item.mtime)}</span><span>${formatBytes(item.size)}</span><span>${item.file_count} 个文件</span></div></div>
      <div class="item-actions"><button class="small-button" data-backup-verify="${escapeHtml(item.name)}" type="button">校验</button>${item.valid ? `<button class="small-button danger" data-backup-restore="${escapeHtml(item.name)}" type="button">恢复</button>` : ''}</div>
    </article>`).join('');
}

function switchView(name) {
  document.querySelectorAll('.nav-button').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
  document.querySelectorAll('.view').forEach((view) => view.classList.toggle('active', view.id === `view-${name}`));
}

async function mutate(path, payload, successRefresh = true) {
  const result = await api(path, payload);
  toast(result.message || '操作完成');
  if (successRefresh) await loadState();
}

function confirmAction(title, message, action) {
  pendingConfirm = action;
  byId('confirmTitle').textContent = title;
  byId('confirmMessage').textContent = message;
  byId('confirmDialog').showModal();
}

function toast(message, isError = false) {
  const node = byId('toast');
  node.textContent = message;
  node.className = `toast show${isError ? ' error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = 'toast'; }, 3200);
}

function formatDateTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ');
  return new Intl.DateTimeFormat('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false}).format(date);
}

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function emptyTableRow(columns, message) { return `<tr><td class="table-empty" colspan="${columns}">${escapeHtml(message)}</td></tr>`; }
function shortId(value) { const text = String(value || '-'); return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text; }
function metric(value) { return typeof value === 'number' && Number.isFinite(value) ? value : null; }
function formatNumber(value) { return new Intl.NumberFormat('zh-CN').format(value); }
function latency(value) { return metric(value) === null ? '-' : `${Math.round(value)} ms`; }
function money(value) { return metric(value) === null ? '-' : `$${Number(value).toFixed(4)}`; }
function elapsedTime(item) {
  const start = new Date(item.started_at || item.created_at || '');
  const end = new Date(item.finished_at || Date.now());
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '-';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `${Math.floor(seconds / 3600)} 时 ${Math.floor((seconds % 3600) / 60)} 分`;
}
function statusText(value) {
  return ({pending:'等待', received:'已接收', planned:'已规划', executing:'执行中', verifying:'验收中', accepted:'已通过', delivered:'已送达', fallback:'已回退', running:'执行中', processing:'处理中', ready:'完成', completed:'完成', succeeded:'成功', healthy:'健康', approved:'已批准', rejected:'已拒绝', expired:'已过期', later:'待处理', failed:'失败', cancelled:'已取消', cancel_requested:'取消中', degraded:'异常', recovering:'恢复中', blocked:'阻断', stopped:'停止', unknown:'未知'})[value] || value || '未知';
}
function statusBadge(value) {
  const good = ['ready', 'completed', 'succeeded', 'healthy', 'approved', 'accepted', 'delivered'].includes(value);
  const bad = ['failed', 'rejected', 'expired', 'degraded', 'blocked', 'fallback', 'cancelled'].includes(value);
  const tone = good ? 'ok' : (bad ? 'bad' : 'neutral');
  return `<span class="badge ${tone}">${escapeHtml(statusText(value))}</span>`;
}

function repeatLabel(value) { return ({daily:'每天', weekly:'每周', monthly:'每月'})[value] || value; }
function backupKind(value) { return ({auto:'自动备份', manual:'手动备份', 'pre-restore':'恢复前备份'})[value] || '未知类型'; }

document.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  try {
    if (button.dataset.view) switchView(button.dataset.view);
    if (button.dataset.go) switchView(button.dataset.go);
    if (button.dataset.service) {
      const action = button.dataset.service;
      const run = () => mutate(`/api/service/${action}`, {});
      action === 'stop' ? confirmAction('停止智能体', '面板会继续运行，但 QQ 将暂时无法回复。确认停止吗？', run) : await run();
    }
    if (button.dataset.memoryDelete) confirmAction('删除长期记忆', `确认删除第 ${button.dataset.memoryDelete} 条记忆？`, () => mutate('/api/memories/delete', {index: Number(button.dataset.memoryDelete)}));
    if (button.dataset.memoryConfirm) await mutate('/api/memory-candidates/confirm', {id: Number(button.dataset.memoryConfirm)});
    if (button.dataset.memoryReject) confirmAction('拒绝候选记忆', `拒绝候选记忆 #${button.dataset.memoryReject}？它不会进入主模型上下文。`, () => mutate('/api/memory-candidates/reject', {id: Number(button.dataset.memoryReject)}));
    if (button.dataset.todoComplete) await mutate('/api/todos/complete', {id: Number(button.dataset.todoComplete)});
    if (button.dataset.todoDelete) confirmAction('删除待办', `确认永久删除待办 #${button.dataset.todoDelete}？`, () => mutate('/api/todos/delete', {id: Number(button.dataset.todoDelete)}));
    if (button.dataset.todoCancel) await mutate('/api/todos/cancel-reminder', {id: Number(button.dataset.todoCancel)});
    if (button.dataset.todoReschedule) {
      rescheduleTodoId = Number(button.dataset.todoReschedule);
      byId('rescheduleInput').value = '';
      byId('rescheduleDialog').showModal();
    }
    if (button.dataset.styleDelete) confirmAction('删除风格反馈', `确认删除第 ${button.dataset.styleDelete} 条风格反馈？`, () => mutate('/api/styles/delete', {index: Number(button.dataset.styleDelete)}));
    if (button.dataset.backupRestore) confirmAction('恢复备份', `将用 ${button.dataset.backupRestore} 覆盖当前受管文件。系统会先自动保存恢复前备份，仍建议确认文件名无误。`, () => mutate('/api/backups/restore', {name: button.dataset.backupRestore}));
    if (button.dataset.backupVerify) await mutate('/api/backups/verify', {name: button.dataset.backupVerify});
    if (button.dataset.taskCancel) confirmAction('取消长任务', `确认取消任务 ${button.dataset.taskCancel}？已完成的步骤不会回滚。`, () => mutate(`/api/tasks/${encodeURIComponent(button.dataset.taskCancel)}/cancel`, {}));
  } catch (error) { toast(error.message, true); }
});

document.querySelectorAll('[data-todo-filter]').forEach((button) => button.addEventListener('click', () => {
  todoFilter = button.dataset.todoFilter;
  document.querySelectorAll('[data-todo-filter]').forEach((item) => item.classList.toggle('active', item === button));
  renderTodos();
}));

byId('memoryForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await mutate('/api/memories', {text: byId('memoryInput').value}); byId('memoryInput').value = ''; } catch (error) { toast(error.message, true); }
});

byId('todoForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await mutate('/api/todos', {command: byId('todoInput').value}); byId('todoInput').value = ''; } catch (error) { toast(error.message, true); }
});

byId('clearMemories').addEventListener('click', () => confirmAction('清空长期记忆', '这会删除全部长期记忆，其他工作区内容不受影响。确认继续？', () => mutate('/api/memories/clear', {})));
byId('clearStyles').addEventListener('click', () => confirmAction('清空风格反馈', '这会清空用户反馈沉淀，但保留 chichu 基础人设。确认继续？', () => mutate('/api/styles/clear', {})));
byId('createBackup').addEventListener('click', async () => { try { await mutate('/api/backups', {}); } catch (error) { toast(error.message, true); } });
byId('pruneBackups').addEventListener('click', () => confirmAction('执行备份保留策略', '只会清理超出保留天数或数量上限的自动备份，手动与恢复前备份会保留。确认继续？', () => mutate('/api/backups/prune', {})));
byId('refreshButton').addEventListener('click', () => loadState(true));
byId('refreshLogs').addEventListener('click', () => loadState(true));

byId('mediaSettingsForm').addEventListener('input', () => { mediaFormDirty = true; });
byId('orchestrationModeForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const result = await api('/api/orchestration/mode', {mode: byId('orchestrationModeInput').value});
    toast(result.message || '编排模式已保存，重启后生效。');
    await loadState();
  } catch (error) { toast(error.message, true); }
});

byId('mediaSettingsForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const budget = byId('mediaBudgetInput').value.trim();
  const payload = {
    enabled: byId('mediaEnabledInput').checked,
    base_url: byId('mediaBaseUrlInput').value.trim(),
    image_model: byId('mediaImageModelInput').value.trim(),
    audio_model: byId('mediaAudioModelInput').value.trim(),
    timeout_seconds: Number(byId('mediaTimeoutInput').value),
    requests_per_minute: Number(byId('mediaRateInput').value),
    daily_budget: budget || '0',
    max_concurrent_calls: Number(byId('mediaConcurrencyInput').value),
  };
  try {
    await api('/api/media/settings', payload);
    mediaFormDirty = false;
    toast('媒体设置已保存，重启智能体后生效。');
    await loadState();
  } catch (error) { toast(error.message, true); }
});

byId('mediaSecretForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const mediaSecretInput = byId('mediaSecretInput');
  try {
    const result = await api('/api/media/secret', {key: mediaSecretInput.value});
    mediaSecretInput.value = '';
    toast(result.message || '媒体 API 密钥已更新。');
    await loadState();
  } catch (error) { toast(error.message, true); }
});

byId('confirmDialog').addEventListener('close', async () => {
  if (byId('confirmDialog').returnValue === 'confirm' && pendingConfirm) {
    const action = pendingConfirm;
    pendingConfirm = null;
    try { await action(); } catch (error) { toast(error.message, true); }
  } else pendingConfirm = null;
});

byId('rescheduleForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const due = byId('rescheduleInput').value.trim();
  if (!due) return;
  byId('rescheduleDialog').close();
  try { await mutate('/api/todos/reschedule', {id: rescheduleTodoId, due}); } catch (error) { toast(error.message, true); }
});

loadState();
setInterval(() => loadState(), 10000);
