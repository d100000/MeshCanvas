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
