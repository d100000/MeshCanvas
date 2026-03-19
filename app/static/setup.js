const setupForm = document.getElementById('setupForm');
const modelList = document.getElementById('modelList');
const addModelBtn = document.getElementById('addModelBtn');
const setupMessage = document.getElementById('setupMessage');

function setMessage(text, type = '') {
  setupMessage.textContent = text || '';
  setupMessage.className = `auth-message ${type}`.trim();
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
  row.querySelector('.model-name-input').focus();
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

addModelBtn.addEventListener('click', () => addModelRow());

setupForm.addEventListener('submit', async (event) => {
  event.preventDefault();

  const formData = new FormData(setupForm);
  const base_url = (formData.get('base_url') || '').trim();
  const api_format = (formData.get('api_format') || 'openai').trim();
  const API_key = (formData.get('API_key') || '').trim();
  const models = collectModels();

  if (!base_url) { setMessage('请填写 API 地址。', 'error'); return; }
  if (!API_key) { setMessage('请填写 API Key。', 'error'); return; }
  if (!models.length) { setMessage('请至少添加一个模型。', 'error'); return; }

  const submitBtn = setupForm.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.textContent = '保存中...';
  setMessage('正在保存配置并初始化数据库...', 'pending');

  try {
    const response = await fetch('/api/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url, api_format, API_key, models }),
      credentials: 'same-origin',
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      setMessage(payload.detail || '保存失败，请重试。', 'error');
      return;
    }
    setMessage('配置成功！正在跳转到注册页面...', 'success');
    setTimeout(() => { window.location.href = '/'; }, 600);
  } catch {
    setMessage('网络异常，请稍后重试。', 'error');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = '保存并开始';
  }
});

addModelRow();
