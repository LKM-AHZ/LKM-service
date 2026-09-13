"""AUTH 读面 HTTP client（M3 B1.2）：把单用户快照 miss 回填跨进程送到 AUTH 读端点。

背景：A1/A6 已把身份读面收敛进 ``auth.snapshot`` 单态内存缝 + ``user:snap`` 缓存；当
AUTH 被部署成**独立进程**（B1.1 ``main_auth`` + compose ``auth``）后，在线读路径（本
monolith 进程缓存 miss）应跨 HTTP 打到 AUTH 进程自己的读端点，而非就地触业务 DB ——
这是 B1.2 在此 build 的「内部读缝 HTTP 化」client。

职责边界 / 不变量：
- **只读缝冻结字段**：本 client 从 wire 校验并切出 ``_SNAP_FIELDS``（frozen、零 PII/凭证），
  不透出 email/phone/hashed_password。url/token 全部来自 config。它**不 import
  ``auth.snapshot``**（快照在调用方侧重建），避免反向循环。
- **fail-open 契约**：任一不可用／超时／4xx/5xx／畸形体都抛 ``UserHttpUnavailable``，
  由调用方（``auth.snapshot.get_user_snapshot``）**回落本进程 DB**，绝不因 AUTH 抖动 crash
  / 以 stale 当 truth。
- **authoritative not-found**：AUTH 明确回 404、或信封 ``data`` 为 null（存在与否由权威裁决）
  → 返回 ``(None, None)`` 且**不抛**——这是权威答案而非故障；调用方不会 fallback DB、也不会缓存。
- wire 信封（内部、非 ApiResp）：``{"data": <冻结字段 dict|null>, "sv": <int|null>}``。
  ``sv`` 是 AUTH 端按 User.updated_at 派生的来源版本（同 ``user_cache.version_of_updated_at``，
  跨进程可比），供 consumer 以真实来源版本做缓存 CAS，不捏造版本。
- ``enabled()`` 与 config 同源；URL 配了但 token 空 → seam 不启用（端点 fail-closed、此处
  fail-open）。此模块仅 auth 内部用、被 auth.snapshot 引用，不新增业务→edge。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.secrets import reveal

# 冻结只读字段（与 auth.snapshot.UserSnapshot 完全一致）；缺任一字段即判畸形 → fail-open。
# raw nickname 已加入快照缝冻结字段（M3.A 残项），HTTP OFF/ON 两侧 `_SNAP_FIELDS` 须同源，
# 否则 HTTP 缝响应会因缺 nickname 被本 client 判畸形。nickname 非 PII/凭证，缝可承载。
_SNAP_FIELDS: tuple[str, ...] = (
    "user_id",
    "username",
    "display_name",
    "avatar",
    "role",
    "account_level",
    "banned",
    "nickname",
)

# 可注入的 AsyncClient 工厂：默认 None → httpx.AsyncClient(timeout)。测试经 monkeypatch 换成
# 返回 ``httpx.MockTransport`` 假 transport 的 client，即可离线端到端驱动 w/ 真 httpx 解析。
_client_factory: Any = None


class UserHttpUnavailable(Exception):
    """internal read 不可用/失败：调用方必须 fail-open 回落 DB。"""


def enabled() -> bool:
    """seam 开关：URL 与 token 都配齐才启用（默认双双为空 → False，保持既有直读 DB 行为）。"""
    return bool(settings.auth_http_url and reveal(settings.auth_http_token))


def _endpoint_path(user_id: int) -> str:
    """拼该用户读端点的绝对路径（api_prefix 前缀 + 内部 auth router 路径）。"""
    return f"{settings.api_prefix}/auth/internal/users/{user_id}/snapshot"


def _build_client() -> httpx.AsyncClient:
    """每请求级 client + 配置超时（与 github.py 同款 httpx 出站风格）；测试可注入假 transport。"""
    if _client_factory is not None:
        return _client_factory()
    return httpx.AsyncClient(timeout=httpx.Timeout(settings.auth_http_timeout_s))


async def fetch_user_http_payload(
    user_id: int,
) -> tuple[dict[str, Any] | None, int | None]:
    """经 AUTH 读端点按 id 拉单用户快照**冻结字段 dict**。返回 ``(fields_dict, source_version)``。

    - AUTH 404／信封 ``data`` 为 null（权威不存在）→ ``(None, None)``，不抛。
    - 任何 401/其他 4xx/5xx/网络/超时/畸形 JSON/缺字段 → 抛 ``UserHttpUnavailable``，
      调用方按 **fail-open 回落本进程 DB**（绝不以失败当"用户不存在"，绝不把不完整当 truth）。
    - ``fields_dict`` 已保证含全部 ``_SNAP_FIELDS`` 键（键就在名面上），可直接重建快照。
    """
    url = f"{settings.auth_http_url}{_endpoint_path(user_id)}"
    headers = {
        "Authorization": f"Bearer {reveal(settings.auth_http_token)}",
        "Accept": "application/json",
    }
    try:
        async with _build_client() as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UserHttpUnavailable(f"auth_http request failed: {exc}") from None

    if resp.status_code == 404:
        # 权威不存在：调用方以 None 语义返回，不 fail-open、不缓存缺行。
        return None, None
    if resp.status_code != 200:
        raise UserHttpUnavailable(f"auth_http unexpected status {resp.status_code}")

    payload = _coerce_json(resp)
    data_obj = payload.get("data")
    if data_obj is None:
        # 信封内明确无此行 == 权威不存在。
        return None, None
    return _to_fields_or_unavailable(data_obj), _coerce_sv(payload.get("sv"))


def _coerce_json(resp: httpx.Response) -> dict[str, Any]:
    """Response → dict；非 JSON/非对象一律判畸形 → fail-open。"""
    try:
        parsed = resp.json()
    except Exception as exc:
        raise UserHttpUnavailable(f"auth_http bad json: {exc}") from None
    if not isinstance(parsed, dict):
        raise UserHttpUnavailable("auth_http payload not an object")
    return parsed


def _coerce_sv(value: Any) -> int | None:
    """sv 字段：非空 int-ish → int；缺失/坏值 → None（调用方按无来源版本处理，仍可写缓存）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_fields_or_unavailable(data: Any) -> dict[str, Any]:
    """信封 ``data`` → 冻结字段 dict（校验键齐全）；对象/缺字段一律 fail-open。"""
    if not isinstance(data, dict):
        raise UserHttpUnavailable("auth_http malformed snapshot: data not object")
    missing = [f for f in _SNAP_FIELDS if f not in data]
    if missing:
        raise UserHttpUnavailable(f"auth_http malformed snapshot, missing={missing}")
    return {f: data[f] for f in _SNAP_FIELDS}


# —— 授权判定 seam（M3.B S3）：monolith deps 把“会话存活/失效/角色政令”委托给 auth 权威 ——

_AUTHZ_FIELDS: tuple[str, ...] = ("ok", "account_level", "role")


async def authorize_via_seam(
    *,
    user_id: int,
    expect_token_version: int,
    iat_ts: float | int | None,
    require_admin: bool = False,
) -> dict[str, object]:
    """经 AUTH internal authz 端点裁决一次会话：返回 ``{"ok","cause","account_level","role"}``。

    **鉴权缝 = fail-closed**（与快照读缝的 fail-open 相反）：鉴权“拿不到/异常”必须以拒绝收场，
    不能保守回落本进程（拆库后本就无本地 auth 真值可退）。故任何网络/超时/4xx/5xx/畸形 → 抛
    ``UserHttpUnavailable``，由 deps 按“不可用即拒”（403）处理。仅当调用方显式配置了 seam
    （``enabled()``）才走 HTTP；seam 关闭时调用方应回落到其本地路径。
    """
    url = f"{settings.auth_http_url}{settings.api_prefix}/auth/internal/authz"
    headers = {
        "Authorization": f"Bearer {reveal(settings.auth_http_token)}",
        "Accept": "application/json",
    }
    body = {
        "user_id": user_id,
        "expect_token_version": expect_token_version,
        "iat_ts": iat_ts,
        "require_admin": require_admin,
    }
    try:
        async with _build_client() as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise UserHttpUnavailable(f"auth_http authz request failed: {exc}") from None

    if resp.status_code != 200:
        raise UserHttpUnavailable(
            f"auth_http authz unexpected status {resp.status_code}"
        )

    payload = _coerce_json(resp)
    for f in _AUTHZ_FIELDS:
        if f not in payload:
            raise UserHttpUnavailable(f"auth_http authz missing field {f}")
    return {
        "ok": bool(payload.get("ok")),
        "cause": payload.get("cause"),
        "account_level": payload.get("account_level"),
        "role": payload.get("role"),
    }


# —— 升权写面 seam（M3.B S5 C）：业务把单向升权（解锁考试/纳入成员）交给 auth 权威写 ——
#
# S5 拆库后业务库不再有 users/profiles（auth 是身份词表唯一 owner，含写）。当业务进程确需把
# 用户"单向升权"（exam 通过→exam_unlock；projects 审核通过→incubation）落为 auth 真值时，只能
# 经 AUTH 内部写端点 ``/auth/internal/grant`` 打到 auth 进程（auth 自持库事务内 execute+commit）。
# 本函数是该写面 HTTP client：URL/作法与 :func:`authorize_via_seam` 一致；不触业务 DB 会话。
#
# **fail-closed**（写不可静默流失）：调用方只在 ``enabled()``（url+token 都配齐）时进入本函数；
# 任一网络/超时/4xx/5xx/畸形 → 抛 ``UserHttpUnavailable``，由 supplier 向上传播，绝不让"升权落空"
# 被当成成功（否则审核标记 approved 而用户未真升权 → 语义漂移）。调用方负责按信封取 ``changed``。

_GRANT_FIELDS: tuple[str, ...] = ("changed",)


async def grant_via_seam(
    *,
    kind: str,
    user_id: int,
    unlock_level: str | None = None,
    unlock_role: str | None = None,
) -> int:
    """经 AUTH internal 写端点执行一次单向升权，返回 ``changed``（0=无真实改动/1=已升权并 bump token）。

    ``kind`` ∈ {"exam_unlock", "incubation"}（与 ``/auth/internal/grant`` 的 ``_GrantIn.kind`` 同源）。
    调用方应已先行判断 ``enabled()``（拆库写只有 seam 路径可用）；本函数不带回退本地 DB。
    """
    url = f"{settings.auth_http_url}{settings.api_prefix}/auth/internal/grant"
    headers = {
        "Authorization": f"Bearer {reveal(settings.auth_http_token)}",
        "Accept": "application/json",
    }
    body: dict[str, object] = {"kind": kind, "user_id": int(user_id)}
    if unlock_level is not None:
        body["unlock_level"] = unlock_level
    if unlock_role is not None:
        body["unlock_role"] = unlock_role
    try:
        async with _build_client() as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise UserHttpUnavailable(f"auth_http grant request failed: {exc}") from None

    if resp.status_code != 200:
        raise UserHttpUnavailable(
            f"auth_http grant unexpected status {resp.status_code}"
        )

    payload = _coerce_json(resp)
    for f in _GRANT_FIELDS:
        if f not in payload:
            raise UserHttpUnavailable(f"auth_http grant missing field {f}")
    try:
        return int(payload["changed"])
    except (TypeError, ValueError):
        raise UserHttpUnavailable(
            f"auth_http grant malformed changed={payload['changed']!r}"
        ) from None
