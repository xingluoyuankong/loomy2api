"""请求级用量台账（面板「用量」页的数据源）。

设计取舍
--------
* **内存环形缓冲 + JSONL 落盘**：用量是观测数据，网关重启频繁，纯内存
  台账会让「用量」页时不时清零；逐条追加写 JSON 行文件，开销可忽略。
* summary 只返回**五档时间窗口**（24小时内 / 当日 / 3天 / 7天 / 30天）
  的聚合，**不返回历史总量**——总量随环形缓冲截断，数字本身就是错的，
  还会误导；用量只看窗口内。
* 记账是**尽力而为**：任何字段缺失都不抛错（上游 usage 结构各模型不一致）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

__all__ = ["UsageLedger", "extract_numbers", "WINDOWS", "WINDOW_KEYS"]

#: 面板展示用的保留条数（30 天窗口要有数据，环形缓冲必须盖住 30 天量级；
#: 当前约 500 条/天，20000 条 ≈ 40 天，内存占用约 8MB，可接受）
DEFAULT_MAX_ENTRIES = 20000

#: 时间窗口定义：key -> (展示名, 秒数)；"today" 为自然日（服务器本地时区），
#: 秒数记 None，单独按当天零点计算 cutoff。
WINDOWS: List[Tuple[str, str, Optional[int]]] = [
    ("24h", "24小时内", 24 * 3600),
    ("today", "当日", None),
    ("3d", "3天内", 3 * 86400),
    ("7d", "7天内", 7 * 86400),
    ("30d", "30天内", 30 * 86400),
]
WINDOW_KEYS = [key for key, _label, _seconds in WINDOWS]


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

    def summary(self) -> Dict[str, Any]:
        """五档时间窗口统计：24小时内 / 当日 / 3天 / 7天 / 30天。

        每档含 totals + by_model + by_account；另附最近 24h 按小时分桶
        （趋势用）。**不返回历史总量**——总量随环形缓冲截断，数字是错的。
        """
        with self._lock:
            items = list(self._entries)
        now = time.time()
        cutoffs: Dict[str, float] = {}
        for key, _label, seconds in WINDOWS:
            if seconds is None:  # 自然日：服务器本地时区当天零点
                lt = time.localtime(now)
                cutoffs[key] = time.mktime(
                    (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                     lt.tm_wday, lt.tm_yday, lt.tm_isdst))
            else:
                cutoffs[key] = now - seconds

        def _blank_totals() -> Dict[str, Any]:
            return {"requests": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "reasoning_tokens": 0,
                    "total_tokens": 0, "points": 0,
                    "latency_sum": 0.0, "ttfb_sum": 0.0, "ttfb_n": 0}

        def _blank_row() -> Dict[str, Any]:
            return {"requests": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "points": 0, "latency_sum": 0.0}

        wins: Dict[str, Dict[str, Any]] = {}
        for key, label, _seconds in WINDOWS:
            wins[key] = {"key": key, "label": label,
                         "totals": _blank_totals(),
                         "by_model": {}, "by_account": {}}

        buckets: Dict[int, Dict[str, Any]] = {}
        cutoff_24h = cutoffs["24h"]

        for e in items:
            ts = e.get("ts", 0)
            status = e.get("status", 0)
            ok = 200 <= status < 300
            pt = e.get("prompt_tokens", 0) or 0
            ct = e.get("completion_tokens", 0) or 0
            rt = e.get("reasoning_tokens", 0) or 0
            points = e.get("points", 0) or 0
            latency = e.get("latency", 0) or 0
            ttfb = e.get("ttfb")
            for key, _label, _seconds in WINDOWS:
                if ts < cutoffs[key]:
                    continue
                w = wins[key]
                t = w["totals"]
                t["requests"] += 1
                t["ok" if ok else "failed"] += 1
                t["prompt_tokens"] += pt
                t["completion_tokens"] += ct
                t["reasoning_tokens"] += rt
                t["total_tokens"] += pt + ct
                t["points"] += points
                t["latency_sum"] += latency
                if ttfb is not None:
                    t["ttfb_sum"] += ttfb
                    t["ttfb_n"] += 1
                for bucket, name in ((w["by_model"], e.get("model") or "(未知)"),
                                     (w["by_account"], e.get("account") or "(未分配)")):
                    row = bucket.setdefault(name, _blank_row())
                    row["requests"] += 1
                    row["ok" if ok else "failed"] += 1
                    row["prompt_tokens"] += pt
                    row["completion_tokens"] += ct
                    row["points"] += points
                    row["latency_sum"] += latency

            if ts >= cutoff_24h:
                hour = int(ts // 3600 * 3600)
                slot = buckets.setdefault(hour, {"requests": 0, "points": 0,
                                                 "completion_tokens": 0})
                slot["requests"] += 1
                slot["points"] += points
                slot["completion_tokens"] += ct

        def _finish_totals(t: Dict[str, Any]) -> Dict[str, Any]:
            n = max(1, t["requests"])
            return {
                "requests": t["requests"],
                "ok": t["ok"],
                "failed": t["failed"],
                "prompt_tokens": t["prompt_tokens"],
                "completion_tokens": t["completion_tokens"],
                "reasoning_tokens": t["reasoning_tokens"],
                "total_tokens": t["total_tokens"],
                "points": t["points"],
                "avg_latency": round(t["latency_sum"] / n, 2),
                "avg_ttfb": round(t["ttfb_sum"] / t["ttfb_n"], 2) if t["ttfb_n"] else 0,
            }

        def _finish_rows(rows: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
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

        windows = {}
        for key, label, _seconds in WINDOWS:
            w = wins[key]
            windows[key] = {
                "key": key,
                "label": label,
                "totals": _finish_totals(w["totals"]),
                "by_model": _finish_rows(w["by_model"]),
                "by_account": _finish_rows(w["by_account"]),
            }
        return {
            "windows": windows,
            "order": [key for key, _label, _seconds in WINDOWS],
            "hours": [dict(buckets[h], hour=h) for h in sorted(buckets)],
            "kept": len(items),
        }

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        return count
