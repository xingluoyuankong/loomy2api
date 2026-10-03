"""Client for the Loomy model gateway (OpenAI-compatible upstream)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .httpc import json_body, open_stream, request as http_request

__all__ = ["ModelGateway", "UpstreamError", "build_model_headers"]


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def build_model_headers(session: str, *, version: str = "",
                        extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Headers the model gateway requires.

    * ``Authorization`` **and** ``token`` — the client's provider uses
      ``useSessionAuth``, and the gateway accepts either, so both are sent.
    * ``traceparent`` — without it the upstream hangs until timeout (measured
      by the client authors, documented in their source).
    * ``loomy-version`` — presence-checked only.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {session}",
        "token": session,
        "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01",
    }
    if version:
        headers["loomy-version"] = str(version)
    for key, value in (extra or {}).items():
        if value:
            headers[key] = str(value)
    return headers


class ModelGateway:
    """Thin wrapper around the upstream HTTP API."""

    def __init__(self, cfg):
        self.cfg = cfg

    # -- helpers --------------------------------------------------------

    @property
    def base(self) -> str:
        return str(self.cfg["upstream"]).rstrip("/")

    def _url(self, suffix: str) -> str:
        return f"{self.base}/{suffix.lstrip('/')}"

    def _headers(self, session: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        return build_model_headers(session, version=str(self.cfg.get("loomy_version") or ""),
                                   extra=extra)

    def _proxy(self, proxy: Optional[str] = None) -> str:
        """账号级代理优先，其次全局 proxy，最后直连。"""
        if proxy is not None:
            return str(proxy)
        return str(self.cfg.get("proxy") or "")

    # -- read-only ------------------------------------------------------

    def models(self, session: str, proxy: Optional[str] = None) -> Dict[str, Any]:
        status, _h, data = http_request(
            self._url("models"), method="GET", headers=self._headers(session),
            timeout=float(self.cfg.get("timeout") or 60),
            proxy=self._proxy(proxy))
        if status != 200:
            raise UpstreamError(f"/models HTTP {status}: "
                                f"{data[:200].decode('utf-8', 'replace')}", status)
        return json.loads(data.decode("utf-8"))

    def points_records(self, session: str, page_size: int = 20,
                       page_no: int = 1,
                       proxy: Optional[str] = None) -> Dict[str, Any]:
        url = self._url(f"points/records?record_type=all&page_no={page_no}"
                        f"&page_size={page_size}")
        status, _h, data = http_request(
            url, method="GET", headers=self._headers(session),
            timeout=30, proxy=self._proxy(proxy))
        if status != 200:
            raise UpstreamError(f"/points/records HTTP {status}", status)
        return json.loads(data.decode("utf-8"))

    def points_totals(self, session: str, *, max_pages: int = 6,
                      proxy: Optional[str] = None) -> Dict[str, Any]:
        """翻页汇总流水：上游**权威**的累计消耗（本地计数重启会丢，以此自愈）。

        返回 ``{points_used, credits, debits, pages, total_records}``。
        每页 100 条，默认最多 6 页（600 条流水，足够覆盖一个账号的日常）。
        """
        used = credits = debits = 0
        pages = 0
        seen: set = set()
        # 分页期间上游可能新增记录导致 total 漂移、条目跨页重复——
        # 必须按 ledgerId 去重，并用「整页读完」作为终止条件（实测踩过）
        for page_no in range(1, max_pages + 1):
            payload = self.points_records(session, page_size=100,
                                          page_no=page_no, proxy=proxy)
            data = payload.get("data") or {}
            records = (data.get("list") or data.get("records")
                       or data.get("items")) or []
            pages += 1
            for item in records:
                if not isinstance(item, dict):
                    continue
                lid = item.get("ledgerId")
                if lid in seen:
                    continue
                seen.add(lid)
                pts = item.get("pointsActual") or 0
                if item.get("direction") == "debit":
                    debits += pts
                    used += pts
                elif item.get("direction") == "credit":
                    credits += pts
            if len(records) < 100:
                break
        return {"points_used": used, "credits": credits, "debits": debits,
                "pages": pages, "records": len(seen)}

    def quota(self, session: str, proxy: Optional[str] = None) -> Dict[str, Any]:
        """→ ``{balance, daily_balance, available}``."""
        payload = self.points_records(session, page_size=1, proxy=proxy)
        data = payload.get("data") or {}
        return {
            "balance": data.get("balance"),
            "daily_balance": data.get("dailyBalance"),
            "available": data.get("availableBalance"),
        }

    def points_breakdown(self, session: str, proxy: Optional[str] = None,
                         max_pages: int = 10) -> Dict[str, Any]:
        """积分构成：额度分桶 + 最近流水 + 各模型实测单价。

        上游 ``points/records`` 的真实字段（实测）::

            ledgerId / modelName / direction(debit|credit) / description /
            createdAt / pointsActual / consumeSource / balanceBefore /
            balanceAfter / dailyCycleDate

        ``modelName + pointsActual`` 正好能统计每个模型的**实际积分单价**，
        比 multiplier 档位更贴近用户账单。

        注意：上游把 ``page_size`` **强制钳到 20**（实测传 100/500 都只回 20），
        所以必须老老实实翻页，否则只能看到最新 20 条 —— 默认模型一直被
        面板轮询刷屏，会把其他模型的扣费记录全部挤出第一页，前端就全显示
        「暂无流水」。
        """
        out_records: List[Dict[str, Any]] = []
        by_model: Dict[str, List[int]] = {}
        seen: set = set()
        total: Any = None
        first_data: Dict[str, Any] = {}
        for page_no in range(1, max_pages + 1):
            payload = self.points_records(session, page_size=20,
                                          page_no=page_no, proxy=proxy)
            data = payload.get("data") or {}
            if page_no == 1:
                first_data = data
                total = data.get("total")
            records = (data.get("list") or data.get("records")
                       or data.get("items")) or []
            for item in records:
                if not isinstance(item, dict):
                    continue
                lid = item.get("ledgerId")
                key = lid if lid is not None else (
                    item.get("createdAt"), item.get("modelName"),
                    item.get("pointsActual"))
                if key in seen:                      # 翻页期间的重复条目
                    continue
                seen.add(key)
                out_records.append({
                    "direction": item.get("direction") or "",
                    "source": item.get("consumeSource") or "",
                    "model": item.get("modelName") or "",
                    "points": item.get("pointsActual") or 0,
                    "balance_after": item.get("balanceAfter"),
                    "time": item.get("createdAt") or "",
                    "desc": (item.get("description") or "")[:80],
                })
                if item.get("direction") == "debit" and item.get("modelName"):
                    by_model.setdefault(item["modelName"], []).append(
                        item.get("pointsActual") or 0)
            # 终止：整页读完 / 已抓完全部（total 为上游权威条数）
            if len(records) < 20:
                break
            if isinstance(total, int) and len(out_records) >= total:
                break
        pricing = {
            m: {"calls": len(v), "min": min(v), "max": max(v),
                "avg": round(sum(v) / len(v), 2)}
            for m, v in sorted(by_model.items())
        }
        data = first_data
        return {
            "balance": data.get("balance"),
            "daily_balance": data.get("dailyBalance"),
            "available": data.get("availableBalance"),
            "total": data.get("totalBalance"),
            "expiring": data.get("expiringBalance"),
            "daily_quota": data.get("dailyQuota"),
            "daily_consumed": data.get("dailyConsumed"),
            "records": out_records,
            "records_total": total,
            "model_pricing": pricing,
        }

    # -- 每日签到（每天首次登录发放奖励；重复调用幂等返回 alreadyProcessed） --

    def points_first_login(self, session: str, proxy: Optional[str] = None,
                           invite_code: str = "",
                           device_id: str = "") -> Dict[str, Any]:
        """触发 ``POST /points/first-login``。

        客户端语义：带 ``inviteCode`` 时**必须**同时带 ``deviceId``
        （校园风控设备号），两者成对出现。

        成功响应的 ``data``（实测）::

            alreadyProcessed   今天是否已处理（true = 已经领过）
            currentBalance / permanentBalance / dailyBalance / dailyQuota
            dailyConsumed / dailyCycleDate
            registerReward / inviteeReward / inviterReward / invitationCodes
        """
        body: Dict[str, Any] = {}
        if str(invite_code or "").strip():
            body["inviteCode"] = str(invite_code).strip()
            if str(device_id or "").strip():
                body["deviceId"] = str(device_id).strip()
        code, _headers, raw = self.call(session, "points/first-login", body,
                                        method="POST", proxy=proxy)
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise UpstreamError(f"/points/first-login 响应不是 JSON: {raw[:120]!r}",
                                code) from exc
        if code != 200:
            raise UpstreamError(f"/points/first-login HTTP {code}", code)
        return payload

    # -- 平台任务（来源：客户端 points-service.js） ----------------------

    def redeem_code(self, session: str, code: str,
                    proxy: Optional[str] = None) -> Dict[str, Any]:
        """兑换积分码：``POST /points/redemption-codes/redeem`` body={code}。"""
        code, _h, raw = self.call(session, "points/redemption-codes/redeem",
                                  {"code": str(code or "").strip()},
                                  method="POST", proxy=proxy)
        return self._json("/points/redemption-codes/redeem", code, raw)

    def bind_invite_code(self, session: str, invite_code: str,
                         device_id: str = "",
                         proxy: Optional[str] = None) -> Dict[str, Any]:
        """绑定邀请码：``POST /points/activation`` body={inviteCode, deviceId?}。

        客户端 ``bindInviteCode`` 在有 campus device id 时会一并上报。"""
        body: Dict[str, Any] = {"inviteCode": str(invite_code or "").strip()}
        if str(device_id or "").strip():
            body["deviceId"] = str(device_id).strip()
        code, _h, raw = self.call(session, "points/activation", body,
                                  method="POST", proxy=proxy)
        return self._json("/points/activation", code, raw)

    def query_activation(self, session: str,
                         proxy: Optional[str] = None) -> Dict[str, Any]:
        """激活状态：``GET /points/activation``。"""
        code, _h, raw = self.call(session, "points/activation", None,
                                  method="GET", proxy=proxy)
        return self._json("/points/activation", code, raw)

    def _json(self, what: str, code: int, raw: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise UpstreamError(f"{what} 响应不是 JSON: {raw[:120]!r}", code) from exc
        if code != 200:
            raise UpstreamError(f"{what} HTTP {code}", code)
        return payload

    # -- request passthrough -------------------------------------------

    def call(self, session: str, suffix: str, payload: Dict[str, Any],
             extra_headers: Optional[Dict[str, str]] = None,
             method: str = "POST", proxy: Optional[str] = None) -> Tuple[int, Dict[str, str], bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        return http_request(
            self._url(suffix), method=method, headers=self._headers(session, extra_headers),
            body=body, timeout=float(self.cfg.get("timeout") or 1200),
            proxy=self._proxy(proxy))

    def stream(self, session: str, suffix: str, payload: Dict[str, Any],
               extra_headers: Optional[Dict[str, str]] = None,
               proxy: Optional[str] = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return open_stream(
            self._url(suffix), method="POST",
            headers=self._headers(session, extra_headers), body=body,
            timeout=float(self.cfg.get("timeout") or 1200),
            proxy=self._proxy(proxy))


def extract_usage(obj: Any) -> Dict[str, Any]:
    """Usage/points accounting that tolerates missing fields."""
    if not isinstance(obj, dict):
        return {}
    usage = obj.get("usage")
    return usage if isinstance(usage, dict) else {}


def sse_usage(line: bytes, sink: Dict[str, Any]) -> None:
    """Fold ``usage`` out of one SSE line into ``sink`` (last write wins)."""
    if not line.startswith(b"data:"):
        return
    chunk = line[5:].strip()
    if not chunk or chunk == b"[DONE]":
        return
    try:
        obj = json.loads(chunk)
    except Exception:                                   # noqa: BLE001
        return
    usage = extract_usage(obj)
    if usage:
        sink.update(usage)


def human_usage(usage: Dict[str, Any]) -> str:
    parts = []
    if usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
        parts.append(f"tok={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning:
        parts.append(f"think={reasoning}")
    if usage.get("points_consumed") is not None:
        parts.append(f"points={usage['points_consumed']}")
    return " ".join(parts)


def now() -> int:
    return int(time.time())
