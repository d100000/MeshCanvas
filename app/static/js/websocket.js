/**
 * websocket.js — WebSocket connection, event handling, and search event queue.
 */

import {
  appState, requestClusters, pendingSearchEvents, nodes,
  modelCount, searchToggleEl, sendBtn,
  sidebarUsernameEl, sidebarAvatarEl,
  MAX_RECONNECT_DELAY,
  CONCLUSION_NODE_WIDTH, MODEL_NODE_HEIGHT,
  updateBalanceDisplay,
} from './state.js';
import { escapeHtml, escapeAttribute, getDisplayName, showAlert } from './utils.js';
import { addEdge, renderEdges, scheduleRenderEdges } from './edges.js';
import {
  renderMinimap, scheduleRenderMinimap, updateClusterBounds,
  clearCanvas,
} from './canvas.js';
import {
  getUserNode, ensureModelTurn, activateTurn,
  appendTurnText, flushTurnRender, setTurnState, setNodeState,
  createConclusionNode, setSearchCollapsed, updateConclusionHint,
} from './nodes.js';
import {
  refreshStatus, updateComposerHint,
} from './selection.js';
import {
  setClusterState, createCluster, materializeClusterModels,
} from './clusters.js';
import { focusCluster } from './canvas.js';

// ── Connect ──────────────────────────────────────────────────────────────────

export function connect() {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  appState.socket = new WebSocket(`${protocol}://${window.location.host}/ws/chat`);

  appState.socket.addEventListener('open', () => {
    appState.socketConnected = true;
    const wasReconnect = appState.reconnectAttempts > 0;
    appState.reconnectAttempts = 0;
    refreshStatus();
    if (wasReconnect && appState.currentCanvasId) {
      // Dynamic import to avoid circular dep at parse time
      import('./sidebar.js').then(m => m.loadCanvasState(appState.currentCanvasId));
    }
  });

  appState.socket.addEventListener('close', (event) => {
    appState.socketConnected = false;
    if (event.code === 4401 || event.code === 4403) {
      window.location.href = '/login';
      return;
    }
    for (const cluster of requestClusters.values()) {
      cluster.isRunning = false;
      cluster.isCancelling = false;
    }
    refreshStatus();
    const delay = Math.min(1200 * Math.pow(1.5, appState.reconnectAttempts), MAX_RECONNECT_DELAY);
    appState.reconnectAttempts += 1;
    setTimeout(connect, delay);
  });

  appState.socket.addEventListener('message', (event) => {
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

// ── Event handler ────────────────────────────────────────────────────────────

function handleEvent(payload) {
  switch (payload.type) {
    case 'meta':
      appState.models = payload.models || [];
      if (modelCount) modelCount.textContent = `${appState.models.length} 个模型`;
      {
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
    case 'user': {
      appState.latestRequestId = payload.request_id;
      // 从队列取出原始问题（避免在用户节点展示完整上下文拼接内容）
      const displayInfo = appState._pendingUserDisplay?.shift();
      createCluster({
        requestId: payload.request_id,
        userMessage: payload.content,
        displayMessage: displayInfo?.displayMessage || '',
        contextNodeCount: displayInfo?.contextNodeCount || 0,
        discussionRounds: payload.discussion_rounds || 1,
        models: payload.models || [],
        searchEnabled: Boolean(payload.search_enabled),
        thinkEnabled: Boolean(payload.think_enabled),
        parentRequestId: payload.parent_request_id || null,
        sourceModel: payload.source_model || null,
        sourceRound: payload.source_round || null,
      });
      // 为继续对话创建上下文连线（从选中节点到新用户节点）
      if (!payload.parent_request_id && appState._pendingContextQueue?.length > 0) {
        const contextNodeIds = appState._pendingContextQueue.shift();
        if (contextNodeIds) {
          const userNodeId = `user-${payload.request_id}`;
          for (const ctxNodeId of contextNodeIds) {
            if (nodes.has(ctxNodeId)) {
              addEdge({
                id: `edge-ctx-${ctxNodeId}-${userNodeId}`,
                sourceId: ctxNodeId,
                targetId: userNodeId,
                type: 'context_continuation',
              });
            }
          }
          scheduleRenderEdges();
        }
      }
      setClusterState(payload.request_id, {
        isRunning: true,
        isCancelling: false,
        badgeText: Boolean(payload.search_enabled) ? '排队搜索' : '排队执行',
      });
      focusCluster(payload.request_id);
      flushPendingSearchEvents(payload.request_id);
      break;
    }
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
        appendTurnText(payload.request_id, payload.model, payload.round, `\n\n[错误] ${payload.content}`);
        flushTurnRender(payload.request_id, payload.model, payload.round);
        setNodeState(payload.request_id, payload.model, payload.content?.includes('超时') ? `第 ${payload.round} 轮超时` : `第 ${payload.round} 轮失败`);
        setTurnState(payload.request_id, payload.model, payload.round, '失败');
      } else if (payload.request_id) {
        setClusterState(payload.request_id, { isRunning: false, isCancelling: false, badgeText: '请求失败' });
      } else {
        showAlert(payload.content);
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
      appState.latestRequestId = payload.request_id || appState.latestRequestId;
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

// ── Preprocess / Search organized handlers ───────────────────────────────────

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

// ── Conclusion handlers ──────────────────────────────────────────────────────

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

  // 使用实际集群边界计算结论节点位置，避免与搜索面板展开后的节点重叠
  updateClusterBounds(payload.request_id);
  let cx = cluster.baseX - CONCLUSION_NODE_WIDTH / 2;
  let cy = cluster.bbox.y + cluster.bbox.height + 40;

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

  appState.latestConclusionMarkdown = payload.markdown || '';
  appState.latestConclusionRequestId = payload.request_id || '';
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

// ── Search event queue ───────────────────────────────────────────────────────

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
    const qCount = payload.query_count || 1;
    const isSmart = payload.smart_search;
    userNode.searchQueryEl.textContent = isSmart
      ? `多方向搜索（${qCount} 个方向）`
      : (payload.query || '正在搜索');
    userNode.searchBadgeEl.textContent = '搜索中';
    userNode.searchToggleBtn?.classList.add('hidden');
    const details = payload.query_details || [];
    const detailHtml = details.length > 0
      ? details.map(d => `<div class="search-item-snippet search-direction">🔍 ${escapeHtml(d)}</div>`).join('')
      : '<div class="search-item-snippet">正在通过 Firecrawl 获取实时网页结果...</div>';
    userNode.searchResultsEl.innerHTML = detailHtml;
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
  const queriesUsed = payload.queries_used || [];
  const queryLabel = queriesUsed.length > 1
    ? `${queriesUsed.length} 个搜索方向 · ${payload.count || 0} 条结果`
    : `${payload.query || ''} · ${payload.count || 0} 条结果`;
  userNode.searchQueryEl.textContent = queryLabel;
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

