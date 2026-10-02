/* 前端渲染冒烟测试：假 DOM + 真实后端数据，逐个跑 load* / render* 函数。
 * 目的是在没有浏览器的情况下验证 app.js 的逻辑不会抛错、且能产出 HTML。
 *   用法: node tools/smoke_panel.js [baseUrl]
 */
'use strict';
const fs = require('fs');
const path = require('path');
const http = require('http');

const BASE = process.argv[2] || 'http://127.0.0.1:17890';
const APP = path.join(__dirname, '..', 'loomy2api', 'web', 'app.js');

/* ---------------------------------------------------- 假 DOM */
const store = new Map();
function makeEl(id) {
  return {
    id, value: '', textContent: '', innerHTML: '', className: '', style: {},
    checked: true, scrollTop: 0, scrollHeight: 0, src: '', hidden: false,
    classList: { toggle() {}, add() {}, remove() {} },
    appendChild() {}, remove() {}, focus() {},
    removeAttribute(name) { this[name] = ''; },
    setAttribute(name, v) { this[name] = v; },
    onclick: null,
  };
}
function el(id) {
  if (!store.has(id)) store.set(id, makeEl(id));
  return store.get(id);
}
global.document = {
  getElementById: el,
  querySelectorAll: () => [],
  createElement: () => makeEl('tmp'),
  documentElement: { setAttribute() {} },
};
global.window = { matchMedia: () => ({ matches: false }), addEventListener() {},
  open: (url) => { global.__opened = url; return null; } };
global.location = { hash: '#accounts' };
global.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); },
};
global.setInterval = () => 0;
global.confirm = () => true;
global.alert = () => {};

/* ------------------------------------------- 真实后端数据（http 直连） */
const KEY = (() => {
  // 面板若配置了 panel_key，请求必须带 Authorization
  try {
    const src = fs.readFileSync(path.join(__dirname, '..', 'config.json'), 'utf8');
    return JSON.parse(src).panel_key || '';
  } catch (e) { return ''; }
})();

function request(pathname, method, payload) {
  return new Promise((resolve, reject) => {
    const body = payload ? JSON.stringify(payload) : null;
    const headers = body ? { 'Content-Type': 'application/json',
                             'Content-Length': Buffer.byteLength(body) } : {};
    if (KEY) headers['Authorization'] = 'Bearer ' + KEY;
    const req = http.request(BASE + pathname, { method, headers }, (res) => {
      let out = '';
      res.on('data', (c) => (out += c));
      res.on('end', () => {
        try { resolve(JSON.parse(out)); }
        catch (e) { reject(new Error(pathname + ': ' + out.slice(0, 200))); }
      });
    });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}
const get = (p) => request(p, 'GET');
const post = (p, b) => request(p, 'POST', b);

global.fetch = async (url, opts) => {
  const path = url.replace(BASE, '');
  const data = (opts && opts.method === 'POST')
    ? await post(path, opts.body ? JSON.parse(opts.body) : {})
    : await get(path);
  return { ok: true, status: 200, json: async () => data };
};

/* ------------------------------------------------- 加载并驱动 app.js */
const errors = [];
const originalError = console.error;
console.error = (...a) => { errors.push(a.join(' ')); };

const src = fs.readFileSync(APP, 'utf8');
// 去掉末尾的 init() 自执行，改为手动驱动
const body = src.replace(/\ninit\(\);\s*$/, '\n');
const mod = { };
const fn = new Function(body + '\n; return {loadState,loadUsage,loadPoints,loadJobs,loadModels,loadProxies,loadConfig,loadLog,show,renderState,openAdd,closeAdd,wxLinkStart,wxLinkPoll,smsSend,smsSubmit,smsPassLogin,setAddPane};');
const api = fn();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const results = [];
  async function step(name, run, check) {
    try {
      await run();
      await sleep(60);
      const ok = check ? check() : true;
      results.push([ok ? 'PASS' : 'FAIL', name, ok ? '' : '断言未通过']);
    } catch (e) {
      results.push(['FAIL', name, e.message]);
    }
  }

  await step('账号池 loadState', () => api.loadState(), () => el('rows').innerHTML.includes('还没有账号') || el('rows').innerHTML.includes('<tr>'));
  await step('用量 loadUsage', () => api.loadUsage(), () => el('uReq').textContent !== '' && el('uRecent').innerHTML.length > 0);
  await step('积分构成 loadPoints', () => api.loadPoints(false), () => el('pRows').innerHTML.length > 0);
  await step('任务中心 loadJobs', () => api.loadJobs(), () => (el('tInviteRows').innerHTML.length > 0) && (el('tActivationRows').innerHTML.length > 0));
  await step('模型与档位 loadModels', () => api.loadModels(), () => el('mdRows').innerHTML.includes('spark-x') || el('mdRows').innerHTML.includes('模型'));
  await step('代理出口 loadProxies', () => api.loadProxies(), () => el('pxRows').innerHTML.length > 0);
  await step('配置 loadConfig', () => api.loadConfig(), () => el('cfg').value.includes('strategy'));
  await step('运行日志 loadLog', () => api.loadLog(), () => typeof el('log').textContent === 'string');

  // 添加账号向导：微信扫码 → 生成登录链接（打开后在那个页面里登录，自动回调）
  await step('向导 openAdd', () => api.openAdd(), () => true);
  await step('向导 生成登录链接', () => api.wxLinkStart(),
    () => String(el('wxLinkUrl').textContent || '').includes('/panel/login-link?state='));
  await step('向导 登录链接自动打开', () => true,
    () => String(global.__opened || '').includes('/panel/login-link?state='));
  await step('向导 轮询登录状态', () => api.wxLinkPoll(), () => true);
  await step('向导 密码登录字段齐全', () => true,
    () => !!el('s-phone') && !!el('s-pass'));
  await step('向导 切换到短信页', () => api.setAddPane('sms'), () => true);
  await step('向导 切换到手动页', () => api.setAddPane('manual'), () => true);
  await step('向导 closeAdd', () => api.closeAdd(), () => true);

  console.error = originalError;
  let failed = 0;
  for (const [st, name, msg] of results) {
    console.log(`  ${st}  ${name}${msg ? '  — ' + msg : ''}`);
    if (st === 'FAIL') failed++;
  }
  if (errors.length) {
    console.log('\n控制台报错:');
    errors.slice(0, 10).forEach((e) => console.log('  ' + e.slice(0, 200)));
    failed += errors.length;
  }
  console.log(`\n${results.length - failed}/${results.length} 通过`);
  process.exit(failed ? 1 : 0);
})();
