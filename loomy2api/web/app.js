/* loomy2api 控制台前端逻辑（无框架、无构建）。 */
'use strict';

const $ = (id) => document.getElementById(id);
let KEY = localStorage.getItem('loomy2api_key') || '';
let VIEW = 'accounts';
let LAST_STATE = null;

const TITLES = {
  accounts: ['账号池', 'accounts'],
  usage: ['用量', 'usage'],
  points: ['积分构成', 'points'],
  tasks: ['任务中心', 'tasks'],
  models: ['模型与档位', 'models'],
  proxies: ['代理出口', 'proxies'],
  config: ['配置', 'config'],
  logs: ['运行日志', 'logs'],
};

/* ------------------------------------------------------------ 基础工具 */

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 9000 : 4200);
}

// ---- 配置页「API 接入」大卡片 ----
let KEY_REVEAL = false;

function renderKeyCard() {
  const el = $('keyBig'), base = $('keyBaseUrl');
  if (!el) return;
  if (base) base.textContent = location.origin + '/v1';
  if (!KEY) { el.textContent = '— 未获取 —'; el.style.opacity = .55; return; }
  el.style.opacity = 1;
  el.textContent = KEY_REVEAL ? KEY
    : (KEY.slice(0, 8) + '•'.repeat(Math.max(4, KEY.length - 12)) + KEY.slice(-4));
}

async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg, 'ok');
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove();
    toast(okMsg, 'ok');
  }
}
/** 复制并给按钮一个「✓ 已复制」瞬时反馈（1.2s 后还原） */
async function copyBtnFeedback(btn, text, okMsg) {
  if (!btn || btn.dataset.busy) return;
  btn.dataset.busy = '1';
  const old = btn.textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove();
  }
  btn.textContent = '✓ 已复制';
  setTimeout(() => { btn.textContent = old; delete btn.dataset.busy; }, 1200);
  toast(okMsg, 'ok');
}

// 面板登录：首次/凭证失效时弹出，密码换 api_key，之后全程 x-api-key
function showLogin() {
  $('loginVeil').classList.add('on');
  setTimeout(() => { try { $('loginPw').focus(); } catch (e) {} }, 50);
}

async function doLogin() {
  const pw = $('loginPw').value;
  const btn = $('loginBtn');
  btn.disabled = true; btn.textContent = '登录中…';
  try {
    const res = await fetch('/api/panel/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.error && d.error.message || ('HTTP ' + res.status));
    KEY = d.api_key || '';
    localStorage.setItem('loomy2api_key', KEY);
    try { $('apiKey').value = KEY; } catch (e) {}
    renderKeyCard();
    $('loginVeil').classList.remove('on');
    $('loginPw').value = '';
    toast('登录成功', 'ok');
    loadAll();
  } catch (e) {
    const box = $('loginErr');
    box.style.display = 'block';
    box.textContent = '登录失败：' + e.message;
  } finally { btn.disabled = false; btn.textContent = '登 录'; }
}

// api() 收到 panel_auth_required → 弹登录层
function maybeShowLogin(e) {
  if (String(e.message || '').includes('登录面板')) showLogin();
}

async function api(path, opts) {
  const o = Object.assign({ headers: {} }, opts || {});
  if (KEY) {
    // 公网域名下 Authorization 头被 nginx Basic Auth 占用，Bearer 会被覆盖
    // → 网关的 _presented_key 同时认 x-api-key，用它避开冲突（本地/隧道同样兼容）
    o.headers['x-api-key'] = KEY;
    if (!location.hostname.startsWith('loomy.')) {
      o.headers['Authorization'] = 'Bearer ' + KEY;
    }
  }
  if (o.body) o.headers['Content-Type'] = 'application/json';
  const res = await fetch(path, o);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const raw = data && data.error;
    const msg = (raw && (raw.message || raw)) || ('HTTP ' + res.status);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    if (res.status === 401 && (raw && raw.code) === 'panel_auth_required') {
      err.code = 'panel_auth_required';
    }
    throw err;
  }
  return data;
}

const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const num = (v) => (v === null || v === undefined) ? '—' : v;
const nf = (v) => (v === null || v === undefined) ? '—' : Number(v).toLocaleString();
const when = (ts) => {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
};
const secs = (s) => {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  return (s / 3600).toFixed(1) + 'h';
};

/* -------------------------------------------------------------- 主题 */

const THEMES = ['light', 'dark', 'system'];
function applyTheme(mode) {
  const dark = mode === 'dark' ||
    (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
}
function initTheme() {
  const mode = localStorage.getItem('loomy2api_theme') || 'light';   // 默认白色调
  applyTheme(mode);
  $('btnTheme').onclick = () => {
    const cur = localStorage.getItem('loomy2api_theme') || 'light';
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    localStorage.setItem('loomy2api_theme', next);
    applyTheme(next);
    toast('主题：' + ({ system: '跟随系统', light: '浅色', dark: '深色' })[next]);
  };
}

/* --------------------------------------------------------- 视图切换 */

function show(view) {
  VIEW = view;
  document.querySelectorAll('.nav li a').forEach((a) =>
    a.classList.toggle('on', a.dataset.view === view));
  document.querySelectorAll('.view').forEach((s) =>
    s.classList.toggle('on', s.id === 'view-' + view));
  const [t, sub] = TITLES[view] || ['loomy2api', ''];
  $('ttl').textContent = t;
  $('subMeta').textContent = sub;
  location.hash = '#' + view;
  loadView(view);
}

function loadView(view) {
  if (view === 'accounts') return loadState();
  if (view === 'usage') return loadUsage();
  if (view === 'points') return loadPoints(false);
  if (view === 'tasks') return loadJobs();
  if (view === 'models') return loadModels();
  if (view === 'proxies') return loadProxies();
  if (view === 'config') return loadConfig();
  if (view === 'logs') return loadLog();
}

/* --------------------------------------------------------- 账号池 */

function badge(a) {
  if (!a.enabled) return '<span class="badge b-off">已禁用</span>';
  if (a.in_breaker) return '<span class="badge b-bad">熔断 ' + secs(a.breaker_seconds_left) + '</span>';
  if (a.in_cooldown) {
    const kind = a.cooldown_kind === 'hard' ? '硬冷却' : '冷却';
    return '<span class="badge b-warn">' + kind + ' ' + secs(a.cooldown_seconds_left) + '</span>';
  }
  if (!a.session) return '<span class="badge b-bad">无登录态</span>';
  if (a.session_days_left !== null && a.session_days_left < 3)
    return '<span class="badge b-warn">即将到期</span>';
  if (a.available === 0) return '<span class="badge b-warn">额度用尽</span>';
  const mc = Object.keys(a.model_cooldowns || {}).length;
  return '<span class="badge b-ok">正常</span>' +
    (mc ? ' <span class="badge b-warn">' + mc + ' 模型限流</span>' : '');
}

function renderState(state) {
  LAST_STATE = state;
  const t = state.totals, c = state.config, p = state.pool || {};
  const cooling = state.accounts.filter((a) => a.enabled && (a.in_cooldown || a.in_breaker)).length;
  const disabled = state.accounts.filter((a) => !a.enabled).length;

  $('sTotal').textContent = t.accounts;
  $('sHealthy').textContent = t.usable;
  $('sCooling').textContent = cooling;
  $('sDisabled').textContent = disabled;
  $('sAvailable').textContent = nf(t.available);
  $('sRequests').textContent = nf(t.requests);

  $('poolHint').textContent = '策略 ' + c.strategy + ' · 选号 Top-' + (p.pick_top_n || '-')
    + ' · 粘性 ' + ((p.sticky || {}).entries ?? '-') + ' 条 / TTL ' + ((p.sticky || {}).ttl_seconds ?? '-') + 's'
    + ' · 在途上限 ' + (p.max_inflight_per_account || '不限')
    + ' · 429 软冷却 ' + (p.soft_rate_base_seconds || '-') + 's 封顶 ' + (p.soft_rate_max_seconds || '-') + 's'
    + ' · 熔断阈值 ' + (p.breaker_threshold || '-')
    + ' · 模型 ' + c.models + ' 个'
    + (c.auth_required ? ' · 已开启 Key 校验' : ' · 未设 Key（仅本机）');

  $('navPool').textContent = t.usable + '/' + t.accounts + ' 可用';
  $('navPulse').className = 'pulse' + (t.usable ? '' : ' bad');
  $('navState').textContent = t.usable ? '运行中' : '无可用账号';

  if (!state.accounts.length) {
    $('rows').innerHTML = '<tr><td colspan="10" class="empty">还没有账号，点右上角「添加账号」</td></tr>';
    return;
  }
  $('rows').innerHTML = state.accounts.map((a) => {
    const id = a.identity || {};
    const mode = a.source === 'client' ? 'client 导入'
      : (a.mode === 'session' ? '仅 session' : '手机号+密码');
    const px = a.proxy ? '<span class="mono">' + esc(a.proxy) + '</span>'
      : '<span class="hint">全局/直连</span>';
    return '<tr>'
      + '<td><b>' + esc(a.name) + '</b><div class="hint" style="margin:0">' + mode + '</div></td>'
      + '<td class="mono">' + esc(a.loginid_masked || '—') + '</td>'
      + '<td>' + badge(a) + '</td>'
      + '<td>' + (a.session_days_left === null ? '—' : a.session_days_left.toFixed(1) + ' 天') + '</td>'
      + '<td><b>' + num(a.available) + '</b><div class="hint" style="margin:0">余 ' + num(a.balance) + ' + 日 ' + num(a.daily_balance) + '</div></td>'
      + '<td>' + px + '</td>'
      + '<td>' + nf(a.requests) + '</td><td>' + nf(a.points_used) + '</td>'
      + '<td class="mono" title="' + esc(id.campus_device_id || '') + '">' + esc(id.devid || '—') + '</td>'
      + '<td><div class="row" style="gap:6px">'
      + '<button onclick="act(\'renew\',\'' + esc(a.name) + '\')">续期</button>'
      + '<button onclick="act(\'identity\',\'' + esc(a.name) + '\')">换标识</button>'
      + '<button onclick="act(\'toggle\',\'' + esc(a.name) + '\',' + (!a.enabled) + ')">' + (a.enabled ? '禁用' : '启用') + '</button>'
      + '<button class="danger" onclick="act(\'remove\',\'' + esc(a.name) + '\')">删除</button>'
      + '</div></td></tr>';
  }).join('');
}

async function loadState(refresh) {
  try {
    renderState(await api('/api/panel/state' + (refresh ? '?refresh=1' : '')));
  } catch (e) { maybeShowLogin(e); toast('读取状态失败：' + e.message, 'err'); }
}

async function act(kind, name, value) {
  const map = {
    renew: ['/api/panel/accounts/renew', { name }],
    identity: ['/api/panel/accounts/identity', { name, regenerate: true }],
    toggle: ['/api/panel/accounts/update', { name, enabled: value }],
    remove: ['/api/panel/accounts/remove', { name }],
  };
  if (kind === 'remove' && !confirm('确认删除账号 ' + name + '？')) return;
  const [path, body] = map[kind];
  try {
    const res = await api(path, { method: 'POST', body: JSON.stringify(body) });
    if (res.error) toast(name + '：' + res.error, 'err');
    else if (kind === 'identity') toast(name + ' 已换新设备标识：' + res.identity.devid, 'ok');
    else toast(name + ' 操作完成', 'ok');
    if (res.state) renderState(res.state); else loadState();
  } catch (e) { toast(name + ' 操作失败：' + e.message, 'err'); }
}

/* ------------------------------------------- 添加账号向导 */

let WX_STATE = null;      // 微信扫码会话 state
let LAST_PRICING = {};
let SMS_STATE = null;     // 短信会话 state
let SMS_PHONE = '';
let SMS_MSGID = '';
let SMS_NAME = '';

/** 短信登录的半成品状态存 sessionStorage：刷新页面（拿新 JS）后不该被迫
 *  重发一次短信 —— 只要手机号对得上，上次发的 msgid 就能继续用来提交验证码。 */
const SMS_STORE = 'loomy2api_sms_pending';
function smsSave() {
  try {
    if (SMS_PHONE && SMS_MSGID) {
      sessionStorage.setItem(SMS_STORE, JSON.stringify(
        { phone: SMS_PHONE, msgid: SMS_MSGID, name: SMS_NAME || '', ts: Date.now() }));
    }
  } catch (e) {}
}
function smsClear() { try { sessionStorage.removeItem(SMS_STORE); } catch (e) {} }

/** 页面刷新后把未完成的短信登录接回来。 */
function smsRestore() {
  try {
    const raw = sessionStorage.getItem(SMS_STORE);
    if (!raw) return;
    const d = JSON.parse(raw);
    // 半小时内有效（验证码本身也就几分钟寿命）
    if (d && d.phone && d.msgid && Date.now() - (d.ts || 0) < 30 * 60 * 1000) {
      SMS_PHONE = d.phone; SMS_MSGID = d.msgid; SMS_NAME = d.name || '';
      const el = $('s-phone');
      if (el && !el.value) el.value = d.phone;   // 帮用户把手机号填回去
    }
  } catch (e) {}
}

function wxNote(text, kind) {
  const el = $('wxNote');
  if (!text) { el.className = 'note'; el.innerHTML = ''; return; }
  el.className = 'note on ' + (kind || 'info');
  el.innerHTML = text;
}
function smsNote(text, kind) {
  const el = $('sNote');
  if (!text) { el.className = 'note'; el.innerHTML = ''; return; }
  el.className = 'note on ' + (kind || 'info');
  el.innerHTML = text;
}

function setAddPane(which) {
  ['sms', 'wechat', 'manual'].forEach((name) => {
    const on = name === which;
    const tab = $({ sms: 'segSms', wechat: 'segWechat', manual: 'segManual' }[name]);
    const pane = $({ sms: 'paneSms', wechat: 'paneWechat', manual: 'paneManual' }[name]);
    if (tab) tab.classList.toggle('on', on);
    if (pane) pane.hidden = !on;
  });
}

function openAdd() {
  stopWxPoll();
  WX_STATE = null;
  SMS_STATE = null;
  SMS_PHONE = '';
  SMS_MSGID = '';
  smsRestore();          // 刷新过的话，把上次发过的验证码状态找回来
  wxNote('');
  smsNote('');
  $('wxLinkBox').hidden = true;
  $('wxLinkUrl').textContent = '';
  const lb = $('wxLink');
  if (lb) { lb.disabled = false; lb.textContent = '生成登录链接'; }
  $('wxWatch').innerHTML = '';
  // 复用向导时重置上次成功/失败留下的按钮状态（否则会卡在「登录中…」且永久禁用）
  const sSub = $('sSubmit');
  if (sSub) { sSub.disabled = false; sSub.textContent = '用验证码登录'; }
  const sPass = $('sPassLogin');
  if (sPass) { sPass.disabled = false; sPass.textContent = '用密码登录'; }
  const sSend = $('sSend');
  if (sSend) { sSend.disabled = false; sSend.textContent = '发送验证码'; }
  ['s-name', 's-phone', 's-code', 's-pass'].forEach((id) => { const e = $(id); if (e) e.value = ''; });
  setAddPane('wechat');
  $('addVeil').classList.add('on');
}

function closeAdd() {
  stopWxPoll();
  $('addVeil').classList.remove('on');
}

/* ---- 微信扫码：面板内出二维码，服务端取码（主链路，零操作） ---- */
/* ---- 微信扫码：生成「登录链接」，打开后在页面里登录，自动回调进池 ---- */
let WX_LINK_TIMER = null;

function stopWxPoll() {
  if (WX_LINK_TIMER) { clearInterval(WX_LINK_TIMER); WX_LINK_TIMER = null; }
}

async function wxLinkStart() {
  const btn = $('wxLink');
  btn.disabled = true;
  btn.textContent = '生成中…';
  wxNote('');
  stopWxPoll();
  try {
    const r = await api('/api/panel/login/wechat/qr/start', {
      method: 'POST', body: JSON.stringify({ name: '' }),
    });
    WX_STATE = r.state;
    $('wxLinkUrl').textContent = r.link;
    $('wxLinkBox').hidden = false;
    btn.textContent = '重新生成链接';
    btn.disabled = false;
    window.open(r.link, '_blank');      // 直接帮你打开
    wxNote('登录页已打开 —— 在那个页面里<b>微信扫码</b>或用手机号登录，' +
      '完成后这边会自动收到账号，<b>不需要复制任何东西</b>。', 'info');
    $('wxWatch').innerHTML = '<span class="spin"></span>等待登录…';
    WX_LINK_TIMER = setInterval(wxLinkPoll, 1500);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '生成登录链接';
    $('wxWatch').innerHTML = '';
    wxNote('生成登录链接失败：' + esc(e.message), 'err');
  }
}

async function wxLinkPoll() {
  if (!WX_STATE) return;
  try {
    const r = await api('/api/panel/login/poll?state=' + encodeURIComponent(WX_STATE));
    if (r.status === 'needs_phone') {
      stopWxPoll();
      $('wxWatch').innerHTML = '';
      wxNote('微信已授权 ✅ 但该微信还没绑手机号 —— 回到登录页切到「手机号 + 密码」完成绑定（即注册）。', 'info');
      return;
    }
    if (r.done) {
      stopWxPoll();
      $('wxWatch').innerHTML = '';
      const q = (r.available === null || r.available === undefined) ? '' : (' · 可用积分 ' + r.available);
      wxNote('✅ 已添加账号 <b>' + esc(r.account) + '</b>' + q + '，1.5 秒后自动关闭…', 'ok');
      setTimeout(() => { closeAdd(); loadState(true); }, 1500);
      return;
    }
    if (r.status === 'error' || r.status === 'expired') {
      stopWxPoll();
      $('wxWatch').innerHTML = '';
      wxNote('没成功：' + esc(r.error || '授权未完成') + '，请重新生成链接。', 'err');
    }
  } catch (e) {
    stopWxPoll();
    $('wxWatch').innerHTML = '';
    wxNote('查询状态失败：' + esc(e.message), 'err');
  }
}

/* ---- 短信验证码 ---- */

async function smsSend() {
  const phone = $('s-phone').value.trim();
  if (!/^1[3-9]\d{9}$/.test(phone)) return smsNote('手机号格式不对（11 位，1 开头）', 'err');
  const btn = $('sSend');
  btn.disabled = true;
  btn.textContent = '发送中…';
  try {
    // 直接发：会话由后端按需创建/重建，不再先走一步 login/start，
    // 也就不会出现「登录会话不存在」把人卡住的情况。
    const r = await api('/api/panel/login/send', {
      method: 'POST',
      body: JSON.stringify({ state: SMS_STATE || '', phone,
                             name: $('s-name').value.trim() }),
    });
    SMS_STATE = r.state || SMS_STATE;   // 后端可能就地重建了会话
    SMS_PHONE = phone;
    SMS_MSGID = r.msgid || '';
    SMS_NAME = $('s-name').value.trim();
    smsSave();
    smsNote('验证码已发送到 ' + esc(phone) + '，请查收短信', 'ok');
    $('s-code').focus();
  } catch (e) {
    smsNote('发送失败：' + esc(e.message), 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '重新发送';
  }
}

async function smsSubmit() {
  const code = $('s-code').value.trim();
  const phoneNow = $('s-phone').value.trim();
  if (SMS_MSGID && phoneNow && SMS_PHONE && phoneNow !== SMS_PHONE) {
    SMS_MSGID = '';                    // 换号了，旧的 msgid 作废
  }
  if (!SMS_MSGID && SMS_PHONE !== phoneNow) SMS_PHONE = phoneNow;
  if (!/^\d{6}$/.test(code)) return smsNote('请输入 6 位验证码', 'err');
  if (!SMS_MSGID && !phoneNow) {
    return smsNote('请先填手机号并点「发送验证码」', 'err');
  }
  const btn = $('sSubmit');
  btn.disabled = true;
  btn.textContent = '登录中…';
  try {
    const r = await api('/api/panel/login/submit', {
      method: 'POST',
      body: JSON.stringify({ state: SMS_STATE || '', phone: SMS_PHONE || phoneNow,
                             msgid: SMS_MSGID || '', code }),
    });
    smsClear();
    smsNote('✅ 已添加账号 <b>' + esc(r.account) + '</b>，1.5 秒后自动关闭…', 'ok');
    setTimeout(() => { closeAdd(); loadState(true); }, 1500);
  } catch (e) {
    smsNote('登录失败：' + esc(e.message), 'err');
    btn.disabled = false;
    btn.textContent = '登录并加入账号池';
  }
}

/** 手机号 + 密码：纯后端登录，零验证码、零浏览器，之后还能无人值守续期。 */
async function smsPassLogin() {
  const phone = $('s-phone').value.trim();
  const password = $('s-pass').value;
  if (!/^1[3-9]\d{9}$/.test(phone)) return smsNote('手机号格式不对（11 位，1 开头）', 'err');
  if (!password) return smsNote('请输入密码（需先在讯飞账号中心设置一次）', 'err');
  const btn = $('sPassLogin');
  btn.disabled = true;
  btn.textContent = '登录中…';
  smsNote('');
  try {
    const r = await api('/api/panel/accounts', {
      method: 'POST',
      body: JSON.stringify({
        name: $('s-name').value.trim(), loginid: phone,
        password, login: true,
      }),
    });
    if (r.logged_in === false) throw new Error(r.error || '登录失败');
    smsNote('✅ 已添加账号 <b>' + esc(r.name) + '</b>，1.5 秒后自动关闭…', 'ok');
    setTimeout(() => { closeAdd(); loadState(true); }, 1500);
  } catch (e) {
    smsNote('登录失败：' + esc(e.message), 'err');
    btn.disabled = false;
    btn.textContent = '用密码登录';
  }
}

/* ---- 手动填写 ---- */

async function addAccount() {
  const body = {
    name: $('f-name').value.trim(),
    loginid: $('f-phone').value.trim(),
    password: $('f-pass').value,
    session: $('f-session').value.trim(),
    proxy: $('f-proxy').value.trim(),
    login: $('f-login').value === '1',
  };
  try {
    const res = await api('/api/panel/accounts', { method: 'POST', body: JSON.stringify(body) });
    if (res.logged_in === false) toast('已添加，但登录失败：' + res.error, 'err');
    else toast('已添加 ' + res.name + '（devid ' + res.identity.devid + '）', 'ok');
    ['f-name', 'f-phone', 'f-pass', 'f-session', 'f-proxy'].forEach((id) => { $(id).value = ''; });
    closeAdd();
    if (res.state) renderState(res.state);
  } catch (e) { toast('添加失败：' + e.message, 'err'); }
}

/* ------------------------------------------------------------- 用量 */

function rowsToTable(items) {
  if (!items || !items.length) return '<tr><td colspan="7" class="empty">—</td></tr>';
  return items.map((r) => '<tr>'
    + '<td><b>' + esc(r.name) + '</b></td>'
    + '<td>' + nf(r.requests) + '</td>'
    + '<td><span class="badge b-ok">' + nf(r.ok) + '</span> <span class="badge ' + (r.failed ? 'b-bad' : 'b-off') + '">' + nf(r.failed) + '</span></td>'
    + '<td>' + nf(r.prompt_tokens) + '</td>'
    + '<td>' + nf(r.completion_tokens) + '</td>'
    + '<td><b>' + nf(r.points) + '</b></td>'
    + '<td>' + (r.avg_latency || 0).toFixed(2) + 's</td>'
    + '</tr>').join('');
}

async function loadUsage() {
  // 本地台账与上游对照**分开请求**：上游偶发网络重置（10054/502）时，
  // 本地统计照常显示，对照区单独标灰 —— 不能让整页跟着空。
  try {
    const data = await api('/api/panel/usage?limit=120');
    renderUsage(data);
  } catch (e) { maybeShowLogin(e); toast('读取用量失败：' + e.message, 'err'); }
  try {
    const upData = await api('/api/panel/usage?limit=1&upstream=1');
    const up = upData.upstream;
    if (up && !up.errors) {
      $('uSince').innerHTML += ' · <b>上游真实扣费</b>：' + nf(up.calls) + ' 次 / '
        + '<b>' + nf(up.points) + '</b> 分（生图 110/次）';
    } else if (up && up.errors) {
      $('uSince').innerHTML += ' · <span class="hint" style="margin:0">上游对照暂不可用'
        + '（' + esc((up.errors[0] || '').slice(0, 60)) + '）</span>';
    }
  } catch (e) {
    $('uSince').innerHTML += ' · <span class="hint" style="margin:0">上游对照暂不可用</span>';
  }
}

function renderUsage(data) {
  try {
    const s = data.summary, t = s.totals;
    $('uReq').textContent = nf(t.requests);
    $('uOk').textContent = nf(t.ok);
    $('uFail').textContent = nf(t.failed);
    $('uTok').textContent = nf(t.prompt_tokens) + ' / ' + nf(t.completion_tokens);
    $('uPoints').textContent = nf(t.points);
    $('uLat').textContent = (t.avg_latency || 0).toFixed(2) + 's / ' + (t.avg_ttfb || 0).toFixed(2) + 's';
    $('uSince').textContent = '保留 ' + s.kept + ' 条 · 自 ' + when(s.since) + ' 起统计';
    $('uModels').innerHTML = rowsToTable(s.by_model);
    $('uAccounts').innerHTML = rowsToTable(s.by_account);
    $('uRecent').innerHTML = (data.recent || []).length
      ? data.recent.map((e) => '<tr>'
        + '<td class="mono">' + when(e.ts) + '</td>'
        + '<td>' + esc(e.account || '—') + '</td>'
        + '<td class="mono">' + esc(e.model || '—') + '</td>'
        + '<td>' + (e.status >= 200 && e.status < 300
          ? '<span class="badge b-ok">' + e.status + '</span>'
          : '<span class="badge b-bad" title="' + esc(e.error) + '">' + (e.status || 'ERR') + '</span>')
        + (e.stream ? ' <span class="tag">流</span>' : '') + '</td>'
        + '<td>' + nf(e.prompt_tokens) + '/' + nf(e.completion_tokens) + '</td>'
        + '<td>' + nf(e.points) + '</td>'
        + '<td>' + (e.latency || 0).toFixed(2) + 's</td>'
        + '<td>' + (e.ttfb === null || e.ttfb === undefined ? '—' : e.ttfb.toFixed(2) + 's') + '</td>'
        + '</tr>').join('')
      : '<tr><td colspan="8" class="empty">还没有请求记录</td></tr>';
  } catch (e) { maybeShowLogin(e); toast('读取用量失败：' + e.message, 'err'); }
}

/* --------------------------------------------------------- 积分构成 */

async function doCheckin() {
  const btn = $('pCheckin');
  btn.disabled = true;
  btn.textContent = '签到中…';
  try {
    const r = await api('/api/panel/checkin', { method: 'POST', body: '{}' });
    const parts = (r.results || []).map((x) => x.name + '：' +
      (x.error ? ('<span style="color:var(--bad)">' + esc(x.error) + '</span>')
       : (x.already ? '今日已领' : '签到成功')) +
      '（余额 ' + nf(x.balance) + '）');
    toast(parts.join('　') || '没有可签到的账号', r.results && r.results.every((x) => x.ok) ? 'ok' : 'err');
    loadPoints(true);
  } catch (e) { toast('签到失败：' + e.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '每日签到'; }
}

async function loadPoints(refresh) {
  try {
    const data = await api('/api/panel/points' + (refresh ? '?refresh=1' : ''));
    const t = data.totals;
    $('pBalance').textContent = nf(t.balance);
    $('pDaily').textContent = nf(t.daily_balance);
    $('pAvailable').textContent = nf(t.available);
    $('pUsed').textContent = nf((LAST_STATE && LAST_STATE.totals.points_used) || 0);

    $('pRows').innerHTML = data.accounts.length ? data.accounts.map((a) => '<tr>'
      + '<td><b>' + esc(a.name) + '</b>' + (a.error ? '<div class="hint" style="margin:0;color:var(--bad)">' + esc(a.error) + '</div>' : '') + '</td>'
      + '<td class="mono">' + esc(a.loginid_masked || '—') + '</td>'
      + '<td>' + nf(a.balance) + '</td>'
      + '<td>' + nf(a.daily_balance) + '</td>'
      + '<td><b>' + nf(a.available) + '</b></td>'
      + '<td>' + nf(a.points_used) + '</td>'
      + '<td class="mono">' + (a.quota_updated_at ? when(a.quota_updated_at) : '—') + '</td>'
      + '</tr>').join('') : '<tr><td colspan="7" class="empty">还没有账号</td></tr>';

    // 上游真实字段：direction(debit|credit) / model / points / balance_after / desc
    const recs = [];
    data.accounts.forEach((a) => (a.records || []).forEach((r) => recs.push(Object.assign({ acc: a.name }, r))));
    $('pRecords').innerHTML = recs.length ? recs.slice(0, 60).map((r) => {
      const debit = r.direction !== 'credit';
      return '<tr>'
      + '<td>' + esc(r.acc) + '</td>'
      + '<td>' + (debit ? '<span class="badge b-bad">支出</span>' : '<span class="badge b-ok">收入</span>')
        + (r.model ? ' <span class="mono hint" style="margin:0">' + esc(r.model) + '</span>' : '') + '</td>'
      + '<td><b style="color:' + (debit ? 'var(--bad)' : 'var(--ok)') + '">' + (debit ? '−' : '+') + nf(r.points) + '</b></td>'
      + '<td class="mono">' + (r.balance_after === null || r.balance_after === undefined ? '—' : nf(r.balance_after)) + '</td>'
      + '<td class="hint" style="margin:0">' + esc(r.desc || '') + '</td>'
      + '<td class="mono">' + (r.time ? when(r.time) : '—') + '</td>'
      + '</tr>';
    }).join('') : '<tr><td colspan="6" class="empty">上游未返回流水</td></tr>';

    // 各模型实测单价（来自流水聚合），供「模型与档位」页合并显示
    const pricing = {};
    data.accounts.forEach((a) => Object.assign(pricing, a.model_pricing || {}));
    LAST_PRICING = pricing;
  } catch (e) { maybeShowLogin(e); toast('读取积分失败：' + e.message, 'err'); }
}

/* --------------------------------------------------------- 任务中心 */

async function loadJobs() {
  try {
    const data = await api('/api/panel/tasks');
    // 签到状态
    const ck = data.checkin || {};
    $('tCheckinInfo').innerHTML = ck.error
      ? ('<span style="color:var(--bad)">' + esc(ck.error) + '</span>')
      : ('余额 <b>' + nf(ck.balance) + '</b>（长期 ' + nf(ck.permanent) + ' · 每日 ' +
         nf(ck.daily) + '/' + nf(ck.daily_quota) + '）· 周期 ' + esc(ck.cycle || '—') +
         (ck.already ? ' · <span class="badge b-ok">今日已领</span>' : ''));
    // 邀请码（本账号自己的，邀请别人注册用）
    const codes = ck.invite_codes || [];
    $('tInviteRows').innerHTML = codes.length ? codes.map((c) => '<tr>'
      + '<td class="mono"><b>' + esc(c.inviteCode || '—') + '</b></td>'
      + '<td>' + nf(c.usedCount) + ' / ' + nf(c.maxUses) + '</td>'
      + '<td><span class="badge ' + (c.status === 'active' ? 'b-ok' : 'b-off') + '">'
        + esc(c.status || '—') + '</span></td>'
      + '</tr>').join('') : '<tr><td colspan="3" class="empty">上游未返回邀请码</td></tr>';
    // 激活状态
    const act = data.activation || {};
    const actRows = act.error
      ? [['状态', '<span style="color:var(--bad)">' + esc(act.error) + '</span>']]
      : Object.entries(act).map(([k, v]) => [esc(k), esc(String(v))]);
    $('tActivationRows').innerHTML = actRows.length
      ? actRows.map(([k, v]) => '<tr><td class="hint" style="margin:0">' + k
          + '</td><td><b>' + v + '</b></td></tr>').join('')
      : '<tr><td colspan="2" class="empty">—</td></tr>';
    // 账号池状态（沿用）
    const st = LAST_STATE || await api('/api/panel/state');
    const p = st.pool || {};
    const rows = [
      ['策略 strategy', st.config.strategy],
      ['选号短名单 Top-N', p.pick_top_n],
      ['粘性会话条目', ((p.sticky || {}).entries) + ' / TTL ' + ((p.sticky || {}).ttl_seconds) + 's'],
      ['429 软冷却', (p.soft_rate_base_seconds || '-') + 's → 封顶 ' + (p.soft_rate_max_seconds || '-') + 's'],
      ['熔断阈值', (p.breaker_threshold || '-') + ' 次连续失败'],
      ['模型数量', st.config.models],
    ];
    $('poolRows').innerHTML = rows.map(([k, v]) =>
      '<tr><td class="hint" style="margin:0">' + esc(k)
      + '</td><td><b>' + esc(v) + '</b></td></tr>').join('');
  } catch (e) { maybeShowLogin(e); toast('读取任务失败：' + e.message, 'err'); }
}

async function doCheckinView() {
  const btn = $('tCheckin');
  btn.disabled = true; btn.textContent = '签到中…';
  try {
    const r = await api('/api/panel/checkin', { method: 'POST', body: '{}' });
    const parts = (r.results || []).map((x) => x.name + '：' +
      (x.error ? ('<span style="color:var(--bad)">' + esc(x.error) + '</span>')
       : (x.already ? '今日已领' : '签到成功')) + '（余额 ' + nf(x.balance) + '）');
    toast(parts.join('　') || '没有可签到的账号', 'ok');
    loadJobs();
  } catch (e) { toast('签到失败：' + e.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '立即签到'; }
}

async function doRedeem() {
  const code = $('tCode').value.trim();
  if (!code) return toast('请输入兑换码', 'err');
  const btn = $('tRedeem');
  btn.disabled = true; btn.textContent = '兑换中…';
  try {
    const r = await api('/api/panel/redeem', {
      method: 'POST', body: JSON.stringify({ code }) });
    toast((r.ok ? '兑换成功：' : '兑换失败：') + esc(r.desc || ''), r.ok ? 'ok' : 'err');
    if (r.ok) $('tCode').value = '';
    loadPoints(true);
  } catch (e) { toast('兑换失败：' + e.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '兑换'; }
}

async function doBindInvite() {
  const invite = $('tInvite').value.trim();
  if (!invite) return toast('请输入邀请码', 'err');
  const btn = $('tBindInvite');
  btn.disabled = true; btn.textContent = '绑定中…';
  try {
    const r = await api('/api/panel/invite', {
      method: 'POST', body: JSON.stringify({ invite_code: invite }) });
    toast((r.ok ? '绑定成功：' : '绑定失败：') + esc(r.desc || ''), r.ok ? 'ok' : 'err');
    if (r.ok) $('tInvite').value = '';
    loadJobs();
  } catch (e) { toast('绑定失败：' + e.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '绑定'; }
}

/* ------------------------------------------------------- 模型与档位 */

const PG_TOOLS = [{
  type: 'function',
  function: { name: 'get_weather', description: '查询城市当前天气',
    parameters: { type: 'object',
                  properties: { city: { type: 'string', description: '城市名' } },
                  required: ['city'] } },
}];

async function pgRun() {
  const model = $('pgModel').value;
  const effort = $('pgEffort').value;
  const maxTok = parseInt($('pgMax').value, 10) || 200;
  const stream = $('pgStream').checked;
  const useTools = $('pgTools').checked;
  const prompt = $('pgPrompt').value.trim() || '你好';
  const btn = $('pgRun');
  btn.disabled = true; btn.textContent = '运行中…';
  $('pgOut').textContent = ''; $('pgReason').style.display = 'none';
  $('pgMeta').textContent = '请求中…';
  const t0 = performance.now();
  const payload = { model, stream, max_tokens: maxTok,
                    messages: [{ role: 'user', content: prompt }] };
  if (effort) payload.reasoning_effort = effort;
  if (useTools) { payload.tools = PG_TOOLS; payload.tool_choice = 'auto'; }
  try {
    if (!stream) {
      const j = await api('/v1/chat/completions', {
        method: 'POST', body: JSON.stringify(payload) });
      const ch = (j.choices || [{}])[0];
      const msg = ch.message || {};
      const u = j.usage || {};
      showPgResult(msg, ch.finish_reason, u, performance.now() - t0, false);
    } else {
      // 流式：手动读 SSE，增量渲染 content / reasoning / tool_calls
      // 头部规则与 api() 一致：公网域名下用 x-api-key（Bearer 会被 nginx Basic Auth 覆盖）
      const sHeaders = Object.assign({}, KEY ? { 'x-api-key': KEY } : {},
        { 'Content-Type': 'application/json' });
      if (KEY && !location.hostname.startsWith('loomy.')) {
        sHeaders['Authorization'] = 'Bearer ' + KEY;
      }
      const res = await fetch('/v1/chat/completions', {
        method: 'POST', headers: sHeaders, body: JSON.stringify(payload) });
      if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + (await res.text()).slice(0, 160));
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let content = '', reason = '', toolArgs = '', toolName = '', finish = '', usage = null, buf = '';
      $('pgOut').textContent = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n'); buf = lines.pop();
        for (const line of lines) {
          const t = line.trim();
          if (!t.startsWith('data:')) continue;
          const p2 = t.slice(5).trim();
          if (p2 === '[DONE]') continue;
          let j2; try { j2 = JSON.parse(p2); } catch (e) { continue; }
          if (j2.usage) usage = j2.usage;
          const d = ((j2.choices || [{}])[0]).delta || {};
          if (d.reasoning_content) { reason += d.reasoning_content;
            const re = $('pgReason'); re.style.display = 'block';
            re.textContent = '🤔 思考：' + reason; }
          if (d.content) { content += d.content; $('pgOut').textContent = content; }
          for (const tc of (d.tool_calls || [])) {
            if (tc.function && tc.function.name) toolName = tc.function.name;
            if (tc.function && tc.function.arguments) toolArgs += tc.function.arguments;
          }
          if (((j2.choices || [{}])[0]).finish_reason) finish = ((j2.choices || [{}])[0]).finish_reason;
        }
      }
      showPgResult(
        { content, reasoning_content: reason,
          tool_calls: toolName ? [{ function: { name: toolName, arguments: toolArgs } }] : [] },
        finish, usage || {}, performance.now() - t0, true);
    }
  } catch (e) {
    $('pgOut').innerHTML = '<span style="color:var(--bad)">失败：' + esc(e.message) + '</span>';
    $('pgMeta').textContent = '';
  } finally { btn.disabled = false; btn.textContent = '运行'; }
}

function showPgResult(msg, finish, usage, ms, streamed) {
  const tcs = msg.tool_calls || [];
  if (tcs.length) {
    $('pgOut').innerHTML = '<b>🔧 工具调用</b> ' + esc(tcs.map((t) => (t.function || {}).name).join(', '))
      + '\n参数：' + esc(tcs.map((t) => (t.function || {}).arguments || '').join(''));
  } else {
    $('pgOut').textContent = msg.content || '（空回复）';
  }
  const u = usage || {};
  $('pgMeta').innerHTML = (streamed ? '流式' : '非流式') + ' · finish=' + esc(finish || '—')
    + ' · tok ' + nf(u.prompt_tokens) + '/' + nf(u.completion_tokens)
    + (u.completion_tokens_details && u.completion_tokens_details.reasoning_tokens
       ? '（思考 ' + nf(u.completion_tokens_details.reasoning_tokens) + '）' : '')
    + ' · 扣 <b>' + nf(u.points_consumed) + '</b> 分 · ' + (ms / 1000).toFixed(1) + 's';
}

/* ------------------------------------------------------- 模型与档位 */

async function loadModels() {
  try {
    const data = await api('/api/panel/models');
    $('mdCount').textContent = data.models.length + ' 个 · 默认 ' + data.default_model;
    const sel = $('pgModel');
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = data.models.map((m) => {
        const label = m.id + (m.multiplier ? '（x' + m.multiplier + '）' : '');
        return '<option value="' + esc(m.id) + '"' +
          (m.id === data.default_model ? ' selected' : '') + '>' + esc(label) + '</option>';
      }).join('');
      if (prev && data.models.some((m) => m.id === prev)) sel.value = prev;
    }
    $('mdRows').innerHTML = data.models.length ? data.models.map((m) => {
      const caps = [];
      if (m.type && m.type !== 'chat') caps.push('<b>' + esc(m.type) + '</b>');
      if (m.reasoning) caps.push('推理');
      if (m.tools) caps.push('工具');
      if (m.streaming) caps.push('流式');
      if (m.vision) caps.push('视觉');
      (m.modalities || []).forEach((x) => { if (x !== 'text') caps.push(x); });
      if (m.reasoning_efforts && m.reasoning_efforts.length) {
        caps.push('<span class="hint" style="margin:0">档位:' + esc(m.reasoning_efforts.join('/')) + '</span>');
      }
      // 上游部分模型 multiplier 字段为 null，倍率只写在名称文本里（如 "（x3.0）"）
      let mult = (m.multiplier === null || m.multiplier === undefined) ? null : Number(m.multiplier);
      if (mult === null || Number.isNaN(mult)) {
        const mm = /(?:x|×)([0-9.]+)/i.exec(m.name || '');
        if (mm) mult = parseFloat(mm[1]);
      }
      const multText = (mult === null || Number.isNaN(mult)) ? '—' : ('x' + mult);
      const pr = LAST_PRICING[m.id];
      const price = pr
        ? ('<b>' + pr.avg + '</b> 分/次 <span class="hint" style="margin:0">(' + pr.calls + ' 次' +
           (pr.min !== pr.max ? '，' + pr.min + '–' + pr.max : '') + ')</span>')
        : '<span class="hint" style="margin:0">暂无流水</span>';
      const tone = (mult !== null && mult <= 1) ? 'b-ok'
        : (mult !== null && mult >= 6 ? 'b-bad' : 'b-warn');
      return '<tr>'
        + '<td class="mono"><b>' + esc(m.id) + '</b></td>'
        + '<td>' + esc(m.name || '') + '</td>'
        + '<td><span class="badge ' + tone + '">' + multText + '</span></td>'
        + '<td>' + price + '</td>'
        + '<td>' + nf(m.context_length) + '</td>'
        + '<td>' + caps.map((c) => '<span class="tag">' + esc(c) + '</span>').join(' ') + '</td>'
        + '<td>' + (m.is_default ? '<span class="badge b-accent">默认</span>' : '') + '</td>'
        + '</tr>';
    }).join('') : '<tr><td colspan="7" class="empty">上游未返回模型（未登录？）</td></tr>';
  } catch (e) { maybeShowLogin(e); toast('读取模型失败：' + e.message, 'err'); }
}

/* --------------------------------------------------------- 代理出口 */

async function loadProxies() {
  try {
    const data = await api('/api/panel/proxies');
    $('pxGlobal').textContent = data.global_effective;
    // 风控提示：多账号同 IP 出口是最典型的关联特征
    const noProxy = (data.accounts || []).filter((a) => !a.proxy).length;
    if ((data.accounts || []).length > 1 && noProxy > 1 && !data.global_proxy) {
      toast('⚠ ' + (data.accounts || []).length + ' 个账号里 ' + noProxy +
        ' 个没配独立出口代理 —— 同 IP 多账号是最典型的关联风控特征，建议逐个配置。', 'err');
    }
    $('pxBound').textContent = data.accounts.filter((a) => a.proxy).length;
    $('pxDirect').textContent = data.accounts.filter((a) => !a.proxy && !data.global_proxy).length;
    $('pxGlobalInput').value = data.global_proxy || '';
    $('pxRows').innerHTML = data.accounts.length ? data.accounts.map((a) => '<tr>'
      + '<td><b>' + esc(a.name) + '</b></td>'
      + '<td class="mono">' + esc(a.loginid_masked || '—') + '</td>'
      + '<td><input value="' + esc(a.proxy) + '" placeholder="留空 = 回落到全局" '
      + 'id="px-' + esc(a.name) + '" style="min-width:190px"></td>'
      + '<td class="mono">' + esc(a.effective) + '</td>'
      + '<td><span class="badge ' + (a.source === '账号级' ? 'b-accent' : (a.source === '全局' ? 'b-warn' : 'b-off')) + '">' + esc(a.source) + '</span></td>'
      + '<td><button onclick="saveProxy(\'' + esc(a.name) + '\')">保存</button></td>'
      + '</tr>').join('') : '<tr><td colspan="6" class="empty">还没有账号</td></tr>';
  } catch (e) { toast('读取代理失败：' + e.message, 'err'); }
}

async function saveProxy(name) {
  const el = $('px-' + name);
  const value = el ? el.value : '';
  try {
    await api('/api/panel/accounts/proxy', {
      method: 'POST', body: JSON.stringify({ name, proxy: value }),
    });
    toast(name + ' 出口代理已更新', 'ok');
    loadProxies();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
}

async function saveGlobalProxy() {
  const proxy = $('pxGlobalInput').value.trim();
  try {
    await api('/api/panel/config', {
      method: 'POST', body: JSON.stringify({ config: { proxy } }),
    });
    toast('全局代理已保存' + (proxy ? '' : '（直连）'), 'ok');
    loadProxies();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
}

/* ------------------------------------------------------------- 配置 */

async function loadConfig() {
  try {
    const res = await api('/api/panel/config');
    $('cfg').value = JSON.stringify(res.config, null, 2);
    $('cfgNote').textContent = '需重启字段：' + (res.restart_keys || []).join(', ');
  } catch (e) { toast('读取配置失败：' + e.message, 'err'); }
}

async function saveConfig() {
  let parsed;
  try { parsed = JSON.parse($('cfg').value); }
  catch (e) { return toast('配置不是合法 JSON：' + e.message, 'err'); }
  // 脱敏占位不能写回去，否则会把真实 Key 覆盖成 "sk-xxx…（已设置）"
  if (Array.isArray(parsed.api_keys) && parsed.api_keys.some((k) => String(k).includes('已设置'))) {
    delete parsed.api_keys;
  }
  try {
    const res = await api('/api/panel/config', {
      method: 'POST', body: JSON.stringify({ config: parsed }),
    });
    const need = res.restart_required || [];
    toast(need.length ? ('已保存；需重启生效：' + need.join(', ')) : '配置已保存并热生效', 'ok');
    loadConfig();
    if (VIEW === 'accounts') loadState();
  } catch (e) { toast('保存配置失败：' + e.message, 'err'); }
}

/* --------------------------------------------------------- 运行日志 */

async function loadLog() {
  try {
    const res = await api('/api/panel/logs?lines=250');
    $('logPath').textContent = res.path;
    const box = $('log');
    box.textContent = res.lines.join('\n');
    box.scrollTop = box.scrollHeight;
  } catch (e) { toast('读取日志失败：' + e.message, 'err'); }
}

/* ------------------------------------------------------------- 启动 */

async function init() {
  initTheme();

  document.querySelectorAll('.nav li a').forEach((a) => {
    a.onclick = (ev) => { ev.preventDefault(); show(a.dataset.view); };
  });

  $('btnAdd').onclick = openAdd;
  $('btnCloseAdd').onclick = closeAdd;
  $('addVeil').addEventListener('click', (ev) => {
    if (ev.target === $('addVeil')) closeAdd();
  });
  $('segWechat').onclick = () => setAddPane('wechat');
  $('segSms').onclick = () => setAddPane('sms');
  $('segManual').onclick = () => setAddPane('manual');
  $('wxLink').onclick = wxLinkStart;
  $('wxLinkOpen').onclick = () => window.open($('wxLinkUrl').textContent, '_blank');
  $('wxLinkCopy').onclick = () => {
    const url = $('wxLinkUrl').textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject())
      .then(() => toast('链接已复制', 'ok'), () => toast('复制失败，请手动选中复制', 'err'));
  };
  $('sSend').onclick = smsSend;
  $('sSubmit').onclick = smsSubmit;
  $('sPassLogin').onclick = smsPassLogin;
  $('s-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') smsPassLogin(); });
  $('s-code').addEventListener('keydown', (e) => { if (e.key === 'Enter') smsSubmit(); });
  $('btnRefresh').onclick = () => {
    loadView(VIEW);
    if (VIEW === 'accounts') loadState(true);
  };
  $('saveKey').onclick = () => {
    KEY = $('apiKey').value.trim();
    renderKeyCard();
    localStorage.setItem('loomy2api_key', KEY);
    toast('已保存 API Key', 'ok');
    loadView(VIEW);
  };
  $('apiKey').value = KEY;

  $('add').onclick = addAccount;
  $('uClear').onclick = async () => {
    if (!confirm('清空用量台账？')) return;
    try {
      await api('/api/panel/usage/clear', { method: 'POST', body: '{}' });
      toast('已清空', 'ok'); loadUsage();
    } catch (e) { toast('清空失败：' + e.message, 'err'); }
  };
  $('pRefresh').onclick = () => loadPoints(true);
  $('pgRun').onclick = pgRun;
  $('loginBtn').onclick = doLogin;
  $('showKey').onclick = () => {
    const el = $('apiKey');
    el.type = el.type === 'password' ? 'text' : 'password';
  };
  $('keyEye').onclick = () => { KEY_REVEAL = !KEY_REVEAL; renderKeyCard(); };
  $('keyCopyBig').onclick = () => {
    if (!KEY) return toast('当前没有 API Key（先登录）', 'err');
    copyBtnFeedback($('keyCopyBig'), KEY, 'API Key 已复制到剪贴板');
  };
  $('keyCopyAll').onclick = () => {
    if (!KEY) return toast('当前没有 API Key（先登录）', 'err');
    copyBtnFeedback($('keyCopyAll'), location.origin + '/v1|' + KEY,
      '已复制「Base URL|Key」，粘贴后按 | 拆分');
  };
  document.querySelectorAll('[data-copy]').forEach((b) => {
    b.onclick = () => copyText(b.getAttribute('data-copy'), 'Base URL 已复制');
  });
  $('copyKey').onclick = async () => {
    const v = $('apiKey').value.trim();
    if (!v) return toast('当前没有 API Key', 'err');
    try {
      await navigator.clipboard.writeText(v);
      toast('API Key 已复制到剪贴板', 'ok');
    } catch (e) {
      // 剪贴板 API 不可用（非安全上下文等）→ 选中输入框内容兜底
      const el = $('apiKey');
      el.type = 'text';
      el.focus(); el.select();
      document.execCommand('copy');
      toast('API Key 已复制', 'ok');
    }
  };
  $('loginPw').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('pCheckin').onclick = doCheckin;
  $('tCheckin').onclick = doCheckinView;
  $('tRedeem').onclick = doRedeem;
  $('tCode').addEventListener('keydown', (e) => { if (e.key === 'Enter') doRedeem(); });
  $('tBindInvite').onclick = doBindInvite;
  $('tInvite').addEventListener('keydown', (e) => { if (e.key === 'Enter') doBindInvite(); });
  $('pxSaveGlobal').onclick = saveGlobalProxy;
  $('cfgLoad').onclick = loadConfig;
  $('cfgSave').onclick = saveConfig;
  $('logLoad').onclick = loadLog;

  await bootstrapKey();
  renderKeyCard();
  const start = (location.hash || '#accounts').slice(1);
  show(TITLES[start] ? start : 'accounts');

  setInterval(() => {
    if (VIEW === 'accounts') loadState();
    else if (VIEW === 'logs' && $('logAuto').checked) loadLog();
    else if (VIEW === 'usage') loadUsage();
  }, 10000);
}

init();

