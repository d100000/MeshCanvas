/**
 * edges.js — Edge rendering, anchor points, and Bezier paths.
 */

import {
  edges, nodes, appState,
  edgeLayerEl,
  USER_NODE_WIDTH, USER_NODE_HEIGHT,
  MODEL_NODE_WIDTH, MODEL_NODE_HEIGHT,
  CONCLUSION_NODE_WIDTH, CONCLUSION_NODE_HEIGHT,
} from './state.js';

let _rafEdgesScheduled = false;

export function scheduleRenderEdges() {
  if (_rafEdgesScheduled) return;
  _rafEdgesScheduled = true;
  requestAnimationFrame(() => {
    _rafEdgesScheduled = false;
    renderEdges();
  });
}

export function addEdge(edge) {
  edges.set(edge.id, edge);
}

export function renderEdges() {
  const activeNodeId = appState.hoverNodeId || appState.selectedNodeId;
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

export function getNodeDimensions(node) {
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

export function isNodeAdjacent(nodeId, activeNodeId) {
  if (!activeNodeId) return false;
  for (const edge of edges.values()) {
    if ((edge.sourceId === nodeId && edge.targetId === activeNodeId) || (edge.targetId === nodeId && edge.sourceId === activeNodeId)) {
      return true;
    }
  }
  return false;
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
