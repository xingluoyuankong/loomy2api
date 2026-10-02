r"""微信 OAuth（网站应用扫码登录）——Loomy 客户端唯一的「跳转授权」链路。

来源：客户端 `electron/xfyun/wechat-oauth.js` + `electron/xfyun/account-service.js`，
AppID 来自加密的 `resources/.env.prod`。

真实链路
--------
1. 打开授权页（纯函数 ``build_auth_url``）::

       https://open.weixin.qq.com/connect/qrconnect
         ?appid=wx18d60be432287cf8
         &redirect_uri=<urlencoded https://loomy.xunfei.cn/oauth/wechat/callback>
         &response_type=code&scope=snsapi_login&state=<32hex>#wechat_redirect

   官方客户端是把这一页塞进 Electron ``BrowserWindow``，在 ``will-redirect``
   里拦截拿 code、并 ``event.preventDefault()`` 不真的加载回调页。

2. 用户扫码授权后，微信 302 到 ``redirect_uri?code=…&state=…``。

3. 用 code 换 session（讯飞账号服务，HMAC 签名）::

       POST /login/thirdAccount/bind/auth   {tcode:{code}, type:"wx"} → {bind, rcode}
         bind=1 → POST /login/thirdAccount/bind/skip    {rcode} → session
         bind=0 → POST /login/thirdAccount/bind/sendMsg {rcode, phone} → msgid
                  POST /login/thirdAccount/bind/checkCode {rcode, mcode, msgid} → session

为什么必须有「把回调 URL 粘回来」这一步
--------------------------------------
微信开放平台只校验 redirect_uri 的**域名**（已登记 ``loomy.xunfei.cn``），
**不校验路径**——实测 ``https://loomy.xunfei.cn/<任意路径>`` 都能正常出二维码，
而 ``https://loomy.xunfei.cn@127.0.0.1:17890/...`` 这类 userinfo 绕过变体一律被拒。
所以回调只能落在 ``loomy.xunfei.cn``，而该路径线上是 404（客户端靠内嵌窗口拦截，
不需要这个页面），code 只出现在浏览器地址栏里 → 由用户粘回来。

这不是「面板内部跳转」：授权页是微信官方的真二维码页，回调是微信真实签发的 code。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, Optional

from . import constants as C

__all__ = ["build_auth_url", "parse_callback", "is_wechat_callback",
           "CALLBACK_HINT", "fetch_login_qr", "poll_login", "qr_image_url",
           "ERR_WAITING", "ERR_SCANNED", "ERR_CONFIRMED", "ERR_CANCELLED",
           "ERR_EXPIRED", "ERRCODE_TEXT"]

#: 二维码图片（面板直接 <img src> 内嵌，不用跳浏览器）
QR_IMAGE_BASE = "https://open.weixin.qq.com/connect/qrcode/"

#: 长轮询端点（页面里的 `fordevtool` 就是它）
LONGPOLL_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"

# 长轮询状态码（页面 JS 的 switch 分支）
ERR_WAITING = 408      # 未扫码
ERR_SCANNED = 404      # 已扫码，等用户在手机上点确认
ERR_CONFIRMED = 405    # 已确认，响应里带 wx_code
ERR_CANCELLED = 403    # 用户取消
ERR_EXPIRED = 402      # 二维码过期

ERRCODE_TEXT = {
    ERR_WAITING: "等待扫码",
    ERR_SCANNED: "已扫码，请在手机上确认",
    ERR_CONFIRMED: "已确认",
    ERR_CANCELLED: "已在手机上取消",
    ERR_EXPIRED: "二维码已过期",
}


def qr_image_url(uuid: str) -> str:
    return QR_IMAGE_BASE + str(uuid or "")


def _wechat_request(url: str, timeout: float = 30.0,
                    referer: str = "https://open.weixin.qq.com/") -> str:
    """直连微信（不走配置里的代理；微信在公网，本机直连即可）。"""
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Referer": referer,
    })
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_login_qr(*, app_id: Optional[str] = None,
                   redirect_uri: Optional[str] = None,
                   state: str = "probe", timeout: float = 30.0) -> Dict[str, Any]:
    """拉一次授权页，解出 ``uuid``（服务端取码用）。

    页面里有官方自己留下的线索::

        var fordevtool = "https://long.open.weixin.qq.com/connect/l/qrconnect?uuid=<uuid>"

    有了 uuid 就能：① 内嵌二维码图片；② 直接长轮询取 code —— 全程不需要浏览器。
    """
    auth_url = build_auth_url(state, redirect_uri=redirect_uri, app_id=app_id)
    page = _wechat_request(auth_url, timeout=timeout)
    m = re.search(r'fordevtool\s*=\s*"([^"]*uuid=([A-Za-z0-9_\-]+))"', page)
    if not m:
        m2 = re.search(r'uuid=([A-Za-z0-9_\-]{10,})', page)
        if not m2:
            raise ValueError(f"授权页里找不到 uuid（页面 {len(page)} 字节）")
        uuid = m2.group(1)
    else:
        uuid = m.group(2)
    return {"uuid": uuid, "qr_url": qr_image_url(uuid), "auth_url": auth_url,
            "page_bytes": len(page)}


def poll_login(uuid: str, last: str = "", timeout: float = 35.0) -> Dict[str, Any]:
    """长轮询一次扫码状态。

    返回 ``{errcode, code, text}``；``errcode`` 见上面常量，``code`` 只在
    ``errcode == 405`` 时非空。网络异常原样抛（调用方决定要不要重试）。
    """
    if not uuid:
        raise ValueError("缺少 uuid")
    import time as _time
    import urllib.parse as _urlparse
    params = {"uuid": uuid}
    if last:
        params["last"] = str(last)
    params["_"] = str(int(_time.time() * 1000))
    url = LONGPOLL_URL + "?" + _urlparse.urlencode(params)
    body = _wechat_request(url, timeout=timeout)
    err = re.search(r"wx_errcode\s*=\s*(\d+)", body)
    code = re.search(r"wx_code\s*=\s*'([^']*)'", body)
    errcode = int(err.group(1)) if err else 0
    return {
        "errcode": errcode,
        "code": (code.group(1) if code else "") or "",
        "text": ERRCODE_TEXT.get(errcode, f"未知状态 {errcode}"),
        "raw": body[:120],
    }

#: 粘回地址栏时给的提示（面板直接展示）
CALLBACK_HINT = (
    "扫码授权后浏览器会跳到 loomy.xunfei.cn 的 404 页 —— 这是正常的"
    "（官方客户端是在内嵌 Electron 窗口里拦这个跳转，网关进程没有浏览器，"
    "所以只能拿到地址栏里的 code）。"
    "在那一页按 <b>Ctrl+L</b> 再 <b>Ctrl+C</b>，面板会自动识别剪贴板里的链接并完成登录；"
    "也可以手动粘贴到下面。"
)

#: code 是微信一次性授权码：字母数字与 -_ ，最长 512（对齐客户端校验）
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
#: 我方 state 是 32 位 hex；微信原样回传
_STATE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


def build_auth_url(state: str, *, redirect_uri: Optional[str] = None,
                   app_id: Optional[str] = None,
                   scope: str = C.WECHAT_SCOPE) -> str:
    """拼装微信扫码授权页 URL（与客户端 ``buildAuthUrl`` 逐字段一致）。"""
    app_id = app_id or C.WECHAT_APP_ID
    redirect_uri = redirect_uri or C.WECHAT_REDIRECT_URI
    if not app_id:
        raise ValueError("缺少微信 AppID")
    if not redirect_uri:
        raise ValueError("缺少 redirect_uri")
    if not state:
        raise ValueError("缺少 state")
    return (
        f"{C.WECHAT_AUTH_BASE}"
        f"?appid={urllib.parse.quote(app_id, safe='')}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(scope, safe='')}"
        f"&state={urllib.parse.quote(state, safe='')}"
        f"#wechat_redirect"
    )


def is_wechat_callback(url: str) -> bool:
    """粗判一条 URL 像不像微信回调（用户可能只粘了 code，也可能粘了整条）。"""
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not u:
        return False
    return ("code=" in u and ("state=" in u or "oauth" in u or "callback" in u)) \
        or _CODE_RE.match(u) is not None


def parse_callback(url: str) -> Dict[str, Any]:
    """从用户粘回来的内容里解出 ``{code, state}``。

    接受三种形态（用户很难粘错）：
      · 整条回调 URL：``https://loomy.xunfei.cn/oauth/wechat/callback?code=…&state=…``
      · 只有 query：``code=…&state=…``
      · 只有 code 本身

    解析失败一律抛 ``ValueError``，不返回半截结果。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("请粘贴微信回调链接（或授权 code）")
    raw = url.strip().strip('"').strip("'")

    # 看起来像 URL / query（带 ? 或 =）就走解析：这样即使微信回的是
    # `?errmsg=access_denied`（没有 code）也能给出准确原因，而不是误报 code 格式错。
    looks_like_url = ("?" in raw) or ("=" in raw) or raw.lower().startswith("http")
    if looks_like_url:
        query = raw.split("?", 1)[1] if "?" in raw else raw
        query = query.split("#", 1)[0]
        params = urllib.parse.parse_qs(query, keep_blank_values=False)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        if not code:
            errmsg = ((params.get("errmsg") or params.get("error") or [""])[0]
                      or "微信未返回 code")
            raise ValueError(f"微信授权失败：{errmsg}")
    else:
        code = raw
        state = ""

    code = code.strip()
    if not code:
        raise ValueError("没解析出 code，请确认粘贴的是完整回调链接")
    if not looks_like_url and len(code) < 8:
        # 实测踩过：用户在 404 页面没按 Ctrl+C，粘进来一段别的东西（比如 4 个字符），
        # 报「code 格式不对」让人一头雾水。这里直接说清楚该复制什么。
        raise ValueError(
            f"粘进来的只有 {len(code)} 个字符，不是微信回调链接。"
            "请在跳转后的 404 页面按 Ctrl+L 然后 Ctrl+C（复制地址栏整条链接），"
            "切回面板后会自动识别；也可以点「📋 从剪贴板粘贴并完成」。")
    if len(code) > 512 or not _CODE_RE.match(code):
        raise ValueError(
            "这条内容里的 code 不合法（含非法字符或过长）。"
            "请确认复制的是跳转后<b>地址栏</b>里那条以 loomy.xunfei.cn/oauth/wechat/callback 开头的链接。")
    if state and (len(state) > 256 or not _STATE_RE.match(state)):
        raise ValueError("state 格式不对，请重新复制")
    return {"code": code, "state": state}
