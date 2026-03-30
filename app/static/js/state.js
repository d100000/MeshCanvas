/**
 * state.js — Shared constants, DOM refs, mutable application state, and Maps.
 *
 * This is the single source of truth for all shared state across modules.
 * Mutable scalars that need cross-module reassignment are wrapped in `appState`.
 * Maps and Sets are exported directly (mutated via .set()/.add(), never reassigned).
 */

// ── Constants ────────────────────────────────────────────────────────────────

export const MODEL_DISPLAY_NAMES = {};

export const MODEL_NODE_WIDTH = 420;
export const USER_NODE_WIDTH = 500;
export const MODEL_NODE_HEIGHT = 700;
export const USER_NODE_HEIGHT = 300;
export const CONCLUSION_NODE_WIDTH = 520;
export const CONCLUSION_NODE_HEIGHT = 400;
export const CLUSTER_GAP_X = 36;
export const CLUSTER_GAP_Y = 54;
export const CLUSTER_PADDING = 56;
export const DEFAULT_SCALE = 0.2;
export const MAX_MESSAGE_LENGTH = 4000;
export const CONCLUSION_CONTEXT_MAX_CHARS = 3000;
export const MAX_RECONNECT_DELAY = 30000;

// ── DOM element references ───────────────────────────────────────────────────

export const statusEl = document.getElementById('status');
export const sendBtn = document.getElementById('sendBtn');
export const clearBtn = document.getElementById('clearBtn');
export const fitBtn = document.getElementById('fitBtn');
export const zoomInBtn = document.getElementById('zoomInBtn');
export const zoomOutBtn = document.getElementById('zoomOutBtn');
export const zoomResetBtn = document.getElementById('zoomResetBtn');
export const messageInput = document.getElementById('messageInput');
export const discussionRoundsEl = document.getElementById('discussionRounds');
export const searchToggleEl = document.getElementById('searchToggle');
export const thinkToggleEl = document.getElementById('thinkToggle');
export const modelCount = document.getElementById('modelCount');
export const viewportEl = document.getElementById('canvasViewport');
export const stageEl = document.getElementById('canvasStage');
export const gridEl = document.querySelector('.canvas-grid');
export const edgeLayerEl = document.getElementById('edgeLayer');
export const minimapContentEl = document.getElementById('minimapContent');
export const minimapNodesEl = document.getElementById('minimapNodes');
export const minimapViewportEl = document.getElementById('minimapViewport');
export const selectedChipsEl = document.getElementById('selectedChips');
export const selectionSummaryEl = document.getElementById('selectionSummary');
export const selectionSummaryCountEl = document.getElementById('selectionSummaryCount');
export const selectionSummaryModelEl = document.getElementById('selectionSummaryModel');
export const selectionSummaryTextEl = document.getElementById('selectionSummaryText');
export const composerEl = document.querySelector('.composer');
export const composerModeEl = document.getElementById('composerMode');
export const saveStatusEl = document.getElementById('saveStatus');
export const selectionActionsEl = document.getElementById('selectionActions');
export const selectionContinueBtn = document.getElementById('selectionContinueBtn');
export const selectionBranchBtn = document.getElementById('selectionBranchBtn');
export const selectionClearBtn = document.getElementById('selectionClearBtn');
export const sidebarEl = document.getElementById('sidebar');
export const sidebarCollapseBtn = document.getElementById('sidebarCollapseBtn');
export const sidebarToggleBtn = document.getElementById('sidebarToggleBtn');
export const sidebarCanvasListEl = document.getElementById('sidebarCanvasList');
export const sidebarNewCanvasBtn = document.getElementById('sidebarNewCanvasBtn');
export const sidebarAvatarEl = document.getElementById('sidebarAvatar');
export const sidebarUsernameEl = document.getElementById('sidebarUsername');
export const sidebarBalanceEl = document.getElementById('sidebarBalance');
export const sidebarLogoutBtn = document.getElementById('sidebarLogoutBtn');

// ── Mutable application state ────────────────────────────────────────────────
// Wrapped in an object so other modules can mutate properties without
// running into ES module read-only binding restrictions.

export const appState = {
  socket: null,
  models: [],
  socketConnected: false,
  latestConclusionMarkdown: '',
  latestConclusionRequestId: '',
  conclusionAutoAttach: true,
  clusterCount: 0,
  latestRequestId: null,
  hoverNodeId: null,
  selectedNodeId: null,
  currentCanvasId: null,
  canvasesList: [],
  reconnectAttempts: 0,
  saveStatusTimer: null,
  selectionSummaryTimer: null,
};

// Pan / zoom / drag state
export const state = {
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

// Selection summary sidebar state
export const selectionSummaryState = {
  key: '',
  count: 0,
  bundle: '',
  text: '',
  model: 'Kimi-K2.5',
  loading: false,
  error: '',
  controller: null,
};

// ── Core data Maps / Sets ────────────────────────────────────────────────────

export const requestClusters = new Map();
export const nodes = new Map();
export const edges = new Map();
export const pendingSearchEvents = new Map();
export const selectedNodeIds = new Set();
export const positionSaveTimers = new Map();

// ── Simple state helpers ─────────────────────────────────────────────────────

export function getCluster(requestId) {
  return requestClusters.get(requestId) || null;
}

export function getSearchMode() {
  const v = searchToggleEl.value;
  if (v === 'auto') return 'auto';
  return v === 'true';
}

export function updateBalanceDisplay(balance) {
  if (sidebarBalanceEl) {
    sidebarBalanceEl.textContent = balance.toFixed(1) + ' 点';
    sidebarBalanceEl.classList.toggle('low', balance <= 10);
  }
}

export function setSaveStatus(text = '', type = '', autoHide = true) {
  if (!saveStatusEl) return;
  if (appState.saveStatusTimer) {
    window.clearTimeout(appState.saveStatusTimer);
    appState.saveStatusTimer = null;
  }
  if (!text) {
    saveStatusEl.textContent = '';
    saveStatusEl.className = 'save-status hidden';
    return;
  }
  saveStatusEl.textContent = text;
  saveStatusEl.className = `save-status ${type}`.trim();
  if (autoHide) {
    appState.saveStatusTimer = window.setTimeout(() => {
      saveStatusEl.textContent = '';
      saveStatusEl.className = 'save-status hidden';
      appState.saveStatusTimer = null;
    }, 1800);
  }
}
