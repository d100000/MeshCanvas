/**
 * 管理后台登录页（须为外部脚本：CSP 为 script-src 'self'，内联脚本会被浏览器拦截）
 */
(function () {
  const form = document.getElementById('adminLoginForm');
  const msg = document.getElementById('adminMessage');
  if (!form || !msg) return;

  const ERR_MSG = {
    badcreds: '用户名或密码错误。',
    forbidden: '该账号没有管理员权限，请使用具备 admin 角色的账号。',
    invalid: '用户名或密码格式不符合要求。',
    rate: '登录尝试过于频繁，请稍后再试。',
    captcha: '验证码错误或已过期，请重新计算后提交。',
    form:
      '无法解析登录表单（请 pip install -e . 安装依赖并重启；排除扩展/代理改写 POST；默认表单不依赖 multipart）。',
    server:
      '服务暂时异常（非表单、非数据库类错误）。请查看 uvicorn 终端中带「admin session-login」的报错。',
    db:
      '数据库无法完成写入（常见于库文件只读、目录无权限、磁盘满或多进程争用同一 SQLite）。请检查 LOCAL_DB_PATH 与终端完整错误。',
    session:
      '未携带有效管理后台登录状态（Cookie 未生效或已过期）。若使用 HTTPS 或反向代理，请确认已转发 X-Forwarded-Proto: https，且 uvicorn 使用 --forwarded-allow-ips=*。',
  };

  function initFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const err = params.get('error');
    // 服务端已注入文案时不再覆盖；仅作缓存旧 HTML 时的兜底
    if (err && !msg.textContent.trim()) {
      msg.textContent =
        ERR_MSG[err] || '登录出现问题（错误代码：' + err + '），请重试或查看服务端日志。';
      msg.classList.add('error');
    }
    const u = (params.get('username') || '').trim();
    const hasPasswordParam = params.has('password');
    if (u) {
      const input = form.querySelector('[name="username"]');
      if (input && !input.value.trim()) input.value = u;
    }
    if (hasPasswordParam) {
      const extra =
        '请勿把密码写在网址中；请在下方密码框输入（默认密码为 admin）。';
      msg.textContent = msg.textContent ? msg.textContent + ' ' + extra : extra;
      msg.classList.add('error');
    }
    if (params.toString()) {
      history.replaceState(null, '', '/admin');
    }
  }

  /* ---- Captcha refresh ---- */
  function refreshCaptcha() {
    fetch('/api/captcha')
      .then(function (res) { return res.json(); })
      .then(function (data) {
        var qEl = form.querySelector('[data-captcha-q]');
        var tEl = form.querySelector('[data-captcha-token]');
        if (qEl) qEl.textContent = data.question;
        if (tEl) tEl.value = data.token;
        var ans = form.querySelector('[name="captcha_answer"]');
        if (ans) ans.value = '';
      })
      .catch(function () { /* ignore */ });
  }

  var refreshBtn = form.querySelector('[data-captcha-refresh]');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function (e) {
      e.preventDefault();
      refreshCaptcha();
    });
  }

  initFromUrl();

  form.addEventListener('submit', function () {
    var btn = form.querySelector('button[type="submit"]');
    if (!btn) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="auth-spinner"></span>登录中';
  });
})();
