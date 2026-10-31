"""站点身份配置的部署契约（评审阻断项 R5 的回归锁）。

**为什么需要这条测试**：`LKM_RP_ID` / `LKM_ORIGIN` / `LKM_GITHUB_REDIRECT_URI` /
`LKM_FRONTEND_CALLBACK` / `LKM_S3_PUBLIC_ENDPOINT_URL` 描述的是「站点自己是谁」。它们一旦
被写成 compose 里的字面量，`cp .env.example .env` 后改域名就**不生效**，而且失效方式是静默的：

- WebAuthn 的 RP ID 必须是页面 origin 的可注册域后缀 → 页面在 `https://域名` 而 RP ID 是 IP，
  浏览器**直接拒绝**注册/登录（不报 5xx，只在控制台/用户侧表现为「点了没反应」）；
- GitHub OAuth 回跳到旧地址 → 跳转后落到不可达的 host；
- 前端回调指向旧地址 → 登录成功后白屏；
- 预签名下载 URL 的 host 与页面 origin 不一致（或 scheme 是 http）→ 混合内容拦截 / 签名不匹配。

k8s 侧本来就有正确的 overlay（`deploy/k8s/overlays/prod/site-identity.yaml`）；compose 侧此前
是字面量，故本测试只锁 compose 模板：值**必须以 `${` 开头**，且同一语义键在 backend 与 auth
两块里的**默认值必须相等**（两进程各自持半份身份配置，不一致就是这类故障的根因）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"

#: 站点身份键（backend 侧全量；auth 侧只用 RP ID）。新增一个键就加进来——这是唯一清单。
_SITE_IDENTITY_KEYS = (
    "LKM_RP_ID",
    "LKM_ORIGIN",
    "LKM_GITHUB_REDIRECT_URI",
    "LKM_FRONTEND_CALLBACK",
    "LKM_S3_PUBLIC_ENDPOINT_URL",
)

_INTERPOLATION = re.compile(r"^\$\{([A-Z0-9_]+)(?::-([^}]*))?\}$")


def _compose() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _env(service: str) -> dict[str, Any]:
    return _compose()["services"][service].get("environment") or {}


def _default_of(expr: str) -> str:
    """从 ``${VAR:-default}`` / ``${VAR}`` / ``${VAR:?msg}`` 里取默认值（无默认则空串）。"""
    m = _INTERPOLATION.match(expr.strip())
    if not m:
        raise AssertionError(f"不是 compose 插值表达式：{expr!r}")
    return m.group(2) or ""


def test_backend_site_identity_is_interpolated() -> None:
    """backend 的 5 个站点身份键必须全部走 `${...}`，不允许再写回字面量。"""
    env = _env("backend")
    literal = {
        key: env.get(key)
        for key in _SITE_IDENTITY_KEYS
        if not isinstance(env.get(key), str) or not env[key].strip().startswith("${")
    }
    assert not literal, (
        "这些站点身份键又变回字面量了（换域名时 OAuth/Passkey/回跳/预签名会静默失效，"
        f"见评审报告 R5）：{literal}"
    )


def test_auth_rp_id_is_interpolated() -> None:
    """auth 侧同一语义键（RP ID）也必须插值——它与 backend 必须同值。"""
    value = _env("auth").get("LKM_RP_ID")
    assert isinstance(value, str) and value.strip().startswith("${"), (
        f"auth 的 LKM_RP_ID 必须是插值表达式，当前：{value!r}"
    )


def test_shared_site_identity_defaults_agree() -> None:
    """两块里同名键的默认值必须一致：各写各的默认值正是这类故障的根因。"""
    backend = _env("backend")
    auth = _env("auth")
    shared = [
        key
        for key in _SITE_IDENTITY_KEYS
        if isinstance(backend.get(key), str) and isinstance(auth.get(key), str)
    ]
    assert shared, "backend 与 auth 应当至少共享 LKM_RP_ID 这一项"
    mismatch = {
        key: (_default_of(backend[key]), _default_of(auth[key]))
        for key in shared
        if _default_of(backend[key]) != _default_of(auth[key])
    }
    assert not mismatch, (
        "同一站点身份键在 backend/auth 两块里的默认值不一致："
        f"{mismatch}（改一处忘另一处会让认证面半新半旧）"
    )


def test_origin_and_rp_id_share_host_default() -> None:
    """RP ID 必须是 origin 的主机名后缀，默认值也要满足这条（否则默认部署就登不上）。"""
    backend = _env("backend")
    rp_id = _default_of(backend["LKM_RP_ID"])
    origin = _default_of(backend["LKM_ORIGIN"])
    host = origin.split("://", 1)[-1].rstrip("/")
    assert rp_id and host, "默认值不能为空"
    assert host == rp_id or host.endswith("." + rp_id), (
        "默认 LKM_RP_ID 与 LKM_ORIGIN 的 host 不匹配，会让 WebAuthn 在默认部署下直接失败："
        f"RP_ID={rp_id!r} origin={origin!r}"
    )
