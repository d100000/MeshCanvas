/**
 * sidebar.js — Sidebar canvas list, switch, load, init.
 */

import {
  appState,
  sidebarCanvasListEl,
} from './state.js';
import { escapeHtml, escapeAttribute, showModal, showAlert } from './utils.js';
import { clearCanvas, renderMinimap } from './canvas.js';
import { updateComposerHint } from './selection.js';
import { replayCluster } from './clusters.js';
import { connect } from './websocket.js';

let _canvasListDelegated = false;

export function renderCanvasList() {
  if (!sidebarCanvasListEl) return;
  sidebarCanvasListEl.innerHTML = appState.canvasesList.map((c) => `
    <div class="sidebar-canvas-item ${c.id === appState.currentCanvasId ? 'active' : ''}" data-id="${escapeAttribute(c.id)}">
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
        if (appState.canvasesList.length <= 1) {
          showAlert('至少需要保留一个画布。');
          return;
        }
        showModal({
          title: '删除画布',
          message: '确认删除该画布及其所有内容？此操作不可撤销。',
          danger: true,
          async onConfirm() {
            const res = await fetch(`/api/canvases/${id}`, { method: 'DELETE', credentials: 'same-origin' });
            if (!res.ok) return;
            appState.canvasesList = appState.canvasesList.filter((c) => c.id !== id);
            if (appState.currentCanvasId === id) {
              await switchCanvas(appState.canvasesList[0].id);
            } else {
              renderCanvasList();
            }
          },
        });
        return;
      }
      const item = e.target.closest('.sidebar-canvas-item');
      if (item) {
        const id = item.dataset.id;
        if (id !== appState.currentCanvasId) {
          switchCanvas(id);
        }
      }
    });
  }
}

export async function switchCanvas(canvasId) {
  appState.currentCanvasId = canvasId;
  renderCanvasList();
  await loadCanvasState(canvasId);
}

export async function loadCanvasState(canvasId) {
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
    showAlert('画布加载失败，请刷新页面重试。');
  }
}

export async function initCanvases() {
  connect();
  try {
    const res = await fetch('/api/canvases', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    appState.canvasesList = data.canvases || [];
    if (!appState.canvasesList.length) {
      const createRes = await fetch('/api/canvases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ name: '画布 1' }),
      });
      if (!createRes.ok) return;
      const created = await createRes.json();
      appState.canvasesList = [{ id: created.canvas_id, name: created.name }];
    }
    renderCanvasList();
    await switchCanvas(appState.canvasesList[0].id);
  } catch (_) {
    console.warn('画布列表加载失败，WebSocket 仍可正常使用。');
  }
}
