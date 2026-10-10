(function () {
  'use strict';

  const LEVELS = ['L0', 'L1', 'L2', 'L3'];
  const gateLabels = {
    seven_days: '运行满 7 天',
    hundred_decisions: '决策满 100 条',
    thirty_labels: '人工标注满 30 条',
    ninety_percent_accuracy: '准确率至少 90%',
    no_forced_risk_misses: '无强制风险漏检',
    dispatch_overhead_p95: '分派开销 P95 合格',
    eligible: '满足晋升门槛',
  };
  let currentHelpers = null;
  let controlsBound = false;
  let routingConfigDirty = false;

  const byId = (id) => document.getElementById(id);
  const displayValue = (value) => value === null || value === undefined || value === '' ? '-' : String(value);
  const isFiniteNumber = (value) => typeof value === 'number' && Number.isFinite(value);

  function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function appendCell(row, text, className) {
    const cell = makeElement('td', className, text);
    row.append(cell);
    return cell;
  }

  function setMetric(id, value) {
    byId(id).textContent = displayValue(value);
  }

  function percentage(value) {
    return isFiniteNumber(value) ? `${(value * 100).toFixed(1)}%` : '-';
  }

  function milliseconds(value) {
    return isFiniteNumber(value) ? `${Math.round(value)} ms` : '-';
  }

  function renderGateList(gate) {
    const root = byId('routingGateList');
    const entries = Object.keys(gateLabels).map((key) => {
      const value = gate[key];
      const state = value === true ? '通过' : (value === false ? '未通过' : '-');
      const tone = value === true ? 'ok' : (value === false ? 'bad' : 'neutral');
      const item = makeElement('div', 'routing-gate');
      item.append(
        makeElement('span', tone, state),
        makeElement('span', 'routing-gate-label', gateLabels[key]),
      );
      return item;
    });
    root.replaceChildren(...entries);
  }

  function renderMetrics(metrics) {
    setMetric('routingDecisionCount', metrics.decision_count);
    setMetric('routingLabeledCount', metrics.labeled_count);
    setMetric('routingAccuracy', percentage(metrics.label_accuracy));
    setMetric('routingForcedRiskMisses', metrics.forced_risk_misses);
    setMetric('routingClassifierP95', milliseconds(metrics.classifier_duration_p95_ms));
    setMetric('routingOverheadP95', milliseconds(metrics.dispatch_overhead_p95_ms));
  }

  function renderConfig(routing) {
    const config = routing.config && typeof routing.config === 'object' ? routing.config : {};
    byId('routingConfigVersion').textContent = `当前版本 ${displayValue(routing.config_version)}`;
    if (routingConfigDirty) return;
    byId('routingEnabledInput').checked = config.classifier_enabled === true;
    byId('routingModelInput').value = displayValue(config.classifier_model) === '-' ? '' : config.classifier_model;
    byId('routingConfidenceInput').value = displayValue(config.confidence_threshold_percent) === '-' ? '' : config.confidence_threshold_percent;
    byId('routingTimeoutInput').value = displayValue(config.classifier_timeout_seconds) === '-' ? '' : config.classifier_timeout_seconds;
    byId('routingDailyCallsInput').value = displayValue(config.max_daily_calls) === '-' ? '' : config.max_daily_calls;
    byId('routingFailureThresholdInput').value = displayValue(config.failure_threshold) === '-' ? '' : config.failure_threshold;
    byId('routingCooldownInput').value = displayValue(config.cooldown_seconds) === '-' ? '' : config.cooldown_seconds;
    byId('routingRuleVersionInput').value = displayValue(config.rule_version) === '-' ? '' : config.rule_version;
  }

  function renderMatrix(metrics) {
    const body = byId('routingMatrixTableBody');
    const matrix = metrics.confusion_matrix && typeof metrics.confusion_matrix === 'object'
      ? metrics.confusion_matrix : {};
    const rows = LEVELS.map((predicted) => {
      const row = document.createElement('tr');
      appendCell(row, predicted);
      LEVELS.forEach((correct) => {
        const value = matrix[predicted]?.[correct];
        appendCell(row, isFiniteNumber(value) ? value : '-');
      });
      return row;
    });
    body.replaceChildren(...rows);
  }

  function appendSelectOption(select, value, label) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    select.append(option);
  }

  function labelControls(decision, routing) {
    const controls = makeElement('div', 'routing-label-controls');
    const verdict = document.createElement('select');
    appendSelectOption(verdict, 'correct', '正确');
    appendSelectOption(verdict, 'upgrade', '调高');
    appendSelectOption(verdict, 'downgrade', '调低');

    const level = document.createElement('select');
    LEVELS.forEach((item) => appendSelectOption(level, item, item));
    level.value = LEVELS.includes(decision.final_level) ? decision.final_level : 'L0';

    const note = document.createElement('input');
    note.type = 'text';
    note.maxLength = 300;
    note.placeholder = '备注（可选）';

    const button = makeElement('button', 'small-button', '提交标注');
    button.type = 'button';
    const isCurrentVersion = String(decision.routing_config_version) === String(routing.config_version);
    const isReady = ['classified', 'rule_only', 'error'].includes(decision.status);
    const alreadyLabeled = decision.labeled === true;
    button.disabled = !isCurrentVersion || !isReady || alreadyLabeled;
    if (!isCurrentVersion) button.title = '仅可标注当前配置版本的决策';
    if (alreadyLabeled) button.title = '该决策已标注';
    button.addEventListener('click', () => submitLabel(decision, verdict.value, level.value, note.value, button));
    controls.append(verdict, level, note, button);
    return controls;
  }

  function decisionDetail(decision) {
    const detail = makeElement('div');
    const preview = decision.message_preview;
    detail.append(makeElement('div', 'routing-preview', displayValue(preview)));
    return detail;
  }

  function renderDecisions(routing) {
    const body = byId('routingDecisionTableBody');
    const decisions = Array.isArray(routing.recent_decisions) ? routing.recent_decisions : [];
    if (!decisions.length) {
      const row = document.createElement('tr');
      const cell = appendCell(row, '还没有近期影子路由决策。', 'table-empty');
      cell.colSpan = 8;
      body.replaceChildren(row);
      return;
    }
    const rows = decisions.map((decision) => {
      const row = document.createElement('tr');
      appendCell(row, currentHelpers.formatDateTime(decision.created_at));
      appendCell(row, displayValue(decision.routing_config_version));
      appendCell(row, displayValue(decision.rule_level));
      appendCell(row, displayValue(decision.model_level));
      appendCell(row, displayValue(decision.final_level));
      appendCell(row, percentage(decision.confidence));
      const detailCell = appendCell(row, undefined);
      detailCell.append(decisionDetail(decision));
      const labelCell = appendCell(row, undefined);
      labelCell.append(labelControls(decision, routing));
      return row;
    });
    body.replaceChildren(...rows);
  }

  async function submitLabel(decision, verdict, correctLevel, note, button) {
    if (!currentHelpers) return;
    button.disabled = true;
    try {
      const result = await currentHelpers.api('/api/routing/label', {
        decision_id: decision.id,
        verdict,
        correct_level: correctLevel,
        expected_updated_at: decision.updated_at,
        note: note.trim(),
      });
      currentHelpers.toast(result.message || '路由决策已标注。');
      await currentHelpers.loadState();
    } catch (error) {
      if (/state changed|已被更新|冲突/i.test(error.message || '')) {
        currentHelpers.toast('决策已被更新，已刷新状态。', true);
        await currentHelpers.loadState();
      } else {
        button.disabled = false;
        currentHelpers.toast(error.message || '标注失败', true);
      }
    }
  }

  function routingPayload() {
    return {
      classifier_enabled: byId('routingEnabledInput').checked,
      classifier_model: byId('routingModelInput').value.trim(),
      confidence_threshold_percent: Number(byId('routingConfidenceInput').value),
      classifier_timeout_seconds: Number(byId('routingTimeoutInput').value),
      max_daily_calls: Number(byId('routingDailyCallsInput').value),
      failure_threshold: Number(byId('routingFailureThresholdInput').value),
      cooldown_seconds: Number(byId('routingCooldownInput').value),
      rule_version: byId('routingRuleVersionInput').value.trim(),
    };
  }

  function bindControls() {
    if (controlsBound) return;
    controlsBound = true;
    const form = byId('routingConfigForm');
    form.addEventListener('input', () => { routingConfigDirty = true; });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!currentHelpers) return;
      const action = event.submitter?.value || 'save';
      try {
        let result;
        if (action === 'rollback') {
          const targetVersion = Number(byId('routingRollbackVersionInput').value);
          if (!Number.isInteger(targetVersion) || targetVersion < 1) {
            currentHelpers.toast('请输入要回滚到的版本号。', true);
            return;
          }
          result = await currentHelpers.api('/api/routing/config/rollback', {target_version: targetVersion});
        } else {
          result = await currentHelpers.api('/api/routing/config', routingPayload());
        }
        routingConfigDirty = false;
        currentHelpers.toast(result.message || '路由配置已保存。');
        await currentHelpers.loadState();
      } catch (error) {
        currentHelpers.toast(error.message || '路由配置保存失败', true);
      }
    });
  }

  window.renderRoutingView = function renderRoutingView(routing, helpers) {
    currentHelpers = helpers;
    bindControls();
    const safeRouting = routing && typeof routing === 'object' ? routing : {};
    const metrics = safeRouting.metrics && typeof safeRouting.metrics === 'object' ? safeRouting.metrics : {};
    renderGateList(safeRouting.promotion_gate && typeof safeRouting.promotion_gate === 'object' ? safeRouting.promotion_gate : {});
    renderMetrics(metrics);
    renderConfig(safeRouting);
    renderMatrix(metrics);
    renderDecisions(safeRouting);
  };
}());
