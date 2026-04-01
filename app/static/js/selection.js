/**
 * selection.js — Selection state, summary, chips, composer mode, and status.
 */

import {
  state, appState, nodes, edges, selectedNodeIds, requestClusters,
  selectionSummaryState,
  statusEl, sendBtn, composerModeEl, composerEl,
  selectedChipsEl, selectionSummaryEl, selectionSummaryCountEl,
  selectionSummaryModelEl, selectionSummaryTextEl,
  selectionActionsEl, selectionContinueBtn, selectionBranchBtn,
  viewportEl, searchToggleEl,
} from './state.js';
import { escapeHtml, escapeAttribute, getDisplayName } from './utils.js';
import { isNodeAdjacent, scheduleRenderEdges } from './edges.js';
import { scheduleRenderMinimap } from './canvas.js';
import { getLatestRound, openBranchComposer } from './nodes.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

export function getSelectedContextNodes() {
  return Array.from(selectedNodeIds)
    .map((nodeId) => nodes.get(nodeId))
    .filter(Boolean)
    .sort((a, b) => (a.y - b.y) || (a.x - b.x));
}

export function getClusterRunningCount() {
  let count = 0;
  for (const cluster of requestClusters.values()) {
    if (cluster.isRunning || cluster.isCancelling) count += 1;
  }
  return count;
}

export function refreshStatus() {
  if (!appState.socketConnected) {
    statusEl.textContent = '连接已断开，正在重连...';
    statusEl.classList.remove('busy');
    return;
  }
  const count = getClusterRunningCount();
  if (count > 0) {
    statusEl.textContent = `已连接 · ${count} 个会话运行中`;
    statusEl.classList.add('busy');
  } else {
    statusEl.textContent = '已连接';
    statusEl.classList.remove('busy');
  }
}

// ── Selection summary ────────────────────────────────────────────────────────

export function shouldUseSelectionSummary(selection = getSelectedContextNodes()) {
  if (!selection.length) return false;
  return !(selection.length === 1 && selection[0].type === 'model' && state.selectionSource === 'click');
}

export function getSelectionSummaryModelLabel(model) {
  const display = getDisplayName(model || 'Kimi-K2.5');
  return /kimi/i.test(display) ? 'Kimi' : display;
}

export function renderSelectionSummary() {
  if (!selectionSummaryEl || !selectionSummaryCountEl || !selectionSummaryModelEl || !selectionSummaryTextEl) return;
  const selection = getSelectedContextNodes();
  if (!shouldUseSelectionSummary(selection)) {
    selectionSummaryEl.classList.add('hidden');
    selectionSummaryEl.classList.remove('loading', 'error');
    selectionSummaryTextEl.textContent = '';
    return;
  }

  const count = selectionSummaryState.count || selection.length;
  selectionSummaryCountEl.textContent = `已圈选 ${count} 个节点`;
  selectionSummaryModelEl.textContent = `${getSelectionSummaryModelLabel(selectionSummaryState.model)} 总结`;

  if (selectionSummaryState.loading) {
    selectionSummaryEl.classList.add('loading');
    selectionSummaryEl.classList.remove('error');
    selectionSummaryTextEl.textContent = '正在使用 Kimi 压缩圈选节点，可直接发送；若未完成会自动回退原始上下文。';
  } else if (selectionSummaryState.error) {
    selectionSummaryEl.classList.add('error');
    selectionSummaryEl.classList.remove('loading');
    selectionSummaryTextEl.textContent = `总结失败：${selectionSummaryState.error}；发送时将自动回退原始上下文。`;
  } else if (selectionSummaryState.text) {
    selectionSummaryEl.classList.remove('loading', 'error');
    selectionSummaryTextEl.textContent = selectionSummaryState.text;
  } else {
    selectionSummaryEl.classList.remove('loading', 'error');
    selectionSummaryTextEl.textContent = '已圈选节点，发送时将自动压缩上下文并带入对话。';
  }

  selectionSummaryEl.classList.remove('hidden');
  const roundHintEl = document.getElementById('roundHint');
  if (roundHintEl) {
    roundHintEl.textContent = getComposerMode(selection).hint;
  }
}

export function clearSelectionSummary(resetState = true) {
  if (appState.selectionSummaryTimer) {
    window.clearTimeout(appState.selectionSummaryTimer);
    appState.selectionSummaryTimer = null;
  }
  if (selectionSummaryState.controller) {
    selectionSummaryState.controller.abort();
    selectionSummaryState.controller = null;
  }
  if (resetState) {
    selectionSummaryState.key = '';
    selectionSummaryState.count = 0;
    selectionSummaryState.bundle = '';
    selectionSummaryState.text = '';
    selectionSummaryState.model = 'Kimi-K2.5';
    selectionSummaryState.error = '';
  }
  selectionSummaryState.loading = false;
  renderSelectionSummary();
}

export function queueSelectionSummaryRefresh() {
  if (appState.selectionSummaryTimer) {
    window.clearTimeout(appState.selectionSummaryTimer);
    appState.selectionSummaryTimer = null;
  }
  const selection = getSelectedContextNodes();
  if (!shouldUseSelectionSummary(selection)) {
    clearSelectionSummary();
    return;
  }
  appState.selectionSummaryTimer = window.setTimeout(() => {
    refreshSelectionSummary().catch(() => {});
  }, 180);
}

export async function refreshSelectionSummary() {
  const selection = getSelectedContextNodes();
  if (!shouldUseSelectionSummary(selection)) {
    clearSelectionSummary();
    return;
  }

  const bundle = buildContextBundleFromSelection();
  const key = `${selection.map((node) => node.nodeId).join('|')}::${bundle}`;
  const count = selection.length;
  if (!bundle) {
    clearSelectionSummary();
    return;
  }

  if (selectionSummaryState.key === key && (selectionSummaryState.text || selectionSummaryState.loading || selectionSummaryState.error)) {
    selectionSummaryState.count = count;
    selectionSummaryState.bundle = bundle;
    renderSelectionSummary();
    return;
  }

  if (selectionSummaryState.controller) {
    selectionSummaryState.controller.abort();
  }

  const controller = new AbortController();
  selectionSummaryState.key = key;
  selectionSummaryState.count = count;
  selectionSummaryState.bundle = bundle;
  selectionSummaryState.text = '';
  selectionSummaryState.model = 'Kimi-K2.5';
  selectionSummaryState.error = '';
  selectionSummaryState.loading = true;
  selectionSummaryState.controller = controller;
  renderSelectionSummary();

  try {
    const response = await fetch('/api/selection-summary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
      body: JSON.stringify({ bundle, count }),
    });
    if (controller.signal.aborted) return;
    const text = await response.text().catch(() => '');
    if (controller.signal.aborted) return;
    let data = {};
    try { data = JSON.parse(text); } catch { /* ignore */ }
    if (!response.ok) {
      throw new Error(String(data?.detail || '总结请求失败'));
    }
    selectionSummaryState.text = String(data.summary || '').trim();
    selectionSummaryState.model = String(data.model || 'Kimi-K2.5').trim() || 'Kimi-K2.5';
    selectionSummaryState.count = Number(data.count || count) || count;
    selectionSummaryState.error = '';
  } catch (error) {
    if (controller.signal.aborted || error?.name === 'AbortError') return;
    selectionSummaryState.text = '';
    selectionSummaryState.error = error instanceof Error ? error.message : '总结失败';
  } finally {
    if (selectionSummaryState.controller === controller) {
      selectionSummaryState.controller = null;
    }
    selectionSummaryState.loading = false;
    renderSelectionSummary();
  }
}

// ── Composer mode ────────────────────────────────────────────────────────────

export function getComposerMode(selection = getSelectedContextNodes()) {
  if (selection.length === 1 && selection[0].type === 'model' && !shouldUseSelectionSummary(selection)) {
    const round = selection[0].activeRound || getLatestRound(selection[0]);
    return {
      key: 'branch',
      label: `继续 ${getDisplayName(selection[0].model)} · 第 ${round} 轮`,
      sendLabel: '分支发送',
      hint: `继续 ${getDisplayName(selection[0].model)} · 第 ${round} 轮，自动继承结论与压缩过程。`,
    };
  }
  if (shouldUseSelectionSummary(selection)) {
    return {
      key: 'context',
      label: `已圈选 ${selection.length} 个节点`,
      sendLabel: '继续对话',
      hint: selectionSummaryState.loading
        ? `已圈选 ${selection.length} 个节点；Kimi 正在压缩上下文。`
        : `已圈选 ${selection.length} 个节点；下一轮会自动带入摘要上下文。`,
    };
  }
  return {
    key: 'plain',
    label: '普通提问',
    sendLabel: '发送',
    hint: '联网默认开启；思考开启后会更注重验证与分析。',
  };
}

export function updateComposerHint() {
  const mode = getComposerMode();
  document.getElementById('roundHint').textContent = mode.hint;
  if (composerModeEl) {
    composerModeEl.textContent = mode.label;
    composerModeEl.dataset.mode = mode.key;
  }
  renderSelectedChips();
  // 不再自动触发总结——仅在发送时按需执行，节省 token
  renderSelectionSummary();
}

// ── Selection chips ──────────────────────────────────────────────────────────

let _chipsDelegated = false;

export function renderSelectedChips() {
  const selection = getSelectedContextNodes();
  const mode = getComposerMode(selection);
  sendBtn.textContent = mode.sendLabel;
  if (!selection.length) {
    selectedChipsEl.classList.add('hidden');
    composerEl.classList.remove('has-selection');
    return;
  }
  selectedChipsEl.classList.remove('hidden');
  composerEl.classList.add('has-selection');
  selectedChipsEl.innerHTML = selection.map((node) => {
    const label = node.type === 'user' ? '用户提问' : getDisplayName(node.model);
    const safeId = escapeAttribute(node.nodeId);
    return `<span class="selected-chip" data-node-id="${safeId}">${escapeHtml(label)}<button type="button" class="chip-close" data-node-id="${safeId}">✕</button></span>`;
  }).join('') + '<button type="button" class="chips-clear">清除全部</button>';

  if (!_chipsDelegated) {
    _chipsDelegated = true;
    selectedChipsEl.addEventListener('click', (e) => {
      const closeBtn = e.target.closest('.chip-close');
      if (closeBtn) {
        const id = closeBtn.dataset.nodeId;
        selectedNodeIds.delete(id);
        if (appState.selectedNodeId === id) {
          appState.selectedNodeId = selectedNodeIds.size === 1 ? Array.from(selectedNodeIds)[0] : null;
        }
        if (!selectedNodeIds.size) {
          state.selectionSource = 'none';
        }
        renderSelectionState();
        updateComposerHint();
        return;
      }
      if (e.target.closest('.chips-clear')) {
        clearSelection();
      }
    });
  }
}

// ── Selection rendering ──────────────────────────────────────────────────────

export function renderSelectionState() {
  const selected = selectedNodeIds;
  const primaryNodeId = appState.hoverNodeId || appState.selectedNodeId || (selected.size === 1 ? Array.from(selected)[0] : null);

  for (const node of nodes.values()) {
    const isSelected = selected.has(node.nodeId);
    node.root.classList.toggle('active', appState.hoverNodeId === node.nodeId);
    node.root.classList.toggle('selected', isSelected);
    node.root.classList.toggle(
      'dimmed',
      Boolean(primaryNodeId && selected.size === 1 && node.nodeId !== primaryNodeId && !isNodeAdjacent(node.nodeId, primaryNodeId))
    );
  }
  updateSelectionActions();
  scheduleRenderEdges();
  scheduleRenderMinimap();
}

export function updateSelectionActions() {
  if (!selectionActionsEl) return;
  const selection = getSelectedContextNodes();
  if (!selection.length) {
    selectionActionsEl.classList.add('hidden');
    return;
  }
  const nodeRects = selection.map((node) => node.root.getBoundingClientRect());
  if (!nodeRects.length) {
    selectionActionsEl.classList.add('hidden');
    return;
  }
  const viewportRect = viewportEl.getBoundingClientRect();
  const minLeft = Math.min(...nodeRects.map((rect) => rect.left));
  const maxRight = Math.max(...nodeRects.map((rect) => rect.right));
  const minTop = Math.min(...nodeRects.map((rect) => rect.top));
  const toolbarWidth = selectionActionsEl.offsetWidth || 260;
  const toolbarHeight = selectionActionsEl.offsetHeight || 44;
  const left = Math.max(12, Math.min(minLeft - viewportRect.left + (maxRight - minLeft) / 2 - toolbarWidth / 2, viewportRect.width - toolbarWidth - 12));
  const top = Math.max(12, minTop - viewportRect.top - toolbarHeight - 12);
  selectionActionsEl.style.left = `${left}px`;
  selectionActionsEl.style.top = `${top}px`;
  selectionActionsEl.classList.remove('hidden');
  const singleModel = selection.length === 1 && selection[0].type === 'model';
  selectionContinueBtn.textContent = singleModel ? '继续此模型' : '继续对话';
  selectionBranchBtn.disabled = !singleModel;
}

export function clearSelection() {
  appState.selectedNodeId = null;
  selectedNodeIds.clear();
  state.selectionSource = 'none';
  clearSelectionSummary();
  renderSelectionState();
  updateComposerHint();
}

export function buildContextBundleFromSelection() {
  const selection = getSelectedContextNodes();
  if (!selection.length) return '';

  const sections = selection.map((node, index) => {
    if (node.type === 'user') {
      const text = node.root.querySelector('.user-message')?.textContent?.trim() || '';
      return `[${index + 1}] 用户问题\n${text.slice(0, 800)}`;
    }
    if (node.type === 'conclusion') {
      const text = (node.markdown || '').trim();
      return text ? `[${index + 1}] 总结\n${text.slice(0, 1000)}` : '';
    }
    if (!node.turns) return '';
    const rounds = Array.from(node.turns.keys()).sort((a, b) => a - b);
    const activeRound = node.activeRound || rounds[rounds.length - 1] || 1;
    const snippets = rounds.slice(-2).map((round) => {
      const turn = node.turns.get(round);
      const raw = (turn?.raw || '').trim();
      return raw ? `第 ${round} 轮：${raw.slice(0, round === activeRound ? 1000 : 280)}` : '';
    }).filter(Boolean).join('\n');
    return `[${index + 1}] ${getDisplayName(node.model)}\n当前轮次：第 ${activeRound} 轮\n${snippets}`;
  }).filter(Boolean);

  return [
    '以下内容来自 NanoBob 画布中用户当前选中的卡片，请先基于这些上下文继续思考。',
    '要求：优先提炼结论、保留关键分歧、压缩中间过程，不要机械重复原文。',
    ...sections,
  ].join('\n\n');
}
