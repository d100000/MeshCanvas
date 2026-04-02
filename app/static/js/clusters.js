/**
 * clusters.js — Cluster creation, placement, materialization, replay, and state.
 */

import {
  state, appState, requestClusters, nodes,
  viewportEl,
  MODEL_NODE_WIDTH, MODEL_NODE_HEIGHT,
  USER_NODE_WIDTH, USER_NODE_HEIGHT,
  CLUSTER_GAP_X, CLUSTER_GAP_Y, CLUSTER_PADDING,
  CONCLUSION_NODE_WIDTH, CONCLUSION_NODE_HEIGHT,
  getCluster, setSaveStatus,
  positionSaveTimers,
} from './state.js';
import { escapeHtml, getDisplayName } from './utils.js';
import { addEdge, renderEdges, scheduleRenderEdges } from './edges.js';
import {
  renderMinimap, scheduleRenderMinimap, updateClusterBounds,
  findAvailableBox,
} from './canvas.js';
import {
  createUserNode, createModelNode, createConclusionNode,
  ensureModelTurn, activateTurn, appendTurnText, flushTurnRender,
  setTurnState, setNodeState, getUserNode, getModelNode,
  setSearchCollapsed, updateConclusionHint,
} from './nodes.js';
import { refreshStatus, updateComposerHint } from './selection.js';

// ── Cluster state management ─────────────────────────────────────────────────

export function setClusterState(requestId, patch = {}) {
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
    userNode.cancelBtn.disabled = !appState.socketConnected || cluster.isCancelling || !cluster.isRunning;
    userNode.cancelBtn.textContent = cluster.isCancelling ? '取消中...' : '停止';
  }
  updateClusterVisualState(requestId);
  scheduleRenderMinimap();
  refreshStatus();
}

export function updateClusterVisualState(requestId) {
  const cluster = getCluster(requestId);
  if (!cluster) return;
  const running = Boolean(cluster.isRunning || cluster.isCancelling);
  const nodeIds = [cluster.userNodeId, ...cluster.modelNodeIds];
  for (const nodeId of nodeIds) {
    const node = nodes.get(nodeId);
    node?.root.classList.toggle('cluster-running', running);
  }
}

// ── Materialize cluster models ───────────────────────────────────────────────

export function materializeClusterModels(requestId) {
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

// ── Position save ────────────────────────────────────────────────────────────

export function schedulePositionSave(requestId) {
  if (!appState.currentCanvasId) return;
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

// ── Create cluster (live) ────────────────────────────────────────────────────

export function createCluster({
  requestId,
  userMessage,
  displayMessage,
  contextNodeCount,
  discussionRounds,
  models: clusterModels,
  searchEnabled,
  thinkEnabled,
  parentRequestId,
  sourceModel,
  sourceRound,
}) {
  const activeModels = clusterModels.length ? clusterModels : appState.models;
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
    displayMessage,
    contextNodeCount,
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

// ── Place cluster ────────────────────────────────────────────────────────────

export function placeCluster({ modelCount, parentRequestId, sourceModel }) {
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
    const colCount = 3;
    const colSpacing = footprintWidth + CLUSTER_PADDING * 2 + 60;
    const rowSpacing = footprintHeight + CLUSTER_PADDING * 2 + 60;
    const baseX = centerWorldX - colCount * colSpacing / 2;
    const baseY = centerWorldY - footprintHeight / 2;
    const candidates = [];
    for (let i = 0; i < 42; i += 1) {
      candidates.push({
        x: baseX + (i % colCount) * colSpacing,
        y: baseY + Math.floor(i / colCount) * rowSpacing,
      });
    }
    // 按距视口中心距离排序，优先使用离用户最近的位置
    candidates.sort((a, b) => {
      return ((a.x - centerWorldX) ** 2 + (a.y - centerWorldY) ** 2)
        - ((b.x - centerWorldX) ** 2 + (b.y - centerWorldY) ** 2);
    });
    bbox = findAvailableBox(candidates, footprintWidth, footprintHeight);
    appState.clusterCount += 1;
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
    modelY: bbox.y + estimatedUserHeight + CLUSTER_GAP_Y,
  };
}

// ── Replay cluster (from saved state) ────────────────────────────────────────

export function replayCluster(req) {
  const {
    request_id, user_message, models: reqModels, discussion_rounds,
    search_enabled, think_enabled, parent_request_id, source_model,
    source_round, position, results,
  } = req;
  const activeModels = reqModels.length ? reqModels : appState.models;
  const modelCountVal = activeModels.length;
  const modelRowWidth = modelCountVal * MODEL_NODE_WIDTH + Math.max(0, modelCountVal - 1) * CLUSTER_GAP_X;
  const footprintWidth = Math.max(USER_NODE_WIDTH, modelRowWidth);
  const estimatedUserHeight = USER_NODE_HEIGHT + 250;
  const footprintHeight = estimatedUserHeight + CLUSTER_GAP_Y + MODEL_NODE_HEIGHT;

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
    const colCount = 3;
    const colSpacing = footprintWidth + CLUSTER_PADDING * 2 + 60;
    const rowSpacing = footprintHeight + CLUSTER_PADDING * 2 + 60;
    const baseX = cx - colCount * colSpacing / 2;
    const baseY = cy - footprintHeight / 2;
    const candidates = [];
    for (let i = 0; i < 42; i += 1) {
      candidates.push({
        x: baseX + (i % colCount) * colSpacing,
        y: baseY + Math.floor(i / colCount) * rowSpacing,
      });
    }
    candidates.sort((a, b) => {
      return ((a.x - cx) ** 2 + (a.y - cy) ** 2) - ((b.x - cx) ** 2 + (b.y - cy) ** 2);
    });
    const bboxResult = findAvailableBox(candidates, footprintWidth, footprintHeight);
    const userX2 = bboxResult.x + (bboxResult.width - USER_NODE_WIDTH) / 2;
    const mRowWidth = modelCountVal * MODEL_NODE_WIDTH + Math.max(0, modelCountVal - 1) * CLUSTER_GAP_X;
    layout = {
      userX: userX2,
      userY: bboxResult.y,
      modelStartX: bboxResult.x + (bboxResult.width - mRowWidth) / 2,
      modelY: bboxResult.y + estimatedUserHeight + CLUSTER_GAP_Y,
      centerX: bboxResult.x + bboxResult.width / 2,
      bbox: bboxResult,
    };
    appState.clusterCount += 1;
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
      const replayTurn = ensureModelTurn(request_id, model, rd.round);
      if (rd.content) {
        appendTurnText(request_id, model, rd.round, rd.content);
        flushTurnRender(request_id, model, rd.round);
      } else if (rd.error_text) {
        // 失败轮次：显示错误信息而非空白
        appendTurnText(request_id, model, rd.round, `> ⚠ 该轮请求未成功\n>\n> ${rd.error_text}\n\n_可点击「重试当前」重新请求。_`);
        flushTurnRender(request_id, model, rd.round);
      } else if (replayTurn) {
        // 无内容也无错误：显示空白占位
        replayTurn.mdEl.innerHTML = '<div class="empty-response-hint">'
          + '<span class="empty-response-icon">⚠</span>'
          + '<p>该轮未获取到模型回复</p>'
          + '<p class="empty-response-sub">可尝试「重试当前」重新请求。</p>'
          + '</div>';
        if (replayTurn.skeletonEl) { replayTurn.skeletonEl.remove(); replayTurn.skeletonEl = null; }
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
    appState.latestConclusionMarkdown = summary.summary_markdown;
    appState.latestConclusionRequestId = request_id;
  }

  updateClusterBounds(request_id);
  setClusterState(request_id, { isRunning: false, isCancelling: false, badgeText: '已完成' });
  renderEdges();
}
