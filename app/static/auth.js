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

let registrationAllowed = true;

function switchTab(name) {
  if (name === 'register' && !registrationAllowed) return;
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

function applyRegistrationStatus(allow) {
  registrationAllowed = allow;
  const regTab = authTabs.find((t) => t.dataset.authTab === 'register');
  if (!regTab) return;
  if (!allow) {
    regTab.classList.add('disabled');
    regTab.setAttribute('aria-disabled', 'true');
    regTab.title = '暂未开放注册';
    // If currently on register tab, switch back to login
    if (regTab.classList.contains('active')) switchTab('login');
  } else {
    regTab.classList.remove('disabled');
    regTab.removeAttribute('aria-disabled');
    regTab.title = '';
  }
}

/* ---- Captcha ---- */

async function loadCaptcha(form) {
  try {
    const res = await fetch('/api/captcha');
    const data = await res.json();
    const qEl = form.querySelector('[data-captcha-q]');
    const tEl = form.querySelector('[data-captcha-token]');
    if (qEl) qEl.textContent = data.question;
    if (tEl) tEl.value = data.token;
    const ansInput = form.querySelector('[name="captcha_answer"]');
    if (ansInput) ansInput.value = '';
  } catch {
    /* ignore – the server-side check will reject anyway */
  }
}

function loadAllCaptchas() {
  loadCaptcha(loginForm);
  loadCaptcha(registerForm);
}

// Refresh buttons
for (const btn of document.querySelectorAll('[data-captcha-refresh]')) {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    const form = btn.closest('form');
    if (form) loadCaptcha(form);
  });
}

/* ---- Submit ---- */

async function submitAuth(form, endpoint) {
  const formData = new FormData(form);
  const username = String(formData.get('username') || '').trim();
  const password = String(formData.get('password') || '');
  const captchaAnswer = String(formData.get('captcha_answer') || '').trim();
  const captchaToken = String(formData.get('captcha_token') || '');
  const website = String(formData.get('website') || '');

  if (!username || !password) {
    setMessage('请填写完整的用户名和密码。', 'error');
    return;
  }
  if (!captchaAnswer) {
    setMessage('请输入验证码计算结果。', 'error');
    return;
  }

  setSubmitting(form, true);
  setMessage('');
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, captcha_answer: captchaAnswer, captcha_token: captchaToken, website }),
      credentials: 'same-origin',
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      setSubmitting(form, false);
      setMessage(payload.detail || '操作失败，请稍后重试。', 'error');
      // Refresh captcha on failure
      loadCaptcha(form);
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.innerHTML = '<span class="auth-check">✓</span>进入画布';
      button.classList.remove('loading');
      button.classList.add('success');
    }
    setMessage('');
    window.location.href = '/app';
  } catch {
    setSubmitting(form, false);
    setMessage('网络异常，请稍后重试。', 'error');
    loadCaptcha(form);
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
loadAllCaptchas();

fetch('/api/auth/session', { credentials: 'same-origin' })
  .then((response) => response.json())
  .then((payload) => {
    if (payload.authenticated) {
      window.location.href = '/app';
    }
  })
  .catch((err) => { console.error('[auth] session check failed', err); });

fetch('/api/auth/registration-status')
  .then((r) => r.json())
  .then((d) => applyRegistrationStatus(d.allow))
  .catch(() => {});
