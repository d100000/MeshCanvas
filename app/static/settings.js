(async function () {
  const infoEl = document.getElementById('accountInfo');

  async function api(path) {
    const res = await fetch(path, { credentials: 'same-origin' });
    if (res.status === 401 || res.status === 403) {
      window.location.href = '/login';
      throw new Error('unauthorized');
    }
    return res.json().catch(() => ({}));
  }

  async function apiWrite(method, path, body) {
    const opts = {
      method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (res.status === 401) { window.location.href = '/login'; throw new Error('unauthorized'); }
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || '操作失败');
    return d;
  }

  function esc(v) { const d = document.createElement('div'); d.textContent = v; return d.innerHTML; }

  try {
    const session = await api('/api/auth/session');
    if (!session.authenticated) { window.location.href = '/login'; return; }
    if (infoEl) infoEl.textContent = `当前登录账号：${session.username || '未知'}`;
  } catch (err) {
    if (infoEl) infoEl.textContent = '加载失败，请刷新重试。';
    return;
  }

  async function loadSummary() {
    try {
      const { summary } = await api('/api/user/usage-summary');
      document.getElementById('sumToday').textContent = `${summary.today_points} 点`;
      document.getElementById('sumTodayCount').textContent = `${summary.today_count} 次调用`;
      document.getElementById('sumWeek').textContent = `${summary.week_points} 点`;
      document.getElementById('sumWeekCount').textContent = `${summary.week_count} 次调用`;
      document.getElementById('sumTotal').textContent = `${summary.total_points} 点`;
      document.getElementById('sumTotalCount').textContent = `${summary.total_count} 次调用`;
      document.getElementById('sumBalance').textContent = `${summary.balance} 点`;
    } catch (_) {}
  }

  async function loadDetail() {
    const tbody = document.getElementById('usageTableBody');
    try {
      const { detail } = await api('/api/user/usage-detail?limit=100');
      if (!detail || !detail.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="settings-muted">暂无用量记录</td></tr>';
        return;
      }
      tbody.innerHTML = detail.map(r => {
        const time = (r.created_at || '').slice(0, 16).replace('T', ' ');
        const duration = r.duration_ms != null ? `${Math.round(r.duration_ms)}ms` : '-';
        const points = r.points_consumed != null ? r.points_consumed.toFixed(4) : '-';
        return `<tr>
          <td>${esc(time)}</td>
          <td>${esc(r.model)}</td>
          <td>${r.prompt_tokens}</td>
          <td>${r.completion_tokens}</td>
          <td>${duration}</td>
          <td>${points}</td>
        </tr>`;
      }).join('');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="6" class="settings-muted">${esc(e.message || '加载失败')}</td></tr>`;
    }
  }

  let _cachedModels = [];
  let _cachedModelKeys = {};

  async function loadCustomKey() {
    const statusEl = document.getElementById('userApiInfo');
    const billingMode = document.getElementById('billingMode');
    const keysSection = document.getElementById('modelKeysSection');
    const keysList = document.getElementById('modelKeysList');

    try {
      const data = await api('/api/user/custom-api-key');
      _cachedModels = data.models || [];
      _cachedModelKeys = data.model_keys || {};

      billingMode.value = data.use_custom_key ? 'custom' : 'points';

      if (data.user_api_base_url) {
        statusEl.innerHTML = `<span class="settings-key-info">用户 API 地址：<code>${esc(data.user_api_base_url)}</code>（${esc(data.user_api_format || 'openai')} 格式）</span>`;
      } else {
        statusEl.innerHTML = '<span class="settings-key-inactive">管理员尚未配置用户自定义 API 地址</span>';
      }

      renderModelKeys(data.use_custom_key);
    } catch (_) {
      if (statusEl) statusEl.textContent = '加载失败';
    }
  }

  function renderModelKeys(showKeys) {
    const keysSection = document.getElementById('modelKeysSection');
    const keysList = document.getElementById('modelKeysList');
    keysSection.classList.toggle('hidden', !showKeys);
    if (!showKeys) return;

    keysList.innerHTML = _cachedModels.map(m => {
      const masked = _cachedModelKeys[m.name] || '';
      const statusText = masked ? `当前：${esc(masked)}` : '未配置';
      const statusClass = masked ? 'settings-key-active' : 'settings-key-inactive';
      return `<div class="settings-model-key-row">
        <span class="settings-model-name">${esc(m.name)}</span>
        <span class="${statusClass} settings-model-key-status">${statusText}</span>
        <input type="password" class="settings-model-key-input" data-model="${esc(m.name)}" placeholder="留空则保持不变" />
      </div>`;
    }).join('') || '<div class="settings-muted">暂无模型</div>';
  }

  document.getElementById('billingMode').addEventListener('change', (e) => {
    renderModelKeys(e.target.value === 'custom');
  });

  document.getElementById('saveCustomKeyBtn').addEventListener('click', async () => {
    const msgEl = document.getElementById('customKeyMsg');
    const useCustom = document.getElementById('billingMode').value === 'custom';

    const modelKeys = {};
    if (useCustom) {
      for (const input of document.querySelectorAll('.settings-model-key-input')) {
        const modelName = input.getAttribute('data-model');
        const val = input.value.trim();
        if (val) {
          modelKeys[modelName] = val;
        }
      }
      const existingKeys = _cachedModelKeys || {};
      for (const [k, v] of Object.entries(existingKeys)) {
        if (v && !(k in modelKeys)) {
          modelKeys[k] = '__KEEP__';
        }
      }
    }

    try {
      await apiWrite('PUT', '/api/user/custom-api-key', { model_keys: modelKeys, use_custom_key: useCustom });
      msgEl.textContent = '已保存，请刷新画布页面使设置生效。';
      msgEl.className = 'settings-msg ok';
      for (const input of document.querySelectorAll('.settings-model-key-input')) {
        input.value = '';
      }
      loadCustomKey();
    } catch (e) {
      msgEl.textContent = e.message || '保存失败';
      msgEl.className = 'settings-msg err';
    }
  });

  document.getElementById('clearCustomKeyBtn').addEventListener('click', async () => {
    const msgEl = document.getElementById('customKeyMsg');
    try {
      await apiWrite('DELETE', '/api/user/custom-api-key');
      msgEl.textContent = '已清除所有自定义 Key，请刷新画布页面。';
      msgEl.className = 'settings-msg ok';
      loadCustomKey();
    } catch (e) {
      msgEl.textContent = e.message || '操作失败';
      msgEl.className = 'settings-msg err';
    }
  });

  loadSummary();
  loadDetail();
  loadCustomKey();
})();
