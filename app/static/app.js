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

/* rAF coalescing factory: returns a scheduler that fires `fn` at most once
   per frame, no matter how many times the scheduler is called in between.
   Used for renderEdges, renderMinimap, applyTransform, updateSelectionActions
   and renderClusterFrames — all of which run in hot pointermove/wheel paths
   and benefit from being deferred to the next animation frame. */
function makeRafScheduler(fn) {
  let scheduled = false;
  return () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      fn();
    });
  };
}

const scheduleRenderEdges = makeRafScheduler(() => renderEdges());
const scheduleRenderMinimap = makeRafScheduler(() => renderMinimap());
const scheduleApplyTransform = makeRafScheduler(() => applyTransform());
const scheduleSelectionActionsRefresh = makeRafScheduler(() => updateSelectionActions());
const scheduleRenderClusterFrames = makeRafScheduler(() => renderClusterFrames());

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
const clusterFrameLayerEl = document.getElementById('clusterFrameLayer');
const alignGuideLayerEl = document.getElementById('alignGuideLayer');
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
  // Multi-select drag: snapshot of starting positions for every node being dragged
  dragOrigins: null,         // Map<nodeId, {x, y}>
  dragClusterIds: null,      // Set<requestId>
  selectionSource: 'none',
  // F1: when a single model card is selected, controls whether the next
  // sendMessage() goes as a single-model branch or as a multi-model chat
  // with the selected model's response quoted as context.
  // 'quote' (default) — send to all models with quote
  // 'branch'          — send only to that one model as a branch
  modelSelectionMode: 'quote',
};

/* Restore last-used model selection mode from localStorage. */
try {
  const saved = localStorage.getItem('nb:modelSelMode');
  if (saved === 'branch' || saved === 'quote') {
    state.modelSelectionMode = saved;
  }
} catch (_e) { /* ignore SSR / private mode */ }

/* ─── Undo / Redo history (C1) ──────────────────────────────────────────────
 * Lightweight client-side history for reversible visual operations.
 *
 * Supported entry types:
 *   - 'move'   { ops: [{nodeId, fromX, fromY, toX, toY}, ...] }
 *   - 'hide'   { clusterId, nodes: [{nodeId, x, y}], frameSnapshot }
 *
 * NOT included in history (they need backend coordination):
 *   - Creating new clusters via chat
 *   - Server-side delete (clearCanvas)
 *
 * Stack semantics:
 *   pushHistory()  → truncates anything past `historyIndex` and appends
 *   undo()         → applies inverse of historyStack[historyIndex], moves index back
 *   redo()         → re-applies historyStack[historyIndex+1], moves index forward
 */
const historyStack = [];
let historyIndex = -1;
const HISTORY_MAX = 100;

function pushHistory(entry) {
  /* Drop anything ahead of current index (lost futures after a new edit) */
  if (historyIndex < historyStack.length - 1) {
    historyStack.splice(historyIndex + 1);
  }
  historyStack.push(entry);
  if (historyStack.length > HISTORY_MAX) {
    historyStack.shift();
  }
  /* Always re-anchor index to the top after a push, so the next push doesn't
     splice phantom future entries based on a stale index. */
  historyIndex = historyStack.length - 1;
  /* Any non-arrow push breaks the arrow-nudge coalescing window. */
  _arrowNudgeEntry = null;
  if (_arrowNudgeTimer) { clearTimeout(_arrowNudgeTimer); _arrowNudgeTimer = null; }
}

/* Arrow-key nudge history coalescing.
   Holding ArrowRight at OS auto-repeat would otherwise create dozens of
   1-pixel history entries. We collapse consecutive nudges on the same
   selection into a single 'move' entry whose `toX/toY` is the latest
   position; the `fromX/fromY` from the very first nudge is preserved so
   one undo reverts the entire gesture. */
let _arrowNudgeEntry = null;
let _arrowNudgeTimer = null;
const ARROW_NUDGE_COALESCE_MS = 500;

function pushArrowNudgeHistory(finalPositions, _dx, _dy) {
  /* Try to extend the existing nudge entry if it covers the SAME node set. */
  const sameSet = _arrowNudgeEntry &&
    _arrowNudgeEntry.ops.length === finalPositions.size &&
    _arrowNudgeEntry.ops.every((op) => finalPositions.has(op.nodeId));
  if (sameSet) {
    for (const op of _arrowNudgeEntry.ops) {
      const pos = finalPositions.get(op.nodeId);
      op.toX = pos.x;
      op.toY = pos.y;
    }
  } else {
    /* New nudge gesture — create a fresh history entry capturing both
       the initial-from positions (current node minus the just-applied delta
       reconstructs the prior position via the original snapshot we no longer
       have, so we recompute fromX = currentX - dx; same for Y). */
    const ops = [];
    for (const [nodeId, pos] of finalPositions) {
      ops.push({ nodeId, fromX: pos.x - _dx, fromY: pos.y - _dy, toX: pos.x, toY: pos.y });
    }
    _arrowNudgeEntry = { type: 'move', ops };
    historyStack.push(_arrowNudgeEntry);
    if (historyStack.length > HISTORY_MAX) historyStack.shift();
    historyIndex = historyStack.length - 1;
  }
  if (_arrowNudgeTimer) clearTimeout(_arrowNudgeTimer);
  _arrowNudgeTimer = setTimeout(() => {
    _arrowNudgeEntry = null;
    _arrowNudgeTimer = null;
  }, ARROW_NUDGE_COALESCE_MS);
}

function applyMoveEntry(entry, direction) {
  /* direction = 'undo' or 'redo' */
  const affectedClusters = new Set();
  for (const op of entry.ops) {
    const node = nodes.get(op.nodeId);
    if (!node) continue;
    if (direction === 'undo') {
      node.x = op.fromX;
      node.y = op.fromY;
    } else {
      node.x = op.toX;
      node.y = op.toY;
    }
    node.root.style.left = `${node.x}px`;
    node.root.style.top = `${node.y}px`;
    const rid = findClusterIdForNode(op.nodeId);
    if (rid) affectedClusters.add(rid);
  }
  for (const rid of affectedClusters) {
    updateClusterBounds(rid);
    schedulePositionSave(rid);
  }
  scheduleRenderEdges();
  scheduleRenderMinimap();
  scheduleSelectionActionsRefresh();
}

/* Symmetric counterpart to hideClusterVisual: re-attach a previously snapshotted
   cluster (its nodes, edges, frame) back into the live scene. */
function restoreClusterVisual(snapshot) {
  const cluster = snapshot.frameSnapshot;
  if (!cluster) return;
  requestClusters.set(cluster.requestId, cluster);
  for (const ns of snapshot.nodes) {
    const node = ns.node;
    node.x = ns.x;
    node.y = ns.y;
    stageEl.appendChild(node.root);
    node.root.style.left = `${node.x}px`;
    node.root.style.top = `${node.y}px`;
    nodes.set(node.nodeId, node);
    observeNodeResize(node.root, node.nodeId);
  }
  for (const e of snapshot.edges || []) {
    edges.set(e.id, e);
  }
  updateClusterBounds(cluster.requestId);
  scheduleRenderEdges();
  scheduleRenderMinimap();
  scheduleRenderClusterFrames();
}

function applyHideEntry(entry, direction) {
  if (direction === 'undo') {
    restoreClusterVisual(entry);
  } else {
    /* Redo = hide again. Pass recordHistory=false so the redo doesn't push
       a duplicate entry on top of itself. */
    hideClusterVisual(entry.frameSnapshot.requestId, false);
  }
}

/* C4: Visually hide a cluster (frontend-only — server data is untouched).
   Returns the snapshot so callers (history) can restore it. */
function hideClusterVisual(requestId, recordHistory = true) {
  const cluster = requestClusters.get(requestId);
  if (!cluster) return null;
  /* Snapshot all nodes that belong to this cluster */
  const nodeIds = [cluster.userNodeId, ...(cluster.modelNodeIds || [])];
  if (cluster.conclusionNodeId) nodeIds.push(cluster.conclusionNodeId);
  const snapshot = { frameSnapshot: cluster, nodes: [], edges: [] };
  for (const id of nodeIds) {
    const node = nodes.get(id);
    if (!node) continue;
    snapshot.nodes.push({ node, x: node.x, y: node.y });
    /* Detach DOM but keep the element in memory; stop the ResizeObserver
       from pinning the (now offscreen) root so it can be GC'd if undo
       eventually evicts the history entry that's holding it. */
    if (_nodeResizeObserver) _nodeResizeObserver.unobserve(node.root);
    node.root.remove();
    nodes.delete(id);
    selectedNodeIds.delete(id);
  }
  /* Snapshot + remove edges that touch this cluster */
  for (const [edgeId, edge] of edges) {
    if (
      nodeIds.includes(edge.sourceId) ||
      nodeIds.includes(edge.targetId)
    ) {
      snapshot.edges.push(edge);
      edges.delete(edgeId);
    }
  }
  requestClusters.delete(requestId);
  if (recordHistory) {
    pushHistory({ type: 'hide', ...snapshot });
  }
  scheduleRenderEdges();
  scheduleRenderMinimap();
  scheduleRenderClusterFrames();
  scheduleSelectionActionsRefresh();
  return snapshot;
}

function deleteSelected() {
  if (!selectedNodeIds.size) return;
  /* Collect unique cluster ids from the selection */
  const clusterIds = new Set();
  for (const nodeId of selectedNodeIds) {
    const rid = findClusterIdForNode(nodeId);
    if (rid) clusterIds.add(rid);
  }
  if (!clusterIds.size) return;
  /* Hide each cluster (one history entry per cluster — keeps undo granular) */
  for (const rid of clusterIds) {
    hideClusterVisual(rid);
  }
}

function undo() {
  if (historyIndex < 0) return;
  const entry = historyStack[historyIndex];
  historyIndex -= 1;
  if (entry.type === 'move') applyMoveEntry(entry, 'undo');
  else if (entry.type === 'hide') applyHideEntry(entry, 'undo');
}

function redo() {
  if (historyIndex >= historyStack.length - 1) return;
  historyIndex += 1;
  const entry = historyStack[historyIndex];
  if (entry.type === 'move') applyMoveEntry(entry, 'redo');
  else if (entry.type === 'hide') applyHideEntry(entry, 'redo');
}

const requestClusters = new Map();
const nodes = new Map();
const edges = new Map();
const pendingSearchEvents = new Map();

function getCluster(requestId) {
  return requestClusters.get(requestId) || null;
}

/* Every node already carries its owning cluster's requestId, set at creation
   time in createUserNode/createModelNode/createConclusionNode. O(1) lookup. */
function findClusterIdForNode(nodeId) {
  return nodes.get(nodeId)?.requestId || null;
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
  /* Single click on a model card is the only selection that historically
     bypassed the summary path (forcing branch mode). With F1, the user can
     now explicitly switch this case to "quote → all models", in which case
     we DO want the summary/context bundle to be built. */
  if (selection.length === 1 && selection[0].type === 'model' && state.selectionSource === 'click') {
    return state.modelSelectionMode === 'quote';
  }
  return true;
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
  /* Single model card selected — show rich preview regardless of mode.
     The mode (branch vs quote) is exposed both in the floating selection
     toolbar AND inline in the composer hint, so the user can always see
     and switch which behavior the next send will use. */
  if (selection.length === 1 && selection[0].type === 'model') {
    const node = selection[0];
    const round = node.activeRound || getLatestRound(node);
    const modelLabel = getDisplayName(node.model);
    if (state.modelSelectionMode === 'branch') {
      return {
        key: 'branch',
        label: `${modelLabel} · 第 ${round} 轮`,
        sendLabel: '分支发送',
        hint: `继续 ${modelLabel} · 第 ${round} 轮 · 仅此模型分支（自动继承结论与压缩过程）`,
      };
    }
    /* quote mode (default) */
    return {
      key: 'quote',
      label: `${modelLabel} · 第 ${round} 轮 → 全部模型`,
      sendLabel: '引用并继续',
      hint: selectionSummaryState.loading
        ? `引用 ${modelLabel} 第 ${round} 轮 → 让所有模型继续讨论；Kimi 正在压缩上下文…`
        : `引用 ${modelLabel} 第 ${round} 轮 → 让所有模型继续讨论`,
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
  /* F1: continue button is the simple "focus composer" action; segmented
     control owns the mode semantics. Label is steady regardless of selection. */
  selectionContinueBtn.textContent = '继续讨论';
  selectionBranchBtn.disabled = !singleModel;
  /* Show / hide the model-mode segmented control */
  const segmentEl = document.getElementById('selectionModeSegment');
  if (segmentEl) {
    segmentEl.classList.toggle('hidden', !singleModel);
    if (singleModel) {
      /* Re-sync active button state in case it got out of sync */
      const segQuote = document.getElementById('selectionModeQuote');
      const segBranch = document.getElementById('selectionModeBranch');
      segQuote?.classList.toggle('active', state.modelSelectionMode === 'quote');
      segBranch?.classList.toggle('active', state.modelSelectionMode === 'branch');
    }
  }
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
  const selection = getSelectedContextNodes();
  const isSingleModel = selection.length === 1 && selection[0].type === 'model';
  document.getElementById('roundHint').textContent = mode.hint;
  if (composerModeEl) {
    /* Single model selected → render the pill as a clickable toggle so the
       user can switch quote↔branch right from the composer (in addition to
       the floating selection-actions toolbar). The click is handled via
       delegation on composerModeEl (wired once at module init below) so we
       don't accumulate listeners on every selection change. */
    if (isSingleModel) {
      const isQuote = state.modelSelectionMode === 'quote';
      composerModeEl.innerHTML = `
        <span class="mode-pill-label">${escapeHtml(mode.label)}</span>
        <button type="button" class="mode-pill-toggle" data-toggle-mode title="切换 引用所有模型 / 仅此模型">
          ${isQuote ? '📤' : '⎇'}
        </button>
      `;
      composerModeEl.dataset.mode = mode.key;
      composerModeEl.classList.add('clickable');
    } else {
      composerModeEl.textContent = mode.label;
      composerModeEl.dataset.mode = mode.key;
      composerModeEl.classList.remove('clickable');
    }
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
    /* Cached label so renderClusterFrames doesn't have to querySelector
       on every redraw. Set once here from the user_message payload. */
    labelText: typeof user_message === 'string' ? user_message.trim().slice(0, 30) : '',
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
    /* Cached label for cluster frame rendering. */
    labelText: typeof userMessage === 'string' ? userMessage.trim().slice(0, 30) : '',
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

  refreshAllClusterBounds();

  let bbox;
  if (parentRequestId && sourceModel) {
    /* ── Branch chat: anchor next to parent model node ── */
    const parentNode = getModelNode(parentRequestId, sourceModel);
    const parentX = parentNode?.x || centerWorldX;
    const parentY = parentNode?.y || centerWorldY;
    /* Try the 4 traditional candidates first, then fall back to spiral search */
    const priorityCandidates = [
      { x: parentX, y: parentY + MODEL_NODE_HEIGHT + 180 },
      { x: parentX + MODEL_NODE_WIDTH + 160, y: parentY + 120 },
      { x: parentX - footprintWidth - 180, y: parentY + 120 },
      { x: parentX + MODEL_NODE_WIDTH + 160, y: parentY - 120 },
    ];
    bbox = findSmartPlacement(footprintWidth, footprintHeight, {
      anchorX: parentX + (MODEL_NODE_WIDTH + 160 - footprintWidth) / 2,
      anchorY: parentY + MODEL_NODE_HEIGHT + 180,
      priorityCandidates,
    });
  } else {
    /* ── Main chat: anchor at the right side of user's selected cluster
         (if any), otherwise at viewport center. ── */
    const selectedBbox = unionBboxOfSelectedClusters();
    let anchorX;
    let anchorY;
    let extraNoGoBoxes = [];
    if (selectedBbox) {
      /* Place to the right of the selection area, top-aligned */
      anchorX = selectedBbox.x + selectedBbox.width + CLUSTER_PADDING;
      anchorY = selectedBbox.y;
      extraNoGoBoxes = [selectedBbox];
    } else {
      anchorX = centerWorldX - footprintWidth / 2;
      anchorY = centerWorldY - footprintHeight / 4;
    }
    bbox = findSmartPlacement(footprintWidth, footprintHeight, {
      anchorX,
      anchorY,
      extraNoGoBoxes,
    });
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

/*
 * Compute the union bounding box of every cluster that contains at least one
 * currently-selected node. Returns null if there is no selection or no
 * matching clusters.
 */
function unionBboxOfSelectedClusters() {
  if (!selectedNodeIds.size) return null;
  const seenClusters = new Set();
  for (const nodeId of selectedNodeIds) {
    const rid = findClusterIdForNode(nodeId);
    if (rid) seenClusters.add(rid);
  }
  if (!seenClusters.size) return null;

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const rid of seenClusters) {
    const cluster = requestClusters.get(rid);
    if (!cluster?.bbox) continue;
    minX = Math.min(minX, cluster.bbox.x);
    minY = Math.min(minY, cluster.bbox.y);
    maxX = Math.max(maxX, cluster.bbox.x + cluster.bbox.width);
    maxY = Math.max(maxY, cluster.bbox.y + cluster.bbox.height);
  }
  if (!Number.isFinite(minX)) return null;
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

/*
 * Smart placement algorithm: directional spiral search around an anchor point.
 *
 *   Phase 1 — Try `priorityCandidates` (caller-provided seed positions).
 *   Phase 2 — Try the 7 cardinal positions around the anchor in priority order
 *             (anchor itself, right, down, right-down, left-down, left, up).
 *   Phase 3 — Spiral outward from the anchor in expanding rings (8 directions
 *             per ring, up to 10 rings).
 *   Phase 4 — Fallback: append below the bottommost cluster.
 *
 * `extraNoGoBoxes` lets the caller add virtual obstacles (e.g. the selected
 * cluster's bbox) so the new card never lands inside the user's current focus.
 */
function findSmartPlacement(width, height, opts = {}) {
  const {
    anchorX = 0,
    anchorY = 0,
    extraNoGoBoxes = [],
    priorityCandidates = [],
    maxRings = 10,
  } = opts;

  const tryBox = (x, y) => {
    const candidate = { x, y, width, height };
    if (!hasClusterOverlap(candidate, extraNoGoBoxes)) return candidate;
    return null;
  };

  /* Phase 1: caller priority candidates */
  for (const c of priorityCandidates) {
    const found = tryBox(c.x, c.y);
    if (found) return found;
  }

  /* Phase 2: cardinal positions around the anchor (priority order) */
  const stepX = width + CLUSTER_PADDING * 2;
  const stepY = height + CLUSTER_PADDING * 2;
  const cardinals = [
    { x: anchorX,         y: anchorY }, // anchor itself (highest)
    { x: anchorX + stepX, y: anchorY }, // right
    { x: anchorX,         y: anchorY + stepY }, // down
    { x: anchorX + stepX, y: anchorY + stepY }, // right-down
    { x: anchorX - stepX, y: anchorY + stepY }, // left-down
    { x: anchorX - stepX, y: anchorY }, // left
    { x: anchorX,         y: anchorY - stepY }, // up
  ];
  for (const c of cardinals) {
    const found = tryBox(c.x, c.y);
    if (found) return found;
  }

  /* Phase 3: spiral search — 8 directions × N rings */
  for (let ring = 1; ring <= maxRings; ring += 1) {
    const offsets = [
      { dx:  ring * stepX, dy: 0              }, // E
      { dx:  ring * stepX, dy:  ring * stepY  }, // SE
      { dx: 0,             dy:  ring * stepY  }, // S
      { dx: -ring * stepX, dy:  ring * stepY  }, // SW
      { dx: -ring * stepX, dy: 0              }, // W
      { dx: -ring * stepX, dy: -ring * stepY  }, // NW
      { dx: 0,             dy: -ring * stepY  }, // N
      { dx:  ring * stepX, dy: -ring * stepY  }, // NE
    ];
    for (const o of offsets) {
      const found = tryBox(anchorX + o.dx, anchorY + o.dy);
      if (found) return found;
    }
  }

  /* Phase 4: fallback — stack below the bottommost existing cluster */
  const fallbackY = Array.from(requestClusters.values()).reduce((maxY, cluster) => {
    return Math.max(maxY, (cluster.bbox?.y || 0) + (cluster.bbox?.height || 0) + 120);
  }, 0);
  return { x: anchorX, y: fallbackY, width, height };
}

/* Legacy helper kept for backward compat — used by replayCluster path */
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

function hasClusterOverlap(nextBox, extraNoGoBoxes = []) {
  /* Check existing clusters */
  for (const cluster of requestClusters.values()) {
    const box = cluster.bbox;
    if (!box) continue;
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
  /* Check extra no-go boxes (e.g. selected cluster union) */
  for (const box of extraNoGoBoxes) {
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
  observeNodeResize(root, nodeId);
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
  observeNodeResize(root, nodeId);

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
  observeNodeResize(root, nodeId);
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
  /* Double-click anywhere on the node body (skipping interactive children)
     zooms-and-centers on the node. */
  root.addEventListener('dblclick', (event) => {
    if (event.target.closest('button, textarea, a, input, select')) return;
    event.preventDefault();
    focusNode(nodeId);
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

/* B4: edge selection state. Tracks which edge id is currently "selected"
   (highlighted via click). Hover-highlight is computed at render time from
   `hoverNodeId` plus this id. */
let selectedEdgeId = null;

function renderEdges() {
  const activeNodeId = hoverNodeId || selectedNodeId;
  /* B4: build real DOM elements (not innerHTML) so we can attach events. */
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const frag = document.createDocumentFragment();
  for (const edge of edges.values()) {
    const source = nodes.get(edge.sourceId);
    const target = nodes.get(edge.targetId);
    if (!source || !target) continue;
    const start = getAnchorPoint(source, edge.type, true);
    const end = getAnchorPoint(target, edge.type, false);
    const path = buildBezierPath(start, end);

    /* Two paths: an invisible thick "hit" path for easy clicking and a
       visible thin "stroke" path. Both share the same `d` attribute. */
    const hit = document.createElementNS(SVG_NS, 'path');
    hit.setAttribute('class', 'edge-hit');
    hit.setAttribute('d', path);
    hit.dataset.edgeId = edge.id;
    hit.addEventListener('mouseenter', () => onEdgeHover(edge.id, true));
    hit.addEventListener('mouseleave', () => onEdgeHover(edge.id, false));
    hit.addEventListener('click', (ev) => {
      ev.stopPropagation();
      onEdgeClick(edge.id);
    });

    const visible = document.createElementNS(SVG_NS, 'path');
    const classes = ['edge-path'];
    if (edge.type === 'branch_from_turn') classes.push('branch');
    if (selectedEdgeId === edge.id) classes.push('selected');
    if (activeNodeId) {
      if (edge.sourceId === activeNodeId || edge.targetId === activeNodeId) {
        classes.push('active');
      } else if (selectedEdgeId !== edge.id) {
        classes.push('dimmed');
      }
    }
    visible.setAttribute('class', classes.join(' '));
    visible.setAttribute('d', path);

    frag.appendChild(hit);
    frag.appendChild(visible);
  }
  edgeLayerEl.innerHTML = '';
  edgeLayerEl.appendChild(frag);
}

function onEdgeHover(edgeId, entering) {
  /* Hover highlights the edge AND its two endpoint nodes via the existing
     activeNodeId pathway. Cheapest path: just toggle a class on the visible
     path elements. */
  for (const path of edgeLayerEl.querySelectorAll('.edge-path')) {
    if (path.previousSibling?.dataset?.edgeId === edgeId) {
      path.classList.toggle('hovered', entering);
    }
  }
}

function onEdgeClick(edgeId) {
  /* Toggle selection. Clicking again deselects. */
  selectedEdgeId = (selectedEdgeId === edgeId) ? null : edgeId;
  /* Selecting an edge clears node selection so the user has a single
     focused object at any time. */
  if (selectedEdgeId) {
    selectedNodeIds.clear();
    selectedNodeId = null;
  }
  renderSelectionState();
  scheduleRenderEdges();
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
  /* B2: cluster frame highlight follows selection. */
  scheduleRenderClusterFrames();
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

/*
 * Cache-first dimensions lookup. Critical performance path: called during
 * edge rendering, minimap rendering and bbox computation. Calling
 * `offsetWidth` here triggers a synchronous layout reflow, so we ONLY do
 * that on a cache miss. The cache is invalidated automatically by the
 * ResizeObserver wired in `observeNodeResize` below whenever node content
 * changes (e.g. streaming response grows the model card).
 */
function getNodeDimensions(node) {
  if (node.cachedWidth && node.cachedHeight) {
    return { width: node.cachedWidth, height: node.cachedHeight };
  }
  /* Cache miss — read from DOM (causes a single reflow). */
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

/* Single global ResizeObserver shared by every node. Browser-batched and
   asynchronous, so it never blocks the main thread. */
const _nodeResizeObserver = (typeof ResizeObserver !== 'undefined')
  ? new ResizeObserver((entries) => {
      let dirty = false;
      for (const entry of entries) {
        const nodeId = entry.target.dataset.nodeId;
        if (!nodeId) continue;
        const node = nodes.get(nodeId);
        if (!node) continue;
        const rect = entry.contentRect;
        /* Only mark dirty if size actually changed by more than 1px */
        if (
          Math.abs((node.cachedWidth || 0) - rect.width) > 1 ||
          Math.abs((node.cachedHeight || 0) - rect.height) > 1
        ) {
          node.cachedWidth = rect.width;
          node.cachedHeight = rect.height;
          dirty = true;
        }
      }
      if (dirty) {
        scheduleRenderEdges();
        scheduleRenderMinimap();
      }
    })
  : null;

function observeNodeResize(root, nodeId) {
  if (!_nodeResizeObserver || !root) return;
  /* Tag the root with its nodeId so the observer callback can map back. */
  if (!root.dataset.nodeId) root.dataset.nodeId = nodeId;
  _nodeResizeObserver.observe(root);
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

/* ─── Smart alignment guides (B3) ───────────────────────────────────────────
 * During drag, detect when the dragged group's edges/centers come close to
 * other nodes' edges/centers and:
 *   1. Snap the drag delta so they line up exactly
 *   2. Render red dashed guide lines to communicate the snap
 *
 * Only checks against the K nearest non-dragging nodes for performance.
 * Snap threshold scales with zoom so it feels constant in screen space.
 */
const SNAP_THRESHOLD_PX = 8;        // screen-space pixels
const SNAP_NEIGHBOR_LIMIT = 12;     // only check K nearest non-dragging nodes

function clearAlignGuides() {
  if (alignGuideLayerEl) alignGuideLayerEl.innerHTML = '';
}

/* Snapshot of all non-dragging nodes' bboxes, captured ONCE at drag start by
   `cacheAlignSnapCandidates()`. Stable for the duration of the drag — only the
   dragged group moves. Per-frame `computeAlignGuides()` reads from this list
   instead of re-walking `nodes` and re-allocating per node. */
let _alignCandidates = null;

function cacheAlignSnapCandidates(draggedIds) {
  const list = [];
  for (const node of nodes.values()) {
    if (draggedIds.has(node.nodeId)) continue;
    const dim = getNodeDimensions(node);
    list.push({
      x1: node.x,
      y1: node.y,
      x2: node.x + dim.width,
      y2: node.y + dim.height,
      cx: node.x + dim.width / 2,
      cy: node.y + dim.height / 2,
    });
  }
  _alignCandidates = list;
}

function clearAlignSnapCandidates() {
  _alignCandidates = null;
}

/* Returns { snapDx, snapDy, lines } where lines is an array of
   { x1, y1, x2, y2 } in world coordinates. */
function computeAlignGuides(draggedBbox, _draggedIds) {
  const threshold = SNAP_THRESHOLD_PX / state.scale;
  const candidates = _alignCandidates;
  if (!candidates || !candidates.length) return { snapDx: 0, snapDy: 0, lines: [] };

  /* For small N (≤ SNAP_NEIGHBOR_LIMIT), skip the sort entirely — every
     candidate is in range. For larger N, sort by squared distance to the
     dragged center and take the top K. */
  let neighbors;
  if (candidates.length <= SNAP_NEIGHBOR_LIMIT) {
    neighbors = candidates;
  } else {
    const dCx = (draggedBbox.x1 + draggedBbox.x2) / 2;
    const dCy = (draggedBbox.y1 + draggedBbox.y2) / 2;
    /* Sort a fresh shallow copy so the cached list stays in original order. */
    neighbors = candidates.slice().sort((a, b) => {
      const da = (a.cx - dCx) ** 2 + (a.cy - dCy) ** 2;
      const db = (b.cx - dCx) ** 2 + (b.cy - dCy) ** 2;
      return da - db;
    }).slice(0, SNAP_NEIGHBOR_LIMIT);
  }

  /* For each axis, find the smallest delta that snaps the dragged group to a
     neighbor's left/center/right (X axis) or top/center/bottom (Y axis). */
  let bestSnapDx = 0;
  let bestSnapDxDist = Infinity;
  let bestSnapDy = 0;
  let bestSnapDyDist = Infinity;
  const lines = [];
  const matchedX = []; // candidates whose X-line matched
  const matchedY = []; // candidates whose Y-line matched

  for (const n of neighbors) {
    /* X axis: try matching left, center, right of dragged to left/center/right of n */
    const xPairs = [
      [draggedBbox.x1, n.x1, 'left'],
      [(draggedBbox.x1 + draggedBbox.x2) / 2, n.cx, 'center'],
      [draggedBbox.x2, n.x2, 'right'],
      [draggedBbox.x1, n.x2, 'leftRight'], // dragged left ↔ neighbor right
      [draggedBbox.x2, n.x1, 'rightLeft'],
    ];
    for (const [drag, nei, kind] of xPairs) {
      const delta = nei - drag;
      const dist = Math.abs(delta);
      if (dist < threshold && dist < Math.abs(bestSnapDxDist) + 0.01) {
        if (dist < Math.abs(bestSnapDxDist) - 0.01) {
          bestSnapDx = delta;
          bestSnapDxDist = delta;
          matchedX.length = 0;
        }
        matchedX.push({ n, kind, x: nei });
      }
    }

    /* Y axis: same idea */
    const yPairs = [
      [draggedBbox.y1, n.y1, 'top'],
      [(draggedBbox.y1 + draggedBbox.y2) / 2, n.cy, 'center'],
      [draggedBbox.y2, n.y2, 'bottom'],
      [draggedBbox.y1, n.y2, 'topBot'],
      [draggedBbox.y2, n.y1, 'botTop'],
    ];
    for (const [drag, nei, kind] of yPairs) {
      const delta = nei - drag;
      const dist = Math.abs(delta);
      if (dist < threshold && dist < Math.abs(bestSnapDyDist) + 0.01) {
        if (dist < Math.abs(bestSnapDyDist) - 0.01) {
          bestSnapDy = delta;
          bestSnapDyDist = delta;
          matchedY.length = 0;
        }
        matchedY.push({ n, kind, y: nei });
      }
    }
  }

  /* Build guide lines for the snapped axes */
  const snapDx = Math.abs(bestSnapDxDist) === Infinity ? 0 : bestSnapDx;
  const snapDy = Math.abs(bestSnapDyDist) === Infinity ? 0 : bestSnapDy;

  if (snapDx !== 0 || matchedX.length > 0) {
    /* Draw a vertical line at the snapped X, from the topmost to the bottommost
       of (dragged + matched neighbors). */
    for (const m of matchedX) {
      const lineX = m.x;
      const minY = Math.min(draggedBbox.y1 + snapDy, m.n.y1) - 12;
      const maxY = Math.max(draggedBbox.y2 + snapDy, m.n.y2) + 12;
      lines.push({ kind: 'v', x: lineX, y1: minY, y2: maxY });
    }
  }
  if (snapDy !== 0 || matchedY.length > 0) {
    for (const m of matchedY) {
      const lineY = m.y;
      const minX = Math.min(draggedBbox.x1 + snapDx, m.n.x1) - 12;
      const maxX = Math.max(draggedBbox.x2 + snapDx, m.n.x2) + 12;
      lines.push({ kind: 'h', y: lineY, x1: minX, x2: maxX });
    }
  }

  return { snapDx, snapDy, lines };
}

function renderAlignGuides(lines) {
  if (!alignGuideLayerEl) return;
  if (!lines || !lines.length) {
    alignGuideLayerEl.innerHTML = '';
    return;
  }
  const parts = [];
  for (const ln of lines) {
    if (ln.kind === 'v') {
      parts.push(`<line class="align-guide-line" x1="${ln.x}" y1="${ln.y1}" x2="${ln.x}" y2="${ln.y2}"/>`);
      parts.push(`<rect class="align-guide-tick" x="${ln.x - 2}" y="${ln.y1 - 2}" width="4" height="4"/>`);
      parts.push(`<rect class="align-guide-tick" x="${ln.x - 2}" y="${ln.y2 - 2}" width="4" height="4"/>`);
    } else {
      parts.push(`<line class="align-guide-line" x1="${ln.x1}" y1="${ln.y}" x2="${ln.x2}" y2="${ln.y}"/>`);
      parts.push(`<rect class="align-guide-tick" x="${ln.x1 - 2}" y="${ln.y - 2}" width="4" height="4"/>`);
      parts.push(`<rect class="align-guide-tick" x="${ln.x2 - 2}" y="${ln.y - 2}" width="4" height="4"/>`);
    }
  }
  alignGuideLayerEl.innerHTML = parts.join('');
}

/* ─── Cluster visual frames (B2) ────────────────────────────────────────────
 * Renders one rounded-rectangle frame per cluster, sitting between the edge
 * layer and the node layer. The frame's size mirrors `cluster.bbox` (which is
 * always kept fresh by `updateClusterBounds`). A small label at the top shows
 * the user's question (first 30 chars), giving the cluster a visible identity.
 *
 * Highlight state ('.active') turns on whenever any node in the cluster is
 * part of the current selection.
 *
 * Performance: 1 div per cluster, no event listeners (pointer-events:none).
 * Re-rendered via rAF coalescing whenever bbox / selection / nodes change.
 */
const FRAME_PADDING = 24;

function renderClusterFrames() {
  if (!clusterFrameLayerEl) return;
  if (!requestClusters.size) {
    clusterFrameLayerEl.innerHTML = '';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const cluster of requestClusters.values()) {
    if (!cluster.bbox) continue;
    const { x, y, width, height } = cluster.bbox;
    const frame = document.createElement('div');
    frame.className = 'cluster-frame';
    frame.dataset.requestId = cluster.requestId;
    frame.style.left = `${x - FRAME_PADDING}px`;
    frame.style.top = `${y - FRAME_PADDING}px`;
    frame.style.width = `${width + FRAME_PADDING * 2}px`;
    frame.style.height = `${height + FRAME_PADDING * 2}px`;

    /* Label = first ~30 chars of the user's question (cached on cluster
       creation, no DOM querySelector in this hot path). */
    const labelText = cluster.labelText;
    if (labelText) {
      const label = document.createElement('div');
      label.className = 'cluster-frame-label';
      label.textContent = labelText.length >= 30 ? labelText + '…' : labelText;
      frame.appendChild(label);
    }

    /* Highlight when any node in the cluster is selected */
    const isActive =
      (cluster.userNodeId && selectedNodeIds.has(cluster.userNodeId)) ||
      (Array.isArray(cluster.modelNodeIds) && cluster.modelNodeIds.some((id) => selectedNodeIds.has(id))) ||
      (cluster.conclusionNodeId && selectedNodeIds.has(cluster.conclusionNodeId));
    if (isActive) frame.classList.add('active');

    frag.appendChild(frame);
  }
  clusterFrameLayerEl.innerHTML = '';
  clusterFrameLayerEl.appendChild(frag);
}

function renderMinimap() {
  const allNodes = Array.from(nodes.values());
  if (!allNodes.length) {
    minimapNodesEl.innerHTML = '';
    minimapViewportEl.style.display = 'none';
    return;
  }

  /* ── PHASE 1: read everything first (zero DOM writes) ──
     Pre-collecting dimensions in a single pass — combined with the cache-first
     getNodeDimensions — ensures the DOM is read at most once per node and
     never interleaved with writes. */
  const dimensions = new Array(allNodes.length);
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (let i = 0; i < allNodes.length; i += 1) {
    const node = allNodes[i];
    const dim = getNodeDimensions(node);
    dimensions[i] = dim;
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x + dim.width);
    maxY = Math.max(maxY, node.y + dim.height);
  }
  const contentWidth = minimapContentEl.clientWidth;
  const contentHeight = minimapContentEl.clientHeight;
  const viewportRect = viewportEl.getBoundingClientRect();

  const padding = 80;
  minX -= padding;
  minY -= padding;
  maxX += padding;
  maxY += padding;

  const worldWidth = Math.max(1, maxX - minX);
  const worldHeight = Math.max(1, maxY - minY);
  const scale = Math.min(contentWidth / worldWidth, contentHeight / worldHeight);
  const offsetX = (contentWidth - worldWidth * scale) / 2;
  const offsetY = (contentHeight - worldHeight * scale) / 2;

  /* ── PHASE 2: write everything in one batch using DocumentFragment ──
     Building outside the live DOM avoids per-append layout invalidations. */
  const frag = document.createDocumentFragment();
  for (let i = 0; i < allNodes.length; i += 1) {
    const node = allNodes[i];
    const { width, height } = dimensions[i];
    const left = offsetX + (node.x - minX) * scale;
    const top = offsetY + (node.y - minY) * scale;
    const active = node.nodeId === (hoverNodeId || selectedNodeId);
    const cluster = getCluster(node.requestId);
    const running = Boolean(cluster?.isRunning || cluster?.isCancelling);
    const el = document.createElement('div');
    el.className = `minimap-node ${node.type}${active ? ' active' : ''}${running ? ' running' : ''}`;
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.style.width = `${Math.max(8, width * scale)}px`;
    el.style.height = `${Math.max(6, height * scale)}px`;
    frag.appendChild(el);
  }
  minimapNodesEl.innerHTML = '';
  minimapNodesEl.appendChild(frag);

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

function centerViewportOn(worldX, worldY, { animate = true } = {}) {
  const rect = viewportEl.getBoundingClientRect();
  const targetX = rect.width / 2 - worldX * state.scale;
  const targetY = rect.height / 2 - worldY * state.scale;
  if (animate) {
    /* B1: smooth pan to target. */
    animateViewTo(state.scale, targetX, targetY);
  } else {
    state.offsetX = targetX;
    state.offsetY = targetY;
    applyTransform();
    renderMinimap();
  }
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

  /* Detach all observed nodes from the ResizeObserver before wiping the
     stage, otherwise the observer keeps strong refs to detached DOM. */
  if (_nodeResizeObserver) {
    for (const node of nodes.values()) {
      _nodeResizeObserver.unobserve(node.root);
    }
  }
  stageEl.innerHTML = '';
  edgeLayerEl.innerHTML = '';
  if (clusterFrameLayerEl) clusterFrameLayerEl.innerHTML = '';
  requestClusters.clear();
  nodes.clear();
  edges.clear();
  pendingSearchEvents.clear();
  selectedNodeIds.clear();
  /* C1: history is invalid after a full canvas wipe */
  historyStack.length = 0;
  historyIndex = -1;
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

    /* B1: any user drag cancels in-flight view animation */
    cancelViewAnimation();

    const node = nodes.get(nodeId);
    if (!node) return;

    /* Decide drag-set: if clicked node is part of a multi-selection, drag the
       whole selection together; otherwise drag only this node. */
    let dragIds;
    if (selectedNodeIds.has(nodeId) && selectedNodeIds.size > 1) {
      dragIds = Array.from(selectedNodeIds);
    } else {
      dragIds = [nodeId];
    }

    /* Snapshot starting positions of every node to be dragged, plus the set
       of clusters that will need to be saved on drag-end. */
    state.dragOrigins = new Map();
    state.dragClusterIds = new Set();
    for (const id of dragIds) {
      const n = nodes.get(id);
      if (!n) continue;
      state.dragOrigins.set(id, { x: n.x, y: n.y });
      n.root.classList.add('dragging');
      const rid = findClusterIdForNode(id);
      if (rid) state.dragClusterIds.add(rid);
    }

    state.draggingNodeId = nodeId;
    state.dragStartX = event.clientX;
    state.dragStartY = event.clientY;
    /* B3: cache the non-dragging nodes' bboxes once at drag start so the
       per-frame snap computation doesn't re-walk all nodes. */
    cacheAlignSnapCandidates(new Set(state.dragOrigins.keys()));
    handle.setPointerCapture(event.pointerId);
  });

  handle.addEventListener('pointermove', (event) => {
    if (state.draggingNodeId !== nodeId) return;
    if (!state.dragOrigins) return;

    let dx = (event.clientX - state.dragStartX) / state.scale;
    let dy = (event.clientY - state.dragStartY) / state.scale;

    /* B3: compute the bbox the dragged group would occupy at this delta,
       then ask the snap helper for an axis-wise correction. */
    let snapDx = 0;
    let snapDy = 0;
    let snapLines = [];
    if (state.dragOrigins.size > 0) {
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const [id, origin] of state.dragOrigins) {
        const n = nodes.get(id);
        if (!n) continue;
        const dim = getNodeDimensions(n);
        const x = origin.x + dx;
        const y = origin.y + dy;
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x + dim.width);
        maxY = Math.max(maxY, y + dim.height);
      }
      if (Number.isFinite(minX)) {
        const draggedSet = new Set(state.dragOrigins.keys());
        const result = computeAlignGuides(
          { x1: minX, y1: minY, x2: maxX, y2: maxY },
          draggedSet,
        );
        snapDx = result.snapDx;
        snapDy = result.snapDy;
        snapLines = result.lines;
      }
    }
    dx += snapDx;
    dy += snapDy;
    renderAlignGuides(snapLines);

    /* Apply the same delta to every node in the drag-set */
    for (const [id, origin] of state.dragOrigins) {
      const n = nodes.get(id);
      if (!n) continue;
      n.x = origin.x + dx;
      n.y = origin.y + dy;
      n.root.style.left = `${n.x}px`;
      n.root.style.top = `${n.y}px`;
    }
    /* Refresh bbox for every affected cluster */
    if (state.dragClusterIds) {
      for (const rid of state.dragClusterIds) updateClusterBounds(rid);
    }
    scheduleRenderEdges();
    scheduleRenderMinimap();
    scheduleSelectionActionsRefresh();
  });

  function stopDrag(event) {
    if (state.draggingNodeId !== nodeId) return;
    handle.releasePointerCapture?.(event.pointerId);

    /* C1: capture move history if any node actually moved by >1px */
    let moveOps = [];
    if (state.dragOrigins) {
      for (const [id, origin] of state.dragOrigins) {
        const n = nodes.get(id);
        if (!n) continue;
        const dx = n.x - origin.x;
        const dy = n.y - origin.y;
        if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
          moveOps.push({ nodeId: id, fromX: origin.x, fromY: origin.y, toX: n.x, toY: n.y });
        }
      }
    }
    if (moveOps.length) {
      pushHistory({ type: 'move', ops: moveOps });
    }

    /* Strip dragging class from every dragged node */
    if (state.dragOrigins) {
      for (const id of state.dragOrigins.keys()) {
        nodes.get(id)?.root.classList.remove('dragging');
      }
    }
    /* Persist position for every affected cluster (each has its own debounce timer) */
    if (state.dragClusterIds) {
      for (const rid of state.dragClusterIds) {
        updateClusterBounds(rid);
        schedulePositionSave(rid);
      }
    }

    state.draggingNodeId = null;
    state.dragOrigins = null;
    state.dragClusterIds = null;
    /* B3: clear alignment guides AND release the cached candidate list. */
    clearAlignGuides();
    clearAlignSnapCandidates();
    scheduleRenderEdges();
    scheduleRenderMinimap();
    scheduleSelectionActionsRefresh();
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
    /* B2: any bbox change → frame must redraw. rAF-coalesced. */
    scheduleRenderClusterFrames();
  }
}

/* Tracks whether the spacebar is currently held down. While true, dragging
   the empty canvas pans instead of triggering box-select. Updated by the
   global keyboard handler at the bottom of the file. */
let _spaceHeld = false;

/* Marquee (box-select) state. Active during drag-from-empty-canvas. */
let _marqueeEl = null;
let _marqueeStart = null; // { worldX, worldY, clientX, clientY, additive }

function bindCanvasPan() {
  window.addEventListener('blur', () => {
    state.panning = false;
    viewportEl.classList.remove('panning');
    _spaceHeld = false;
    viewportEl.classList.remove('space-held');
    cancelMarquee();
  });

  viewportEl.addEventListener('pointerdown', (event) => {
    if (event.target.closest('.node, .minimap, .selection-actions, .needs-setup-banner')) return;
    if (event.button !== 0 && event.button !== 1) return; // left or middle
    /* B1: any user-initiated viewport interaction cancels in-flight animation */
    cancelViewAnimation();
    /* Decide mode:
       - Middle mouse button → pan
       - Space held → pan
       - Otherwise → box select */
    const wantPan = event.button === 1 || _spaceHeld;
    if (wantPan) {
      state.panning = true;
      state.panStartX = event.clientX;
      state.panStartY = event.clientY;
      state.originOffsetX = state.offsetX;
      state.originOffsetY = state.offsetY;
      viewportEl.classList.add('panning');
      viewportEl.setPointerCapture(event.pointerId);
    } else {
      /* Start marquee selection */
      const rect = viewportEl.getBoundingClientRect();
      const worldX = (event.clientX - rect.left - state.offsetX) / state.scale;
      const worldY = (event.clientY - rect.top - state.offsetY) / state.scale;
      _marqueeStart = {
        worldX,
        worldY,
        clientX: event.clientX,
        clientY: event.clientY,
        additive: event.shiftKey,
        baseSelection: event.shiftKey ? new Set(selectedNodeIds) : null,
      };
      state.panning = false;
      viewportEl.setPointerCapture(event.pointerId);
    }
  });

  viewportEl.addEventListener('pointermove', (event) => {
    if (state.panning) {
      state.offsetX = state.originOffsetX + (event.clientX - state.panStartX);
      state.offsetY = state.originOffsetY + (event.clientY - state.panStartY);
      /* Coalesce multiple pointermove events into one rAF callback */
      scheduleApplyTransform();
      scheduleRenderMinimap();
      return;
    }
    if (_marqueeStart) {
      updateMarquee(event);
    }
  });

  function stopPan(event) {
    /* End pan mode */
    if (state.panning) {
      const moved = Math.abs(event.clientX - state.panStartX) > 4 ||
                    Math.abs(event.clientY - state.panStartY) > 4;
      state.panning = false;
      viewportEl.classList.remove('panning');
      try { viewportEl.releasePointerCapture(event.pointerId); } catch (_) {}

      if (!moved && !event.target.closest('.node')) {
        clearSelection();
      }
      return;
    }
    /* End marquee mode */
    if (_marqueeStart) {
      const moved = Math.abs(event.clientX - _marqueeStart.clientX) > 4 ||
                    Math.abs(event.clientY - _marqueeStart.clientY) > 4;
      try { viewportEl.releasePointerCapture(event.pointerId); } catch (_) {}
      finishMarquee(moved);
    }
  }

  viewportEl.addEventListener('pointerup', stopPan);
  viewportEl.addEventListener('pointercancel', stopPan);

  viewportEl.addEventListener(
    'wheel',
    (event) => {
      event.preventDefault();
      /* B1: scrolling/zooming cancels animation immediately */
      if (_viewAnim) cancelViewAnimation();
      if (event.ctrlKey || event.metaKey) {
        // Pinch-to-zoom (trackpad) or Ctrl+scroll — gentler step + rAF coalesce
        const factor = event.deltaY < 0 ? 1.012 : 0.988;
        zoomAtPoint(factor, event.clientX, event.clientY);
      } else {
        // Two-finger scroll = pan — coalesce via rAF
        state.offsetX -= event.deltaX;
        state.offsetY -= event.deltaY;
        scheduleApplyTransform();
        scheduleRenderMinimap();
      }
    },
    { passive: false }
  );
}

/* ─── Marquee (box-select) helpers ─────────────────────────────────────── */

function ensureMarqueeEl() {
  if (_marqueeEl) return _marqueeEl;
  _marqueeEl = document.createElement('div');
  _marqueeEl.className = 'marquee';
  viewportEl.appendChild(_marqueeEl);
  return _marqueeEl;
}

function cancelMarquee() {
  if (_marqueeEl) {
    _marqueeEl.remove();
    _marqueeEl = null;
  }
  _marqueeStart = null;
}

function updateMarquee(event) {
  const start = _marqueeStart;
  if (!start) return;
  const rect = viewportEl.getBoundingClientRect();
  const x1 = Math.min(start.clientX, event.clientX) - rect.left;
  const y1 = Math.min(start.clientY, event.clientY) - rect.top;
  const x2 = Math.max(start.clientX, event.clientX) - rect.left;
  const y2 = Math.max(start.clientY, event.clientY) - rect.top;
  const el = ensureMarqueeEl();
  el.style.left = `${x1}px`;
  el.style.top = `${y1}px`;
  el.style.width = `${x2 - x1}px`;
  el.style.height = `${y2 - y1}px`;

  /* Recompute selection live so the user sees what they're about to grab. */
  const worldX2 = (event.clientX - rect.left - state.offsetX) / state.scale;
  const worldY2 = (event.clientY - rect.top - state.offsetY) / state.scale;
  const minX = Math.min(start.worldX, worldX2);
  const minY = Math.min(start.worldY, worldY2);
  const maxX = Math.max(start.worldX, worldX2);
  const maxY = Math.max(start.worldY, worldY2);

  const inside = new Set();
  for (const node of nodes.values()) {
    const dim = getNodeDimensions(node);
    /* Use intersection test (touches at all) — generous matches feel better
       than strict containment for fast-flick selections. */
    if (
      node.x + dim.width >= minX &&
      node.x <= maxX &&
      node.y + dim.height >= minY &&
      node.y <= maxY
    ) {
      inside.add(node.nodeId);
    }
  }

  selectedNodeIds.clear();
  if (start.additive && start.baseSelection) {
    for (const id of start.baseSelection) selectedNodeIds.add(id);
  }
  for (const id of inside) selectedNodeIds.add(id);
  selectedNodeId = selectedNodeIds.size === 1 ? Array.from(selectedNodeIds)[0] : null;
  state.selectionSource = 'marquee';
  renderSelectionState();
  updateComposerHint();
}

function finishMarquee(moved) {
  if (!_marqueeStart) return;
  const additive = _marqueeStart.additive;
  cancelMarquee();
  if (!moved) {
    /* It was a click, not a drag — clear selection (matching old behavior),
       unless the user was holding shift to add to selection. */
    if (!additive) clearSelection();
  }
  /* When moved=true, selection state was already live-updated by updateMarquee(). */
}

/* ─── Right-click context menu (C3) ────────────────────────────────────────── */
const contextMenuEl = document.getElementById('contextMenu');

function hideContextMenu() {
  if (contextMenuEl) contextMenuEl.classList.add('hidden');
}

function showContextMenu(clientX, clientY, items) {
  if (!contextMenuEl) return;
  /* Build the menu DOM */
  const frag = document.createDocumentFragment();
  for (const item of items) {
    if (item.separator) {
      const sep = document.createElement('div');
      sep.className = 'context-menu-separator';
      frag.appendChild(sep);
      continue;
    }
    const el = document.createElement('div');
    el.className = `context-menu-item${item.danger ? ' danger' : ''}`;
    el.setAttribute('role', 'menuitem');
    if (item.disabled) el.setAttribute('aria-disabled', 'true');
    const label = document.createElement('span');
    label.textContent = item.label;
    el.appendChild(label);
    if (item.shortcut) {
      const sc = document.createElement('span');
      sc.className = 'ctx-shortcut';
      sc.textContent = item.shortcut;
      el.appendChild(sc);
    }
    if (!item.disabled && item.action) {
      el.addEventListener('click', () => {
        hideContextMenu();
        try { item.action(); } catch (e) { console.error('[ctx menu]', e); }
      });
    }
    frag.appendChild(el);
  }
  contextMenuEl.innerHTML = '';
  contextMenuEl.appendChild(frag);
  contextMenuEl.classList.remove('hidden');
  /* Position with viewport bounds clamping */
  const rect = viewportEl.getBoundingClientRect();
  const menuW = contextMenuEl.offsetWidth || 220;
  const menuH = contextMenuEl.offsetHeight || 200;
  const left = Math.min(clientX - rect.left, rect.width - menuW - 8);
  const top = Math.min(clientY - rect.top, rect.height - menuH - 8);
  contextMenuEl.style.left = `${Math.max(8, left)}px`;
  contextMenuEl.style.top = `${Math.max(8, top)}px`;
}

function buildContextMenuItems(targetNodeId) {
  const items = [];
  const targetNode = targetNodeId ? nodes.get(targetNodeId) : null;
  const isModel = targetNode?.type === 'model';
  const isMulti = selectedNodeIds.size > 1;
  const hasSelection = selectedNodeIds.size > 0;

  /* Focus current target */
  if (targetNode) {
    items.push({
      label: '聚焦此卡片',
      shortcut: 'F',
      action: () => focusNode(targetNodeId),
    });
  } else if (selectedNodeIds.size) {
    items.push({
      label: '聚焦选中',
      shortcut: 'F',
      action: () => focusSelection(),
    });
  }
  items.push({
    label: '适配全部到视口',
    shortcut: '⌘0',
    action: () => fitAll(),
  });

  /* Selection actions */
  if (targetNode || hasSelection) {
    items.push({ separator: true });
    if (isModel && !isMulti) {
      items.push({
        label: '建立分支',
        action: () => openBranchComposer(targetNode),
      });
    }
    items.push({
      label: '清除选择',
      shortcut: 'Esc',
      disabled: !hasSelection,
      action: () => clearSelection(),
    });
  }

  /* Edit ops */
  items.push({ separator: true });
  items.push({
    label: '撤销',
    shortcut: '⌘Z',
    disabled: historyIndex < 0,
    action: () => undo(),
  });
  items.push({
    label: '重做',
    shortcut: '⌘⇧Z',
    disabled: historyIndex >= historyStack.length - 1,
    action: () => redo(),
  });

  /* Destructive */
  if (hasSelection || targetNode) {
    items.push({ separator: true });
    items.push({
      label: '隐藏选中（可撤销）',
      shortcut: 'Del',
      danger: true,
      disabled: !hasSelection && !targetNode,
      action: () => {
        if (!selectedNodeIds.size && targetNodeId) {
          selectedNodeIds.add(targetNodeId);
        }
        deleteSelected();
      },
    });
  }

  return items;
}

/* Listen for contextmenu on the viewport */
viewportEl?.addEventListener('contextmenu', (event) => {
  /* Skip if right-clicking on UI controls (they may have their own menus) */
  if (event.target.closest('button, input, textarea, select, .minimap, .selection-actions')) {
    return;
  }
  event.preventDefault();
  /* Determine target: if right-click on a node, that's the target; if user
     had a selection that includes the clicked node, use that selection. */
  const nodeEl = event.target.closest('.node');
  let targetNodeId = nodeEl?.dataset.nodeId || null;
  /* If right-clicking a node not in selection, treat it as a fresh target */
  if (targetNodeId && !selectedNodeIds.has(targetNodeId)) {
    selectedNodeIds.clear();
    selectedNodeIds.add(targetNodeId);
    selectedNodeId = targetNodeId;
    state.selectionSource = 'click';
    renderSelectionState();
  }
  showContextMenu(event.clientX, event.clientY, buildContextMenuItems(targetNodeId));
});

/* Click anywhere else closes the menu */
document.addEventListener('mousedown', (event) => {
  if (!contextMenuEl || contextMenuEl.classList.contains('hidden')) return;
  if (event.target.closest('.context-menu')) return;
  hideContextMenu();
});

/* ─── Global search overlay (E1) ──────────────────────────────────────────── */
const searchOverlayEl = document.getElementById('searchOverlay');
const searchInputEl = document.getElementById('searchInput');
const searchResultsEl = document.getElementById('searchResults');
let _searchResults = [];
let _searchActiveIndex = 0;

function openSearch() {
  if (!searchOverlayEl) return;
  searchOverlayEl.classList.remove('hidden');
  searchInputEl.value = '';
  _searchResults = [];
  _searchActiveIndex = 0;
  renderSearchResults();
  /* Focus after a microtask so the keydown that opened it doesn't echo */
  setTimeout(() => searchInputEl?.focus(), 0);
}

function closeSearch() {
  if (!searchOverlayEl) return;
  searchOverlayEl.classList.add('hidden');
  searchInputEl.value = '';
  _searchResults = [];
}

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function highlightMatch(text, query) {
  if (!query) return escapeHtml(text);
  const re = new RegExp(`(${escapeRegex(query)})`, 'ig');
  return escapeHtml(text).replace(re, '<mark>$1</mark>');
}

function performSearch(query) {
  const q = query.trim().toLowerCase();
  if (!q) {
    _searchResults = [];
    renderSearchResults();
    return;
  }
  const matches = [];
  for (const cluster of requestClusters.values()) {
    /* Search in user message text */
    const userNode = nodes.get(cluster.userNodeId);
    const userText = userNode?.root?.querySelector('.user-message')?.textContent || '';
    /* Also search in model responses (concatenate all rounds) */
    let modelText = '';
    for (const mid of (cluster.modelNodeIds || [])) {
      const m = nodes.get(mid);
      if (!m?.turns) continue;
      for (const turn of m.turns.values()) {
        modelText += ' ' + (turn.raw || '');
      }
    }
    const haystack = (userText + ' ' + modelText).toLowerCase();
    if (haystack.includes(q)) {
      matches.push({
        requestId: cluster.requestId,
        title: userText || `(对话 ${cluster.requestId.slice(0, 8)})`,
        meta: `${cluster.modelNodeIds?.length || 0} 个模型 · ${cluster.kind === 'branch' ? '分支' : '主对话'}`,
        score: userText.toLowerCase().includes(q) ? 2 : 1, // user-text matches rank higher
      });
    }
  }
  matches.sort((a, b) => b.score - a.score);
  _searchResults = matches.slice(0, 30);
  _searchActiveIndex = 0;
  renderSearchResults(q);
}

function renderSearchResults(query = '') {
  if (!searchResultsEl) return;
  if (!_searchResults.length) {
    searchResultsEl.innerHTML = '';
    return;
  }
  const frag = document.createDocumentFragment();
  _searchResults.forEach((m, i) => {
    const el = document.createElement('div');
    el.className = `search-result${i === _searchActiveIndex ? ' active' : ''}`;
    el.dataset.requestId = m.requestId;
    el.innerHTML = `
      <div class="search-result-title">${highlightMatch(m.title.slice(0, 80), query)}</div>
      <div class="search-result-meta">${escapeHtml(m.meta)}</div>
    `;
    el.addEventListener('click', () => {
      jumpToSearchResult(i);
    });
    frag.appendChild(el);
  });
  searchResultsEl.innerHTML = '';
  searchResultsEl.appendChild(frag);
}

function jumpToSearchResult(index) {
  const m = _searchResults[index];
  if (!m) return;
  closeSearch();
  focusCluster(m.requestId);
  /* Also select the user node so context is clear */
  const cluster = requestClusters.get(m.requestId);
  if (cluster?.userNodeId) {
    selectedNodeIds.clear();
    selectedNodeIds.add(cluster.userNodeId);
    selectedNodeId = cluster.userNodeId;
    state.selectionSource = 'keyboard';
    renderSelectionState();
  }
}

searchInputEl?.addEventListener('input', (event) => {
  performSearch(event.target.value);
});

searchInputEl?.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    closeSearch();
    return;
  }
  if (event.key === 'Enter') {
    event.preventDefault();
    if (_searchResults.length) jumpToSearchResult(_searchActiveIndex);
    return;
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    if (_searchResults.length) {
      _searchActiveIndex = (_searchActiveIndex + 1) % _searchResults.length;
      renderSearchResults(searchInputEl.value.trim());
    }
    return;
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault();
    if (_searchResults.length) {
      _searchActiveIndex = (_searchActiveIndex - 1 + _searchResults.length) % _searchResults.length;
      renderSearchResults(searchInputEl.value.trim());
    }
    return;
  }
});

/* Click on overlay backdrop closes search */
searchOverlayEl?.addEventListener('mousedown', (event) => {
  if (event.target === searchOverlayEl) {
    closeSearch();
  }
});

function zoomAtPoint(factor, clientX, clientY) {
  const rect = viewportEl.getBoundingClientRect();
  const pointerX = clientX - rect.left;
  const pointerY = clientY - rect.top;
  const worldX = (pointerX - state.offsetX) / state.scale;
  const worldY = (pointerY - state.offsetY) / state.scale;

  state.scale = clamp(state.scale * factor, 0.2, 1.8);
  state.offsetX = pointerX - worldX * state.scale;
  state.offsetY = pointerY - worldY * state.scale;
  /* Coalesce rapid wheel events into a single rAF transform commit */
  scheduleApplyTransform();
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
  /* B1: animated center on the cluster, keeping current zoom. */
  const cx = cluster.bbox.x + cluster.bbox.width / 2;
  const cy = cluster.bbox.y + cluster.bbox.height / 2;
  const rect = viewportEl.getBoundingClientRect();
  const targetX = rect.width / 2 - cx * state.scale;
  const targetY = rect.height / 2 - cy * state.scale;
  animateViewTo(state.scale, targetX, targetY);
}

/*
 * Animate viewport scale + offset so an axis-aligned world-space bbox fits
 * inside the viewport with the given padding. Shared by fitAll and
 * focusSelection so they don't reimplement the same scale/offset math.
 */
function fitBboxToViewport(minX, minY, maxX, maxY, { padding = 80, duration = 260 } = {}) {
  const worldWidth = (maxX - minX) + padding * 2;
  const worldHeight = (maxY - minY) + padding * 2;
  const rect = viewportEl.getBoundingClientRect();
  const targetScale = clamp(
    Math.min(rect.width / worldWidth, rect.height / worldHeight),
    0.05,
    1.8,
  );
  const centerWorldX = (minX + maxX) / 2;
  const centerWorldY = (minY + maxY) / 2;
  animateViewTo(
    targetScale,
    rect.width / 2 - centerWorldX * targetScale,
    rect.height / 2 - centerWorldY * targetScale,
    duration,
  );
}

/*
 * Compute the union bbox of every existing cluster, then set scale + offset
 * so the entire union fits inside the viewport with some padding. Used by
 * Cmd/Ctrl+0 and the "fit-all" toolbar button (Shift+click).
 */
function fitAll() {
  refreshAllClusterBounds();
  const allClusters = Array.from(requestClusters.values());
  if (!allClusters.length) {
    setZoom(DEFAULT_SCALE);
    return;
  }
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const cluster of allClusters) {
    const bbox = cluster.bbox;
    if (!bbox) continue;
    minX = Math.min(minX, bbox.x);
    minY = Math.min(minY, bbox.y);
    maxX = Math.max(maxX, bbox.x + bbox.width);
    maxY = Math.max(maxY, bbox.y + bbox.height);
  }
  if (!Number.isFinite(minX)) {
    setZoom(DEFAULT_SCALE);
    return;
  }
  fitBboxToViewport(minX, minY, maxX, maxY, { padding: 120, duration: 320 });
}

/*
 * Zoom and center the viewport on a single node. If the current zoom level
 * is small (<0.5) we bump it up to 0.7 so users can actually read the card.
 * Otherwise we keep the current zoom and just recenter.
 */
function focusNode(nodeId) {
  const node = nodes.get(nodeId);
  if (!node) return;
  const dim = getNodeDimensions(node);
  const cx = node.x + dim.width / 2;
  const cy = node.y + dim.height / 2;
  const rect = viewportEl.getBoundingClientRect();
  /* B1: bump zoom (if too small) and recenter, both via animation. */
  const targetScale = state.scale < 0.5 ? 0.7 : state.scale;
  animateViewTo(
    targetScale,
    rect.width / 2 - cx * targetScale,
    rect.height / 2 - cy * targetScale,
  );
}

/*
 * Zoom and center to fit the current selection. If only one node is selected,
 * delegates to focusNode for a snappier feel. Otherwise computes the union
 * bbox of selected nodes and fits it via the shared helper.
 */
function focusSelection() {
  if (!selectedNodeIds.size) return;
  if (selectedNodeIds.size === 1) {
    focusNode(Array.from(selectedNodeIds)[0]);
    return;
  }
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const id of selectedNodeIds) {
    const node = nodes.get(id);
    if (!node) continue;
    const dim = getNodeDimensions(node);
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x + dim.width);
    maxY = Math.max(maxY, node.y + dim.height);
  }
  if (!Number.isFinite(minX)) return;
  fitBboxToViewport(minX, minY, maxX, maxY);
}

function applyTransform() {
  const transform = `translate(${state.offsetX}px, ${state.offsetY}px) scale(${state.scale})`;
  stageEl.style.transform = transform;
  gridEl.style.transform = transform;
  edgeLayerEl.style.transform = transform;
  /* Keep cluster-frame and alignment-guide layers in lockstep. */
  if (clusterFrameLayerEl) clusterFrameLayerEl.style.transform = transform;
  if (alignGuideLayerEl) alignGuideLayerEl.style.transform = transform;
  zoomResetBtn.textContent = `${Math.round(state.scale * 100)}%`;
  /* Defer selection-toolbar reposition to a rAF tick so heavy
     getBoundingClientRect work doesn't run on every wheel/pan event. */
  scheduleSelectionActionsRefresh();
}

/* ─── Smooth view transitions (B1) ──────────────────────────────────────────
 * Single source of truth for animated camera moves. Used by fitAll, focusNode,
 * focusSelection, focusCluster, setZoom, and minimap clicks. NOT used by
 * wheel/pan handlers — those need instant per-frame response.
 *
 * Cancels any in-flight animation when called again, so mid-flight retargets
 * smoothly continue from the current frame instead of jumping back.
 *
 * Cancels itself when the user starts dragging or scrolling, so user input
 * always wins over animation.
 */
let _viewAnim = null;

function cancelViewAnimation() {
  if (_viewAnim?.raf) {
    cancelAnimationFrame(_viewAnim.raf);
  }
  _viewAnim = null;
}

function animateViewTo(toScale, toOffsetX, toOffsetY, duration = 260) {
  cancelViewAnimation();
  /* No-op if already there */
  const dScale = Math.abs(toScale - state.scale);
  const dX = Math.abs(toOffsetX - state.offsetX);
  const dY = Math.abs(toOffsetY - state.offsetY);
  if (dScale < 0.001 && dX < 0.5 && dY < 0.5) {
    state.scale = toScale;
    state.offsetX = toOffsetX;
    state.offsetY = toOffsetY;
    applyTransform();
    scheduleRenderMinimap();
    return;
  }

  _viewAnim = {
    startTime: performance.now(),
    duration,
    fromScale: state.scale,
    fromX: state.offsetX,
    fromY: state.offsetY,
    toScale,
    toX: toOffsetX,
    toY: toOffsetY,
    raf: 0,
  };

  const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);

  const tick = (now) => {
    if (!_viewAnim) return;
    const t = Math.min((now - _viewAnim.startTime) / _viewAnim.duration, 1);
    const e = easeOutCubic(t);
    state.scale = _viewAnim.fromScale + (_viewAnim.toScale - _viewAnim.fromScale) * e;
    state.offsetX = _viewAnim.fromX + (_viewAnim.toX - _viewAnim.fromX) * e;
    state.offsetY = _viewAnim.fromY + (_viewAnim.toY - _viewAnim.fromY) * e;
    applyTransform();
    scheduleRenderMinimap();
    if (t < 1) {
      _viewAnim.raf = requestAnimationFrame(tick);
    } else {
      _viewAnim = null;
    }
  };
  _viewAnim.raf = requestAnimationFrame(tick);
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
  /* F1: single-model selection respects state.modelSelectionMode.
     - 'branch' → send a branch_chat to that one model only
     - 'quote'  → fall through to the multi-model chat path below; the
                  selection summary pipeline will quote the model's
                  response into the context bundle automatically. */
  if (
    selected.length === 1 &&
    selected[0].type === 'model' &&
    state.modelSelectionMode === 'branch' &&
    !shouldUseSelectionSummary(selected)
  ) {
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

/* F1: segmented control for choosing how a single-model selection behaves
   when the user sends a new message. */
const segModeQuote = document.getElementById('selectionModeQuote');
const segModeBranch = document.getElementById('selectionModeBranch');

function setModelSelectionMode(mode) {
  if (mode !== 'branch' && mode !== 'quote') return;
  if (mode === state.modelSelectionMode) return;
  state.modelSelectionMode = mode;
  try { localStorage.setItem('nb:modelSelMode', mode); } catch (_e) {}
  if (segModeQuote) {
    segModeQuote.classList.toggle('active', mode === 'quote');
    segModeQuote.setAttribute('aria-selected', String(mode === 'quote'));
  }
  if (segModeBranch) {
    segModeBranch.classList.toggle('active', mode === 'branch');
    segModeBranch.setAttribute('aria-selected', String(mode === 'branch'));
  }
  /* Switching from branch→quote or vice versa changes whether the
     selection-summary pipeline should run for the current selection. */
  if (typeof queueSelectionSummaryRefresh === 'function') {
    queueSelectionSummaryRefresh();
  }
}

segModeQuote?.addEventListener('click', () => setModelSelectionMode('quote'));
segModeBranch?.addEventListener('click', () => setModelSelectionMode('branch'));

/* Delegated handler for the composer-area mode-pill toggle. Wired ONCE at
   module init so updateComposerHint can rebuild innerHTML safely without
   re-attaching listeners every render. Mirrors the pattern used by
   `_chipsDelegated` for selectedChipsEl. */
composerModeEl?.addEventListener('click', (event) => {
  if (!event.target.closest('[data-toggle-mode]')) return;
  event.stopPropagation();
  setModelSelectionMode(state.modelSelectionMode === 'quote' ? 'branch' : 'quote');
  updateComposerHint();
});

/* Initial visual state synced with whatever was restored from localStorage.
   We can't call setModelSelectionMode here since it now early-returns on
   no-op, so just toggle the classes directly. */
{
  const initialMode = state.modelSelectionMode;
  segModeQuote?.classList.toggle('active', initialMode === 'quote');
  segModeQuote?.setAttribute('aria-selected', String(initialMode === 'quote'));
  segModeBranch?.classList.toggle('active', initialMode === 'branch');
  segModeBranch?.setAttribute('aria-selected', String(initialMode === 'branch'));
}
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
fitBtn.addEventListener('click', (event) => {
  /* Shift+click → fit all clusters; plain click → focus latest cluster. */
  if (event.shiftKey) {
    fitAll();
    return;
  }
  if (latestRequestId) {
    focusCluster(latestRequestId);
  } else {
    fitAll();
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

/* Bug fix: removed the document-wide "click anything not a .node = clearSelection"
   handler. It was clearing the selection whenever the user clicked the composer,
   sidebar, toolbar, minimap, or any UI chrome — making it impossible to
   "select a card, then type a question". Empty-canvas clicks are already
   handled by `stopPan()` in bindCanvasPan via the click-vs-drag detection,
   so this listener was redundant. */

/* ─── Global keyboard shortcuts ──────────────────────────────────────────
 *  Esc          清除选择
 *  Cmd/Ctrl+A   全选
 *  Cmd/Ctrl+0   适配全部到视口
 *  Cmd/Ctrl+1   重置缩放
 *  Cmd/Ctrl++   放大
 *  Cmd/Ctrl+-   缩小
 *  F            聚焦当前选中
 *  ↑↓←→         推动选中节点 1px (Shift = 10px)
 *  Space (按住) 平移模式（鼠标拖动 = 平移而非框选）
 *
 *  Always skipped when the user is typing in an input/textarea/select or
 *  contenteditable element. */
function isTypingTarget(target) {
  if (!target) return false;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (target.isContentEditable) return true;
  return false;
}

document.addEventListener('keydown', (event) => {
  /* Track space key for pan-mode toggle, regardless of focus. */
  if (event.code === 'Space' && !isTypingTarget(event.target)) {
    if (!_spaceHeld) {
      _spaceHeld = true;
      viewportEl.classList.add('space-held');
    }
    /* Don't preventDefault here when typing — only when on canvas */
    event.preventDefault();
  }

  /* E1: Cmd/Ctrl+K opens search — works even when typing in the composer
     (Figma / VSCode / Linear convention). */
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    /* Don't hijack when search is already open */
    if (searchOverlayEl && !searchOverlayEl.classList.contains('hidden')) return;
    event.preventDefault();
    openSearch();
    return;
  }

  if (isTypingTarget(event.target)) return;

  const cmd = event.metaKey || event.ctrlKey;

  /* Esc — clear selection / cancel marquee */
  if (event.key === 'Escape') {
    cancelMarquee();
    if (selectedNodeIds.size) clearSelection();
    event.preventDefault();
    return;
  }

  /* C4: Delete / Backspace — hide selected clusters (undoable) */
  if ((event.key === 'Delete' || event.key === 'Backspace') && selectedNodeIds.size) {
    deleteSelected();
    event.preventDefault();
    return;
  }

  /* Cmd/Ctrl+A — select all nodes on canvas */
  if (cmd && event.key.toLowerCase() === 'a') {
    selectedNodeIds.clear();
    for (const id of nodes.keys()) selectedNodeIds.add(id);
    selectedNodeId = selectedNodeIds.size === 1 ? Array.from(selectedNodeIds)[0] : null;
    state.selectionSource = 'keyboard';
    renderSelectionState();
    updateComposerHint();
    event.preventDefault();
    return;
  }

  /* Cmd/Ctrl+0 — fit all clusters to viewport */
  if (cmd && event.key === '0') {
    fitAll();
    event.preventDefault();
    return;
  }

  /* Cmd/Ctrl+1 — reset zoom to default */
  if (cmd && event.key === '1') {
    setZoom(DEFAULT_SCALE);
    event.preventDefault();
    return;
  }

  /* Cmd/Ctrl++ / Cmd/Ctrl+= — zoom in */
  if (cmd && (event.key === '+' || event.key === '=')) {
    setZoom(clamp(state.scale * 1.12, 0.2, 1.8));
    event.preventDefault();
    return;
  }

  /* Cmd/Ctrl+- — zoom out */
  if (cmd && event.key === '-') {
    setZoom(clamp(state.scale * 0.88, 0.2, 1.8));
    event.preventDefault();
    return;
  }

  /* F — focus current selection (or latest cluster if nothing selected) */
  if (event.key === 'f' || event.key === 'F') {
    if (cmd) return; // don't hijack Cmd+F (browser find)
    if (selectedNodeIds.size) {
      focusSelection();
    } else if (latestRequestId) {
      focusCluster(latestRequestId);
    }
    event.preventDefault();
    return;
  }

  /* Arrow keys — nudge selected nodes (1px or 10px with Shift).
     Consecutive nudges within 500ms on the same selection are coalesced into
     a SINGLE history entry, so a held-down arrow key (OS auto-repeat fires
     ~30 times/sec) doesn't blow through HISTORY_MAX in 3 seconds. */
  if (selectedNodeIds.size && (event.key === 'ArrowLeft' || event.key === 'ArrowRight' || event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
    const step = event.shiftKey ? 10 : 1;
    let dx = 0, dy = 0;
    if (event.key === 'ArrowLeft') dx = -step;
    if (event.key === 'ArrowRight') dx = step;
    if (event.key === 'ArrowUp') dy = -step;
    if (event.key === 'ArrowDown') dy = step;
    const affectedClusters = new Set();
    /* Move every selected node and collect the per-node final positions. */
    const finalPositions = new Map();
    for (const id of selectedNodeIds) {
      const n = nodes.get(id);
      if (!n) continue;
      n.x += dx;
      n.y += dy;
      n.root.style.left = `${n.x}px`;
      n.root.style.top = `${n.y}px`;
      finalPositions.set(id, { x: n.x, y: n.y });
      const rid = findClusterIdForNode(id);
      if (rid) affectedClusters.add(rid);
    }
    /* Coalesce with the previous arrow nudge if it's still "alive". */
    pushArrowNudgeHistory(finalPositions, dx, dy);
    for (const rid of affectedClusters) {
      updateClusterBounds(rid);
      schedulePositionSave(rid);
    }
    scheduleRenderEdges();
    scheduleRenderMinimap();
    scheduleSelectionActionsRefresh();
    event.preventDefault();
    return;
  }

  /* Cmd/Ctrl+Z — undo */
  if (cmd && !event.shiftKey && event.key.toLowerCase() === 'z') {
    undo();
    event.preventDefault();
    return;
  }
  /* Cmd/Ctrl+Shift+Z — redo */
  if (cmd && event.shiftKey && event.key.toLowerCase() === 'z') {
    redo();
    event.preventDefault();
    return;
  }
  /* Cmd/Ctrl+Y — redo (Windows convention) */
  if (cmd && event.key.toLowerCase() === 'y') {
    redo();
    event.preventDefault();
    return;
  }
});

document.addEventListener('keyup', (event) => {
  if (event.code === 'Space') {
    _spaceHeld = false;
    viewportEl.classList.remove('space-held');
  }
});

autoResizeComposer();
bindCanvasPan();
applyTransform();
renderMinimap();
updateComposerHint();
initCanvases();
