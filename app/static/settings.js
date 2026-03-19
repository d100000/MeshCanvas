const settingsForm = document.getElementById('settingsForm');
const modelList = document.getElementById('modelList');
const addModelBtn = document.getElementById('addModelBtn');
const settingsMessage = document.getElementById('settingsMessage');
const apiKeyHint = document.getElementById('apiKeyHint');
const fcKeyHint = document.getElementById('fcKeyHint');

function setMessage(text, type = '') {
  settingsMessage.textContent = text || '';
  settingsMessage.className = `auth-message ${type}`.trim();
}

function escapeAttr(value) {
  return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function addModelRow(name = '', id = '') {
  const row = document.createElement('div');
  row.className = 'setup-model-row';
  row.innerHTML = `
    <input type="text" class="model-name-input" placeholder="如 GPT-5" value="${escapeAttr(name)}" required />
    <input type="text" class="model-id-input" placeholder="如 gpt-5-turbo" value="${escapeAttr(id)}" required />
    <button type="button" class="model-remove-btn" title="删除此模型">✕</button>
  `;
  row.querySelector('.model-remove-btn').addEventListener('click', () => {
    if (modelList.children.length > 1) {
      row.remove();
    }
  });
  modelList.appendChild(row);
}

function collectModels() {
  const rows = modelList.querySelectorAll('.setup-model-row');
  const models = [];
  for (const row of rows) {
    const name = row.querySelector('.model-name-input').value.trim();
    const id = row.querySelector('.model-id-input').value.trim();
    if (name && id) {
      models.push({ name, id });
    }
  }
  return models;
}

async function loadSettings() {
  try {
    const res = await fetch('/api/settings', { credentials: 'same-origin' });
    if (res.status === 401) {
      window.location.href = '/login';
      return;
    }
    const data = await res.json();

    settingsForm.api_base_url.value = data.api_base_url || '';
    settingsForm.api_format.value = data.api_format || 'openai';
    settingsForm.api_key.value = '';
    settingsForm.firecrawl_api_key.value = '';
    settingsForm.firecrawl_country.value = data.firecrawl_country || 'CN';
    settingsForm.firecrawl_timeout_ms.value = data.firecrawl_timeout_ms || 45000;

    if (apiKeyHint) {
      apiKeyHint.textContent = data.api_key_masked ? `（当前：${data.api_key_masked}）` : '';
    }
    if (fcKeyHint) {
      fcKeyHint.textContent = data.firecrawl_api_key_masked ? `（当前：${data.firecrawl_api_key_masked}）` : '';
    }

    modelList.innerHTML = '';
    const models = data.models || [];
    if (models.length) {
      for (const m of models) {
        addModelRow(m.name || '', m.id || '');
      }
    } else {
      addModelRow();
    }
  } catch {
    setMessage('加载设置失败，请刷新重试。', 'error');
  }
}

addModelBtn.addEventListener('click', () => addModelRow());

settingsForm.addEventListener('submit', async (event) => {
  event.preventDefault();

  const formData = new FormData(settingsForm);
  const api_base_url = (formData.get('api_base_url') || '').trim();
  const api_format = (formData.get('api_format') || 'openai').trim();
  const api_key = (formData.get('api_key') || '').trim();
  const firecrawl_api_key = (formData.get('firecrawl_api_key') || '').trim();
  const firecrawl_country = (formData.get('firecrawl_country') || 'CN').trim();
  const firecrawl_timeout_ms = parseInt(formData.get('firecrawl_timeout_ms') || '45000', 10) || 45000;
  const models = collectModels();

  if (!api_base_url) { setMessage('请填写 API 地址。', 'error'); return; }
  if (!models.length) { setMessage('请至少添加一个模型。', 'error'); return; }

  const submitBtn = settingsForm.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.innerHTML = '<span class="auth-spinner"></span>保存中';
  submitBtn.classList.add('loading');
  setMessage('');

  try {
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        api_base_url, api_format, api_key, models,
        firecrawl_api_key, firecrawl_country, firecrawl_timeout_ms,
      }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      setMessage(payload.detail || '保存失败，请重试。', 'error');
      return;
    }
    submitBtn.innerHTML = '<span class="auth-check">✓</span>已保存';
    submitBtn.classList.remove('loading');
    submitBtn.classList.add('success');
    setMessage('');
    setTimeout(() => {
      submitBtn.textContent = '保存设置';
      submitBtn.classList.remove('success');
      loadSettings();
    }, 1200);
  } catch {
    setMessage('网络异常，请稍后重试。', 'error');
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove('loading');
  }
});

loadSettings();
