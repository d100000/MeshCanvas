const API = (path, opts = {}) => fetch(path, { credentials: 'same-origin', ...opts }).then(async r => {
  if (r.status === 401) { window.location.href = '/admin?error=session'; throw new Error('unauthorized'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || 'request failed');
  return d;
});

const POST = (path, body) => API(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const PUT = (path, body) => API(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const DEL = (path) => API(path, { method: 'DELETE' });

function esc(v) { const d = document.createElement('div'); d.textContent = v; return d.innerHTML; }

/** 用于 data-* 等 HTML 属性，避免引号与脚本注入 */
function escAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;');
}

function tableErrorRow(colspan, message) {
  return `<tr><td colspan="${colspan}" class="admin-msg err">${esc(message)}</td></tr>`;
}

// Tab switching
document.querySelectorAll('.admin-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.admin-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`panel-${tab.dataset.tab}`).classList.add('active');
    if (tab.dataset.tab === 'users') loadUsers();
    if (tab.dataset.tab === 'model-config') loadModelConfig();
    if (tab.dataset.tab === 'pricing') loadPricing();
    if (tab.dataset.tab === 'usage') { loadUsage(); loadRechargeLogs(); }
    if (tab.dataset.tab === 'config') loadConfig();
    if (tab.dataset.tab === 'audit') loadAuditLogs();
  });
});

// Logout
document.getElementById('adminLogoutBtn').addEventListener('click', async () => {
  await POST('/api/admin/logout', {}).catch(() => {});
  window.location.href = '/admin';
});

// 定价按钮全部委托：「设定单价」填充表单 + 「删除」删除定价
document.getElementById('pricingQuickBody').addEventListener('click', async (e) => {
  // 删除
  const delBtn = e.target.closest('button[data-delete-pricing]');
  if (delBtn) {
    const mid = delBtn.getAttribute('data-delete-pricing');
    if (!mid) return;
    const midDisplay = mid.replace(/\n/g, '\\n').replace(/\r/g, '');
    if (!confirm(`确认删除 ${midDisplay} 的定价？`)) return;
    try {
      await DEL(`/api/admin/pricing/${encodeURIComponent(mid)}`);
      loadPricing();
    } catch (err) {
      alert(err.message || '删除失败');
    }
    return;
  }
  // 填充表单
  const fillBtn = e.target.closest('[data-action="fill-price"]');
  if (fillBtn) {
    const mid = fillBtn.getAttribute('data-model-id');
    const mname = fillBtn.getAttribute('data-model-name');
    const p = _pricingCache[mid];
    document.getElementById('priceModelId').value = mid;
    document.getElementById('priceDisplayName').value = mname;
    document.getElementById('priceInput').value = p ? p.input_points_per_1k : 1;
    document.getElementById('priceOutput').value = p ? p.output_points_per_1k : 2;
    document.getElementById('priceInput').scrollIntoView({ behavior: 'smooth', block: 'center' });
    document.getElementById('priceInput').focus();
  }
});

// ── Users（行内「调积分」展开行 + 事件委托）──
async function loadUsers() {
  try {
    const { users } = await API('/api/admin/users');
    document.getElementById('userTableBody').innerHTML = users.map((u) => {
      const roleBtn = u.role === 'admin' ? '降为用户' : '升为管理员';
      return `
      <tr data-user-main="${u.id}">
        <td>${u.id}</td>
        <td>${esc(u.username)}</td>
        <td>${u.role === 'admin' ? '<strong>管理员</strong>' : '普通用户'}</td>
        <td>${u.balance.toFixed(2)}</td>
        <td>${u.total_recharged.toFixed(2)}</td>
        <td>${u.total_consumed.toFixed(2)}</td>
        <td>${u.created_at?.slice(0, 10) || ''}</td>
        <td class="admin-actions">
          <button type="button" class="admin-btn admin-btn-sm primary" data-action="recharge-toggle" data-user-id="${u.id}" data-username="${escAttr(u.username)}" data-balance="${u.balance}">调积分</button>
          <button type="button" class="admin-btn admin-btn-sm" data-action="toggle-role" data-user-id="${u.id}" data-role="${escAttr(u.role)}">${roleBtn}</button>
          <button type="button" class="admin-btn admin-btn-sm" data-action="reset-password" data-user-id="${u.id}" data-username="${escAttr(u.username)}">重置密码</button>
        </td>
      </tr>
      <tr class="admin-recharge-expand" data-expand-for="${u.id}" hidden>
        <td colspan="8" class="admin-recharge-cell">
          <div class="admin-recharge-meta" data-recharge-meta></div>
          <div class="admin-recharge-form">
            <label><span>点数</span><input type="number" step="0.01" placeholder="正数充值 / 负数扣减" data-recharge-points /></label>
            <label><span>备注</span><input type="text" placeholder="可选" data-recharge-remark /></label>
            <button type="button" class="admin-btn primary" data-action="recharge-submit" data-user-id="${u.id}">确认调整</button>
            <button type="button" class="admin-btn" data-action="recharge-cancel" data-user-id="${u.id}">取消</button>
          </div>
          <div class="admin-msg" data-recharge-msg></div>
        </td>
      </tr>`;
    }).join('');
  } catch (e) {
    document.getElementById('userTableBody').innerHTML = tableErrorRow(8, e.message || '加载失败');
  }
}

document.getElementById('userTableBody').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn || !btn.dataset.action) return;

  const tbody = document.getElementById('userTableBody');

  if (btn.dataset.action === 'recharge-toggle') {
    const uid = btn.dataset.userId;
    const expand = tbody.querySelector(`tr.admin-recharge-expand[data-expand-for="${uid}"]`);
    if (!expand) return;
    const opening = expand.hidden;
    tbody.querySelectorAll('tr.admin-recharge-expand').forEach((tr) => { tr.hidden = true; });
    if (opening) {
      expand.hidden = false;
      const meta = expand.querySelector('[data-recharge-meta]');
      const uname = btn.getAttribute('data-username') || '';
      const bal = parseFloat(btn.getAttribute('data-balance') || '0');
      if (meta) {
        meta.innerHTML = `为 <strong>${esc(uname)}</strong>（ID ${uid}）调整 · 当前余额 <strong>${bal.toFixed(2)}</strong>`;
      }
      const pts = expand.querySelector('[data-recharge-points]');
      const rmk = expand.querySelector('[data-recharge-remark]');
      const msg = expand.querySelector('[data-recharge-msg]');
      if (pts) pts.value = '';
      if (rmk) rmk.value = '';
      if (msg) { msg.textContent = ''; msg.className = 'admin-msg'; }
      pts?.focus();
    }
    return;
  }

  if (btn.dataset.action === 'recharge-cancel') {
    const uid = btn.dataset.userId;
    const expand = tbody.querySelector(`tr.admin-recharge-expand[data-expand-for="${uid}"]`);
    if (expand) expand.hidden = true;
    return;
  }

  if (btn.dataset.action === 'recharge-submit') {
    const uid = parseInt(btn.dataset.userId, 10);
    const expand = btn.closest('tr.admin-recharge-expand');
    if (!expand) return;
    const ptsEl = expand.querySelector('[data-recharge-points]');
    const rmkEl = expand.querySelector('[data-recharge-remark]');
    const msgEl = expand.querySelector('[data-recharge-msg]');
    const pts = parseFloat(ptsEl?.value || '');
    const rmk = (rmkEl?.value || '').trim();
    if (!uid || !pts) {
      if (msgEl) { msgEl.textContent = '请填写有效点数'; msgEl.className = 'admin-msg err'; }
      return;
    }
    if (pts < 0 && !confirm(`确认扣减用户 ${uid} 的 ${Math.abs(pts)} 点积分？`)) return;
    try {
      const r = await POST('/api/admin/recharge', { user_id: uid, points: pts, remark: rmk });
      const label = pts > 0 ? '充值' : '扣减';
      if (msgEl) {
        msgEl.textContent = `${label}成功，当前余额：${r.balance.toFixed(2)}`;
        msgEl.className = 'admin-msg ok';
      }
      expand.hidden = true;
      loadUsers();
    } catch (err) {
      if (msgEl) { msgEl.textContent = err.message || '失败'; msgEl.className = 'admin-msg err'; }
    }
    return;
  }

  if (btn.dataset.action === 'toggle-role') {
    const uid = parseInt(btn.dataset.userId, 10);
    const current = btn.getAttribute('data-role') || 'user';
    const newRole = current === 'admin' ? 'user' : 'admin';
    if (!confirm(`确认将用户 ${uid} 设为 ${newRole === 'admin' ? '管理员' : '普通用户'}？`)) return;
    try {
      await POST('/api/admin/set-role', { user_id: uid, role: newRole });
      loadUsers();
    } catch (err) {
      alert(err.message || '操作失败');
    }
  }

  if (btn.dataset.action === 'reset-password') {
    const uid = parseInt(btn.dataset.userId, 10);
    const uname = btn.getAttribute('data-username') || '';
    const newPwd = prompt(`为用户 ${uname}（ID ${uid}）设置新密码（至少 8 位）：`);
    if (!newPwd) return;
    if (newPwd.length < 8) { alert('密码至少需要 8 位。'); return; }
    try {
      await POST('/api/admin/reset-password', { user_id: uid, new_password: newPwd });
      alert(`用户 ${uname} 的密码已重置。`);
    } catch (err) {
      alert(err.message || '重置失败');
    }
  }
});

// ── Model Config ──
function mcEscAttr(v) {
  return String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function mcAddModelRow(name = '', id = '') {
  const row = document.createElement('div');
  row.className = 'admin-model-row';
  row.innerHTML = `
    <input type="text" class="mc-name" placeholder="显示名称（如 GPT-4o）" value="${mcEscAttr(name)}" />
    <input type="text" class="mc-id" placeholder="模型 ID（如 gpt-4o）" value="${mcEscAttr(id)}" />
    <div class="mc-actions">
      <button type="button" class="admin-btn admin-btn-sm primary" data-action="mc-test">测试</button>
      <button type="button" class="admin-btn admin-btn-sm danger" data-action="mc-remove">删除</button>
    </div>
    <div class="admin-msg mc-test-msg" data-mc-test-msg></div>
  `;
  document.getElementById('mcModelList').appendChild(row);
}

function mcCollectModels() {
  return Array.from(document.querySelectorAll('#mcModelList .admin-model-row')).map(r => ({
    name: r.querySelector('.mc-name').value.trim(),
    id: r.querySelector('.mc-id').value.trim(),
  })).filter(m => m.name && m.id);
}

function mcSetRowMsg(row, text, type = '') {
  const msgEl = row.querySelector('[data-mc-test-msg]');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.className = `admin-msg mc-test-msg${type ? ` ${type}` : ''}`;
}

function mcFormatUsage(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const pt = Number(usage.prompt_tokens || 0);
  const ct = Number(usage.completion_tokens || 0);
  const tt = Number(usage.total_tokens || 0);
  if (!Number.isFinite(pt) || !Number.isFinite(ct) || !Number.isFinite(tt)) return '';
  if (tt > 0) return `tokens ${tt}（in ${Math.max(0, pt)} / out ${Math.max(0, ct)}）`;
  if (pt > 0 || ct > 0) return `tokens ${Math.max(0, pt + ct)}`;
  return '';
}

document.getElementById('mcModelList')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const action = btn.getAttribute('data-action');
  if (!action || !['mc-remove', 'mc-test'].includes(action)) return;
  const row = btn.closest('.admin-model-row');
  if (!row) return;

  if (action === 'mc-remove') {
    const list = document.getElementById('mcModelList');
    if (list && list.children.length > 1) row.remove();
    return;
  }

  if (btn.disabled) return;
  const name = row.querySelector('.mc-name')?.value.trim() || '';
  const id = row.querySelector('.mc-id')?.value.trim() || '';
  if (!name || !id) {
    mcSetRowMsg(row, '请先填写该行模型名称与模型 ID。', 'err');
    return;
  }

  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '测试中...';
  row.classList.add('is-testing');
  mcSetRowMsg(row, '正在测试连通性，请稍候...');
  try {
    const r = await POST('/api/admin/model-config/test', { model_name: name, model_id: id });
    const bits = [];
    if (Number.isFinite(Number(r.latency_ms))) bits.push(`${r.latency_ms}ms`);
    const usageText = mcFormatUsage(r.usage);
    if (usageText) bits.push(usageText);
    const preview = (r.preview || '').trim();
    const detail = bits.length ? `（${bits.join('，')}）` : '';
    const previewText = preview ? `：${preview}` : '';
    mcSetRowMsg(row, `测试成功${detail}${previewText}`, 'ok');
  } catch (err) {
    mcSetRowMsg(row, err.message || '测试失败', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = oldText || '测试';
    row.classList.remove('is-testing');
  }
});

async function loadModelConfig() {
  const msgEl = document.getElementById('mcMsg');
  try {
    const d = await API('/api/admin/model-config');
    document.getElementById('mcApiBase').value = d.api_base_url || '';
    document.getElementById('mcApiFormat').value = d.api_format || 'openai';
    document.getElementById('mcApiKey').value = '';
    document.getElementById('mcFcKey').value = '';
    document.getElementById('mcFcCountry').value = d.firecrawl_country || 'CN';
    document.getElementById('mcFcTimeout').value = d.firecrawl_timeout_ms || 45000;
    const hint = document.getElementById('mcApiKeyHint');
    if (hint) hint.textContent = d.api_key_masked ? `（当前：${d.api_key_masked}）` : '';
    const fcHint = document.getElementById('mcFcKeyHint');
    if (fcHint) fcHint.textContent = d.firecrawl_api_key_masked ? `（当前：${d.firecrawl_api_key_masked}）` : '';
    const list = document.getElementById('mcModelList');
    list.innerHTML = '';
    const models = d.models || [];
    if (models.length) models.forEach(m => mcAddModelRow(m.name, m.id));
    else mcAddModelRow();
    const ppSelect = document.getElementById('mcPreprocessModel');
    if (ppSelect) {
      ppSelect.innerHTML = '<option value="">不启用</option>';
      for (const m of models) {
        const opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = m.name;
        if (m.name === (d.preprocess_model || '')) opt.selected = true;
        ppSelect.appendChild(opt);
      }
    }
    const extraEl = document.getElementById('mcExtraParams');
    if (extraEl) {
      const ep = d.extra_params || {};
      extraEl.value = Object.keys(ep).length ? JSON.stringify(ep, null, 2) : '';
    }
    const extraHeadersEl = document.getElementById('mcExtraHeaders');
    if (extraHeadersEl) {
      const eh = d.extra_headers || {};
      extraHeadersEl.value = Object.keys(eh).length ? JSON.stringify(eh, null, 2) : '';
    }
    const userApiBase = document.getElementById('mcUserApiBase');
    if (userApiBase) userApiBase.value = d.user_api_base_url || '';
    const userApiFormat = document.getElementById('mcUserApiFormat');
    if (userApiFormat) userApiFormat.value = d.user_api_format || 'openai';
    if (msgEl) { msgEl.textContent = ''; msgEl.className = 'admin-msg'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = e.message || '加载失败'; msgEl.className = 'admin-msg err'; }
  }
}

function parseJsonField(elId, label) {
  const raw = (document.getElementById(elId)?.value || '').trim();
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    throw new Error(`${label} 不是合法 JSON`);
  }
}

document.getElementById('mcAddModelBtn')?.addEventListener('click', () => mcAddModelRow());

document.getElementById('mcSaveBtn')?.addEventListener('click', async () => {
  const msgEl = document.getElementById('mcMsg');
  const models = mcCollectModels();
  if (!document.getElementById('mcApiBase').value.trim()) {
    msgEl.textContent = '请填写 API 地址'; msgEl.className = 'admin-msg err'; return;
  }
  if (!models.length) {
    msgEl.textContent = '请至少添加一个模型'; msgEl.className = 'admin-msg err'; return;
  }
  try {
    const extraParams = parseJsonField('mcExtraParams', '请求额外参数');
    const extraHeaders = parseJsonField('mcExtraHeaders', '请求头覆盖');
    await PUT('/api/admin/model-config', {
      api_base_url: document.getElementById('mcApiBase').value.trim(),
      api_format: document.getElementById('mcApiFormat').value,
      api_key: document.getElementById('mcApiKey').value.trim(),
      models,
      firecrawl_api_key: document.getElementById('mcFcKey').value.trim(),
      firecrawl_country: document.getElementById('mcFcCountry').value.trim() || 'CN',
      firecrawl_timeout_ms: parseInt(document.getElementById('mcFcTimeout').value) || 45000,
      preprocess_model: document.getElementById('mcPreprocessModel')?.value || '',
      extra_params: extraParams,
      extra_headers: extraHeaders,
      user_api_base_url: document.getElementById('mcUserApiBase')?.value.trim() || '',
      user_api_format: document.getElementById('mcUserApiFormat')?.value || 'openai',
    });
    msgEl.textContent = '已保存'; msgEl.className = 'admin-msg ok';
    loadModelConfig();
  } catch (e) { msgEl.textContent = e.message; msgEl.className = 'admin-msg err'; }
});

// ── Pricing ──

/** 缓存：{model_id -> {display_name, input_points_per_1k, output_points_per_1k, is_active}} */
let _pricingCache = {};

async function loadPricing() {
  const tbody = document.getElementById('pricingQuickBody');
  const msgEl = document.getElementById('priceMsg');

  // 并行拉取定价 + 全局模型列表
  let configModels = [];
  let existingPricing = [];
  try {
    const [cfgRes, pricingRes] = await Promise.all([
      API('/api/admin/model-config'),
      API('/api/admin/pricing'),
    ]);
    configModels = cfgRes.models || [];
    existingPricing = pricingRes.pricing || [];
  } catch (e) {
    tbody.innerHTML = tableErrorRow(6, e.message || '加载失败');
    return;
  }

  // 建立 model_id -> pricing 映射
  _pricingCache = {};
  for (const p of existingPricing) _pricingCache[p.model_id] = p;

  if (!configModels.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="admin-muted-cell">尚未配置任何模型，请先在「模型配置」Tab 添加模型。</td></tr>';
    return;
  }

  tbody.innerHTML = configModels.map(m => {
    const p = _pricingCache[m.id];
    const inPts = p ? p.input_points_per_1k : '—';
    const outPts = p ? p.output_points_per_1k : '—';
    const status = p ? (p.is_active ? '启用' : '禁用') : '<span class="admin-status-muted">未设置</span>';
    return `<tr>
      <td>${esc(m.name)}</td>
      <td><code class="model-id-code">${esc(m.id)}</code></td>
      <td>${inPts}</td>
      <td>${outPts}</td>
      <td>${status}</td>
      <td class="pricing-actions-cell">
        <div class="pricing-actions">
          <button type="button" class="admin-btn admin-btn-sm primary"
            data-action="fill-price"
            data-model-id="${escAttr(m.id)}"
            data-model-name="${escAttr(m.name)}">设定单价</button>
          ${p ? `<button type="button" class="admin-btn admin-btn-sm danger"
            data-delete-pricing="${escAttr(m.id)}">删除</button>` : ''}
        </div>
      </td>
    </tr>`;
  }).join('');
}

document.getElementById('priceSaveBtn').addEventListener('click', async () => {
  const msgEl = document.getElementById('priceMsg');
  try {
    await PUT('/api/admin/pricing', {
      model_id: document.getElementById('priceModelId').value.trim(),
      display_name: document.getElementById('priceDisplayName').value.trim(),
      input_points_per_1k: parseFloat(document.getElementById('priceInput').value) || 1,
      output_points_per_1k: parseFloat(document.getElementById('priceOutput').value) || 2,
      is_active: 1,
    });
    msgEl.textContent = '已保存'; msgEl.className = 'admin-msg ok';
    loadPricing();
  } catch (e) { msgEl.textContent = e.message; msgEl.className = 'admin-msg err'; }
});

// ── Usage ──
async function loadUsage() {
  const uid = document.getElementById('usageUserId').value.trim();
  const q = uid ? `?user_id=${encodeURIComponent(uid)}` : '';
  try {
    const { stats } = await API(`/api/admin/usage${q}`);
    document.getElementById('usageTableBody').innerHTML = stats.map(s => `
      <tr><td>${esc(s.model)}</td><td>${s.count}</td><td>${s.prompt_tokens}</td><td>${s.completion_tokens}</td><td>${s.total_tokens}</td><td>${s.points_consumed.toFixed(2)}</td></tr>
    `).join('') || '<tr><td colspan="6">暂无数据</td></tr>';
  } catch (e) {
    document.getElementById('usageTableBody').innerHTML = tableErrorRow(6, e.message || '加载失败');
  }
}

async function loadRechargeLogs() {
  try {
    const { logs } = await API('/api/admin/recharge-logs');
    document.getElementById('rechargeLogBody').innerHTML = logs.map(l => `
      <tr><td>${l.created_at?.slice(0,16) || ''}</td><td>${esc(l.username)}</td><td>${esc(l.admin_name)}</td><td class="${l.points < 0 ? 'points-neg' : 'points-pos'}">${l.points >= 0 ? '+' : ''}${l.points}</td><td>${esc(l.remark)}</td></tr>
    `).join('') || '<tr><td colspan="5">暂无记录</td></tr>';
  } catch (e) {
    document.getElementById('rechargeLogBody').innerHTML = tableErrorRow(5, e.message || '加载失败');
  }
}

document.getElementById('usageRefreshBtn').addEventListener('click', loadUsage);

// ── Config ──
async function loadConfig() {
  const msgEl = document.getElementById('cfgMsg');
  try {
    const { config } = await API('/api/admin/config');
    document.getElementById('cfgDefaultPoints').value = config.config_default_points || '100';
    document.getElementById('cfgLowThreshold').value = config.config_low_balance_threshold || '10';
    document.getElementById('cfgSearchPoints').value = config.config_search_points_per_call || '5';
    document.getElementById('cfgAllowReg').value = config.config_allow_registration || 'true';
  } catch (e) {
    if (msgEl) {
      msgEl.textContent = e.message || '配置加载失败';
      msgEl.className = 'admin-msg err';
    }
  }
}

document.getElementById('cfgSaveBtn').addEventListener('click', async () => {
  const msgEl = document.getElementById('cfgMsg');
  try {
    await PUT('/api/admin/config', {
      config_default_points: document.getElementById('cfgDefaultPoints').value,
      config_low_balance_threshold: document.getElementById('cfgLowThreshold').value,
      config_search_points_per_call: document.getElementById('cfgSearchPoints').value,
      config_allow_registration: document.getElementById('cfgAllowReg').value,
    });
    msgEl.textContent = '已保存'; msgEl.className = 'admin-msg ok';
  } catch (e) { msgEl.textContent = e.message; msgEl.className = 'admin-msg err'; }
});

// ── Audit Log ──
const _AUDIT_ACTION_LABELS = {
  recharge: '积分调整',
  set_role: '角色变更',
  reset_password: '重置密码',
  upsert_pricing: '更新定价',
  delete_pricing: '删除定价',
  update_config: '修改配置',
  test_model_connectivity: '模型连通性测试',
};

async function loadAuditLogs() {
  const action = document.getElementById('auditActionFilter')?.value || '';
  const q = action ? `?action=${encodeURIComponent(action)}` : '';
  try {
    const { logs } = await API(`/api/admin/audit-logs${q}`);
    document.getElementById('auditTableBody').innerHTML = logs.map(l => {
      const label = _AUDIT_ACTION_LABELS[l.action] || esc(l.action);
      const detail = Object.entries(l.detail || {})
        .map(([k, v]) => `${esc(k)}=${esc(String(v))}`)
        .join(' · ');
      return `<tr>
        <td>${l.created_at?.slice(0, 16) || ''}</td>
        <td>${esc(l.admin_name)}</td>
        <td>${label}</td>
        <td>${l.target_username ? esc(l.target_username) : '-'}</td>
        <td class="audit-detail">${detail}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="5">暂无记录</td></tr>';
  } catch (e) {
    document.getElementById('auditTableBody').innerHTML = tableErrorRow(5, e.message || '加载失败');
  }
}

document.getElementById('auditRefreshBtn')?.addEventListener('click', loadAuditLogs);

// Initial load
loadUsers();
