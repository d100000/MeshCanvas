const authTabs = Array.from(document.querySelectorAll('[data-auth-tab]'));
const authPanels = Array.from(document.querySelectorAll('[data-auth-panel]'));
const authMessage = document.getElementById('authMessage');
const loginForm = document.getElementById('loginForm');
const registerForm = document.getElementById('registerForm');

function setMessage(text, type = '') {
  authMessage.textContent = text || '';
  authMessage.className = `auth-message ${type}`.trim();
}

function setSubmitting(form, isSubmitting) {
  const button = form.querySelector('button[type="submit"]');
  if (!button) return;
  button.disabled = isSubmitting;
  button.textContent = isSubmitting
    ? (form.dataset.authPanel === 'login' ? '登录中...' : '注册中...')
    : (form.dataset.authPanel === 'login' ? '登录' : '注册并进入');
}

function switchTab(name) {
  for (const tab of authTabs) {
    const active = tab.dataset.authTab === name;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  }
  for (const panel of authPanels) {
    panel.classList.toggle('hidden', panel.dataset.authPanel !== name);
  }
  setMessage('');
}

async function submitAuth(form, endpoint) {
  const formData = new FormData(form);
  const username = String(formData.get('username') || '').trim();
  const password = String(formData.get('password') || '');
  if (!username || !password) {
    setMessage('请填写完整的用户名和密码。', 'error');
    return;
  }

  setSubmitting(form, true);
  setMessage('处理中...', 'pending');
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
      credentials: 'same-origin',
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      setMessage(payload.detail || '操作失败，请稍后重试。', 'error');
      return;
    }
    setMessage('成功，正在进入画布...', 'success');
    window.location.href = '/';
  } catch {
    setMessage('网络异常，请稍后重试。', 'error');
  } finally {
    setSubmitting(form, false);
  }
}

for (const tab of authTabs) {
  tab.addEventListener('click', () => switchTab(tab.dataset.authTab));
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  await submitAuth(loginForm, '/api/auth/login');
});

registerForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  await submitAuth(registerForm, '/api/auth/register');
});

switchTab('login');

fetch('/api/auth/session', { credentials: 'same-origin' })
  .then((response) => response.json())
  .then((payload) => {
    if (payload.authenticated) {
      window.location.href = '/';
    }
  })
  .catch(() => {});
