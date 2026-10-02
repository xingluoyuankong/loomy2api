"""选号、会话粘性、在途租约。

三块能力都借鉴自 workbuddy2api-panel（Go 版），按 Loomy 的语义重写：

1. **加权选号**（``weighted_pick``）——原版只有 balance/round_robin/lru，热点账号会被
   一直打（惊群）。这里改成「healthy 候选按三因子权重取 Top-N 短名单 → 短名单内加权
   随机抽签」，并把权重相等的账号先洗牌，避免字典序靠后的账号永远进不了短名单。

   三因子（对齐原版 `weightOf`）：
     · 积分占比  ×10   —— 余额多的多干活
     · 闲置补偿  ×1    —— 越久没用越该轮到（打散热点）
     · 成功率    ×3    —— 老是失败的少用

2. **会话粘性**（``StickyRouter``）——多轮对话必须落在同一个账号上，否则上下文会
   在两个账号的会话里各说各话。绑定 ``conversation_id → 账号名``，TTL 滚动续期；
   请求失败自动解绑，下次重新分配。客户端不发 ``conversation_id`` 时（通用 OpenAI
   客户端都不发），用 ``system + 首条 user`` 的哈希派生一个稳定键，照样有粘性。

3. **在途租约**（``InflightTracker``）——同一账号并发占满就跳过，避免把单号打爆
   （原版 `inFlightFull`）。``limit<=0`` 表示不限。
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "weight_of", "weighted_pick", "session_key",
    "StickyRouter", "InflightTracker",
]

#: 三因子默认权重（对齐 workbuddy2api-panel 的 ×10 / ×1 / ×3）。
W_CREDITS = 10.0
W_IDLE = 1.0
W_SUCCESS = 3.0

#: 闲置补偿的饱和时长（秒）。超过它闲置收益不再增长，否则久置账号会长期霸榜。
IDLE_SATURATE_SECONDS = 300.0


def weight_of(acc, max_available: Optional[int], now: Optional[float] = None,
              *, w_credits: float = W_CREDITS, w_idle: float = W_IDLE,
              w_success: float = W_SUCCESS) -> float:
    """算一个账号的选号权重（恒 > 0，避免除零/全零抽签）。

    ``max_available`` 是**全集口径**的最大可用积分（不是子集），这样截断排序和
    抽签权重共享同一基准，两个阶段的权重可比。
    """
    now = time.time() if now is None else now

    credits = acc.available if acc.available is not None else max_available
    if max_available and max_available > 0 and isinstance(credits, (int, float)):
        credit_ratio = max(0.0, min(1.0, float(credits) / float(max_available)))
    else:
        credit_ratio = 1.0

    idle = max(0.0, now - float(acc.last_used or 0))
    idle_ratio = min(1.0, idle / IDLE_SATURATE_SECONDS)

    total = int(acc.requests or 0) + int(acc.failures or 0)
    success_rate = (float(acc.requests) / total) if total > 0 else 0.5

    w = credit_ratio * w_credits + idle_ratio * w_idle + success_rate * w_success
    return max(w, 1e-4)


def weighted_pick(candidates: Sequence[Any],
                  weight_fn: Callable[[Any], float],
                  *, top_n: int = 5, rng: Optional[random.Random] = None):
    """Top-N 短名单 + 加权随机抽签。

    为什么要先截断再抽签：直接对全体加权随机，低权重账号仍有概率被抽中；
    只按权重取最大值又会让一个账号被反复打。Top-N + 短名单内加权随机，
    兼顾「优先用余额多的」和「别老是同一个号」。

    等权重洗牌：当短名单截断边界上存在并列权重时，先 Fisher-Yates 洗牌，
    否则按字典序截断会让排序靠后的等权重账号永远进不了短名单（原版踩过这个坑）。
    """
    if not candidates:
        return None
    rng = rng or random
    scored: List[Tuple[float, int, Any]] = [
        (weight_fn(c), i, c) for i, c in enumerate(candidates)
    ]
    top_n = max(1, int(top_n))
    if len(scored) > top_n:
        weights = [s[0] for s in scored]
        if len(set(weights)) < len(weights):      # 存在并列 → 洗牌打破字典序
            rng.shuffle(scored)
    scored.sort(key=lambda item: -item[0])
    short = scored[:top_n]

    total = sum(s[0] for s in short)
    if total <= 0:
        return short[0][2]
    roll = rng.random() * total
    acc = 0.0
    for w, _idx, cand in short:
        acc += w
        if roll <= acc:
            return cand
    return short[-1][2]


def session_key(payload: Dict[str, Any]) -> Optional[str]:
    """从请求体派生会话键（用于粘性绑定）。

    优先级：
      1. 显式会话标识（``conversation_id`` / ``conversationId`` / ``chat_id`` / ``ChatId``）
      2. 派生键 ``d-<sha1(system + 首条 user)[:16]>`` —— 通用 OpenAI 客户端不发会话 id，
         用它也能拿到粘性（原版「粘性会话内容回退」）。

    返回 None 表示无法派生（比如空 messages），此时不做粘性，走普通选号。
    """
    if not isinstance(payload, dict):
        return None
    for field in ("conversation_id", "conversationId", "chat_id", "chatId"):
        value = payload.get(field)
        if value:
            return f"c-{value}"

    messages = payload.get("messages") or []
    parts: List[str] = []
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = msg.get("content")
            if isinstance(content, list):          # 多模态 content 数组
                content = " ".join(
                    str(p.get("text") or "") for p in content
                    if isinstance(p, dict)
                )
            text = str(content or "")[:2000]
            if role == "system" and text:
                parts.append(text)
            elif role == "user" and text:
                parts.append(text)
                break
    if not parts:
        return None
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"d-{digest}"


class StickyRouter:
    """``会话键 → 账号名`` 的粘性绑定表（TTL 滚动 + 容量上限）。"""

    def __init__(self, ttl: float = 1800.0, max_entries: int = 2000):
        self.ttl = max(0.0, float(ttl))
        self.max_entries = max(0, int(max_entries))
        self._map: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0 and self.max_entries != 0

    def get(self, key: Optional[str]) -> Optional[str]:
        if not key or not self.enabled:
            return None
        now = time.time()
        with self._lock:
            item = self._map.get(key)
            if not item:
                return None
            name, expire = item
            if expire <= now:
                self._map.pop(key, None)
                return None
            # 命中即滚动续期：活跃会话不会中途掉绑定
            self._map[key] = (name, now + self.ttl)
            return name

    def bind(self, key: Optional[str], name: str) -> None:
        if not key or not self.enabled or not name:
            return
        now = time.time()
        with self._lock:
            if len(self._map) >= self.max_entries:
                self._prune_locked(now)
                if len(self._map) >= self.max_entries:
                    # 仍满：丢掉最早到期的一条，保证新会话能绑上
                    oldest = min(self._map.items(), key=lambda kv: kv[1][1], default=None)
                    if oldest:
                        self._map.pop(oldest[0], None)
            self._map[key] = (name, now + self.ttl)

    def unbind(self, key: Optional[str], name: Optional[str] = None) -> None:
        """解绑。传 name 时只在当前绑定确实是它时才解（避免误删新绑定）。"""
        if not key:
            return
        with self._lock:
            item = self._map.get(key)
            if not item:
                return
            if name and item[0] != name:
                return
            self._map.pop(key, None)

    def _prune_locked(self, now: float) -> None:
        dead = [k for k, (_n, exp) in self._map.items() if exp <= now]
        for k in dead:
            self._map.pop(k, None)

    def prune(self) -> int:
        now = time.time()
        with self._lock:
            before = len(self._map)
            self._prune_locked(now)
            return before - len(self._map)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ttl_seconds": self.ttl,
                "entries": len(self._map),
                "max_entries": self.max_entries,
            }


class InflightTracker:
    """单账号在途请求计数（租约）。

    一个账号同时被太多请求占用时，它的失败率会飙升（上游按账号限流），
    所以选号时跳过已占满的账号，宁可分给别的号。
    """

    def __init__(self, limit: int = 0):
        self.limit = max(0, int(limit))
        self._counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def full(self, name: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            return self._counts.get(name, 0) >= self.limit

    def acquire(self, name: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1

    def release(self, name: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            current = self._counts.get(name, 0)
            if current <= 1:
                self._counts.pop(name, None)
            else:
                self._counts[name] = current - 1

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)
