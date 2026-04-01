/**
 * app.js — Entry point. Imports all modules, binds event listeners, and starts.
 */

import {
  appState, state, selectedNodeIds,
  sendBtn, clearBtn, fitBtn, zoomInBtn, zoomOutBtn, zoomResetBtn,
  messageInput, discussionRoundsEl, searchToggleEl, thinkToggleEl,
  selectionContinueBtn, selectionBranchBtn, selectionClearBtn,
  sidebarCollapseBtn, sidebarToggleBtn, sidebarNewCanvasBtn, sidebarLogoutBtn,
  DEFAULT_SCALE, MAX_MESSAGE_LENGTH, CONCLUSION_CONTEXT_MAX_CHARS,
  getSearchMode, selectionSummaryState,
} from './state.js';
import { clamp, showModal, showAlert } from './utils.js';
import {
  applyTransform, bindCanvasPan, setZoom, focusCluster, fitAllNodes,
  renderMinimap, toggleSidebar,
} from './canvas.js';
import {
  getSelectedContextNodes, shouldUseSelectionSummary,
  getComposerMode, getSelectionSummaryModelLabel,
  updateComposerHint, clearSelection, updateSelectionActions,
  buildContextBundleFromSelection, queueSelectionSummaryRefresh,
} from './selection.js';
import { sendBranch, openBranchComposer, getConclusionNode } from './nodes.js';
import { initCanvases, switchCanvas, renderCanvasList } from './sidebar.js';

// ── Send message ─────────────────────────────────────────────────────────────

let _sendThrottled = false;
function sendMessage() {
  const message = messageInput.value.trim();
  if (!message || appState.socket?.readyState !== WebSocket.OPEN || _sendThrottled) {
    return;
  }
  _sendThrottled = true;
  sendBtn.disabled = true;
  setTimeout(() => { _sendThrottled = false; sendBtn.disabled = false; }, 500);

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

  if (!contextBundle && appState.conclusionAutoAttach && appState.latestConclusionRequestId) {
    const conclusionNode = getConclusionNode(appState.latestConclusionRequestId);
    const conclusionMd = conclusionNode?.markdown || appState.latestConclusionMarkdown || '';
    if (conclusionMd) {
      let clipped = conclusionMd;
      if (clipped.length > CONCLUSION_CONTEXT_MAX_CHARS) {
        clipped = clipped.slice(0, CONCLUSION_CONTEXT_MAX_CHARS) + '\n\n[结论文档已截断]';
      }
      contextBundle = `以下是上一轮多模型讨论的最终结论文档，请基于此上下文继续回答：\n\n${clipped}`;
    }
  }

  // 捕获选中的上下文节点 ID，用于后续创建连线
  if (selected.length > 0) {
    if (!appState._pendingContextQueue) appState._pendingContextQueue = [];
    appState._pendingContextQueue.push(selected.map((n) => n.nodeId));
  }

  // 保存原始问题用于用户节点展示（避免展示完整上下文拼接内容）
  if (!appState._pendingUserDisplay) appState._pendingUserDisplay = [];
  appState._pendingUserDisplay.push({
    displayMessage: message,
    contextNodeCount: contextBundle ? selected.length || 1 : 0,
  });

  const suffix = contextBundle ? `\n\n用户新的继续问题：${message}` : '';
  const maxContextLength = Math.max(0, MAX_MESSAGE_LENGTH - suffix.length);
  let clippedContext = contextBundle;
  if (contextBundle && contextBundle.length > maxContextLength) {
    clippedContext = `${contextBundle.slice(0, Math.max(0, maxContextLength - 12))}\n\n[上下文已截断]`;
  }
  const finalMessage = clippedContext ? `${clippedContext}${suffix}` : message;

  try {
    appState.socket.send(
      JSON.stringify({
        action: 'chat',
        message: finalMessage,
        discussion_rounds: Number(discussionRoundsEl.value || 2),
        search_enabled: getSearchMode(),
        think_enabled: thinkToggleEl.checked,
        canvas_id: appState.currentCanvasId,
      })
    );
  } catch (_e) {
    showAlert('消息发送失败，请检查网络连接。');
    return;
  }

  messageInput.value = '';
  autoResizeComposer();
  messageInput.focus();
}

function autoResizeComposer() {
  messageInput.style.height = 'auto';
  const nextHeight = Math.min(Math.max(messageInput.scrollHeight, 46), 108);
  messageInput.style.height = `${nextHeight}px`;
}

// ── Event listeners ──────────────────────────────────────────────────────────

selectionContinueBtn?.addEventListener('click', () => {
  // 点击"继续对话"时才触发圈选总结（节省 token）
  queueSelectionSummaryRefresh();
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
  if (appState.socket?.readyState === WebSocket.OPEN) {
    appState.socket.send(JSON.stringify({ action: 'clear', canvas_id: appState.currentCanvasId }));
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
let _fitClickTimer = null;
fitBtn.addEventListener('click', () => {
  if (_fitClickTimer) { clearTimeout(_fitClickTimer); _fitClickTimer = null; }
  _fitClickTimer = setTimeout(() => { _fitClickTimer = null; fitAllNodes(); }, 250);
});
fitBtn.addEventListener('dblclick', () => {
  if (_fitClickTimer) { clearTimeout(_fitClickTimer); _fitClickTimer = null; }
  if (appState.latestRequestId) focusCluster(appState.latestRequestId);
});
zoomInBtn.addEventListener('click', () => setZoom(clamp(state.scale * 1.15, 0.05, 3.0)));
zoomOutBtn.addEventListener('click', () => setZoom(clamp(state.scale * 0.85, 0.05, 3.0)));
zoomResetBtn.addEventListener('click', () => setZoom(DEFAULT_SCALE));
messageInput.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    sendMessage();
  }
});
messageInput.addEventListener('input', autoResizeComposer);
if (sidebarCollapseBtn) sidebarCollapseBtn.addEventListener('click', () => toggleSidebar(false));
if (sidebarToggleBtn) sidebarToggleBtn.addEventListener('click', () => toggleSidebar(true));

if (sidebarNewCanvasBtn) sidebarNewCanvasBtn.addEventListener('click', () => {
  showModal({
    title: '新建画布',
    inputValue: '新画布',
    placeholder: '输入画布名称',
    async onConfirm(name) {
      const canvasName = (name || '').trim() || '新画布';
      const res = await fetch('/api/canvases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ name: canvasName }),
      });
      if (!res.ok) return;
      const created = await res.json();
      appState.canvasesList.push({ id: created.canvas_id, name: created.name });
      await switchCanvas(created.canvas_id);
    },
  });
});

document.addEventListener('click', (event) => {
  if (event.target.closest('.node, .composer-shell, .sidebar, .topbar, .selection-actions, .selected-chips')) return;
  clearSelection();
});

// ── Keyboard shortcuts ──────────────────────────────────────────────────────

document.addEventListener('keydown', (e) => {
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

  if (e.key === 'Escape') {
    clearSelection();
  }
  if ((e.ctrlKey || e.metaKey) && (e.key === '=' || e.key === '+')) {
    e.preventDefault();
    setZoom(clamp(state.scale * 1.15, 0.05, 3.0));
  }
  if ((e.ctrlKey || e.metaKey) && e.key === '-') {
    e.preventDefault();
    setZoom(clamp(state.scale * 0.85, 0.05, 3.0));
  }
  if ((e.ctrlKey || e.metaKey) && e.key === '0') {
    e.preventDefault();
    setZoom(DEFAULT_SCALE);
  }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'f' || e.key === 'F')) {
    e.preventDefault();
    fitAllNodes();
  }
  if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    if (appState.latestRequestId) focusCluster(appState.latestRequestId);
  }
});

// ── Init ─────────────────────────────────────────────────────────────────────

autoResizeComposer();
bindCanvasPan();
applyTransform();
renderMinimap();
updateComposerHint();
initCanvases();
