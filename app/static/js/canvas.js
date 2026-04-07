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
let _minimapThrottleTimer = null;

export function scheduleRenderMinimap() {
  if (_rafMinimapScheduled) return;
  if (_minimapThrottleTimer) return;
  _minimapThrottleTimer = setTimeout(() => {
    _minimapThrottleTimer = null;
    _rafMinimapScheduled = true;
    requestAnimationFrame(() => {
      _rafMinimapScheduled = false;
      renderMinimap();
    });
  }, 33);
}

// Cluster-frame layer (B2): rendered between edges and nodes, holds the
// dashed bounding-box outlines for each cluster. Created lazily on first
// applyTransform so HTML structure changes are localized to canvas.js.
let _clusterFrameLayerEl = null;
function ensureClusterFrameLayer() {
  if (_clusterFrameLayerEl) return _clusterFrameLayerEl;
  _clusterFrameLayerEl = document.getElementById('clusterFrameLayer');
  if (!_clusterFrameLayerEl) {
    _clusterFrameLayerEl = document.createElement('div');
    _clusterFrameLayerEl.id = 'clusterFrameLayer';
    _clusterFrameLayerEl.className = 'cluster-frame-layer';
    /* Insert between edge-layer and stage so frames sit under the nodes. */
    edgeLayerEl.parentNode.insertBefore(_clusterFrameLayerEl, stageEl);
  }
  return _clusterFrameLayerEl;
}

export function applyTransform() {
  const transform = `translate(${state.offsetX}px, ${state.offsetY}px) scale(${state.scale})`;
  stageEl.style.transform = transform;
  gridEl.style.transform = transform;
  edgeLayerEl.style.transform = transform;
  const frameLayer = ensureClusterFrameLayer();
  if (frameLayer) frameLayer.style.transform = transform;
  zoomResetBtn.textContent = `${Math.round(state.scale * 100)}%`;
  updateSelectionActions();
}

// ── Cluster visual frames (B2) ──────────────────────────────────────────────
const FRAME_PADDING = 24;
let _rafClusterFramesScheduled = false;

export function scheduleRenderClusterFrames() {
  if (_rafClusterFramesScheduled) return;
  _rafClusterFramesScheduled = true;
  requestAnimationFrame(() => {
    _rafClusterFramesScheduled = false;
    renderClusterFrames();
  });
}

export function renderClusterFrames() {
  const layer = ensureClusterFrameLayer();
  if (!layer) return;
  if (!requestClusters.size) {
    layer.innerHTML = '';
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

    /* Label = first ~30 chars of the user's question (cached on cluster). */
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
  layer.innerHTML = '';
  layer.appendChild(frag);
}

// Marquee (box-select) state — module-private. Set during a left-drag from
// empty canvas with no Space key held; cleared on pointerup / blur.
let _marqueeStart = null; // { worldX, worldY, clientX, clientY, additive, baseSelection }
let _marqueeEl = null;
// Tracks whether the spacebar is currently held — toggles plain drag from
// box-select to pan, matching Figma/Miro convention.
let _spaceHeld = false;
export function isSpaceHeld() { return _spaceHeld; }
export function setSpaceHeld(v) {
  _spaceHeld = !!v;
  viewportEl.classList.toggle('space-held', _spaceHeld);
}

function ensureMarqueeEl() {
  if (_marqueeEl) return _marqueeEl;
  _marqueeEl = document.createElement('div');
  _marqueeEl.className = 'marquee';
  viewportEl.appendChild(_marqueeEl);
  return _marqueeEl;
}
function cancelMarquee() {
  if (_marqueeEl) { _marqueeEl.remove(); _marqueeEl = null; }
  _marqueeStart = null;
}
function updateMarquee(event) {
  if (!_marqueeStart) return;
  const rect = viewportEl.getBoundingClientRect();
  const x1 = Math.min(_marqueeStart.clientX, event.clientX) - rect.left;
  const y1 = Math.min(_marqueeStart.clientY, event.clientY) - rect.top;
  const x2 = Math.max(_marqueeStart.clientX, event.clientX) - rect.left;
  const y2 = Math.max(_marqueeStart.clientY, event.clientY) - rect.top;
  const el = ensureMarqueeEl();
  el.style.left = `${x1}px`;
  el.style.top = `${y1}px`;
  el.style.width = `${x2 - x1}px`;
  el.style.height = `${y2 - y1}px`;

  /* Live selection update so the user sees what they're about to grab. */
  const worldX2 = (event.clientX - rect.left - state.offsetX) / state.scale;
  const worldY2 = (event.clientY - rect.top - state.offsetY) / state.scale;
  const minX = Math.min(_marqueeStart.worldX, worldX2);
  const minY = Math.min(_marqueeStart.worldY, worldY2);
  const maxX = Math.max(_marqueeStart.worldX, worldX2);
  const maxY = Math.max(_marqueeStart.worldY, worldY2);

  const inside = new Set();
  for (const node of nodes.values()) {
    const dim = getNodeDimensions(node);
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
  if (_marqueeStart.additive && _marqueeStart.baseSelection) {
    for (const id of _marqueeStart.baseSelection) selectedNodeIds.add(id);
  }
  for (const id of inside) selectedNodeIds.add(id);
  appState.selectedNodeId = selectedNodeIds.size === 1 ? Array.from(selectedNodeIds)[0] : null;
  state.selectionSource = 'marquee';
  /* Don't call renderSelectionState (would trigger circular import); just
     toggle the basic .selected class on dragged-into nodes. */
  for (const node of nodes.values()) {
    node.root.classList.toggle('selected', selectedNodeIds.has(node.nodeId));
  }
  updateSelectionActions();
  scheduleRenderClusterFrames();
}
function finishMarquee(moved) {
  if (!_marqueeStart) return;
  const additive = _marqueeStart.additive;
  cancelMarquee();
  if (!moved && !additive) {
    /* Treat as a click on empty canvas → clear selection */
    clearSelection();
  }
  /* When moved=true, selection was live-updated by updateMarquee. */
}

export function bindCanvasPan() {
  window.addEventListener('blur', () => {
    state.panning = false;
    viewportEl.classList.remove('panning');
    setSpaceHeld(false);
    cancelMarquee();
  });

  viewportEl.addEventListener('pointerdown', (event) => {
    if (event.target.closest('.node, .minimap, .selection-actions, .needs-setup-banner')) return;
    if (event.button !== 0 && event.button !== 1) return; // left or middle
    /* Decide mode:
       - Middle mouse OR Space held → pan
       - Otherwise → marquee box-select */
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
      applyTransform();
      scheduleRenderMinimap();
      return;
    }
    if (_marqueeStart) {
      updateMarquee(event);
    }
  });

  function stopPan(event) {
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
      // 可滚动容器内的滚轮事件应交给浏览器处理（而非触发画布缩放/平移）
      const scrollable = event.target.closest('.tab-panel, .conclusion-body');
      if (scrollable && !event.ctrlKey && !event.metaKey) {
        const { scrollTop, scrollHeight, clientHeight } = scrollable;
        if (scrollHeight > clientHeight) {
          const atTop = scrollTop <= 0;
          const atBottom = scrollTop + clientHeight >= scrollHeight - 1;
          // 未到达滚动边界时，让浏览器处理内容滚动
          if (!(atBottom && event.deltaY > 0) && !(atTop && event.deltaY < 0)) {
            return;
          }
        }
      }
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

  state.scale = clamp(state.scale * factor, 0.05, 3.0);
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

// ── 视口动画 ────────────────────────────────────────────────────────────────

let _animatingViewport = false;

export function animateViewportTo(worldX, worldY, targetScale, duration = 280) {
  _animatingViewport = true;
  const rect = viewportEl.getBoundingClientRect();
  const targetOffsetX = rect.width / 2 - worldX * targetScale;
  const targetOffsetY = rect.height / 2 - worldY * targetScale;
  const startX = state.offsetX, startY = state.offsetY, startScale = state.scale;
  const startTime = performance.now();

  function step(now) {
    const t = Math.min((now - startTime) / duration, 1);
    const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
    state.offsetX = startX + (targetOffsetX - startX) * ease;
    state.offsetY = startY + (targetOffsetY - startY) * ease;
    state.scale = startScale + (targetScale - startScale) * ease;
    applyTransform();
    scheduleRenderMinimap();
    if (t < 1) {
      requestAnimationFrame(step);
    } else {
      _animatingViewport = false;
    }
  }
  requestAnimationFrame(step);
}

export function centerViewportOn(worldX, worldY) {
  animateViewportTo(worldX, worldY, state.scale);
}

export function focusCluster(requestId) {
  const cluster = requestClusters.get(requestId);
  if (!cluster) return;
  centerViewportOn(cluster.bbox.x + cluster.bbox.width / 2, cluster.bbox.y + cluster.bbox.height / 2);
}

export function fitAllNodes() {
  if (nodes.size === 0) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const node of nodes.values()) {
    const { width, height } = getNodeDimensions(node);
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x + width);
    maxY = Math.max(maxY, node.y + height);
  }
  const padding = 80;
  const rect = viewportEl.getBoundingClientRect();
  const scaleX = rect.width / (maxX - minX + padding * 2);
  const scaleY = rect.height / (maxY - minY + padding * 2);
  const newScale = clamp(Math.min(scaleX, scaleY), 0.05, 1.0);
  animateViewportTo((minX + maxX) / 2, (minY + maxY) / 2, newScale);
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

  // 存储小地图坐标映射参数，供拖拽使用
  _minimapMapping.minX = minX;
  _minimapMapping.minY = minY;
  _minimapMapping.scale = scale;
  _minimapMapping.offsetX = offsetX;
  _minimapMapping.offsetY = offsetY;

  minimapContentEl.onclick = (event) => {
    if (_minimapDragMoved) return; // 拖拽结束时不触发点击
    const rect = minimapContentEl.getBoundingClientRect();
    const localX = event.clientX - rect.left;
    const localY = event.clientY - rect.top;
    const worldX = minX + (localX - offsetX) / scale;
    const worldY = minY + (localY - offsetY) / scale;
    centerViewportOn(worldX, worldY);
  };
}

// ── 小地图拖拽视口框 ────────────────────────────────────────────────────────

const _minimapMapping = { minX: 0, minY: 0, scale: 1, offsetX: 0, offsetY: 0 };
let _minimapDragging = false;
let _minimapDragMoved = false;
let _minimapDragStartX = 0;
let _minimapDragStartY = 0;
let _minimapDragOriginOffsetX = 0;
let _minimapDragOriginOffsetY = 0;

minimapViewportEl.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  e.stopPropagation();
  _minimapDragging = true;
  _minimapDragMoved = false;
  minimapViewportEl.setPointerCapture(e.pointerId);
  _minimapDragStartX = e.clientX;
  _minimapDragStartY = e.clientY;
  _minimapDragOriginOffsetX = state.offsetX;
  _minimapDragOriginOffsetY = state.offsetY;
});

document.addEventListener('pointermove', (e) => {
  if (!_minimapDragging) return;
  _minimapDragMoved = true;
  const ms = _minimapMapping.scale;
  if (ms <= 0) return;
  const dx = (e.clientX - _minimapDragStartX) / ms;
  const dy = (e.clientY - _minimapDragStartY) / ms;
  state.offsetX = _minimapDragOriginOffsetX - dx * state.scale;
  state.offsetY = _minimapDragOriginOffsetY - dy * state.scale;
  applyTransform();
  scheduleRenderMinimap();
});

document.addEventListener('pointerup', () => {
  if (_minimapDragging) {
    _minimapDragging = false;
    // 延迟重置 _minimapDragMoved 以阻止 onclick 误触发
    setTimeout(() => { _minimapDragMoved = false; }, 50);
  }
});

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
    /* B2: any bbox change re-renders the cluster frame */
    scheduleRenderClusterFrames();
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
  // fallback：放在所有现有 cluster 下方，以视口中心水平居中
  const vr = viewportEl.getBoundingClientRect();
  const fallbackCenterX = (vr.width / 2 - state.offsetX) / state.scale;
  let fallbackY = 0;
  const gap = CLUSTER_PADDING * 2 + 60;
  for (const cluster of requestClusters.values()) {
    fallbackY = Math.max(fallbackY, cluster.bbox.y + cluster.bbox.height + gap);
  }
  let fallbackBox = { x: fallbackCenterX - width / 2, y: fallbackY, width, height };
  let attempts = 0;
  while (hasClusterOverlap(fallbackBox) && attempts < 10) {
    fallbackBox = { x: fallbackBox.x + width + gap, y: fallbackBox.y, width, height };
    attempts += 1;
  }
  return fallbackBox;
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
  // Clean up minimap throttle timer
  if (_minimapThrottleTimer) {
    clearTimeout(_minimapThrottleTimer);
    _minimapThrottleTimer = null;
  }
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
  if (_clusterFrameLayerEl) _clusterFrameLayerEl.innerHTML = '';
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
