(function () {
  'use strict';

  const opsLabels = {
    configured: '已配置',
    pending: '待实现',
  };

  window.renderOrchestrationView = function renderOrchestrationView(orchestration, helpers) {
    const {escapeHtml, statusBadge, formatDateTime, elapsedTime, emptyTableRow} = helpers;
    const recent_runs = Array.isArray(orchestration.recent_runs) ? orchestration.recent_runs : [];
    const ops = orchestration.ops_status || {};
    document.getElementById('orchestrationModeInput').value = orchestration.mode === 'direct' ? 'direct' : 'legacy_direct';
    document.getElementById('orchestrationVersion').textContent = orchestration.config_version || '-';
    document.getElementById('orchestrationFallbacks').textContent = orchestration.fallback_count || 0;
    document.getElementById('opsLaunchAgents').textContent = opsLabels[ops.launch_agents] || '未知';
    document.getElementById('opsLogging').textContent = opsLabels[ops.logging] || '未知';
    document.getElementById('opsBackupEncryption').textContent = opsLabels[ops.backup_encryption] || '未知';

    const body = document.getElementById('orchestrationRunTableBody');
    if (!recent_runs.length) {
      body.innerHTML = emptyTableRow(8, '还没有编排运行记录。');
      return;
    }
    body.innerHTML = recent_runs.map((item) => {
      const detail = item.fallback_reason || item.safe_error_class || '-';
      return `<tr>
        <td class="mono-cell" title="${escapeHtml(item.run_id)}">${escapeHtml(item.run_id)}</td>
        <td>${escapeHtml(item.mode || '-')}</td>
        <td>${escapeHtml(item.risk_level || '-')}</td>
        <td>${statusBadge(item.status)}</td>
        <td>${escapeHtml(item.config_version || '-')}</td>
        <td>${formatDateTime(item.started_at)}</td>
        <td>${escapeHtml(elapsedTime(item))}</td>
        <td>${escapeHtml(detail)}</td>
      </tr>`;
    }).join('');
  };
}());
