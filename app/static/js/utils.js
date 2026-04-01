/**
 * utils.js — Pure utility functions with no side effects.
 */

import { MODEL_DISPLAY_NAMES } from './state.js';

export function getDisplayName(model) {
  return MODEL_DISPLAY_NAMES[model] || model;
}

export function escapeHtml(value) {
  return window.escapeHtmlShared ? window.escapeHtmlShared(value) : String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

export function escapeAttribute(value) {
  return escapeHtml(value).replaceAll('`', '&#096;');
}

export function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function stripMarkdownSummary(value) {
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

export function summarizeText(value, maxLength = 34) {
  const cleaned = stripMarkdownSummary(value);
  if (!cleaned) return '等待摘要';
  return cleaned.length > maxLength ? `${cleaned.slice(0, maxLength - 1)}…` : cleaned;
}

export function flashButtonLabel(button, label) {
  const original = button.textContent;
  button.textContent = label;
  window.setTimeout(() => {
    button.textContent = original;
  }, 1600);
}

// ── 通用模态框 ──────────────────────────────────────────────────────────────

/**
 * showModal({ title, message?, inputValue?, placeholder?, danger?, confirmText?, onConfirm, onCancel? })
 * 返回 Promise<string|boolean|null>：输入型返回字符串或 null（取消），确认型返回 true/false。
 */
export function showModal({
  title,
  message = '',
  inputValue = '',
  placeholder = '',
  danger = false,
  confirmText = '',
  onConfirm,
  onCancel,
} = {}) {
  const isInput = placeholder || inputValue;
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  const dialog = document.createElement('div');
  dialog.className = `modal-dialog${danger ? ' modal-danger' : ''}`;
  dialog.innerHTML = `
    <div class="modal-title">${escapeHtml(title)}</div>
    ${message ? `<div class="modal-message">${escapeHtml(message)}</div>` : ''}
    ${isInput ? `<input class="modal-input" value="${escapeAttribute(inputValue)}" placeholder="${escapeAttribute(placeholder)}" autofocus>` : ''}
    <div class="modal-actions">
      <button type="button" class="modal-btn modal-cancel">取消</button>
      <button type="button" class="modal-btn modal-confirm${danger ? ' danger' : ''}">${escapeHtml(confirmText || (danger ? '确认删除' : '确认'))}</button>
    </div>
  `;
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  // 入场动画
  requestAnimationFrame(() => overlay.classList.add('active'));

  const input = dialog.querySelector('.modal-input');
  const cleanup = () => {
    overlay.classList.remove('active');
    setTimeout(() => overlay.remove(), 180);
  };

  dialog.querySelector('.modal-cancel').addEventListener('click', () => { cleanup(); onCancel?.(); });
  dialog.querySelector('.modal-confirm').addEventListener('click', () => {
    cleanup();
    onConfirm?.(isInput ? (input?.value ?? '') : true);
  });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) { cleanup(); onCancel?.(); }
  });

  // 键盘
  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      cleanup();
      onConfirm?.(isInput ? (input?.value ?? '') : true);
      document.removeEventListener('keydown', onKey);
    }
    if (e.key === 'Escape') {
      cleanup();
      onCancel?.();
      document.removeEventListener('keydown', onKey);
    }
  };
  document.addEventListener('keydown', onKey);

  if (input) {
    input.focus();
    input.select();
  } else {
    dialog.querySelector('.modal-confirm').focus();
  }
}

/**
 * showAlert(message) — 替代 window.alert()
 */
export function showAlert(message) {
  showModal({
    title: '提示',
    message,
    confirmText: '确定',
    onConfirm() {},
  });
}

// ── 全屏 Markdown 预览 ──────────────────────────────────────────────────────

/**
 * showPreview({ title, markdown, onClose? })
 * 全屏弹窗展示渲染后的 Markdown 内容。
 */
export function showPreview({ title, markdown, onClose } = {}) {
  const overlay = document.createElement('div');
  overlay.className = 'preview-overlay';

  const dialog = document.createElement('div');
  dialog.className = 'preview-dialog';
  dialog.innerHTML = `
    <header class="preview-header">
      <div class="preview-title">${escapeHtml(title)}</div>
      <div class="preview-actions">
        <button type="button" class="preview-btn preview-copy" title="复制 Markdown 源码">复制</button>
        <button type="button" class="preview-btn preview-close" title="关闭 (Esc)">✕</button>
      </div>
    </header>
    <div class="preview-body"></div>
  `;

  const body = dialog.querySelector('.preview-body');
  body.innerHTML = window.renderMarkdown ? window.renderMarkdown(markdown) : escapeHtml(markdown);

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add('active'));

  const cleanup = () => {
    overlay.classList.remove('active');
    setTimeout(() => overlay.remove(), 200);
    document.removeEventListener('keydown', onKey);
    onClose?.();
  };

  dialog.querySelector('.preview-close').addEventListener('click', cleanup);
  dialog.querySelector('.preview-copy').addEventListener('click', (e) => {
    navigator.clipboard.writeText(markdown).then(() => {
      const btn = e.currentTarget;
      btn.textContent = '已复制';
      setTimeout(() => { btn.textContent = '复制'; }, 1500);
    });
  });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) cleanup();
  });

  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      cleanup();
    }
  };
  document.addEventListener('keydown', onKey);
}
