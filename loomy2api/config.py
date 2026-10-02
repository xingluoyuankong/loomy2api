"""Configuration loading.

Precedence: environment variables > config.json > built-in defaults.
Paths in the config are resolved relative to the project root (the directory
holding config.json), so the project can live anywhere.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import constants as C

__all__ = ["Config", "load_config", "PROJECT_ROOT", "ensure_config"]

#: Repository root = parent of this package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 17890,

    # upstreams -----------------------------------------------------------
    "upstream": C.DEFAULT_MODEL_BASE,
    "account_base": C.DEFAULT_ACCOUNT_BASE,
    "account_appid": C.DEFAULT_ACCOUNT_APPID,
    "access_key_id": "",          # empty → use the client-shipped constant
    "access_key_secret": "",
    "loomy_version": C.DEFAULT_LOOMY_VERSION,
    "proxy": "",                  # "" = direct; e.g. http://127.0.0.1:7877

    # gateway -------------------------------------------------------------
    "api_keys": [],               # [] = no auth; otherwise Bearer / x-api-key
    "default_model": C.DEFAULT_MODEL,
    "timeout": 1200,
    "request_purpose": "chat.message",
    "log_dir": "logs",
    "log_requests": True,
    "strip_model_prefix": True,   # accept "imodel/deepseek-v4-flash-0731"

    # account pool --------------------------------------------------------
    "accounts_file": "accounts.json",
    "strategy": "weighted",       # weighted(三因子加权随机) | balance | round_robin | lru
    "max_retries": 2,             # extra accounts tried on auth/quota errors
    "cooldown_seconds": 300,      # 兜底冷却（未分类错误）
    "session_renew_before_days": 3,
    "quota_refresh_minutes": 30,
    "sessions_from_client": True, # also honour the desktop client's session
    "client_root": "",            # override the client's data dir (tests)
    # identity_mode: per_account → each account gets its own devid / campus id;
    #                client       → mirror the shipped client exactly.
    "identity_mode": "per_account",

    # 选号 / 冷却 / 粘性（借鉴 workbuddy2api-panel）--------------------------
    "pick_top_n": 5,              # 加权选号的 Top-N 短名单大小
    "soft_rate_base_seconds": 600,   # 429 软冷却基数（无重置墙钟时指数退避）
    "soft_rate_max_seconds": 7200,   # 软冷却封顶（2h）
    "not_found_cooldown_seconds": 60,  # 404 短冷却
    "breaker_threshold": 5,          # 连续 N 次上游 5xx → 熔断
    "breaker_cooldown_seconds": 60,  # 熔断基数（逐次加倍）
    "breaker_cooldown_max_seconds": 1800,
    "sticky_ttl_seconds": 1800,      # 会话粘性 TTL（0 = 关闭）
    "sticky_max_entries": 2000,      # 粘性表容量上限
    "max_inflight_per_account": 0,   # 单账号在途上限（0 = 不限）
    "login_flow_ttl_seconds": 600,   # 跳转登录链接有效期（秒）

    # 网关自身 -------------------------------------------------------------
    "security_headers": True,     # 面板响应加 CSP 等安全头
    "auto_generate_config": True, # 首启无 config.json 时自动生成（含随机 api_key）
}

ENV_MAP = {
    "LOOMY_HOST": "host",
    "LOOMY_PORT": "port",
    "LOOMY_UPSTREAM": "upstream",
    "LOOMY_ACCOUNT_BASE": "account_base",
    "LOOMY_AK_ID": "access_key_id",
    "LOOMY_AK_SECRET": "access_key_secret",
    "LOOMY_API_KEYS": "api_keys",
    "LOOMY_DEFAULT_MODEL": "default_model",
    "LOOMY_PROXY": "proxy",
    "LOOMY_ACCOUNTS_FILE": "accounts_file",
    "LOOMY_LOG_DIR": "log_dir",
    "LOOMY_STRATEGY": "strategy",
    "LOOMY_PICK_TOP_N": "pick_top_n",
    "LOOMY_STICKY_TTL": "sticky_ttl_seconds",
    "LOOMY_MAX_INFLIGHT": "max_inflight_per_account",
    "LOOMY_SECURITY_HEADERS": "security_headers",
}


class Config(dict):
    """A dict with attribute access plus a few derived helpers."""

    def __getattr__(self, item: str) -> Any:          # pragma: no cover
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    # -- derived -------------------------------------------------------

    @property
    def ak_id(self) -> str:
        return str(self.get("access_key_id") or C.DEFAULT_ACCESS_KEY_ID)

    @property
    def ak_secret(self) -> str:
        return str(self.get("access_key_secret") or C.DEFAULT_ACCESS_KEY_SECRET)

    @property
    def api_keys(self) -> List[str]:
        keys = self.get("api_keys") or []
        return [str(k) for k in keys if k]

    def path(self, key: str, default: str = "") -> Path:
        raw = str(self.get(key) or default)
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p

    # -- 热更新（借鉴 workbuddy2api-panel 的在线配置）----------------------

    #: 改这些键需要重启进程才生效（装配期字段）
    RESTART_KEYS = ("host", "port", "upstream", "account_base", "log_dir",
                    "accounts_file")

    def _file(self) -> Path:
        return Path(self.get("_config_path") or (PROJECT_ROOT / "config.json"))

    def reload(self) -> "Config":
        """重新读 config.json + 环境变量覆盖，就地更新（不重启进程）。"""
        fresh = load_config(self._file())
        keep_path = self.get("_config_path")
        self.clear()
        self.update(fresh)
        if keep_path:
            self["_config_path"] = keep_path
        return self

    def patch(self, updates: Dict[str, Any]) -> List[str]:
        """深合并写回 config.json（保留未知键），返回需要重启才生效的键名。

        写盘用「临时文件 + os.replace」原子替换，避免面板保存到一半被读到半截配置。
        """
        if not isinstance(updates, dict):
            raise ValueError("配置补丁必须是对象 / patch must be an object")
        path = self._file()
        current: Dict[str, Any] = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception:                           # noqa: BLE001
                current = {}
        if not isinstance(current, dict):
            current = {}
        _deep_merge(current, {k: v for k, v in updates.items()
                              if not str(k).startswith("_")})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
        self.reload()
        return [k for k in updates if k in self.RESTART_KEYS]

    def public_view(self) -> Dict[str, Any]:
        """给面板看的配置（api_keys 脱敏，绝不回显明文）。"""
        out = {k: v for k, v in self.items() if not str(k).startswith("_")}
        keys = self.api_keys
        out["api_keys"] = [f"{k[:6]}…（已设置）" if k else "" for k in keys]
        out["api_keys_set"] = bool(keys)
        return out


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """就地深合并：dict 递归，其余类型直接覆盖。"""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _random_api_key() -> str:
    """生成一个随机的网关 API Key（crypto/rand，绝不落日志明文）。"""
    return "sk-loomy-" + secrets.token_urlsafe(24)


def ensure_config(path: str | os.PathLike | None = None,
                  *, log=print) -> Tuple[Path, Optional[str]]:
    """首启自举：config.json 不存在时生成一份（含随机 api_key）。

    返回 ``(路径, 新生成的 key 或 None)``。调用方负责把新 key **只打印一次**，
    不写日志文件、不进仓库（config.json 已在 .gitignore 里）。
    """
    target = Path(path) if path else PROJECT_ROOT / "config.json"
    if target.exists():
        return target, None
    key = _random_api_key()
    payload = {
        "_comment": "由 loomy2api 首启自动生成。改完保存即热生效（host/port 需重启）。",
        "host": "127.0.0.1",
        "port": 17890,
        "upstream": C.DEFAULT_MODEL_BASE,
        "account_base": C.DEFAULT_ACCOUNT_BASE,
        "account_appid": C.DEFAULT_ACCOUNT_APPID,
        "api_keys": [key],
        "default_model": C.DEFAULT_MODEL,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    try:
        os.chmod(target, 0o600)                         # 里面有密钥
    except Exception:                                   # noqa: BLE001
        pass
    return target, key


def _coerce(cfg: Dict[str, Any], key: str, value: str) -> Any:
    base = DEFAULTS.get(key)
    if key in ("host", "upstream", "proxy") or isinstance(base, str):
        return value
    if isinstance(base, bool):
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(base, int):
        try:
            return int(value)
        except ValueError:
            return base
    if isinstance(base, list) and key == "api_keys":
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config(DEFAULTS)

    config_path = Path(path) if path else PROJECT_ROOT / "config.json"
    cfg["_config_path"] = str(config_path)          # 供 reload()/patch() 定位
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            raise RuntimeError(f"config.json 解析失败 / parse error: {exc}") from exc
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})

    for env_key, cfg_key in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value:
            cfg[cfg_key] = _coerce(cfg, cfg_key, value)

    return cfg
