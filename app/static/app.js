const MODEL_DISPLAY_NAMES = {};

const MODEL_NODE_WIDTH = 420;
const USER_NODE_WIDTH = 500;
const MODEL_NODE_HEIGHT = 700;
const USER_NODE_HEIGHT = 300;
const CONCLUSION_NODE_WIDTH = 520;
const CONCLUSION_NODE_HEIGHT = 400;
const CLUSTER_GAP_X = 36;
const CLUSTER_GAP_Y = 54;
const CLUSTER_PADDING = 56;
const DEFAULT_SCALE = 0.2;
const MAX_MESSAGE_LENGTH = 4000;
const CONCLUSION_CONTEXT_MAX_CHARS = 3000;

let _rafEdgesScheduled = false;
let _rafMinimapScheduled = false;

function scheduleRenderEdges() {
  if (_rafEdgesScheduled) return;
  _rafEdgesScheduled = true;
  requestAnimationFrame(() => {
    _rafEdgesScheduled = false;
    renderEdges();
  });
}

function scheduleRenderMinimap() {
  if (_rafMinimapScheduled) return;
  _rafMinimapScheduled = true;
  requestAnimationFrame(() => {
    _rafMinimapScheduled = false;
    renderMinimap();
  });
}

function getDisplayName(model) {
  return MODEL_DISPLAY_NAMES[model] || model;
}

const statusEl = document.getElementById('status');
const sendBtn = document.getElementById('sendBtn');
const clearBtn = document.getElementById('clearBtn');
const fitBtn = document.getElementById('fitBtn');
const zoomInBtn = document.getElementById('zoomInBtn');
const zoomOutBtn = document.getElementById('zoomOutBtn');
const zoomResetBtn = document.getElementById('zoomResetBtn');
const messageInput = document.getElementById('messageInput');
const discussionRoundsEl = document.getElementById('discussionRounds');
const searchToggleEl = document.getElementById('searchToggle');
const thinkToggleEl = document.getElementById('thinkToggle');
const modelCount = document.getElementById('modelCount');
const viewportEl = document.getElementById('canvasViewport');
const stageEl = document.getElementById('canvasStage');
const gridEl = document.querySelector('.canvas-grid');
const edgeLayerEl = document.getElementById('edgeLayer');
const minimapContentEl = document.getElementById('minimapContent');
const minimapNodesEl = document.getElementById('minimapNodes');
const minimapViewportEl = document.getElementById('minimapViewport');
const selectedChipsEl = document.getElementById('selectedChips');
const selectionSummaryEl = document.getElementById('selectionSummary');
const selectionSummaryCountEl = document.getElementById('selectionSummaryCount');
const selectionSummaryModelEl = document.getElementById('selectionSummaryModel');
const selectionSummaryTextEl = document.getElementById('selectionSummaryText');
const composerEl = document.querySelector('.composer');
const composerModeEl = document.getElementById('composerMode');
const saveStatusEl = document.getElementById('saveStatus');
const selectionActionsEl = document.getElementById('selectionActions');
const selectionContinueBtn = document.getElementById('selectionContinueBtn');
const selectionBranchBtn = document.getElementById('selectionBranchBtn');
const selectionClearBtn = document.getElementById('selectionClearBtn');
const sidebarEl = document.getElementById('sidebar');
const sidebarCollapseBtn = document.getElementById('sidebarCollapseBtn');
const sidebarToggleBtn = document.getElementById('sidebarToggleBtn');
const sidebarCanvasListEl = document.getElementById('sidebarCanvasList');
const sidebarNewCanvasBtn = document.getElementById('sidebarNewCanvasBtn');
const sidebarAvatarEl = document.getElementById('sidebarAvatar');
const sidebarUsernameEl = document.getElementById('sidebarUsername');
const sidebarBalanceEl = document.getElementById('sidebarBalance');
const sidebarLogoutBtn = document.getElementById('sidebarLogoutBtn');

let socket;

function updateBalanceDisplay(balance) {
  if (sidebarBalanceEl) {
    sidebarBalanceEl.textContent = balance.toFixed(1) + ' 点';
    sidebarBalanceEl.classList.toggle('low', balance <= 10);
  }
}

function getSearchMode() {
  const v = searchToggleEl.value;
  if (v === 'auto') return 'auto';
  return v === 'true';
}

let models = [];
let socketConnected = false;
let latestConclusionMarkdown = '';
let latestConclusionRequestId = '';
let conclusionAutoAttach = true;
let clusterCount = 0;
let latestRequestId = null;
let hoverNodeId = null;
let selectedNodeId = null;
const selectedNodeIds = new Set();
let currentCanvasId = null;
let canvasesList = [];
const positionSaveTimers = new Map();
let saveStatusTimer = null;
let selectionSummaryTimer = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 30000;

const selectionSummaryState = {
  key: '',
  count: 0,
  bundle: '',
  text: '',
  model: 'Kimi-K2.5',
  loading: false,
  error: '',
  controller: null,
};

const state = {
  scale: DEFAULT_SCALE,
  offsetX: 280,
  offsetY: 120,
  panning: false,
  panStartX: 0,
  panStartY: 0,
  originOffsetX: 0,
  originOffsetY: 0,
  draggingNodeId: null,
  dragStartX: 0,
  dragStartY: 0,
  originNodeX: 0,
  originNodeY: 0,
  selectionSource: 'none',
};

const requestClusters = new Map();
const nodes = new Map();
const edges = new Map();
const pendingSearchEvents = new Map();

function getCluster(requestId) {
  return requestClusters.get(requestId) || null;
}

function setSaveStatus(text = '', type = '', autoHide = true) {
  if (!saveStatusEl) return;
  if (saveStatusTimer) {
    window.clearTimeout(saveStatusTimer);
    saveStatusTimer = null;
  }
  if (!text) {
    saveStatusEl.textContent = '';
    saveStatusEl.className = 'save-status hidden';
    return;
  }
  saveStatusEl.textContent = text;
  saveStatusEl.className = `save-status ${type}`.trim();
  if (autoHide) {
    saveStatusTimer = window.setTimeout(() => {
      saveStatusEl.textContent = '';
      saveStatusEl.className = 'save-status hidden';
      saveStatusTimer = null;
    }, 1800);
  }
}

function stripMarkdownSummary(value) {
  return String(value || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^[>#\-*\d.\s]+/gm, '')
    .replace(/[\*_~|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function summarizeText(value, maxLength = 34) {
  const cleaned = stripMarkdownSummary(value);
  if (!cleaned) return '等待摘要';
  return cleaned.length > maxLength ? `${cleaned.slice(0, maxLength - 1)}…` : cleaned;
}

function shouldUseSelectionSummary(selection = getSelectedContextNodes()) {
  if (!selection.length) return false;
  return !(selection.length === 1 && selection[0].type === 'model' && state.selectionSource === 'click');
}

function getSelectionSummaryModelLabel(model) {
  const display = getDisplayName(model || 'Kimi-K2.5');
  return /kimi/i.test(display) ? 'Kimi' : display;
}

function renderSelectionSummary() {
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
  } else {
    selectionSummaryEl.classList.remove('loading', 'error');
    selectionSummaryTextEl.textContent = selectionSummaryState.text || '已圈选节点，发送时会自动带入上下文。';
  }

  selectionSummaryEl.classList.remove('hidden');
  const roundHintEl = document.getElementById('roundHint');
  if (roundHintEl) {
    roundHintEl.textContent = getComposerMode(selection).hint;
  }
}

function clearSelectionSummary(resetState = true) {
  if (selectionSummaryTimer) {
    window.clearTimeout(selectionSummaryTimer);
    selectionSummaryTimer = null;
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

function queueSelectionSummaryRefresh() {
  if (selectionSummaryTimer) {
    window.clearTimeout(selectionSummaryTimer);
    selectionSummaryTimer = null;
  }
  const selection = getSelectedContextNodes();
  if (!shouldUseSelectionSummary(selection)) {
    clearSelectionSummary();
    return;
  }
  selectionSummaryTimer = window.setTimeout(() => {
    refreshSelectionSummary();
  }, 180);
}

async function refreshSelectionSummary() {
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
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(String(data?.detail || '总结请求失败'));
    }
    if (controller.signal.aborted) return;
    selectionSummaryState.text = String(data.summary || '').trim();
    selectionSummaryState.model = String(data.model || 'Kimi-K2.5').trim() || 'Kimi-K2.5';
    selectionSummaryState.count = Number(data.count || count) || count;
    selectionSummaryState.error = '';
  } catch (error) {
    if (controller.signal.aborted) return;
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

function getComposerMode(selection = getSelectedContextNodes()) {
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

function openBranchComposer(node) {
  if (!node || node.type !== 'model' || !node.turns.size) return;
  node.branchBox.classList.remove('hidden');
  node.branchInput.focus();
}

function updateSelectionActions() {
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

function setSearchCollapsed(userNode, collapsed) {
  if (!userNode?.searchResultsEl || !userNode?.searchToggleBtn) return;
  userNode.searchCollapsed = Boolean(collapsed);
  userNode.searchResultsEl.classList.toggle('collapsed', userNode.searchCollapsed);
  userNode.searchToggleBtn.textContent = userNode.searchCollapsed ? '展开结果' : '收起结果';
}

function updateClusterVisualState(requestId) {
  const cluster = getCluster(requestId);
  if (!cluster) return;
  const running = Boolean(cluster.isRunning || cluster.isCancelling);
  const nodeIds = [cluster.userNodeId, ...cluster.modelNodeIds];
  for (const nodeId of nodeIds) {
    const node = nodes.get(nodeId);
    node?.root.classList.toggle('cluster-running', running);
  }
}

function refreshClusterOutline(requestId) {
  const userNode = getUserNode(requestId);
  const cluster = getCluster(requestId);
  if (!userNode?.outlineListEl || !userNode.outlinePanel || !cluster) return;
  const items = cluster.modelNodeIds
    .map((nodeId) => nodes.get(nodeId))
    .filter((node) => node?.type === 'model' && node.activeRound && node.turns.get(node.activeRound)?.summary)
    .map((node) => {
      const turn = node.turns.get(node.activeRound);
      return `
        <div class="outline-item">
          <strong>${escapeHtml(getDisplayName(node.model))}</strong>
          <span>第 ${node.activeRound} 轮 · ${escapeHtml(turn.summary)}</span>
        </div>
      `;
    });
  userNode.outlineListEl.innerHTML = items.join('');
  userNode.outlinePanel.classList.toggle('hidden', items.length === 0);
}

function updateTurnSummary(turn) {
  if (!turn) return;
  const summary = summarizeText(turn.raw, 32);
  turn.summary = summary;
  if (turn.summaryEl) {
    turn.summaryEl.textContent = summary;
    turn.btn.title = `${turn.btn.dataset.roundLabel || ''} ${summary}`.trim();
  }
  if (turn.summaryChipEl) {
    turn.summaryChipEl.textContent = `摘要：${summary}`;
  }
}

function getSelectedContextNodes() {
  return Array.from(selectedNodeIds)
    .map((nodeId) => nodes.get(nodeId))
    .filter(Boolean)
    .sort((a, b) => (a.y - b.y) || (a.x - b.x));
}

function getClusterRunningCount() {
  let count = 0;
  for (const cluster of requestClusters.values()) {
    if (cluster.isRunning || cluster.isCancelling) count += 1;
  }
  return count;
}

function refreshStatus() {
  if (!socketConnected) {
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

function updateComposerHint() {
  const mode = getComposerMode();
  document.getElementById('roundHint').textContent = mode.hint;
  if (composerModeEl) {
    composerModeEl.textContent = mode.label;
    composerModeEl.dataset.mode = mode.key;
  }
  renderSelectedChips();
  queueSelectionSummaryRefresh();
}

let _chipsDelegated = false;

function renderSelectedChips() {
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
        if (selectedNodeId === id) {
          selectedNodeId = selectedNodeIds.size === 1 ? Array.from(selectedNodeIds)[0] : null;
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

function setClusterState(requestId, patch = {}) {
  const cluster = getCluster(requestId);
  if (!cluster) return;
  Object.assign(cluster, patch);
  const userNode = getUserNode(requestId);
  if (userNode?.badge && patch.badgeText) {
    userNode.badge.textContent = patch.badgeText;
  }
  if (userNode?.cancelBtn) {
    const visible = Boolean(cluster.isRunning || cluster.isCancelling);
    userNode.cancelBtn.classList.toggle('hidden', !visible);
    userNode.cancelBtn.disabled = !socketConnected || cluster.isCancelling || !cluster.isRunning;
    userNode.cancelBtn.textContent = cluster.isCancelling ? '取消中...' : '停止';
  }
  updateClusterVisualState(requestId);
  scheduleRenderMinimap();
  refreshStatus();
}

function clearSelection() {
  selectedNodeId = null;
  selectedNodeIds.clear();
  state.selectionSource = 'none';
  clearSelectionSummary();
  renderSelectionState();
  updateComposerHint();
}

function buildContextBundleFromSelection() {
  const selection = getSelectedContextNodes();
  if (!selection.length) return '';

  const sections = selection.map((node, index) => {
    if (node.type === 'user') {
      const text = node.root.querySelector('.user-message')?.textContent?.trim() || '';
      return `[${index + 1}] 用户问题\n${text.slice(0, 800)}`;
    }
    const rounds = Array.from(node.turns.keys()).sort((a, b) => a - b);
    const activeRound = node.activeRound || rounds[rounds.length - 1] || 1;
    const activeTurn = node.turns.get(activeRound);
    const snippets = rounds.slice(-2).map((round) => {
      const turn = node.turns.get(round);
      const raw = (turn?.raw || '').trim();
      return raw ? `第 ${round} 轮：${raw.slice(0, round === activeRound ? 1000 : 280)}` : '';
    }).filter(Boolean).join('\n');
    return `[${index + 1}] ${getDisplayName(node.model)}\n当前轮次：第 ${activeRound} 轮\n${snippets}`;
  });

  return [
    '以下内容来自 NanoBob 画布中用户当前选中的卡片，请先基于这些上下文继续思考。',
    '要求：优先提炼结论、保留关键分歧、压缩中间过程，不要机械重复原文。',
    ...sections,
  ].join('\n\n');
}

function cancelRequest(requestId) {
  if (!requestId || socket.readyState !== WebSocket.OPEN) return;
  const cluster = getCluster(requestId);
  if (!cluster || !cluster.isRunning || cluster.isCancelling) return;
  try { socket.send(JSON.stringify({ action: 'cancel_request', request_id: requestId })); } catch (_e) { return; }
  setClusterState(requestId, { isCancelling: true, badgeText: '取消中...' });
}

function materializeClusterModels(requestId) {
  const cluster = getCluster(requestId);
  if (!cluster || cluster.modelsReady) return;
  const modelNames = cluster.pendingModels || [];
  cluster.modelsReady = true;

  modelNames.forEach((model, index) => {
    const nodeId = `model-${requestId}-${model}`;
    createModelNode({
      nodeId,
      requestId,
      model,
      x: cluster.modelStartX + index * (MODEL_NODE_WIDTH + CLUSTER_GAP_X),
      y: cluster.modelY,
    });
    cluster.modelNodeIds.push(nodeId);
    addEdge({
      id: `edge-${cluster.userNodeId}-${nodeId}`,
      sourceId: cluster.userNodeId,
      targetId: nodeId,
      type: 'question_to_answer',
    });
  });
  updateClusterBounds(requestId);
  renderEdges();
  renderMinimap();
}

function schedulePositionSave(requestId) {
  if (!currentCanvasId) return;
  if (positionSaveTimers.has(requestId)) {
    clearTimeout(positionSaveTimers.get(requestId));
  }
  setSaveStatus('位置保存中...', 'pending', false);
  positionSaveTimers.set(requestId, setTimeout(async () => {
    positionSaveTimers.delete(requestId);
    const cluster = requestClusters.get(requestId);
    const userNode = nodes.get(cluster?.userNodeId);
    if (!cluster || !userNode) return;
    try {
      const response = await fetch(`/api/cluster-positions/${requestId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ user_x: userNode.x, user_y: userNode.y, model_y: cluster.modelY }),
      });
      if (!response.ok) {
        throw new Error('save failed');
      }
      setSaveStatus('布局已保存', 'ok');
    } catch (_error) {
      setSaveStatus('布局保存失败', 'error');
    }
  }, 500));
}

let _canvasListDelegated = false;

function renderCanvasList() {
  if (!sidebarCanvasListEl) return;
  sidebarCanvasListEl.innerHTML = canvasesList.map((c) => `
    <div class="sidebar-canvas-item ${c.id === currentCanvasId ? 'active' : ''}" data-id="${escapeAttribute(c.id)}">
      <span class="sidebar-canvas-item-name">${escapeHtml(c.name)}</span>
      <button type="button" class="sidebar-canvas-item-del" data-id="${escapeAttribute(c.id)}" title="删除画布">✕</button>
    </div>
  `).join('');

  if (!_canvasListDelegated) {
    _canvasListDelegated = true;
    sidebarCanvasListEl.addEventListener('click', async (e) => {
      const delBtn = e.target.closest('.sidebar-canvas-item-del');
      if (delBtn) {
        e.stopPropagation();
        const id = delBtn.dataset.id;
        if (canvasesList.length <= 1) {
          alert('至少需要保留一个画布。');
          return;
        }
        if (!confirm('确认删除该画布及其所有内容？此操作不可撤销。')) return;
        const res = await fetch(`/api/canvases/${id}`, { method: 'DELETE', credentials: 'same-origin' });
        if (!res.ok) return;
        canvasesList = canvasesList.filter((c) => c.id !== id);
        if (currentCanvasId === id) {
          await switchCanvas(canvasesList[0].id);
        } else {
          renderCanvasList();
        }
        return;
      }
      const item = e.target.closest('.sidebar-canvas-item');
      if (item) {
        const id = item.dataset.id;
        if (id !== currentCanvasId) {
          switchCanvas(id);
        }
      }
    });
  }
}

async function switchCanvas(canvasId) {
  currentCanvasId = canvasId;
  renderCanvasList();
  await loadCanvasState(canvasId);
}

async function loadCanvasState(canvasId) {
  clearCanvas();
  try {
    const res = await fetch(`/api/canvases/${canvasId}/state`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    for (const req of data.requests) {
      replayCluster(req);
    }
    renderMinimap();
    updateComposerHint();
  } catch (_) {
    // fail silently
  }
}

function replayCluster(req) {
  const {
    request_id, user_message, models: reqModels, discussion_rounds,
    search_enabled, think_enabled, parent_request_id, source_model,
    source_round, position, results,
  } = req;
  const activeModels = reqModels.length ? reqModels : models;
  const modelCount = activeModels.length;
  const modelRowWidth = modelCount * MODEL_NODE_WIDTH + Math.max(0, modelCount - 1) * CLUSTER_GAP_X;
  const footprintWidth = Math.max(USER_NODE_WIDTH, modelRowWidth);
  const footprintHeight = USER_NODE_HEIGHT + CLUSTER_GAP_Y + MODEL_NODE_HEIGHT;

  let layout;
  if (position) {
    layout = {
      userX: position.user_x,
      userY: position.user_y,
      modelStartX: position.user_x + (USER_NODE_WIDTH - modelRowWidth) / 2,
      modelY: position.model_y,
      centerX: position.user_x + USER_NODE_WIDTH / 2,
      bbox: {
        x: position.user_x + Math.min(0, (USER_NODE_WIDTH - modelRowWidth) / 2),
        y: position.user_y,
        width: footprintWidth,
        height: position.model_y - position.user_y + MODEL_NODE_HEIGHT,
      },
    };
  } else {
    const viewportRect = viewportEl.getBoundingClientRect();
    const cx = (viewportRect.width / 2 - state.offsetX) / state.scale;
    const cy = (viewportRect.height / 2 - state.offsetY) / state.scale;
    const topX = cx - footprintWidth / 2 + (clusterCount % 2) * 80;
    const topY = cy - 120 + clusterCount * 120;
    const candidates = [];
    for (let row = 0; row < 40; row += 1) {
      candidates.push({ x: topX, y: topY + row * (footprintHeight + 80) });
    }
    const bbox = findAvailableBox(candidates, footprintWidth, footprintHeight);
    const userX2 = bbox.x + (bbox.width - USER_NODE_WIDTH) / 2;
    const mRowWidth = modelCount * MODEL_NODE_WIDTH + Math.max(0, modelCount - 1) * CLUSTER_GAP_X;
    layout = {
      userX: userX2,
      userY: bbox.y,
      modelStartX: bbox.x + (bbox.width - mRowWidth) / 2,
      modelY: bbox.y + USER_NODE_HEIGHT + CLUSTER_GAP_Y,
      centerX: bbox.x + bbox.width / 2,
      bbox,
    };
    clusterCount += 1;
  }

  const userNodeId = `user-${request_id}`;
  createUserNode({
    nodeId: userNodeId,
    requestId: request_id,
    x: layout.userX,
    y: layout.userY,
    content: user_message,
    discussionRounds: discussion_rounds,
    searchEnabled: search_enabled,
    thinkEnabled: think_enabled,
    parentRequestId: parent_request_id,
    sourceModel: source_model,
    sourceRound: source_round,
  });

  if (parent_request_id && source_model) {
    addEdge({
      id: `edge-model-${parent_request_id}-${source_model}-${userNodeId}`,
      sourceId: `model-${parent_request_id}-${source_model}`,
      targetId: userNodeId,
      type: 'branch_from_turn',
    });
  }

  const cluster = {
    requestId: request_id,
    kind: parent_request_id ? 'branch' : 'main',
    userNodeId,
    modelNodeIds: [],
    pendingModels: activeModels,
    modelsReady: true,
    modelStartX: layout.modelStartX,
    modelY: layout.modelY,
    baseX: layout.centerX,
    baseY: layout.modelY,
    discussionRounds: discussion_rounds,
    bbox: layout.bbox,
    parentRequestId: parent_request_id,
    sourceModel: source_model,
    searchEnabled: false,
    isRunning: false,
    isCancelling: false,
  };
  requestClusters.set(request_id, cluster);

  activeModels.forEach((model, index) => {
    const nodeId = `model-${request_id}-${model}`;
    createModelNode({
      nodeId,
      requestId: request_id,
      model,
      x: layout.modelStartX + index * (MODEL_NODE_WIDTH + CLUSTER_GAP_X),
      y: layout.modelY,
    });
    cluster.modelNodeIds.push(nodeId);
    addEdge({
      id: `edge-${userNodeId}-${nodeId}`,
      sourceId: userNodeId,
      targetId: nodeId,
      type: 'question_to_answer',
    });
  });

  const modelResultsMap = {};
  for (const r of results) {
    if (!modelResultsMap[r.model]) modelResultsMap[r.model] = [];
    modelResultsMap[r.model].push(r);
  }
  for (const [model, rounds] of Object.entries(modelResultsMap)) {
    const sorted = rounds.slice().sort((a, b) => a.round - b.round);
    for (const rd of sorted) {
      ensureModelTurn(request_id, model, rd.round);
      if (rd.content) {
        appendTurnText(request_id, model, rd.round, rd.content);
        flushTurnRender(request_id, model, rd.round);
      }
      setTurnState(request_id, model, rd.round, rd.status === 'success' ? '已完成' : '失败');
      setNodeState(request_id, model, `第 ${rd.round} 轮完成`);
    }
    if (sorted.length > 0) {
      activateTurn(request_id, model, sorted[sorted.length - 1].round);
    }
  }

  if (search_enabled) {
    const userNode = getUserNode(request_id);
    if (userNode?.searchBadgeEl) {
      userNode.searchBadgeEl.textContent = '已完成';
      userNode.searchQueryEl.textContent = '搜索已完成';
      userNode.searchToggleBtn?.classList.add('hidden');
    }
  }

  const summary = req.summary;
  if (summary && summary.status === 'success' && summary.summary_markdown) {
    const conclusionNodeId = `conclusion-${request_id}`;
    const modelNodes = cluster.modelNodeIds.map(id => nodes.get(id)).filter(Boolean);
    let cx = layout.centerX - CONCLUSION_NODE_WIDTH / 2;
    let cy = layout.modelY + MODEL_NODE_HEIGHT + 40;
    if (modelNodes.length > 0) {
      const lastModel = modelNodes[modelNodes.length - 1];
      cy = lastModel.y + MODEL_NODE_HEIGHT + 40;
    }
    createConclusionNode({
      nodeId: conclusionNodeId,
      requestId: request_id,
      x: cx,
      y: cy,
      model: summary.summary_model || '',
      markdown: summary.summary_markdown,
      status: 'success',
    });
    cluster.conclusionNodeId = conclusionNodeId;
    addEdge({
      id: `edge-user-${request_id}-${conclusionNodeId}`,
      sourceId: `user-${request_id}`,
      targetId: conclusionNodeId,
      type: 'conclusion_edge',
    });
    latestConclusionMarkdown = summary.summary_markdown;
    latestConclusionRequestId = request_id;
  }

  updateClusterBounds(request_id);
  setClusterState(request_id, { isRunning: false, isCancelling: false, badgeText: '已完成' });
  renderEdges();
}

async function initCanvases() {
  connect();
  try {
    const res = await fetch('/api/canvases', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    canvasesList = data.canvases || [];
    if (!canvasesList.length) {
      const createRes = await fetch('/api/canvases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ name: '画布 1' }),
      });
      if (!createRes.ok) return;
      const created = await createRes.json();
      canvasesList = [{ id: created.canvas_id, name: created.name }];
    }
    renderCanvasList();
    await switchCanvas(canvasesList[0].id);
  } catch (_) {
    // fail silently — WebSocket still works
  }
}

function connect() {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${protocol}://${window.location.host}/ws/chat`);

  socket.addEventListener('open', () => {
    socketConnected = true;
    const wasReconnect = reconnectAttempts > 0;
    reconnectAttempts = 0;
    refreshStatus();
    if (wasReconnect && currentCanvasId) {
      loadCanvasState(currentCanvasId);
    }
  });

  socket.addEventListener('close', (event) => {
    socketConnected = false;
    if (event.code === 4401 || event.code === 4403) {
      window.location.href = '/login';
      return;
    }
    for (const cluster of requestClusters.values()) {
      cluster.isRunning = false;
      cluster.isCancelling = false;
    }
    refreshStatus();
    const delay = Math.min(1200 * Math.pow(1.5, reconnectAttempts), MAX_RECONNECT_DELAY);
    reconnectAttempts += 1;
    setTimeout(connect, delay);
  });

  socket.addEventListener('message', (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      console.error('[ws] invalid JSON from server:', err, event.data?.slice?.(0, 200));
      return;
    }
    handleEvent(payload);
  });
}

function handleEvent(payload) {
  switch (payload.type) {
    case 'meta':
      models = payload.models || [];
      modelCount.textContent = `${models.length} 个模型`;
      const wasSearchDisabled = searchToggleEl.disabled;
      if (payload.search_available === false) {
        searchToggleEl.value = 'false';
        searchToggleEl.disabled = true;
      } else {
        searchToggleEl.disabled = false;
        if (wasSearchDisabled) {
          searchToggleEl.value = payload.preprocess_available ? 'auto' : 'true';
        }
      }
      {
        const banner = document.getElementById('needsSetupBanner');
        if (banner) {
          banner.classList.toggle('hidden', !payload.needs_setup);
        }
        sendBtn.disabled = Boolean(payload.needs_setup);
        if (typeof payload.balance === 'number') updateBalanceDisplay(payload.balance);
        if (payload.username && sidebarUsernameEl) {
          sidebarUsernameEl.textContent = payload.username;
        }
        if (payload.username && sidebarAvatarEl) {
          sidebarAvatarEl.textContent = payload.username.charAt(0).toUpperCase();
        }
        const pending = payload.pending_requests || [];
        for (const rid of pending) {
          const cluster = requestClusters.get(rid);
          if (cluster) {
            setClusterState(rid, { isRunning: true, isCancelling: false, badgeText: '后台运行中' });
          }
        }
      }
      refreshStatus();
      break;
    case 'usage':
      if (typeof payload.balance === 'number') updateBalanceDisplay(payload.balance);
      break;
    case 'user':
      latestRequestId = payload.request_id;
      createCluster({
        requestId: payload.request_id,
        userMessage: payload.content,
        discussionRounds: payload.discussion_rounds || 1,
        models: payload.models || [],
        searchEnabled: Boolean(payload.search_enabled),
        thinkEnabled: Boolean(payload.think_enabled),
        parentRequestId: payload.parent_request_id || null,
        sourceModel: payload.source_model || null,
        sourceRound: payload.source_round || null,
      });
      setClusterState(payload.request_id, {
        isRunning: true,
        isCancelling: false,
        badgeText: Boolean(payload.search_enabled) ? '排队搜索' : '排队执行',
      });
      focusCluster(payload.request_id);
      flushPendingSearchEvents(payload.request_id);
      break;
    case 'search_started':
    case 'search_complete':
    case 'search_error':
      queueOrApplySearchEvent(payload);
      break;
    case 'preprocess_start':
      handlePreprocessStart(payload);
      break;
    case 'preprocess_result':
      handlePreprocessResult(payload);
      break;
    case 'search_organized':
      handleSearchOrganized(payload);
      break;
    case 'round_start':
      materializeClusterModels(payload.request_id);
      setClusterState(payload.request_id, {
        isRunning: true,
        isCancelling: false,
        badgeText: `第 ${payload.round}/${payload.total_rounds} 轮生成中`,
      });
      break;
    case 'discussion_stopped':
      setClusterState(payload.request_id, { isRunning: false, isCancelling: false, badgeText: '讨论提前结束' });
      break;
    case 'start':
      materializeClusterModels(payload.request_id);
      ensureModelTurn(payload.request_id, payload.model, payload.round);
      activateTurn(payload.request_id, payload.model, payload.round);
      setNodeState(payload.request_id, payload.model, `第 ${payload.round} 轮生成中`);
      setTurnState(payload.request_id, payload.model, payload.round, '生成中...');
      break;
    case 'delta':
      materializeClusterModels(payload.request_id);
      ensureModelTurn(payload.request_id, payload.model, payload.round);
      activateTurn(payload.request_id, payload.model, payload.round);
      appendTurnText(payload.request_id, payload.model, payload.round, payload.content);
      break;
    case 'done':
      materializeClusterModels(payload.request_id);
      ensureModelTurn(payload.request_id, payload.model, payload.round);
      activateTurn(payload.request_id, payload.model, payload.round);
      flushTurnRender(payload.request_id, payload.model, payload.round);
      setNodeState(payload.request_id, payload.model, `第 ${payload.round} 轮完成`);
      setTurnState(payload.request_id, payload.model, payload.round, '已完成');
      break;
    case 'error':
      if (payload.model) {
        materializeClusterModels(payload.request_id);
        ensureModelTurn(payload.request_id, payload.model, payload.round);
        activateTurn(payload.request_id, payload.model, payload.round);
        appendTurnText(payload.request_id, payload.model, payload.round, `

[错误] ${payload.content}`);
        flushTurnRender(payload.request_id, payload.model, payload.round);
        setNodeState(payload.request_id, payload.model, payload.content?.includes('超时') ? `第 ${payload.round} 轮超时` : `第 ${payload.round} 轮失败`);
        setTurnState(payload.request_id, payload.model, payload.round, '失败');
      } else if (payload.request_id) {
        setClusterState(payload.request_id, { isRunning: false, isCancelling: false, badgeText: '请求失败' });
      } else {
        alert(payload.content);
      }
      break;
    case 'cancel_requested':
      setClusterState(payload.request_id, { isRunning: true, isCancelling: true, badgeText: '取消中...' });
      break;
    case 'cancelled':
      setClusterState(payload.request_id, { isRunning: false, isCancelling: false, badgeText: '已取消' });
      break;
    case 'round_complete':
      setClusterState(payload.request_id, { isRunning: false, isCancelling: false, badgeText: '已完成' });
      latestRequestId = payload.request_id || latestRequestId;
      break;
    case 'conclusion_start':
      handleConclusionStart(payload);
      break;
    case 'conclusion_done':
      handleConclusionDone(payload);
      break;
    case 'conclusion_error':
      handleConclusionError(payload);
      break;
    case 'conclusion_retry_queued':
      setClusterState(payload.request_id, { isRunning: true, isCancelling: false, badgeText: '结论重试排队中' });
      break;
    case 'cleared':
      clearCanvas();
      break;
    default:
      break;
  }
}

function handlePreprocessStart(payload) {
  const userNode = getUserNode(payload.request_id);
  if (!userNode) return;
  setClusterState(payload.request_id, {
    isRunning: true,
    isCancelling: false,
    badgeText: `预处理分析中（${escapeHtml(getDisplayName(payload.model || ''))})`,
  });
  if (userNode.searchPanel) {
    userNode.searchPanel.classList.remove('hidden');
    if (userNode.searchQueryEl) userNode.searchQueryEl.textContent = '预处理模型分析中...';
    if (userNode.searchBadgeEl) userNode.searchBadgeEl.textContent = '分析中';
  }
}

function handlePreprocessResult(payload) {
  const userNode = getUserNode(payload.request_id);
  if (!userNode) return;
  const needSearch = Boolean(payload.need_search);
  const keywords = (payload.keywords || []).join(', ');
  const reason = payload.reason || '';
  if (userNode.searchQueryEl) {
    userNode.searchQueryEl.textContent = needSearch
      ? `需要搜索：${keywords}`
      : `无需搜索${reason ? `（${reason}）` : ''}`;
  }
  if (userNode.searchBadgeEl) {
    userNode.searchBadgeEl.textContent = needSearch ? '待搜索' : '已跳过';
  }
  if (!needSearch && userNode.searchPanel) {
    setClusterState(payload.request_id, {
      isRunning: true,
      isCancelling: false,
      badgeText: '排队执行',
    });
  }
  updateClusterBounds(payload.request_id);
  scheduleRenderEdges();
  scheduleRenderMinimap();
}

function handleSearchOrganized(payload) {
  const userNode = getUserNode(payload.request_id);
  if (!userNode) return;
  if (userNode.searchBadgeEl) userNode.searchBadgeEl.textContent = '已整理';
  if (userNode.searchQueryEl) {
    userNode.searchQueryEl.textContent = `搜索结果已由 ${escapeHtml(getDisplayName(payload.model || ''))} 整理`;
  }
  if (userNode.searchResultsEl && payload.organized_markdown) {
    const rendered = window.renderMarkdown ? window.renderMarkdown(payload.organized_markdown) : escapeHtml(payload.organized_markdown);
    userNode.searchResultsEl.innerHTML = `<div class="search-organized-content">${rendered}</div>`;
  }
  if (userNode.searchToggleBtn) userNode.searchToggleBtn.classList.remove('hidden');
  updateClusterBounds(payload.request_id);
  scheduleRenderEdges();
  scheduleRenderMinimap();
}

function handleConclusionStart(payload) {
  const cluster = requestClusters.get(payload.request_id);
  if (!cluster) return;
  const nodeId = `conclusion-${payload.request_id}`;

  if (nodes.has(nodeId)) {
    const existing = nodes.get(nodeId);
    existing.status = 'pending';
    existing.badge.textContent = '生成中...';
    existing.badge.className = 'badge conclusion-badge';
    existing.mdEl.textContent = '正在综合各模型讨论结果，生成最终结论文档...';
    existing.markdown = '';
    if (existing.retryBtn) existing.retryBtn.disabled = true;
    return;
  }

  if (cluster.conclusionNodeId && cluster.conclusionNodeId !== nodeId) {
    const oldNode = nodes.get(cluster.conclusionNodeId);
    if (oldNode) { oldNode.root.remove(); nodes.delete(cluster.conclusionNodeId); }
  }

  const modelNodes = cluster.modelNodeIds.map(id => nodes.get(id)).filter(Boolean);
  let cx = cluster.baseX - CONCLUSION_NODE_WIDTH / 2;
  let cy = cluster.modelY + MODEL_NODE_HEIGHT + 40;
  if (modelNodes.length > 0) {
    const lastModel = modelNodes[modelNodes.length - 1];
    cy = lastModel.y + MODEL_NODE_HEIGHT + 40;
  }

  createConclusionNode({
    nodeId,
    requestId: payload.request_id,
    x: cx,
    y: cy,
    model: payload.model || '',
    markdown: '',
    status: 'pending',
  });

  addEdge({
    id: `edge-user-${payload.request_id}-${nodeId}`,
    sourceId: `user-${payload.request_id}`,
    targetId: nodeId,
    type: 'conclusion_edge',
  });

  cluster.conclusionNodeId = nodeId;
  updateClusterBounds(payload.request_id);
  renderEdges();
  renderMinimap();
}

function handleConclusionDone(payload) {
  const nodeId = `conclusion-${payload.request_id}`;
  const node = nodes.get(nodeId);
  if (!node) {
    handleConclusionStart(payload);
    const created = nodes.get(nodeId);
    if (created) {
      created.markdown = payload.markdown || '';
      created.status = 'success';
      created.model = payload.model || '';
      created.badge.textContent = '已完成';
      created.badge.className = 'badge conclusion-badge ok';
      created.mdEl.innerHTML = window.renderMarkdown ? window.renderMarkdown(payload.markdown || '') : escapeHtml(payload.markdown || '');
      if (created.retryBtn) created.retryBtn.disabled = false;
    }
  } else {
    node.markdown = payload.markdown || '';
    node.status = 'success';
    node.model = payload.model || '';
    node.badge.textContent = '已完成';
    node.badge.className = 'badge conclusion-badge ok';
    node.mdEl.innerHTML = window.renderMarkdown ? window.renderMarkdown(payload.markdown || '') : escapeHtml(payload.markdown || '');
    if (node.retryBtn) node.retryBtn.disabled = false;
  }

  latestConclusionMarkdown = payload.markdown || '';
  latestConclusionRequestId = payload.request_id || '';
  updateConclusionHint();
  updateClusterBounds(payload.request_id);
  renderMinimap();
}

function handleConclusionError(payload) {
  const nodeId = `conclusion-${payload.request_id}`;
  const node = nodes.get(nodeId);
  if (node) {
    node.status = 'failed';
    node.badge.textContent = '失败';
    node.badge.className = 'badge conclusion-badge err';
    node.mdEl.textContent = payload.content || '结论生成失败';
    if (node.retryBtn) node.retryBtn.disabled = false;
  }
}

function createCluster({
  requestId,
  userMessage,
  discussionRounds,
  models: clusterModels,
  searchEnabled,
  thinkEnabled,
  parentRequestId,
  sourceModel,
  sourceRound,
}) {
  const activeModels = clusterModels.length ? clusterModels : models;
  const layout = placeCluster({
    modelCount: activeModels.length,
    parentRequestId,
    sourceModel,
  });

  const userNodeId = `user-${requestId}`;
  createUserNode({
    nodeId: userNodeId,
    requestId,
    x: layout.userX,
    y: layout.userY,
    content: userMessage,
    discussionRounds,
    searchEnabled,
    thinkEnabled,
    parentRequestId,
    sourceModel,
    sourceRound,
  });

  if (parentRequestId && sourceModel) {
    addEdge({
      id: `edge-model-${parentRequestId}-${sourceModel}-${userNodeId}`,
      sourceId: `model-${parentRequestId}-${sourceModel}`,
      targetId: userNodeId,
      type: 'branch_from_turn',
    });
  }

  const cluster = {
    requestId,
    kind: parentRequestId ? 'branch' : 'main',
    userNodeId,
    modelNodeIds: [],
    pendingModels: activeModels,
    modelsReady: !searchEnabled,
    modelStartX: layout.modelStartX,
    modelY: layout.modelY,
    baseX: layout.centerX,
    baseY: layout.modelY,
    discussionRounds,
    bbox: layout.bbox,
    parentRequestId,
    sourceModel,
    searchEnabled,
    isRunning: true,
    isCancelling: false,
  };
  requestClusters.set(requestId, cluster);
  if (!searchEnabled) {
    materializeClusterModels(requestId);
  }
  updateClusterBounds(requestId);
  renderEdges();
  renderMinimap();
}

function placeCluster({ modelCount, parentRequestId, sourceModel }) {
  const footprintWidth = Math.max(
    USER_NODE_WIDTH,
    modelCount * MODEL_NODE_WIDTH + Math.max(0, modelCount - 1) * CLUSTER_GAP_X
  );
  const estimatedUserHeight = USER_NODE_HEIGHT + 250;
  const footprintHeight = estimatedUserHeight + CLUSTER_GAP_Y + MODEL_NODE_HEIGHT;

  const viewportRect = viewportEl.getBoundingClientRect();
  const centerWorldX = (viewportRect.width / 2 - state.offsetX) / state.scale;
  const centerWorldY = (viewportRect.height / 2 - state.offsetY) / state.scale;

  let bbox;
  if (parentRequestId && sourceModel) {
    const parentNode = getModelNode(parentRequestId, sourceModel);
    const parentX = parentNode?.x || centerWorldX;
    const parentY = parentNode?.y || centerWorldY;
    const candidates = [
      { x: parentX, y: parentY + MODEL_NODE_HEIGHT + 180 },
      { x: parentX + MODEL_NODE_WIDTH + 160, y: parentY + 120 },
      { x: parentX - footprintWidth - 180, y: parentY + 120 },
      { x: parentX + MODEL_NODE_WIDTH + 160, y: parentY - 120 },
    ];
    bbox = findAvailableBox(candidates, footprintWidth, footprintHeight);
  } else {
    const topX = centerWorldX - footprintWidth / 2 + (clusterCount % 2) * 80;
    const topY = centerWorldY - 120 + clusterCount * 120;
    const candidates = [];
    for (let row = 0; row < 40; row += 1) {
      candidates.push({ x: topX, y: topY + row * (footprintHeight + 80) });
    }
    bbox = findAvailableBox(candidates, footprintWidth, footprintHeight);
    clusterCount += 1;
  }

  const userX = bbox.x + (bbox.width - USER_NODE_WIDTH) / 2;
  const modelRowWidth = modelCount * MODEL_NODE_WIDTH + Math.max(0, modelCount - 1) * CLUSTER_GAP_X;
  const modelStartX = bbox.x + (bbox.width - modelRowWidth) / 2;

  return {
    bbox,
    centerX: bbox.x + bbox.width / 2,
    userX,
    userY: bbox.y,
    modelStartX,
    modelY: bbox.y + USER_NODE_HEIGHT + CLUSTER_GAP_Y,
  };
}

function refreshAllClusterBounds() {
  for (const rid of requestClusters.keys()) {
    updateClusterBounds(rid);
  }
}

function findAvailableBox(candidates, width, height) {
  refreshAllClusterBounds();
  for (const candidate of candidates) {
    const bbox = { x: candidate.x, y: candidate.y, width, height };
    if (!hasClusterOverlap(bbox)) {
      return bbox;
    }
  }
  const fallbackY = Array.from(requestClusters.values()).reduce((maxY, cluster) => {
    return Math.max(maxY, cluster.bbox.y + cluster.bbox.height + 120);
  }, 0);
  return { x: 0, y: fallbackY, width, height };
}

function hasClusterOverlap(nextBox) {
  for (const cluster of requestClusters.values()) {
    const box = cluster.bbox;
    if (
      nextBox.x + nextBox.width + CLUSTER_PADDING < box.x ||
      box.x + box.width + CLUSTER_PADDING < nextBox.x ||
      nextBox.y + nextBox.height + CLUSTER_PADDING < box.y ||
      box.y + box.height + CLUSTER_PADDING < nextBox.y
    ) {
      continue;
    }
    return true;
  }
  return false;
}

function createUserNode({
  nodeId,
  requestId,
  x,
  y,
  content,
  discussionRounds,
  searchEnabled,
  thinkEnabled,
  parentRequestId,
  sourceModel,
  sourceRound,
}) {
  const root = document.createElement('section');
  root.className = 'node user-node';
  root.dataset.nodeId = nodeId;
  root.style.left = `${x}px`;
  root.style.top = `${y}px`;

  const branchText = parentRequestId && sourceModel
    ? `分支自 ${escapeHtml(getDisplayName(sourceModel))} · 第 ${sourceRound || 1} 轮`
    : `主问题 · ${discussionRounds} 轮讨论`;
  const branchChip = parentRequestId && sourceModel
    ? `<span class="info-chip branch-origin">来自 ${escapeHtml(getDisplayName(sourceModel))} · 第 ${sourceRound || 1} 轮</span>`
    : '';

  root.innerHTML = `
    <header class="node-header" data-drag-handle="true">
      <div class="node-title">
        <span class="node-dot"></span>
        <div>
          <strong>用户提问</strong>
          <div class="node-subtitle">请求 ${requestId.slice(0, 8)} · ${branchText}</div>
        </div>
      </div>
      <span class="badge">已发送</span>
    </header>
    <div class="user-content">
      <div class="user-message"></div>
      <div class="user-flags">
        <span class="info-chip">${discussionRounds} 轮</span>
        <span class="info-chip">联网 ${searchEnabled ? '开启' : '关闭'}</span>
        <span class="info-chip">思考 ${thinkEnabled ? '开启' : '关闭'}</span>
        ${branchChip}
      </div>
      <div class="user-node-actions">
        <button type="button" class="small-btn danger user-cancel hidden">停止</button>
        <div class="branch-meta">搜索完成后再展开模型执行，避免视觉阻塞。</div>
      </div>
      <section class="search-panel ${searchEnabled ? '' : 'hidden'}">
        <div class="search-head">
          <div>
            <strong>联网搜索</strong>
            <div class="search-query">等待搜索</div>
          </div>
          <div class="search-head-right">
            <button type="button" class="search-toggle hidden">收起结果</button>
            <span class="badge search-badge">待命</span>
          </div>
        </div>
        <div class="search-results"></div>
      </section>
      <section class="cluster-outline hidden">
        <div class="turn-meta"><span>本轮摘要</span></div>
        <div class="outline-list"></div>
      </section>
    </div>
  `;
  root.querySelector('.user-message').textContent = content;
  stageEl.appendChild(root);
  bindNodeInteractions(root, nodeId, requestId);
  const node = {
    nodeId,
    requestId,
    type: 'user',
    x,
    y,
    root,
    badge: root.querySelector('.badge'),
    cancelBtn: root.querySelector('.user-cancel'),
    searchPanel: root.querySelector('.search-panel'),
    searchQueryEl: root.querySelector('.search-query'),
    searchResultsEl: root.querySelector('.search-results'),
    searchBadgeEl: root.querySelector('.search-badge'),
    searchToggleBtn: root.querySelector('.search-toggle'),
    searchCollapsed: false,
    outlinePanel: root.querySelector('.cluster-outline'),
    outlineListEl: root.querySelector('.outline-list'),
  };
  nodes.set(nodeId, node);
  node.cancelBtn.addEventListener('click', () => cancelRequest(requestId));
  node.searchToggleBtn?.addEventListener('click', () => setSearchCollapsed(node, !node.searchCollapsed));
}

function createModelNode({ nodeId, requestId, model, x, y }) {
  const root = document.createElement('section');
  root.className = 'node';
  root.dataset.nodeId = nodeId;
  root.style.left = `${x}px`;
  root.style.top = `${y}px`;
  root.innerHTML = `
    <header class="node-header" data-drag-handle="true">
      <div class="node-title">
        <span class="node-dot"></span>
        <div>
          <strong>${escapeHtml(getDisplayName(model))}</strong>
          <div class="node-subtitle">模型 ID: ${escapeHtml(model)}</div>
        </div>
      </div>
      <span class="badge">待命</span>
    </header>
    <nav class="tabs"></nav>
    <div class="tab-panels"></div>
    <div class="node-actions">
      <div class="node-actions-left">
        <button type="button" class="small-btn copy-current">复制当前</button>
        <button type="button" class="small-btn copy-all">复制全部</button>
        <button type="button" class="small-btn retry-current">重试当前</button>
        <button type="button" class="small-btn branch-toggle">分支</button>
      </div>
      <div class="branch-meta">默认基于当前 tab 延续上一轮结论与压缩过程</div>
    </div>
    <div class="branch-box hidden">
      <textarea class="branch-input" rows="1" placeholder="继续上一轮：补充动作、质疑、延展或下一步指令"></textarea>
      <div class="branch-controls">
        <button type="button" class="small-btn branch-cancel">取消</button>
        <button type="button" class="small-btn branch-send">发送分支</button>
      </div>
    </div>
  `;
  stageEl.appendChild(root);
  bindNodeInteractions(root, nodeId, requestId);

  const node = {
    nodeId,
    requestId,
    type: 'model',
    model,
    x,
    y,
    root,
    badge: root.querySelector('.badge'),
    tabsEl: root.querySelector('.tabs'),
    panelsEl: root.querySelector('.tab-panels'),
    turns: new Map(),
    activeRound: null,
    branchBox: root.querySelector('.branch-box'),
    branchInput: root.querySelector('.branch-input'),
  };
  nodes.set(nodeId, node);

  root.querySelector('.branch-toggle').addEventListener('click', () => {
    openBranchComposer(node);
  });
  root.querySelector('.branch-cancel').addEventListener('click', () => {
    node.branchBox.classList.add('hidden');
    node.branchInput.value = '';
  });
  root.querySelector('.branch-send').addEventListener('click', () => {
    sendBranch(node);
  });
  root.querySelector('.copy-current').addEventListener('click', (event) => {
    copyCurrentTurn(node, event.currentTarget);
  });
  root.querySelector('.copy-all').addEventListener('click', (event) => {
    copyAllTurns(node, event.currentTarget);
  });
  root.querySelector('.retry-current').addEventListener('click', (event) => {
    retryCurrentTurn(node, event.currentTarget);
  });
  node.branchInput.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      sendBranch(node);
    }
  });
}

function createConclusionNode({ nodeId, requestId, x, y, model, markdown, status }) {
  const root = document.createElement('section');
  root.className = 'node conclusion-node';
  root.dataset.nodeId = nodeId;
  root.style.left = `${x}px`;
  root.style.top = `${y}px`;

  const modelLabel = model ? escapeHtml(getDisplayName(model)) : '系统';
  const statusLabel = status === 'pending' ? '生成中...' : (status === 'success' ? '已完成' : '失败');
  const statusClass = status === 'pending' ? '' : (status === 'success' ? 'ok' : 'err');

  root.innerHTML = `
    <header class="node-header conclusion-header" data-drag-handle="true">
      <div class="node-title">
        <span class="node-dot conclusion-dot"></span>
        <div>
          <strong>最终结论</strong>
          <div class="node-subtitle">由 ${modelLabel} 综合生成</div>
        </div>
      </div>
      <span class="badge conclusion-badge ${statusClass}">${statusLabel}</span>
    </header>
    <div class="conclusion-body">
      <div class="md conclusion-md"></div>
    </div>
    <div class="conclusion-actions">
      <button type="button" class="small-btn conclusion-copy">复制 Markdown</button>
      <button type="button" class="small-btn conclusion-retry">重试结论</button>
      <button type="button" class="small-btn conclusion-attach ${conclusionAutoAttach ? 'active' : ''}">
        ${conclusionAutoAttach ? '自动注入：开' : '自动注入：关'}
      </button>
    </div>
  `;

  const mdEl = root.querySelector('.conclusion-md');
  if (markdown && status === 'success') {
    mdEl.innerHTML = window.renderMarkdown ? window.renderMarkdown(markdown) : escapeHtml(markdown);
  } else if (status === 'pending') {
    mdEl.textContent = '正在综合各模型讨论结果，生成最终结论文档...';
  } else {
    mdEl.textContent = '结论生成失败';
  }

  stageEl.appendChild(root);
  bindNodeInteractions(root, nodeId, requestId);

  const node = {
    nodeId,
    requestId,
    type: 'conclusion',
    x,
    y,
    root,
    badge: root.querySelector('.conclusion-badge'),
    retryBtn: root.querySelector('.conclusion-retry'),
    mdEl,
    markdown: markdown || '',
    model: model || '',
    status: status || 'pending',
  };
  nodes.set(nodeId, node);
  if (node.retryBtn) node.retryBtn.disabled = node.status === 'pending';

  root.querySelector('.conclusion-copy').addEventListener('click', async (e) => {
    const text = node.markdown || '';
    if (!text) { flashButtonLabel(e.currentTarget, '无内容'); return; }
    try {
      await navigator.clipboard.writeText(text);
      flashButtonLabel(e.currentTarget, '已复制');
    } catch { flashButtonLabel(e.currentTarget, '复制失败'); }
  });

  root.querySelector('.conclusion-attach').addEventListener('click', (e) => {
    conclusionAutoAttach = !conclusionAutoAttach;
    e.currentTarget.textContent = conclusionAutoAttach ? '自动注入：开' : '自动注入：关';
    e.currentTarget.classList.toggle('active', conclusionAutoAttach);
    updateConclusionHint();
  });

  root.querySelector('.conclusion-retry').addEventListener('click', (e) => {
    retryConclusion(requestId, e.currentTarget);
  });

  return node;
}

function retryConclusion(requestId, button) {
  if (!requestId || socket.readyState !== WebSocket.OPEN) {
    flashButtonLabel(button, '不可重试');
    return;
  }
  try {
    socket.send(
      JSON.stringify({
        action: 'retry_conclusion',
        source_request_id: requestId,
        canvas_id: currentCanvasId,
      })
    );
  } catch (_e) {
    flashButtonLabel(button, '发送失败');
    return;
  }
  button.disabled = true;
  flashButtonLabel(button, '已发起');
}

function getConclusionNode(requestId) {
  return nodes.get(`conclusion-${requestId}`) || null;
}

function updateConclusionHint() {
  const hintEl = document.getElementById('conclusionHint');
  if (!hintEl) return;
  if (conclusionAutoAttach && latestConclusionMarkdown) {
    const charCount = latestConclusionMarkdown.length;
    hintEl.textContent = `下一轮将附带结论文档（${charCount} 字）`;
    hintEl.classList.remove('hidden');
  } else {
    hintEl.classList.add('hidden');
    hintEl.textContent = '';
  }
}

function bindNodeInteractions(root, nodeId, requestId) {
  bindNodeDrag(root, nodeId, requestId);
  root.addEventListener('mouseenter', () => {
    hoverNodeId = nodeId;
    renderSelectionState();
  });
  root.addEventListener('mouseleave', () => {
    if (hoverNodeId === nodeId) {
      hoverNodeId = null;
      renderSelectionState();
    }
  });
  root.addEventListener('click', (event) => {
    if (event.target.closest('button, textarea, a, input, select')) return;
    state.selectionSource = 'click';
    if (event.shiftKey) {
      if (selectedNodeIds.has(nodeId)) {
        selectedNodeIds.delete(nodeId);
      } else {
        selectedNodeIds.add(nodeId);
      }
      selectedNodeId = selectedNodeIds.size === 1 ? Array.from(selectedNodeIds)[0] : nodeId;
    } else {
      selectedNodeIds.clear();
      selectedNodeIds.add(nodeId);
      selectedNodeId = nodeId;
    }
    if (!selectedNodeIds.size) {
      state.selectionSource = 'none';
    }
    renderSelectionState();
    updateComposerHint();
  });
}

async function copyCurrentTurn(node, button) {
  const round = node.activeRound || getLatestRound(node);
  const turn = node.turns.get(round);
  const text = turn?.raw?.trim();
  if (!text) {
    flashButtonLabel(button, '无内容');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    flashButtonLabel(button, '已复制');
  } catch {
    flashButtonLabel(button, '复制失败');
  }
}

async function copyAllTurns(node, button) {
  const rounds = Array.from(node.turns.keys()).sort((a, b) => a - b);
  const text = rounds
    .map((round) => {
      const turn = node.turns.get(round);
      return turn?.raw?.trim() ? `## 第 ${round} 轮\n\n${turn.raw.trim()}` : '';
    })
    .filter(Boolean)
    .join('\n\n');
  if (!text) {
    flashButtonLabel(button, '无内容');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    flashButtonLabel(button, '已复制');
  } catch {
    flashButtonLabel(button, '复制失败');
  }
}

function flashButtonLabel(button, label) {
  const original = button.textContent;
  button.textContent = label;
  window.setTimeout(() => {
    button.textContent = original;
  }, 1600);
}

function retryCurrentTurn(node, button) {
  const round = node.activeRound || getLatestRound(node);
  const turn = node.turns.get(round);
  if (!round || !turn?.raw?.trim() || socket.readyState !== WebSocket.OPEN) {
    flashButtonLabel(button, '不可重试');
    return;
  }
  try {
    socket.send(
      JSON.stringify({
        action: 'retry_model',
        source_request_id: node.requestId,
        source_model: node.model,
        source_round: round,
        canvas_id: currentCanvasId,
      })
    );
  } catch (_e) { return; }
  flashButtonLabel(button, '已发起');
}

function sendBranch(node, overrideMessage = null) {
  const message = String(overrideMessage ?? node.branchInput.value).trim();
  if (!message || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  const round = node.activeRound || getLatestRound(node);
  if (!round) return;
  try {
    socket.send(
      JSON.stringify({
        action: 'branch_chat',
        message,
        source_request_id: node.requestId,
        source_model: node.model,
        source_round: round,
        discussion_rounds: Number(discussionRoundsEl.value || 2),
        search_enabled: getSearchMode(),
        think_enabled: thinkToggleEl.checked,
        canvas_id: currentCanvasId,
      })
    );
  } catch (_e) { return; }
  node.branchInput.value = '';
  node.branchBox.classList.add('hidden');
}


function getLatestRound(node) {
  return Math.max(...Array.from(node.turns.keys()), 1);
}

function ensureModelTurn(requestId, model, round) {
  const node = getModelNode(requestId, model);
  if (!node) return null;
  if (node.turns.has(round)) {
    return node.turns.get(round);
  }

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'tab-btn';
  button.dataset.roundLabel = `第 ${round} 轮`;
  button.innerHTML = `<span class="tab-btn-main">第 ${round} 轮</span><span class="tab-btn-summary">等待摘要</span>`;
  button.addEventListener('click', () => activateTurn(requestId, model, round));

  const panel = document.createElement('section');
  panel.className = 'tab-panel';
  panel.innerHTML = `
    <div class="turn-meta">状态：<span class="turn-state">等待中</span><span class="turn-summary-chip">摘要：等待摘要</span></div>
    <div class="md"></div>
  `;

  node.tabsEl.appendChild(button);
  node.panelsEl.appendChild(panel);

  const turn = {
    round,
    btn: button,
    panel,
    stateEl: panel.querySelector('.turn-state'),
    summaryEl: button.querySelector('.tab-btn-summary'),
    summaryChipEl: panel.querySelector('.turn-summary-chip'),
    mdEl: panel.querySelector('.md'),
    raw: '',
    summary: '等待摘要',
    renderTimer: null,
  };
  node.turns.set(round, turn);
  return turn;
}

function activateTurn(requestId, model, round) {
  const node = getModelNode(requestId, model);
  if (!node || !node.turns.has(round)) return;
  node.activeRound = round;

  for (const turn of node.turns.values()) {
    const active = turn.round === round;
    turn.btn.classList.toggle('active', active);
    turn.panel.classList.toggle('active', active);
  }
  refreshClusterOutline(requestId);
  updateComposerHint();
}

function appendTurnText(requestId, model, round, chunk) {
  const turn = ensureModelTurn(requestId, model, round);
  if (!turn) return;
  turn.raw += chunk;
  scheduleTurnRender(turn);
}

function scheduleTurnRender(turn, force = false) {
  if (!turn) return;

  const render = () => {
    turn.renderTimer = null;
    updateTurnSummary(turn);
    turn.mdEl.innerHTML = window.renderMarkdown(turn.raw);
    turn.panel.scrollTop = turn.panel.scrollHeight;
  };

  if (force) {
    if (turn.renderTimer) {
      window.clearTimeout(turn.renderTimer);
      turn.renderTimer = null;
    }
    render();
    return;
  }

  if (turn.renderTimer) return;
  const renderDelay = turn.raw.length > 6000 ? 110 : 72;
  turn.renderTimer = window.setTimeout(render, renderDelay);
}

function flushTurnRender(requestId, model, round) {
  const turn = ensureModelTurn(requestId, model, round);
  if (!turn) return;
  scheduleTurnRender(turn, true);
}

function setTurnState(requestId, model, round, text) {
  const turn = ensureModelTurn(requestId, model, round);
  if (!turn) return;
  turn.stateEl.textContent = text;
}

function setNodeState(requestId, model, text) {
  const node = getModelNode(requestId, model);
  if (!node) return;
  node.badge.textContent = text;
  refreshClusterOutline(requestId);
}

function getModelNode(requestId, model) {
  return nodes.get(`model-${requestId}-${model}`) || null;
}

function getUserNode(requestId) {
  return nodes.get(`user-${requestId}`) || null;
}

function queueOrApplySearchEvent(payload) {
  const userNode = getUserNode(payload.request_id);
  if (!userNode) {
    const queued = pendingSearchEvents.get(payload.request_id) || [];
    queued.push(payload);
    pendingSearchEvents.set(payload.request_id, queued);
    return;
  }
  applySearchEvent(userNode, payload);
}

function flushPendingSearchEvents(requestId) {
  const queued = pendingSearchEvents.get(requestId) || [];
  const userNode = getUserNode(requestId);
  if (!userNode) return;
  for (const payload of queued) {
    applySearchEvent(userNode, payload);
  }
  pendingSearchEvents.delete(requestId);
}

function applySearchEvent(userNode, payload) {
  if (!userNode.searchPanel) return;
  userNode.searchPanel.classList.remove('hidden');
  if (payload.type === 'search_started') {
    setClusterState(userNode.requestId, { badgeText: '搜索中', isRunning: true });
    userNode.searchQueryEl.textContent = payload.query || '正在搜索';
    userNode.searchBadgeEl.textContent = '搜索中';
    userNode.searchToggleBtn?.classList.add('hidden');
    userNode.searchResultsEl.innerHTML = '<div class="search-item-snippet">正在通过 Firecrawl 获取实时网页结果...</div>';
    setSearchCollapsed(userNode, false);
    updateClusterBounds(userNode.requestId);
    scheduleRenderEdges();
    scheduleRenderMinimap();
    return;
  }
  if (payload.type === 'search_error') {
    materializeClusterModels(userNode.requestId);
    setClusterState(userNode.requestId, { badgeText: '搜索失败，继续执行', isRunning: true });
    userNode.searchBadgeEl.textContent = '失败';
    userNode.searchToggleBtn?.classList.remove('hidden');
    userNode.searchResultsEl.innerHTML = `<div class="search-item-snippet">${escapeHtml(payload.content || '搜索失败')}</div>`;
    setSearchCollapsed(userNode, false);
    updateClusterBounds(userNode.requestId);
    scheduleRenderEdges();
    scheduleRenderMinimap();
    return;
  }
  materializeClusterModels(userNode.requestId);
  userNode.searchQueryEl.textContent = `${payload.query || ''} · ${payload.count || 0} 条结果`;
  userNode.searchBadgeEl.textContent = '已完成';
  setClusterState(userNode.requestId, { badgeText: '搜索完成，等待模型', isRunning: true });
  const results = payload.results || [];
  userNode.searchResultsEl.innerHTML = results
    .map(
      (item) => `
        <article class="search-item">
          <div class="search-item-title">${escapeHtml(item.title || 'Untitled')}</div>
          <a class="search-item-link" href="${escapeAttribute(item.url || '#')}" target="_blank" rel="noreferrer">${escapeHtml(item.url || '')}</a>
          <div class="search-item-snippet">${escapeHtml(item.snippet || '无摘要')}</div>
        </article>
      `
    )
    .join('');
  userNode.searchToggleBtn?.classList.remove('hidden');
  setSearchCollapsed(userNode, results.length > 2);
  updateClusterBounds(userNode.requestId);
  scheduleRenderEdges();
  scheduleRenderMinimap();
}

function addEdge(edge) {
  edges.set(edge.id, edge);
}

function renderEdges() {
  const activeNodeId = hoverNodeId || selectedNodeId;
  const paths = [];
  for (const edge of edges.values()) {
    const source = nodes.get(edge.sourceId);
    const target = nodes.get(edge.targetId);
    if (!source || !target) continue;
    const start = getAnchorPoint(source, edge.type, true);
    const end = getAnchorPoint(target, edge.type, false);
    const path = buildBezierPath(start, end);
    const classes = ['edge-path'];
    if (edge.type === 'branch_from_turn') classes.push('branch');
    if (activeNodeId) {
      if (edge.sourceId === activeNodeId || edge.targetId === activeNodeId) {
        classes.push('active');
      } else {
        classes.push('dimmed');
      }
    }
    paths.push(`<path class="${classes.join(' ')}" d="${path}" />`);
  }
  edgeLayerEl.innerHTML = paths.join('');
}

function renderSelectionState() {
  const selected = selectedNodeIds;
  const primaryNodeId = hoverNodeId || selectedNodeId || (selected.size === 1 ? Array.from(selected)[0] : null);

  for (const node of nodes.values()) {
    const isSelected = selected.has(node.nodeId);
    node.root.classList.toggle('active', hoverNodeId === node.nodeId);
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

function isNodeAdjacent(nodeId, activeNodeId) {
  if (!activeNodeId) return false;
  for (const edge of edges.values()) {
    if ((edge.sourceId === nodeId && edge.targetId === activeNodeId) || (edge.targetId === nodeId && edge.sourceId === activeNodeId)) {
      return true;
    }
  }
  return false;
}

function getNodeDimensions(node) {
  const w = node.root.offsetWidth;
  const h = node.root.offsetHeight;
  if (w > 0 && h > 0) {
    node.cachedWidth = w;
    node.cachedHeight = h;
    return { width: w, height: h };
  }
  const defaultWidth = node.type === 'user' ? USER_NODE_WIDTH : (node.type === 'conclusion' ? CONCLUSION_NODE_WIDTH : MODEL_NODE_WIDTH);
  const defaultHeight = node.type === 'user' ? USER_NODE_HEIGHT : (node.type === 'conclusion' ? CONCLUSION_NODE_HEIGHT : MODEL_NODE_HEIGHT);
  return {
    width: node.cachedWidth || defaultWidth,
    height: node.cachedHeight || defaultHeight,
  };
}

function getAnchorPoint(node, edgeType, isSource) {
  const { width, height } = getNodeDimensions(node);

  if (edgeType === 'branch_from_turn') {
    if (isSource) {
      return { x: node.x + width / 2, y: node.y + height };
    }
    return { x: node.x + width / 2, y: node.y };
  }

  if (isSource) {
    return { x: node.x + width / 2, y: node.y + height };
  }
  return { x: node.x + width / 2, y: node.y };
}

function buildBezierPath(start, end) {
  const horizontal = Math.abs(end.x - start.x);
  const vertical = Math.abs(end.y - start.y);
  const bend = Math.max(70, Math.max(horizontal, vertical) * 0.32);
  const c1x = start.x;
  const c1y = start.y + bend;
  const c2x = end.x;
  const c2y = end.y - bend * 0.5;
  return `M ${start.x} ${start.y} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${end.x} ${end.y}`;
}

function renderMinimap() {
  const allNodes = Array.from(nodes.values());
  if (!allNodes.length) {
    minimapNodesEl.innerHTML = '';
    minimapViewportEl.style.display = 'none';
    return;
  }

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;

  for (const node of allNodes) {
    const { width, height } = getNodeDimensions(node);
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x + width);
    maxY = Math.max(maxY, node.y + height);
  }

  const padding = 80;
  minX -= padding;
  minY -= padding;
  maxX += padding;
  maxY += padding;

  const worldWidth = Math.max(1, maxX - minX);
  const worldHeight = Math.max(1, maxY - minY);
  const contentWidth = minimapContentEl.clientWidth;
  const contentHeight = minimapContentEl.clientHeight;
  const scale = Math.min(contentWidth / worldWidth, contentHeight / worldHeight);
  const offsetX = (contentWidth - worldWidth * scale) / 2;
  const offsetY = (contentHeight - worldHeight * scale) / 2;

  minimapNodesEl.innerHTML = '';
  for (const node of allNodes) {
    const { width, height } = getNodeDimensions(node);
    const left = offsetX + (node.x - minX) * scale;
    const top = offsetY + (node.y - minY) * scale;
    const active = node.nodeId === (hoverNodeId || selectedNodeId);
    const running = Boolean(getCluster(node.requestId)?.isRunning || getCluster(node.requestId)?.isCancelling);
    const el = document.createElement('div');
    el.className = `minimap-node ${node.type}${active ? ' active' : ''}${running ? ' running' : ''}`;
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.style.width = `${Math.max(8, width * scale)}px`;
    el.style.height = `${Math.max(6, height * scale)}px`;
    minimapNodesEl.appendChild(el);
  }

  const viewportRect = viewportEl.getBoundingClientRect();
  const visibleLeft = (-state.offsetX) / state.scale;
  const visibleTop = (-state.offsetY) / state.scale;
  const visibleWidth = viewportRect.width / state.scale;
  const visibleHeight = viewportRect.height / state.scale;

  minimapViewportEl.style.display = 'block';
  minimapViewportEl.style.left = `${offsetX + (visibleLeft - minX) * scale}px`;
  minimapViewportEl.style.top = `${offsetY + (visibleTop - minY) * scale}px`;
  minimapViewportEl.style.width = `${Math.max(18, visibleWidth * scale)}px`;
  minimapViewportEl.style.height = `${Math.max(14, visibleHeight * scale)}px`;

  minimapContentEl.onclick = (event) => {
    const rect = minimapContentEl.getBoundingClientRect();
    const localX = event.clientX - rect.left;
    const localY = event.clientY - rect.top;
    const worldX = minX + (localX - offsetX) / scale;
    const worldY = minY + (localY - offsetY) / scale;
    centerViewportOn(worldX, worldY);
  };
}

function centerViewportOn(worldX, worldY) {
  const rect = viewportEl.getBoundingClientRect();
  state.offsetX = rect.width / 2 - worldX * state.scale;
  state.offsetY = rect.height / 2 - worldY * state.scale;
  applyTransform();
  renderMinimap();
}

function clearCanvas() {
  // Clean up render timers before clearing
  for (const node of nodes.values()) {
    if (node.turns) {
      for (const turn of node.turns.values()) {
        if (turn.renderTimer) {
          window.clearTimeout(turn.renderTimer);
          turn.renderTimer = null;
        }
      }
    }
  }
  // Clean up position save timers
  for (const timer of positionSaveTimers.values()) {
    clearTimeout(timer);
  }
  positionSaveTimers.clear();

  stageEl.innerHTML = '';
  edgeLayerEl.innerHTML = '';
  requestClusters.clear();
  nodes.clear();
  edges.clear();
  pendingSearchEvents.clear();
  selectedNodeIds.clear();
  clearSelectionSummary();
  state.selectionSource = 'none';
  clusterCount = 0;
  latestRequestId = null;
  latestConclusionMarkdown = '';
  latestConclusionRequestId = '';
  hoverNodeId = null;
  selectedNodeId = null;
  selectionActionsEl?.classList.add('hidden');
  setSaveStatus('');
  applyTransform();
  renderMinimap();
  updateComposerHint();
  updateConclusionHint();
  refreshStatus();
}

function bindNodeDrag(root, nodeId, requestId) {
  const handle = root.querySelector('[data-drag-handle="true"]');
  if (!handle) return;

  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.stopPropagation();

    const node = nodes.get(nodeId);
    if (!node) return;

    state.draggingNodeId = nodeId;
    state.dragStartX = event.clientX;
    state.dragStartY = event.clientY;
    state.originNodeX = node.x;
    state.originNodeY = node.y;
    root.classList.add('dragging');
    handle.setPointerCapture(event.pointerId);
  });

  handle.addEventListener('pointermove', (event) => {
    if (state.draggingNodeId !== nodeId) return;
    const node = nodes.get(nodeId);
    if (!node) return;

    const dx = (event.clientX - state.dragStartX) / state.scale;
    const dy = (event.clientY - state.dragStartY) / state.scale;
    node.x = state.originNodeX + dx;
    node.y = state.originNodeY + dy;
    node.root.style.left = `${node.x}px`;
    node.root.style.top = `${node.y}px`;
    updateClusterBounds(requestId);
    scheduleRenderEdges();
    scheduleRenderMinimap();
    updateSelectionActions();
  });

  function stopDrag(event) {
    if (state.draggingNodeId !== nodeId) return;
    handle.releasePointerCapture?.(event.pointerId);
    state.draggingNodeId = null;
    root.classList.remove('dragging');
    updateClusterBounds(requestId);
    scheduleRenderEdges();
    scheduleRenderMinimap();
    schedulePositionSave(requestId);
  }

  handle.addEventListener('pointerup', stopDrag);
  handle.addEventListener('pointercancel', stopDrag);
}

function updateClusterBounds(requestId) {
  const cluster = requestClusters.get(requestId);
  if (!cluster) return;
  const clusterNodeIds = [cluster.userNodeId, ...cluster.modelNodeIds];
  if (cluster.conclusionNodeId) clusterNodeIds.push(cluster.conclusionNodeId);
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;

  for (const nodeId of clusterNodeIds) {
    const node = nodes.get(nodeId);
    if (!node) continue;
    const { width, height } = getNodeDimensions(node);
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x + width);
    maxY = Math.max(maxY, node.y + height);
  }

  if (Number.isFinite(minX)) {
    cluster.bbox = {
      x: minX,
      y: minY,
      width: maxX - minX,
      height: maxY - minY,
    };
    cluster.baseX = minX + cluster.bbox.width / 2;
    cluster.baseY = minY;
  }
}

function bindCanvasPan() {
  window.addEventListener('blur', () => {
    state.panning = false;
    viewportEl.classList.remove('panning');
  });

  viewportEl.addEventListener('pointerdown', (event) => {
    if (event.target.closest('.node, .minimap, .selection-actions, .needs-setup-banner')) return;
    if (event.button !== 0) return;

    state.panning = true;
    state.panStartX = event.clientX;
    state.panStartY = event.clientY;
    state.originOffsetX = state.offsetX;
    state.originOffsetY = state.offsetY;
    viewportEl.classList.add('panning');
    viewportEl.setPointerCapture(event.pointerId);
  });

  viewportEl.addEventListener('pointermove', (event) => {
    if (!state.panning) return;
    state.offsetX = state.originOffsetX + (event.clientX - state.panStartX);
    state.offsetY = state.originOffsetY + (event.clientY - state.panStartY);
    applyTransform();
    scheduleRenderMinimap();
  });

  function stopPan(event) {
    if (!state.panning) return;
    const moved = Math.abs(event.clientX - state.panStartX) > 4 ||
                  Math.abs(event.clientY - state.panStartY) > 4;
    state.panning = false;
    viewportEl.classList.remove('panning');
    try { viewportEl.releasePointerCapture(event.pointerId); } catch (_) {}

    if (!moved && !event.target.closest('.node')) {
      clearSelection();
    }
  }

  viewportEl.addEventListener('pointerup', stopPan);
  viewportEl.addEventListener('pointercancel', stopPan);

  viewportEl.addEventListener(
    'wheel',
    (event) => {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        // Pinch-to-zoom (trackpad) or Ctrl+scroll
        const factor = event.deltaY < 0 ? 1.02 : 0.98;
        zoomAtPoint(factor, event.clientX, event.clientY);
      } else {
        // Two-finger scroll = pan
        state.offsetX -= event.deltaX;
        state.offsetY -= event.deltaY;
        applyTransform();
        scheduleRenderMinimap();
      }
    },
    { passive: false }
  );
}

function zoomAtPoint(factor, clientX, clientY) {
  const rect = viewportEl.getBoundingClientRect();
  const pointerX = clientX - rect.left;
  const pointerY = clientY - rect.top;
  const worldX = (pointerX - state.offsetX) / state.scale;
  const worldY = (pointerY - state.offsetY) / state.scale;

  state.scale = clamp(state.scale * factor, 0.2, 1.8);
  state.offsetX = pointerX - worldX * state.scale;
  state.offsetY = pointerY - worldY * state.scale;
  applyTransform();
  scheduleRenderMinimap();
}

function setZoom(nextScale) {
  const rect = viewportEl.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const factor = nextScale / state.scale;
  zoomAtPoint(factor, centerX, centerY);
}

function focusCluster(requestId) {
  const cluster = requestClusters.get(requestId);
  if (!cluster) return;
  centerViewportOn(cluster.bbox.x + cluster.bbox.width / 2, cluster.bbox.y + cluster.bbox.height / 2);
}

function applyTransform() {
  const transform = `translate(${state.offsetX}px, ${state.offsetY}px) scale(${state.scale})`;
  stageEl.style.transform = transform;
  gridEl.style.transform = transform;
  edgeLayerEl.style.transform = transform;
  zoomResetBtn.textContent = `${Math.round(state.scale * 100)}%`;
  updateSelectionActions();
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function toggleSidebar(show) {
  const shell = document.querySelector('.shell');
  if (typeof show !== 'boolean') show = shell.classList.contains('sidebar-collapsed');
  shell.classList.toggle('sidebar-collapsed', !show);
  shell.classList.toggle('sidebar-open', show);
  document.documentElement.style.setProperty('--sidebar-width', show ? '260px' : '0px');
}

function sendMessage() {
  const message = messageInput.value.trim();
  if (!message || socket.readyState !== WebSocket.OPEN) {
    return;
  }

  const selected = getSelectedContextNodes();
  if (selected.length === 1 && selected[0].type === 'model' && !shouldUseSelectionSummary(selected)) {
    sendBranch(selected[0], message);
    messageInput.value = '';
    autoResizeComposer();
    messageInput.focus();
    return;
  }

  let contextBundle = '';
  if (shouldUseSelectionSummary(selected)) {
    const rawBundle = buildContextBundleFromSelection();
    const currentKey = `${selected.map((node) => node.nodeId).join('|')}::${rawBundle}`;
    if (selectionSummaryState.text && selectionSummaryState.key === currentKey) {
      contextBundle = [
        `以下是 ${getSelectionSummaryModelLabel(selectionSummaryState.model)} 对用户圈选的 ${selected.length} 个节点做的压缩总结，请基于这些上下文继续回答：`,
        selectionSummaryState.text,
      ].join('\n\n');
    } else {
      contextBundle = rawBundle;
    }
  }

  if (!contextBundle && conclusionAutoAttach && latestConclusionRequestId) {
    const conclusionNode = getConclusionNode(latestConclusionRequestId);
    const conclusionMd = conclusionNode?.markdown || latestConclusionMarkdown || '';
    if (conclusionMd) {
      let clipped = conclusionMd;
      if (clipped.length > CONCLUSION_CONTEXT_MAX_CHARS) {
        clipped = clipped.slice(0, CONCLUSION_CONTEXT_MAX_CHARS) + '\n\n[结论文档已截断]';
      }
      contextBundle = `以下是上一轮多模型讨论的最终结论文档，请基于此上下文继续回答：\n\n${clipped}`;
    }
  }

  const suffix = contextBundle ? `\n\n用户新的继续问题：${message}` : '';
  const maxContextLength = Math.max(0, MAX_MESSAGE_LENGTH - suffix.length);
  let clippedContext = contextBundle;
  if (contextBundle && contextBundle.length > maxContextLength) {
    clippedContext = `${contextBundle.slice(0, Math.max(0, maxContextLength - 12))}\n\n[上下文已截断]`;
  }
  const finalMessage = clippedContext ? `${clippedContext}${suffix}` : message;

  try {
    socket.send(
      JSON.stringify({
        action: 'chat',
        message: finalMessage,
        discussion_rounds: Number(discussionRoundsEl.value || 2),
        search_enabled: getSearchMode(),
        think_enabled: thinkToggleEl.checked,
        canvas_id: currentCanvasId,
      })
    );
  } catch (_e) { return; }

  messageInput.value = '';
  autoResizeComposer();
  messageInput.focus();
}
function autoResizeComposer() {
  messageInput.style.height = 'auto';
  const nextHeight = Math.min(Math.max(messageInput.scrollHeight, 46), 108);
  messageInput.style.height = `${nextHeight}px`;
}

function escapeHtml(value) {
  return window.escapeHtmlShared ? window.escapeHtmlShared(value) : String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll('`', '&#096;');
}

selectionContinueBtn?.addEventListener('click', () => {
  messageInput.focus();
});
selectionBranchBtn?.addEventListener('click', () => {
  const selection = getSelectedContextNodes();
  if (selection.length === 1 && selection[0].type === 'model') {
    openBranchComposer(selection[0]);
  }
});
selectionClearBtn?.addEventListener('click', clearSelection);
window.addEventListener('resize', updateSelectionActions);
sendBtn.addEventListener('click', sendMessage);
clearBtn.addEventListener('click', () => {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ action: 'clear', canvas_id: currentCanvasId }));
  }
});
if (sidebarLogoutBtn) {
  sidebarLogoutBtn.addEventListener('click', async () => {
    try {
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } finally {
      window.location.href = '/login';
    }
  });
}
fitBtn.addEventListener('click', () => {
  if (latestRequestId) {
    focusCluster(latestRequestId);
  }
});
zoomInBtn.addEventListener('click', () => setZoom(clamp(state.scale * 1.12, 0.2, 1.8)));
zoomOutBtn.addEventListener('click', () => setZoom(clamp(state.scale * 0.88, 0.2, 1.8)));
zoomResetBtn.addEventListener('click', () => setZoom(DEFAULT_SCALE));
messageInput.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    sendMessage();
  }
});
messageInput.addEventListener('input', autoResizeComposer);
if (sidebarCollapseBtn) sidebarCollapseBtn.addEventListener('click', () => toggleSidebar(false));
if (sidebarToggleBtn) sidebarToggleBtn.addEventListener('click', () => toggleSidebar(true));

if (sidebarNewCanvasBtn) sidebarNewCanvasBtn.addEventListener('click', async () => {
  const name = prompt('新画布名称：', '新画布') || '新画布';
  const res = await fetch('/api/canvases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ name }),
  });
  if (!res.ok) return;
  const created = await res.json();
  canvasesList.push({ id: created.canvas_id, name: created.name });
  await switchCanvas(created.canvas_id);
});

document.addEventListener('click', (event) => {
  if (!event.target.closest('.node')) {
    clearSelection();
  }
});

autoResizeComposer();
bindCanvasPan();
applyTransform();
renderMinimap();
updateComposerHint();
initCanvases();
