/**
 * nodes.js — Node CRUD (user / model / conclusion), turn management,
 *             drag binding, copy/retry/branch, and node interaction.
 */

import {
  state, appState, nodes, selectedNodeIds,
  stageEl, discussionRoundsEl, searchToggleEl, thinkToggleEl,
  MODEL_NODE_WIDTH, MODEL_NODE_HEIGHT,
  USER_NODE_WIDTH, USER_NODE_HEIGHT,
  CONCLUSION_NODE_WIDTH,
  getCluster, getSearchMode,
} from './state.js';
import { escapeHtml, escapeAttribute, getDisplayName, flashButtonLabel, summarizeText, showPreview, showAlert } from './utils.js';
import { scheduleRenderEdges } from './edges.js';
import { scheduleRenderMinimap, updateClusterBounds } from './canvas.js';
import { renderSelectionState, updateComposerHint, updateSelectionActions } from './selection.js';
// schedulePositionSave is in clusters.js — runtime circular dep (safe for ES modules)
import { schedulePositionSave } from './clusters.js';

// ── Getters ──────────────────────────────────────────────────────────────────

export function getModelNode(requestId, model) {
  return nodes.get(`model-${requestId}-${model}`) || null;
}

export function getUserNode(requestId) {
  return nodes.get(`user-${requestId}`) || null;
}

export function getConclusionNode(requestId) {
  return nodes.get(`conclusion-${requestId}`) || null;
}

export function getLatestRound(node) {
  return Math.max(...Array.from(node.turns.keys()), 1);
}

// ── Conclusion hint ──────────────────────────────────────────────────────────

export function updateConclusionHint() {
  const hintEl = document.getElementById('conclusionHint');
  if (!hintEl) return;
  if (appState.conclusionAutoAttach && appState.latestConclusionMarkdown) {
    const charCount = appState.latestConclusionMarkdown.length;
    hintEl.textContent = `下一轮将附带结论文档（${charCount} 字）`;
    hintEl.classList.remove('hidden');
  } else {
    hintEl.classList.add('hidden');
    hintEl.textContent = '';
  }
}

// ── Search collapsed ─────────────────────────────────────────────────────────

export function setSearchCollapsed(userNode, collapsed) {
  if (!userNode?.searchResultsEl || !userNode?.searchToggleBtn) return;
  userNode.searchCollapsed = Boolean(collapsed);
  userNode.searchResultsEl.classList.toggle('collapsed', userNode.searchCollapsed);
  userNode.searchToggleBtn.textContent = userNode.searchCollapsed ? '展开结果' : '收起结果';
}

// ── Turn management ──────────────────────────────────────────────────────────

export function updateTurnSummary(turn) {
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

export function ensureModelTurn(requestId, model, round) {
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
    <div class="turn-meta">状态：<span class="turn-state">等待中</span><span class="turn-word-count">0 字</span><span class="turn-summary-chip">摘要：等待摘要</span></div>
    <div class="md"></div>
  `;

  node.tabsEl.appendChild(button);
  node.panelsEl.appendChild(panel);

  // 插入骨架屏 Loading 占位
  const skeletonEl = document.createElement('div');
  skeletonEl.className = 'skeleton-loading';
  skeletonEl.innerHTML = '<div class="skeleton-line w80"></div><div class="skeleton-line w60"></div><div class="skeleton-line w90"></div>';
  panel.querySelector('.md').after(skeletonEl);

  const turn = {
    round,
    btn: button,
    panel,
    stateEl: panel.querySelector('.turn-state'),
    wordCountEl: panel.querySelector('.turn-word-count'),
    summaryEl: button.querySelector('.tab-btn-summary'),
    summaryChipEl: panel.querySelector('.turn-summary-chip'),
    mdEl: panel.querySelector('.md'),
    skeletonEl,
    raw: '',
    summary: '等待摘要',
    renderTimer: null,
  };
  node.turns.set(round, turn);
  return turn;
}

export function activateTurn(requestId, model, round) {
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

export function appendTurnText(requestId, model, round, chunk) {
  const turn = ensureModelTurn(requestId, model, round);
  if (!turn) return;
  turn.raw += chunk;
  scheduleTurnRender(turn);
}

export function scheduleTurnRender(turn, force = false) {
  if (!turn) return;

  const render = () => {
    turn.renderTimer = null;
    updateTurnSummary(turn);
    // 更新字数统计
    if (turn.wordCountEl) {
      const len = turn.raw.length;
      turn.wordCountEl.textContent = len >= 1000 ? `${(len / 1000).toFixed(1)}k 字` : `${len} 字`;
    }
    // 移除骨架屏（如果有）
    if (turn.skeletonEl) { turn.skeletonEl.remove(); turn.skeletonEl = null; }
    turn.mdEl.innerHTML = window.renderMarkdown ? window.renderMarkdown(turn.raw) : escapeHtml(turn.raw);
    // 智能滚动：仅当用户已在底部附近时才自动滚动，避免打断回读
    const threshold = 60;
    const isNearBottom = turn.panel.scrollHeight - turn.panel.scrollTop - turn.panel.clientHeight < threshold;
    if (isNearBottom) {
      turn.panel.scrollTop = turn.panel.scrollHeight;
    }
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

export function flushTurnRender(requestId, model, round) {
  const turn = ensureModelTurn(requestId, model, round);
  if (!turn) return;
  scheduleTurnRender(turn, true);
}

export function setTurnState(requestId, model, round, text) {
  const turn = ensureModelTurn(requestId, model, round);
  if (!turn) return;
  turn.stateEl.textContent = text;
}

export function setNodeState(requestId, model, text) {
  const node = getModelNode(requestId, model);
  if (!node) return;
  node.badge.textContent = text;
  // badge 动画：生成中时显示 spinner
  node.badge.classList.toggle('generating', text.includes('生成中'));
  refreshClusterOutline(requestId);
}

export function refreshClusterOutline(requestId) {
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

// ── Node creation ────────────────────────────────────────────────────────────

export function createUserNode({
  nodeId,
  requestId,
  x,
  y,
  content,
  displayMessage,
  contextNodeCount,
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
      ${contextNodeCount > 0 ? `<div class="context-indicator">基于 ${contextNodeCount} 个节点的上下文继续对话</div>` : ''}
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
  root.querySelector('.user-message').textContent = (displayMessage && contextNodeCount > 0) ? displayMessage : content;
  root.classList.add('entering');
  stageEl.appendChild(root);
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('entering')));
  bindNodeInteractions(root, nodeId, requestId);
  const node = {
    nodeId,
    requestId,
    type: 'user',
    x,
    y,
    fullContent: content,
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

export function createModelNode({ nodeId, requestId, model, x, y }) {
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
        <button type="button" class="small-btn preview-btn-trigger">预览</button>
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
  root.classList.add('entering');
  stageEl.appendChild(root);
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('entering')));
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
  root.querySelector('.preview-btn-trigger').addEventListener('click', (e) => {
    const turn = node.turns.get(node.activeRound);
    if (!turn || !turn.raw.trim()) { flashButtonLabel(e.currentTarget, '无内容'); return; }
    showPreview({
      title: getDisplayName(model),
      markdown: turn.raw,
    });
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
  node.branchInput.addEventListener('input', () => {
    node.branchInput.style.height = 'auto';
    node.branchInput.style.height = Math.min(node.branchInput.scrollHeight, 160) + 'px';
  });
}

export function createConclusionNode({ nodeId, requestId, x, y, model, markdown, status }) {
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
      <button type="button" class="small-btn preview-btn-trigger">预览</button>
      <button type="button" class="small-btn conclusion-copy">复制 Markdown</button>
      <button type="button" class="small-btn conclusion-download">下载 MD</button>
      <button type="button" class="small-btn conclusion-retry">重试结论</button>
      <button type="button" class="small-btn conclusion-attach ${appState.conclusionAutoAttach ? 'active' : ''}">
        ${appState.conclusionAutoAttach ? '自动注入：开' : '自动注入：关'}
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

  root.classList.add('entering');
  stageEl.appendChild(root);
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('entering')));
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

  root.querySelector('.conclusion-download').addEventListener('click', (e) => {
    const text = node.markdown || '';
    if (!text) { flashButtonLabel(e.currentTarget, '无内容'); return; }
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `conclusion-${requestId.slice(0, 8)}-${new Date().toISOString().slice(0, 10)}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    flashButtonLabel(e.currentTarget, '已下载');
  });

  root.querySelector('.conclusion-attach').addEventListener('click', (e) => {
    appState.conclusionAutoAttach = !appState.conclusionAutoAttach;
    e.currentTarget.textContent = appState.conclusionAutoAttach ? '自动注入：开' : '自动注入：关';
    e.currentTarget.classList.toggle('active', appState.conclusionAutoAttach);
    updateConclusionHint();
  });

  root.querySelector('.conclusion-retry').addEventListener('click', (e) => {
    retryConclusion(requestId, e.currentTarget);
  });

  root.querySelector('.preview-btn-trigger').addEventListener('click', () => {
    if (!node.markdown) return;
    showPreview({
      title: '最终结论',
      markdown: node.markdown,
    });
  });

  return node;
}

// ── Node interactions ────────────────────────────────────────────────────────

export function bindNodeInteractions(root, nodeId, requestId) {
  bindNodeDrag(root, nodeId, requestId);
  root.addEventListener('mouseenter', () => {
    appState.hoverNodeId = nodeId;
    renderSelectionState();
  });
  root.addEventListener('mouseleave', () => {
    if (appState.hoverNodeId === nodeId) {
      appState.hoverNodeId = null;
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
      appState.selectedNodeId = selectedNodeIds.size === 1 ? Array.from(selectedNodeIds)[0] : nodeId;
    } else {
      selectedNodeIds.clear();
      selectedNodeIds.add(nodeId);
      appState.selectedNodeId = nodeId;
    }
    if (!selectedNodeIds.size) {
      state.selectionSource = 'none';
    }
    renderSelectionState();
    updateComposerHint();
  });
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

// ── Copy / Retry / Branch ────────────────────────────────────────────────────

export function openBranchComposer(node) {
  if (!node || node.type !== 'model' || !node.turns.size) return;
  node.branchBox.classList.remove('hidden');
  node.branchInput.focus();
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

function retryCurrentTurn(node, button) {
  const round = node.activeRound || getLatestRound(node);
  const turn = node.turns.get(round);
  if (!round || !turn?.raw?.trim() || appState.socket?.readyState !== WebSocket.OPEN) {
    flashButtonLabel(button, '不可重试');
    return;
  }
  try {
    appState.socket.send(
      JSON.stringify({
        action: 'retry_model',
        source_request_id: node.requestId,
        source_model: node.model,
        source_round: round,
        canvas_id: appState.currentCanvasId,
      })
    );
  } catch (_e) {
    flashButtonLabel(button, '发送失败');
    return;
  }
  flashButtonLabel(button, '已发起');
}

export function sendBranch(node, overrideMessage = null) {
  const message = String(overrideMessage ?? node.branchInput.value).trim();
  if (!message || appState.socket?.readyState !== WebSocket.OPEN) {
    return;
  }
  const round = node.activeRound || getLatestRound(node);
  if (!round) return;
  try {
    appState.socket.send(
      JSON.stringify({
        action: 'branch_chat',
        message,
        source_request_id: node.requestId,
        source_model: node.model,
        source_round: round,
        discussion_rounds: Number(discussionRoundsEl.value || 2),
        search_enabled: getSearchMode(),
        think_enabled: thinkToggleEl.checked,
        canvas_id: appState.currentCanvasId,
      })
    );
  } catch (_e) {
    showAlert('分支发送失败，请检查网络连接。');
    return;
  }
  node.branchInput.value = '';
  node.branchBox.classList.add('hidden');
}

export function cancelRequest(requestId) {
  if (!requestId || appState.socket?.readyState !== WebSocket.OPEN) return;
  const cluster = getCluster(requestId);
  if (!cluster || !cluster.isRunning || cluster.isCancelling) return;
  try { appState.socket.send(JSON.stringify({ action: 'cancel_request', request_id: requestId })); } catch (_e) { return; }
  // setClusterState imported at runtime via clusters.js — use inline logic here
  // to avoid circular import issues with clusters.js
  cluster.isCancelling = true;
  const userNode = getUserNode(requestId);
  if (userNode?.badge) userNode.badge.textContent = '取消中...';
  if (userNode?.cancelBtn) {
    userNode.cancelBtn.disabled = true;
    userNode.cancelBtn.textContent = '取消中...';
  }
}

export function retryConclusion(requestId, button) {
  if (!requestId || appState.socket?.readyState !== WebSocket.OPEN) {
    flashButtonLabel(button, '不可重试');
    return;
  }
  try {
    appState.socket.send(
      JSON.stringify({
        action: 'retry_conclusion',
        source_request_id: requestId,
        canvas_id: appState.currentCanvasId,
      })
    );
  } catch (_e) {
    flashButtonLabel(button, '发送失败');
    return;
  }
  button.disabled = true;
  flashButtonLabel(button, '已发起');
}
