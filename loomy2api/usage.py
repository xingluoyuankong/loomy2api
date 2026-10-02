"""请求级用量台账（面板「用量」页的数据源）。

设计取舍
--------
* **内存环形缓冲**，不落盘：用量是观测数据，重启丢历史可以接受；落盘反而会把
  一个高频写入点变成磁盘压力（原版走 Redis 镜像，本机单进程没必要）。
* 同时维护**聚合**（总量 / 按模型 / 按账号 / 按小时），面板不用每次重算全表。
* 记账是**尽力而为**：任何字段缺失都不抛错（上游 usage 结构各模型不一致）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

__all__ = ["UsageLedger", "extract_numbers"]

#: 面板展示用的保留条数
DEFAULT_MAX_ENTRIES = 500


def extract_numbers(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """从上游 usage 里抽出可加总的数字（缺字段一律按 0）。"""
    usage = usage or {}

    def num(*keys) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    reasoning = 0
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), (int, float)):
        reasoning = int(details["reasoning_tokens"])
    return {
        "prompt_tokens": num("prompt_tokens", "input_tokens"),
        "completion_tokens": num("completion_tokens", "output_tokens"),
        "reasoning_tokens": reasoning,
        "points": num("points_consumed", "cost_points", "points"),
    }


class UsageLedger:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 persist_path: Optional[Path] = None):
        self.max_entries = max(10, int(max_entries))
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=self.max_entries)
        self._lock = threading.Lock()
        self._started_at = time.time()
        #: 落盘：网关重启频繁，内存台账会让「用量」页时不时清零，
        #: 用户看到的统计自然是错的。JSON 行文件，逐条追加。
        self._persist_path = persist_path
        self._load()

    # ------------------------------------------------------------ record
    def record(self, *, account: str = "", model: str = "", status: int = 200,
               usage: Optional[Dict[str, Any]] = None, latency: float = 0.0,
               ttfb: Optional[float] = None, stream: bool = False,
               kind: str = "chat", error: str = "", proxy: str = "") -> Dict[str, Any]:
        """记一条请求。返回写入的条目（便于调用方复用）。"""
        nums = extract_numbers(usage)
        entry = {
            "ts": int(time.time()),
            "account": account,
            "model": model,
            "kind": kind,
            "status": int(status or 0),
            "stream": bool(stream),
            "latency": round(float(latency or 0), 2),
            "ttfb": (round(float(ttfb), 2) if ttfb is not None else None),
            "proxy": proxy or "",
            "error": (error or "")[:200],
            **nums,
        }
        with self._lock:
            self._entries.append(entry)
        self._append_persist(entry)
        return entry

    # ------------------------------------------------------------ persist
    def _load(self) -> None:
        import json as _json
        if not self._persist_path:
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = _json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        self._entries.append(item)
        except OSError:
            pass

    def _append_persist(self, entry: Dict[str, Any]) -> None:
        import json as _json
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -------------------------------------------------------------- read
    def recent(self, limit: int = 100, *, account: str = "", model: str = "") -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 100), self.max_entries))
        with self._lock:
            items = list(self._entries)
        if account:
            items = [e for e in items if e["account"] == account]
        if model:
            items = [e for e in items if e["model"] == model]
        return list(reversed(items[-limit:]))

    def summary(self, *, window_hours: int = 24) -> Dict[str, Any]:
        """总量 + 按模型 + 按账号 + 最近 N 小时分桶。"""
        with self._lock:
            items = list(self._entries)
        now = time.time()
        cutoff = now - max(1, int(window_hours)) * 3600

        totals = {"requests": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                  "completion_tokens": 0, "reasoning_tokens": 0, "points": 0,
                  "latency_sum": 0.0, "ttfb_sum": 0.0, "ttfb_n": 0}
        by_model: Dict[str, Dict[str, Any]] = {}
        by_account: Dict[str, Dict[str, Any]] = {}
        buckets: Dict[int, Dict[str, Any]] = {}

        for e in items:
            ok = 200 <= e["status"] < 300
            totals["requests"] += 1
            totals["ok" if ok else "failed"] += 1
            for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "points"):
                totals[key] += e.get(key, 0)
            totals["latency_sum"] += e.get("latency", 0)
            if e.get("ttfb") is not None:
                totals["ttfb_sum"] += e["ttfb"]
                totals["ttfb_n"] += 1

            for bucket, key_name in ((by_model, e.get("model") or "(未知)"),
                                     (by_account, e.get("account") or "(未分配)")):
                item = bucket.setdefault(key_name, {
                    "requests": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "points": 0, "latency_sum": 0.0})
                item["requests"] += 1
                item["ok" if ok else "failed"] += 1
                for key in ("prompt_tokens", "completion_tokens", "points"):
                    item[key] += e.get(key, 0)
                item["latency_sum"] += e.get("latency", 0)

            if e["ts"] >= cutoff:
                hour = int(e["ts"] // 3600 * 3600)
                slot = buckets.setdefault(hour, {"requests": 0, "points": 0,
                                                 "completion_tokens": 0})
                slot["requests"] += 1
                slot["points"] += e.get("points", 0)
                slot["completion_tokens"] += e.get("completion_tokens", 0)

        def _finish(rows: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
            out = []
            for name, item in rows.items():
                requests = max(1, item["requests"])
                out.append({
                    "name": name,
                    "requests": item["requests"],
                    "ok": item["ok"],
                    "failed": item["failed"],
                    "prompt_tokens": item["prompt_tokens"],
                    "completion_tokens": item["completion_tokens"],
                    "points": item["points"],
                    "avg_latency": round(item["latency_sum"] / requests, 2),
                })
            out.sort(key=lambda r: (-r["points"], -r["requests"]))
            return out

        avg_latency = (totals["latency_sum"] / totals["requests"]) if totals["requests"] else 0
        avg_ttfb = (totals["ttfb_sum"] / totals["ttfb_n"]) if totals["ttfb_n"] else 0
        return {
            "totals": {
                "requests": totals["requests"],
                "ok": totals["ok"],
                "failed": totals["failed"],
                "prompt_tokens": totals["prompt_tokens"],
                "completion_tokens": totals["completion_tokens"],
                "reasoning_tokens": totals["reasoning_tokens"],
                "points": totals["points"],
                "avg_latency": round(avg_latency, 2),
                "avg_ttfb": round(avg_ttfb, 2),
            },
            "by_model": _finish(by_model),
            "by_account": _finish(by_account),
            "hours": [dict(buckets[h], hour=h) for h in sorted(buckets)],
            "window_hours": window_hours,
            "kept": len(items),
            "since": int(self._started_at),
        }

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        return count
