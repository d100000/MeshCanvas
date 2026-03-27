const passwordForm = document.getElementById('passwordForm');
const settingsMessage = document.getElementById('settingsMessage');

function setMessage(text, type = '') {
  settingsMessage.textContent = text || '';
  settingsMessage.className = `auth-message ${type}`.trim();
}

async function loadSettings() {
  try {
    const res = await fetch('/api/settings', { credentials: 'same-origin' });
    if (res.status === 401) {
      window.location.href = '/login';
      return;
    }
    const data = await res.json();

    document.getElementById('infoBaseUrl').textContent = data.api_base_url || '-';
    document.getElementById('infoApiFormat').textContent = (data.api_format || 'openai').toUpperCase();
    document.getElementById('infoApiKey').textContent = data.api_key_masked || '-';

    const models = data.models || [];
    document.getElementById('infoModels').textContent = models.length
      ? models.map(m => m.name || m.id).join(', ')
      : '-';

    const searchAvail = data.search_available;
    document.getElementById('infoSearch').textContent = searchAvail
      ? `已启用（${data.firecrawl_api_key_masked || 'Firecrawl'}）`
      : '未启用';
  } catch {
    setMessage('加载设置失败，请刷新重试。', 'error');
  }
}

passwordForm.addEventListener('submit', async (event) => {
  event.preventDefault();

  const formData = new FormData(passwordForm);
  const old_password = (formData.get('old_password') || '').trim();
  const new_password = (formData.get('new_password') || '').trim();
  const confirm_password = (formData.get('confirm_password') || '').trim();

  if (!old_password) { setMessage('请输入当前密码。', 'error'); return; }
  if (!new_password || new_password.length < 8) { setMessage('新密码至少 8 位。', 'error'); return; }
  if (new_password !== confirm_password) { setMessage('两次输入的新密码不一致。', 'error'); return; }

  const submitBtn = passwordForm.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.innerHTML = '<span class="auth-spinner"></span>修改中';
  submitBtn.classList.add('loading');
  setMessage('');

  try {
    const res = await fetch('/api/settings/password', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ old_password, new_password }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      setMessage(payload.detail || '修改失败，请重试。', 'error');
      return;
    }
    submitBtn.innerHTML = '<span class="auth-check">&check;</span>已修改';
    submitBtn.classList.remove('loading');
    submitBtn.classList.add('success');
    setMessage('密码修改成功。', 'success');
    passwordForm.reset();
    setTimeout(() => {
      submitBtn.textContent = '修改密码';
      submitBtn.classList.remove('success');
    }, 2000);
  } catch {
    setMessage('网络异常，请稍后重试。', 'error');
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove('loading');
  }
});

loadSettings();
