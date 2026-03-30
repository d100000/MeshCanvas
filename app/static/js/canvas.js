/**
 * canvas.js — Canvas pan/zoom/transform, minimap, cluster bounds, clearCanvas.
 */

import {
  state, appState, requestClusters, nodes, edges, pendingSearchEvents,
  selectedNodeIds, positionSaveTimers,
  viewportEl, stageEl, gridEl, edgeLayerEl, zoomResetBtn,
  minimapContentEl, minimapNodesEl, minimapViewportEl,
  selectionActionsEl, saveStatusEl,
  CLUSTER_PADDING, USER_NODE_WIDTH, USER_NODE_HEIGHT,
  MODEL_NODE_WIDTH, MODEL_NODE_HEIGHT,
  getCluster, setSaveStatus,
} from './state.js';
import { clamp } from './utils.js';
import { getNodeDimensions, renderEdges, scheduleRenderEdges } from './edges.js';
// Lazy imports to avoid circular deps at parse time
// (these functions are only called at runtime from event handlers)
import { updateSelectionActions, clearSelection, clearSelectionSummary, updateComposerHint, refreshStatus } from './selection.js';
import { updateConclusionHint } from './nodes.js';

let _rafMinimapScheduled = false;

export function scheduleRenderMinimap() {
  if (_rafMinimapScheduled) return;
  _rafMinimapScheduled = true;
  requestAnimationFrame(() => {
    _rafMinimapScheduled = false;
    renderMinimap();
  });
}

export function applyTransform() {
  const transform = `translate(${state.offsetX}px, ${state.offsetY}px) scale(${state.scale})`;
  stageEl.style.transform = transform;
  gridEl.style.transform = transform;
  edgeLayerEl.style.transform = transform;
  zoomResetBtn.textContent = `${Math.round(state.scale * 100)}%`;
  updateSelectionActions();
}

export function bindCanvasPan() {
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
        const factor = event.deltaY < 0 ? 1.02 : 0.98;
        zoomAtPoint(factor, event.clientX, event.clientY);
      } else {
        state.offsetX -= event.deltaX;
        state.offsetY -= event.deltaY;
        applyTransform();
        scheduleRenderMinimap();
      }
    },
    { passive: false }
  );
}

export function zoomAtPoint(factor, clientX, clientY) {
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

export function setZoom(nextScale) {
  const rect = viewportEl.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const factor = nextScale / state.scale;
  zoomAtPoint(factor, centerX, centerY);
}

export function centerViewportOn(worldX, worldY) {
  const rect = viewportEl.getBoundingClientRect();
  state.offsetX = rect.width / 2 - worldX * state.scale;
  state.offsetY = rect.height / 2 - worldY * state.scale;
  applyTransform();
  renderMinimap();
}

export function focusCluster(requestId) {
  const cluster = requestClusters.get(requestId);
  if (!cluster) return;
  centerViewportOn(cluster.bbox.x + cluster.bbox.width / 2, cluster.bbox.y + cluster.bbox.height / 2);
}

export function renderMinimap() {
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
    const active = node.nodeId === (appState.hoverNodeId || appState.selectedNodeId);
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

export function updateClusterBounds(requestId) {
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

export function refreshAllClusterBounds() {
  for (const rid of requestClusters.keys()) {
    updateClusterBounds(rid);
  }
}

export function findAvailableBox(candidates, width, height) {
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

export function hasClusterOverlap(nextBox) {
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

export function clearCanvas() {
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
  appState.clusterCount = 0;
  appState.latestRequestId = null;
  appState.latestConclusionMarkdown = '';
  appState.latestConclusionRequestId = '';
  appState.hoverNodeId = null;
  appState.selectedNodeId = null;
  selectionActionsEl?.classList.add('hidden');
  setSaveStatus('');
  applyTransform();
  renderMinimap();
  updateComposerHint();
  updateConclusionHint();
  refreshStatus();
}

export function toggleSidebar(show) {
  const shell = document.querySelector('.shell');
  if (typeof show !== 'boolean') show = shell.classList.contains('sidebar-collapsed');
  shell.classList.toggle('sidebar-collapsed', !show);
  shell.classList.toggle('sidebar-open', show);
  document.documentElement.style.setProperty('--sidebar-width', show ? '260px' : '0px');
}
