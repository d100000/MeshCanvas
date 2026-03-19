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
  if (isSubmitting) {
    const label = form.dataset.authPanel === 'login' ? '登录中' : '注册中';
    button.innerHTML = `<span class="auth-spinner"></span>${label}`;
    button.classList.add('loading');
  } else {
    button.textContent = form.dataset.authPanel === 'login' ? '登录' : '注册并进入';
    button.classList.remove('loading');
  }
  for (const input of form.querySelectorAll('input')) {
    input.readOnly = isSubmitting;
  }
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
  setMessage('');
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
      credentials: 'same-origin',
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      setSubmitting(form, false);
      setMessage(payload.detail || '操作失败，请稍后重试。', 'error');
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.innerHTML = '<span class="auth-check">✓</span>进入画布';
      button.classList.remove('loading');
      button.classList.add('success');
    }
    setMessage('');
    window.location.href = '/';
  } catch {
    setSubmitting(form, false);
    setMessage('网络异常，请稍后重试。', 'error');
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
