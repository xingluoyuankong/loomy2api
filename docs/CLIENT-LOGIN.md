# 客户端登录链路（逆向 + 实抓包）

这份文档回答一个问题：**Loomy 桌面客户端到底会「跳转」到哪个官方登录网址？**

结论：**只有一个 —— 微信开放平台「网站应用」扫码登录页。**
除此之外，密码登录和短信登录都是**客户端内部的 API 调用**，没有任何外部页面。

---

## 1. 证据来源

客户端本机没装，从官方下载接口拿绿色版：

```
GET https://loomy.xunfei.cn/api/download?platform=windows&variant=secondary
  → 302 https://static-res.xfinfr.com/loomy/win/Loomy-0.9.38-win.zip   (553 MB)
GET https://loomy.xunfei.cn/api/download?platform=windows
  → 302 https://static-res.xfinfr.com/loomy/win/Loomy-Setup-0.9.38.exe (444 MB)
```

ZIP 支持 Range，所以**只取需要的文件**，不整包下载
（`_loomy_client/zipgrab.py` 取中央目录 + 指定字节区间，`fetch_electron.py` 批量拉主进程源码）。

主进程源码是明文的，位于 `resources/app.asar.unpacked/electron/`（449 个 js，3.9 MB）。

## 2. 客户端注册了自定义协议

`electron/utils/deep-link-handler.js`：

```js
// 目前识别两种形态（其他全部返回 null，防滥用）：
//   - loomy://import-buddy/<shareId>          灵魂角色分享
//   - loomy://oauth/wechat?code=xxx&state=yy  微信登录回调（静态回调页 fallback 唤起）
const ALLOWED_SCHEME = 'loomy:'
```

## 3. 唯一的外部登录网址：微信 qrconnect

`electron/xfyun/wechat-oauth.js`：

```js
const AUTH_URL_BASE = 'https://open.weixin.qq.com/connect/qrconnect'
const SCOPE = 'snsapi_login'

export function buildAuthUrl({ wxAppId, redirectUri, state, scope = SCOPE } = {}) {
  return `${AUTH_URL_BASE}?appid=${encodeURIComponent(wxAppId)}`
       + `&redirect_uri=${encodeURIComponent(redirectUri)}`
       + `&response_type=code&scope=${encodeURIComponent(scope)}`
       + `&state=${encodeURIComponent(state)}#wechat_redirect`
}
```

AppID 与回调域来自加密的 `resources/.env.prod`
（`LOOMYENC1:` = base64(salt16‖iv12‖tag16‖ct)，AES-256-GCM，`scrypt(passphrase, salt, 32)`；
口令硬编码在 `electron/utils/env-file-crypto.js` —— 官方注释自己写着
「本质是『混淆』而非真正的密钥保密」）：

```
LOOMY_WECHAT_APP_ID=wx18d60be432287cf8        # 微信开放平台「网站应用」，科迅创想主体
DEFAULT_WECHAT_REDIRECT_URI = 'https://loomy.xunfei.cn/oauth/wechat/callback'
```

## 4. 客户端怎么拿到 code（关键）

**它自己就是个浏览器** —— 把授权页塞进 Electron `BrowserWindow`，在 `will-redirect`
里把跳转拦下来、`preventDefault()` **不真的加载回调页**：

```js
const authWin = new BrowserWindow({ width: 460, height: 620, title: '微信登录', ... })
authWin.webContents.on('will-redirect', (event, url) => {
  if (tryHandleCallback(url)) { event.preventDefault() }   // ← 拿到 code 且不跳转
})
authWin.webContents.on('did-navigate', (_e, url) => { tryHandleCallback(url) })  // 兜底
```

## 5. 完整编排（`electron/ipc/xfyun-ipc.js:100`）

```js
// 微信扫码登录：主进程编排 = 拉授权页 → OAuth code → 讯飞 bindAuth
//   - bind=1（讯飞侧已绑手机号）→ 自动 bindSkip 换 session（走 §3.2.7 skip 分支）
//   - bind=0（未绑）→ 返回 { needBindPhone: true, rcode, ... } 给渲染层
ipcMain.handle("xfyun:account:loginByWechat", async (event) => {
  const { wxAppId, redirectUri } = xfyunAccountService.getWechatOAuthParams();
  const authResult = await openWechatAuthWindow({ wxAppId, redirectUri, parentWindow });
  const code = authResult.code;
  const authRes = await xfyunAccountService.bindAuthThirdAccount({ code, type: "wx" });
  const { bind, rcode } = authRes.data.data;
  if (bind === 1) return xfyunAccountService.bindSkip({ rcode });   // → session
  return { success: true, needBindPhone: true, rcode };             // → 绑手机 UI
});
```

对应的账号服务端点（`electron/xfyun/account-service.js`，全部 HMAC 签名）：

```
POST /login/thirdAccount/bind/auth      {tcode:{code}, type:"wx"}  → {bind, rcode, isnew, nickname}
POST /login/thirdAccount/bind/skip      {rcode, expire}            → {session, userid}
POST /login/thirdAccount/bind/sendMsg   {rcode, phone, ccode}      → {msgid}
POST /login/thirdAccount/bind/checkCode {rcode, mcode, msgid}      → {session, userid, phone}
```

**没有**扫码轮询（qrcode poll）类的接口 —— 客户端源码里 `qrcode` 只出现在企业微信
（`wecom-service.js`）和支付码场景，与登录无关。

## 6. 实抓包

用**客户端自己的 `buildAuthUrl`**（剥掉 Electron import，其余一字不改）产出 URL，
再实打一次真实 HTTP 交换：

```
=== 客户端 buildAuthUrl() ===
https://open.weixin.qq.com/connect/qrconnect?appid=wx18d60be432287cf8
  &redirect_uri=https%3A%2F%2Floomy.xunfei.cn%2Foauth%2Fwechat%2Fcallback
  &response_type=code&scope=snsapi_login&state=<32hex>#wechat_redirect

=== 本网关 wechat.build_auth_url() ===
（同上，逐字节一致：True）

=== 实抓包 ===
请求行 : GET /connect/qrconnect?appid=wx18d60be432287cf8&redirect_uri=… 
请求头 : Host=open.weixin.qq.com  UA=Chrome/126
响应行 : HTTP/1.1 200 OK
响应头 : Content-Type: text/html; charset=utf-8
响应头 : Content-Length: 43804
页面指纹: len=42892  title=微信登录
含二维码: True    回显 state: True
```

复现：`_loomy_client/capture_and_compare.py`。

## 7. 为什么回调拿不到（对照实验）

| redirect_uri | 结果 |
|---|---|
| `https://loomy.xunfei.cn/oauth/wechat/callback` | ✅ 出二维码（len 42892） |
| `https://loomy.xunfei.cn/<任意路径>` | ✅ 也出 → **微信只校验域名，不校验路径** |
| `http://127.0.0.1:17890/panel/oauth/wechat/callback` | ❌ 微信报错页（len 881） |
| `https://loomy.xunfei.cn@127.0.0.1:17890/...` | ❌ 被拒 |
| `https://loomy.xunfei.cn:443@127.0.0.1:17890/...` | ❌ 被拒 |
| `https://loomy.xunfei.cn.evil.com/...` | ❌ 被拒 |
| 线上 `GET /oauth/wechat/callback` | ❌ **404**（客户端靠内嵌窗口拦跳转，不需要这页） |

**所以：** 回调只能落在 `loomy.xunfei.cn`，而该路径线上是 404，`code` 只出现在浏览器
地址栏里。官方客户端能全自动，是因为它自己就是浏览器；网关进程没有浏览器，
拿不到这个跳转 —— 除非你在 `loomy.xunfei.cn` 域下有自己能读的页面。

## 8. 本网关怎么落地的

完全照抄第 5 节的编排，只把「内嵌窗口拦截」换成「用户回传一次 code」：

```
面板「添加账号」→ 微信扫码 → 打开 open.weixin.qq.com/connect/qrconnect?…
  → 用户微信扫码授权
  → 浏览器跳到 loomy.xunfei.cn/oauth/wechat/callback?code=…&state=…
  → 用户在 404 页按 Ctrl+L / Ctrl+C（面板轮询剪贴板，自动识别并提交）
  → POST /api/panel/login/wechat/complete
       → bind/auth → bind=1 ? bind/skip : 绑手机(sendMsg+checkCode)
       → session 落盘 + 热加载进池
```

* `wechat.build_auth_url()` 与客户端 `buildAuthUrl()` **逐字节一致**（有测试断言）。
* `wechat.parse_callback()` 容错三种粘贴形态（整条 URL / 裸 query / 只有 code），
  并校验 `state` 与本次会话一致（防串改）。
* 剪贴板自动识别失败时（权限被拒/不支持）自动退回手动粘贴，不阻塞流程。
