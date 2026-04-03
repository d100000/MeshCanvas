/* ═══════════════════════════════════════════════════════════════════
   Pixel Banana Login — Mouse-Interactive SVG Characters
   Based on banana-demo.html with real-time eye tracking
   ═══════════════════════════════════════════════════════════════════ */

const authTabs = Array.from(document.querySelectorAll('[data-auth-tab]'));
const authPanels = Array.from(document.querySelectorAll('[data-auth-panel]'));
const authMessage = document.getElementById('authMessage');
const loginForm = document.getElementById('loginForm');
const registerForm = document.getElementById('registerForm');
const bananaScene = document.getElementById('bananaScene');

/* ═══════════════════════════════════════════════════════════════════
   BananaActor — per-character SVG DOM controller
   Ported from banana-demo.html BananaDemo class
   ═══════════════════════════════════════════════════════════════════ */

/* Pupil coordinate data per character type (from demo) */
const PUPIL_DATA = {
  pirate: {
    /* One visible eye (right); left eye is a patch */
    eyes: [
      { selector: '.pupil', centerX: 112, centerY: 98, minX: 106, maxX: 118, minY: 94, maxY: 103 }
    ]
  },
  ninja: {
    eyes: [
      { selector: '.pupil-left',  centerX: 82,  centerY: 95, minX: 79, maxX: 87, minY: 92, maxY: 99 },
      { selector: '.pupil-right', centerX: 106, centerY: 95, minX: 103, maxX: 111, minY: 92, maxY: 99 }
    ]
  },
  hero: {
    eyes: [
      { selector: '.pupil-left',  centerX: 76,  centerY: 98, minX: 73, maxX: 81, minY: 95, maxY: 102 },
      { selector: '.pupil-right', centerX: 112, centerY: 98, minX: 109, maxX: 117, minY: 95, maxY: 102 }
    ]
  }
};

class BananaActor {
  constructor(type) {
    this.type = type;
    this.svg = document.getElementById(`${type}-banana`);
    this.pupilData = PUPIL_DATA[type];
    this._pupils = [];
    /* Cache pupil DOM elements */
    if (this.svg) {
      for (const pd of this.pupilData.eyes) {
        const el = this.svg.querySelector(pd.selector);
        if (el) this._pupils.push({ el, data: pd });
      }
    }
  }

  _getEl(id) {
    return document.getElementById(`${this.type}-${id}`);
  }

  /* ── Pupil animation (easeOutCubic via rAF) ── */
  animatePupil(element, targetX, targetY) {
    if (!element) return;
    const startX = parseFloat(element.getAttribute('x'));
    const startY = parseFloat(element.getAttribute('y'));
    const startTime = performance.now();
    const duration = 250;
    const animate = (time) => {
      const t = Math.min((time - startTime) / duration, 1);
      const ease = 1 - Math.pow(1 - t, 3);
      element.setAttribute('x', startX + (targetX - startX) * ease);
      if (targetY !== undefined) element.setAttribute('y', startY + (targetY - startY) * ease);
      if (t < 1) requestAnimationFrame(animate);
    };
    requestAnimationFrame(animate);
  }

  /* ── Mouse-driven pupil positioning (instant, called per frame) ── */
  setPupilFromMouse(mouseX, mouseY) {
    if (!this.svg) return;
    const rect = this.svg.getBoundingClientRect();
    const scaleX = rect.width / 200;
    const scaleY = rect.height / 250;
    for (const { el, data } of this._pupils) {
      const eyePageX = rect.left + data.centerX * scaleX;
      const eyePageY = rect.top + data.centerY * scaleY;
      const dx = mouseX - eyePageX;
      const dy = mouseY - eyePageY;
      const maxDist = 300;
      const dist = Math.hypot(dx, dy);
      const ratio = Math.min(dist / maxDist, 1);
      const angle = Math.atan2(dy, dx);
      const maxShiftX = (data.maxX - data.minX) / 2;
      const maxShiftY = (data.maxY - data.minY) / 2;
      const px = data.centerX + Math.cos(angle) * maxShiftX * ratio;
      const py = data.centerY + Math.sin(angle) * maxShiftY * ratio;
      el.setAttribute('x', clamp(px, data.minX, data.maxX));
      el.setAttribute('y', clamp(py, data.minY, data.maxY));
    }
  }

  /* ── State methods (from demo) ── */
  showNormalEyes() {
    /* Show normal eye groups, hide alternatives */
    const eyes = this._getEl('eyes');
    if (eyes) eyes.style.display = '';
    /* Show individual eye sub-groups for pirate */
    const eyeRight = this._getEl('eye-right');
    if (eyeRight) eyeRight.style.display = '';
    const eyeLeft = this._getEl('eye-left');
    if (eyeLeft) eyeLeft.style.display = '';
    /* Hide alternatives */
    for (const suffix of ['eye-closed', 'eyes-closed', 'eye-surprised', 'eyes-surprised']) {
      const el = this._getEl(suffix);
      if (el) el.style.display = 'none';
    }
    /* Hide hand-cover */
    const hc = this._getEl('hand-cover');
    if (hc) hc.style.display = 'none';
    /* Restore normal mouth */
    const m = this._getEl('mouth');
    if (m) m.style.display = '';
    const ms = this._getEl('mouth-surprised');
    if (ms) ms.style.display = 'none';
  }

  lookRight() {
    this.showNormalEyes();
    for (const { el, data } of this._pupils) {
      this.animatePupil(el, data.maxX, data.centerY);
    }
  }

  lookLeft() {
    this.showNormalEyes();
    for (const { el, data } of this._pupils) {
      this.animatePupil(el, data.minX, data.centerY);
    }
  }

  lookCenter() {
    this.showNormalEyes();
    for (const { el, data } of this._pupils) {
      this.animatePupil(el, data.centerX, data.centerY);
    }
  }

  lookDown() {
    this.showNormalEyes();
    for (const { el, data } of this._pupils) {
      this.animatePupil(el, data.centerX, data.maxY);
    }
  }

  closeEyes() {
    /* Hide normal eyes, show closed */
    if (this.type === 'pirate') {
      const er = this._getEl('eye-right');
      if (er) er.style.display = 'none';
      const ec = this._getEl('eye-closed');
      if (ec) ec.style.display = '';
    } else {
      const el = this._getEl('eye-left');
      if (el) el.style.display = 'none';
      const er = this._getEl('eye-right');
      if (er) er.style.display = 'none';
      const ec = this._getEl('eyes-closed');
      if (ec) ec.style.display = '';
    }
  }

  surprised() {
    /* Show surprised eyes + mouth, hide normal */
    this._getEl('hand-cover')?.style.setProperty('display', 'none');
    if (this.type === 'pirate') {
      const er = this._getEl('eye-right');
      if (er) er.style.display = 'none';
      const es = this._getEl('eye-surprised');
      if (es) es.style.display = '';
    } else {
      const el = this._getEl('eye-left');
      if (el) el.style.display = 'none';
      const er = this._getEl('eye-right');
      if (er) er.style.display = 'none';
      const es = this._getEl('eyes-surprised');
      if (es) es.style.display = '';
    }
    const m = this._getEl('mouth');
    if (m) m.style.display = 'none';
    const ms = this._getEl('mouth-surprised');
    if (ms) ms.style.display = '';
  }

  coverEyes() {
    const hc = this._getEl('hand-cover');
    if (hc) hc.style.display = '';
    const eyes = this._getEl('eyes');
    if (eyes) eyes.style.display = 'none';
  }

  reset() {
    this.showNormalEyes();
    this.lookCenter();
  }
}

/* ═══════════════════════════════════════════════════════════════════
   BananaController — orchestrates all three actors
   ═══════════════════════════════════════════════════════════════════ */

function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }
function lerp(a, b, t) { return a + (b - a) * t; }

class BananaController {
  constructor(scene) {
    this.scene = scene;
    if (!scene) return;
    this._state = 'idle';
    this._formFocused = false;
    this._blinkTimer = null;
    this._mouseX = 0;
    this._mouseY = 0;
    this._rafId = null;

    this.actors = [
      new BananaActor('pirate'),
      new BananaActor('ninja'),
      new BananaActor('hero'),
    ];

    /* Start idle behaviors */
    this._startBlinking();
    this._startMouseTracking();
    this._setupClickEasterEggs();
  }

  /* ── State management ── */
  setState(state) {
    if (!this.scene) return;
    if (this._state === state) return;
    this.scene.classList.remove(
      'state-idle', 'state-username', 'state-password',
      'state-captcha', 'state-success', 'state-error'
    );
    this._state = state;
    this.scene.classList.add(`state-${state}`);

    /* Trigger actor actions based on state */
    switch (state) {
      case 'password':
        this._formFocused = true;
        this.actors.forEach(a => a.coverEyes());
        break;
      case 'captcha':
        this._formFocused = true;
        this.actors.forEach(a => a.lookDown());
        break;
      case 'username':
        this._formFocused = true;
        this.actors.forEach(a => a.lookRight());
        break;
      case 'error':
        this._formFocused = false;
        this.actors.forEach(a => a.surprised());
        break;
      case 'success':
        this._formFocused = false;
        this.actors.forEach(a => a.showNormalEyes());
        break;
      case 'idle':
      default:
        this._formFocused = false;
        this.actors.forEach(a => a.showNormalEyes());
        break;
    }
  }

  /* ── Mouse tracking ── */
  _startMouseTracking() {
    document.addEventListener('mousemove', (e) => {
      this._mouseX = e.clientX;
      this._mouseY = e.clientY;
    }, { passive: true });

    const tick = () => {
      if (!this._formFocused && this._state !== 'error') {
        this.actors.forEach(a => a.setPupilFromMouse(this._mouseX, this._mouseY));
      }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  }

  /* ── Username input progress → pupil horizontal tracking ── */
  setLookProgress(progress) {
    if (!this.scene) return;
    for (const actor of this.actors) {
      for (const { el, data } of actor._pupils) {
        const targetX = lerp(data.centerX, data.maxX, progress);
        el.setAttribute('x', clamp(targetX, data.minX, data.maxX));
      }
    }
  }

  /* ── Blinking ── */
  _startBlinking() {
    if (!this.scene) return;
    const doBlink = () => {
      if (this._state === 'password' || this._state === 'error') {
        this._blinkTimer = setTimeout(doBlink, 2000 + Math.random() * 2000);
        return;
      }
      this.actors.forEach(a => a.closeEyes());
      setTimeout(() => {
        if (this._state !== 'password' && this._state !== 'error') {
          this.actors.forEach(a => a.showNormalEyes());
        }
      }, 150);
      this._blinkTimer = setTimeout(doBlink, 3000 + Math.random() * 3000);
    };
    this._blinkTimer = setTimeout(doBlink, 2000 + Math.random() * 2000);
  }

  /* ── Click easter eggs ── */
  _setupClickEasterEggs() {
    const bubbleSymbols = ['!', '\u266a', '\u2605', '\u2764', '\u2728'];
    for (const type of ['pirate', 'ninja', 'hero']) {
      const wrap = document.getElementById(`wrap-${type}`);
      const bubble = document.getElementById(`bubble-${type}`);
      if (!wrap || !bubble) continue;
      wrap.addEventListener('click', () => {
        /* Jump */
        wrap.classList.remove('click-jump');
        void wrap.offsetWidth; /* reflow */
        wrap.classList.add('click-jump');
        setTimeout(() => wrap.classList.remove('click-jump'), 500);
        /* Bubble */
        bubble.textContent = bubbleSymbols[Math.floor(Math.random() * bubbleSymbols.length)];
        bubble.classList.remove('pop');
        void bubble.offsetWidth;
        bubble.classList.add('pop');
        setTimeout(() => bubble.classList.remove('pop'), 800);
      });
    }
  }

  /* ── Confetti ── */
  celebrate() {
    if (!this.scene) return;
    const colors = ['#7B26E8', '#FFD93D', '#059669', '#A96BF0', '#34D399', '#6366F1'];
    const rect = this.scene.getBoundingClientRect();
    for (let i = 0; i < 14; i++) {
      const p = document.createElement('div');
      p.className = 'confetti-particle';
      p.style.background = colors[i % colors.length];
      p.style.left = `${rect.width * 0.15 + Math.random() * rect.width * 0.7}px`;
      p.style.top = `${rect.height * 0.15}px`;
      const size = 3 + Math.random() * 5;
      p.style.width = `${size}px`;
      p.style.height = `${size}px`;
      p.style.borderRadius = Math.random() > 0.5 ? '50%' : '1px';
      p.style.animationDuration = `${0.6 + Math.random() * 0.6}s`;
      p.style.animationDelay = `${Math.random() * 0.15}s`;
      p.style.setProperty('--drift', `${(Math.random() - 0.5) * 100}px`);
      this.scene.appendChild(p);
      setTimeout(() => p.remove(), 1500);
    }
  }
}

const bananaCtrl = new BananaController(bananaScene);

/* ── Wire banana interactions to form fields ── */

function findFormInputs(form) {
  return {
    username: form.querySelector('input[name="username"]'),
    password: form.querySelector('input[name="password"]'),
    captcha: form.querySelector('input[name="captcha_answer"]'),
  };
}

function wireFormBananas(form) {
  const inputs = findFormInputs(form);
  if (inputs.username) {
    inputs.username.addEventListener('focus', () => {
      bananaCtrl.setState('username');
    });
    inputs.username.addEventListener('input', (e) => {
      const progress = e.target.value.length / (e.target.maxLength || 32);
      bananaCtrl.setLookProgress(progress);
    });
    inputs.username.addEventListener('blur', () => {
      bananaCtrl.setState('idle');
    });
  }
  if (inputs.password) {
    inputs.password.addEventListener('focus', () => {
      bananaCtrl.setState('password');
    });
    inputs.password.addEventListener('blur', () => {
      bananaCtrl.setState('idle');
    });
  }
  if (inputs.captcha) {
    inputs.captcha.addEventListener('focus', () => {
      bananaCtrl.setState('captcha');
    });
    inputs.captcha.addEventListener('blur', () => {
      bananaCtrl.setState('idle');
    });
  }
}

wireFormBananas(loginForm);
wireFormBananas(registerForm);

/* ═══════════════════════════════════════════════════════════════════
   Auth Logic (preserved from original)
   ═══════════════════════════════════════════════════════════════════ */

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
  if (name === 'register' && bananaCtrl) {
    bananaCtrl.setState('idle');
  }
}

function applyRegistrationStatus(allow) {
  registrationAllowed = allow;
  const regTab = authTabs.find((t) => t.dataset.authTab === 'register');
  if (!regTab) return;
  if (!allow) {
    regTab.classList.add('disabled');
    regTab.setAttribute('aria-disabled', 'true');
    regTab.title = '暂未开放注册';
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
  } catch { /* ignore */ }
}

function loadAllCaptchas() {
  loadCaptcha(loginForm);
  loadCaptcha(registerForm);
}

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
      loadCaptcha(form);
      if (bananaCtrl) {
        bananaCtrl.setState('error');
        setTimeout(() => bananaCtrl.setState('idle'), 800);
      }
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.innerHTML = '<span class="auth-check">✓</span>进入画布';
      button.classList.remove('loading');
      button.classList.add('success');
    }
    setMessage('');
    if (bananaCtrl) {
      bananaCtrl.setState('success');
      bananaCtrl.celebrate();
    }
    window.location.href = '/app';
  } catch {
    setSubmitting(form, false);
    setMessage('网络异常，请稍后重试。', 'error');
    loadCaptcha(form);
    if (bananaCtrl) {
      bananaCtrl.setState('error');
      setTimeout(() => bananaCtrl.setState('idle'), 800);
    }
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
  .then((r) => r.json())
  .then((p) => { if (p.authenticated) window.location.href = '/app'; })
  .catch((err) => console.error('[auth] session check failed', err));

fetch('/api/auth/registration-status')
  .then((r) => r.json())
  .then((d) => applyRegistrationStatus(d.allow))
  .catch(() => {});
