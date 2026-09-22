"""AUTH 读面 HTTP client（M3 B1.2）：把单用户快照 miss 回填跨进程送到 AUTH 读端点。

背景：A1/A6 已把身份读面收敛进 ``auth.snapshot`` 单态内存缝 + ``user:snap`` 缓存；当
AUTH 被部署成**独立进程**（B1.1 ``auth.main`` + compose ``auth``）后，在线读路径（本
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

import uuid
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


def _endpoint_path(user_id: uuid.UUID) -> str:
    """拼该用户读端点的绝对路径（api_prefix 前缀 + 内部 auth router 路径）。"""
    return f"{settings.api_prefix}/auth/internal/users/{user_id}/snapshot"


def _build_client() -> httpx.AsyncClient:
    """每请求级 client + 配置超时（与 github.py 同款 httpx 出站风格）；测试可注入假 transport。"""
    if _client_factory is not None:
        return _client_factory()
    return httpx.AsyncClient(timeout=httpx.Timeout(settings.auth_http_timeout_s))


async def fetch_user_http_payload(
    user_id: uuid.UUID,
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
    # 区分「显式 data: null」（权威不存在）与「压根没有 data 键」（信封畸形）：后者按
    # fail-open 契约必须抛 Unavailable 让调用方回落 DB，绝不能当作用户不存在。
    if "data" not in payload:
        raise UserHttpUnavailable("auth_http payload missing data")
    data_obj = payload["data"]
    if data_obj is None:
        # 信封内明确无此行 == 权威不存在。
        return None, None
    return _to_fields_or_unavailable(data_obj), _coerce_sv(payload.get("sv"))


async def fetch_users_http_batch(
    user_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[dict[str, Any] | None, int | None]]:
    """经 AUTH 读端点**一次**拉一批快照（M6.5），返回 ``{user_id: (fields_dict|None, sv|None)}``。

    调用方保证单次 ``len(user_ids) <= snapshot.BATCH_IDS_MAX``（超限端点回 400 → 本函数抛
    ``UserHttpUnavailable``）；返回的 dict 覆盖入参里的全部 id（权威不存在 → ``(None, None)``）。

    - 与单条 :func:`fetch_user_http_payload` 同纪律：任何 4xx/5xx/网络/超时/畸形 JSON/缺字段
      → 抛 ``UserHttpUnavailable``，由调用方按「整块跳过」处理（跨 realm 批量展示读无本地
      回退可抛，见 ``auth.snapshot._retrieve_fields_batch``）。
    - 不再逐 id 往返：`len(ids) = N` 时 HTTP 调用数为 ``⌈N / BATCH_IDS_MAX⌉``（由调用方分块）。
    """
    url = f"{settings.auth_http_url}{settings.api_prefix}/auth/internal/users/by-ids"
    headers = {
        "Authorization": f"Bearer {reveal(settings.auth_http_token)}",
        "Accept": "application/json",
    }
    params = {"ids": ",".join(str(i) for i in user_ids)}
    try:
        async with _build_client() as client:
            resp = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise UserHttpUnavailable(f"auth_http batch request failed: {exc}") from None

    if resp.status_code != 200:
        raise UserHttpUnavailable(
            f"auth_http batch unexpected status {resp.status_code}"
        )

    payload = _coerce_json(resp)
    items = payload.get("items")
    if not isinstance(items, list):
        raise UserHttpUnavailable("auth_http batch payload missing items")
    out: dict[uuid.UUID, tuple[dict[str, Any] | None, int | None]] = {}
    for item in items:
        if not isinstance(item, dict) or "user_id" not in item:
            raise UserHttpUnavailable("auth_http batch malformed item")
        try:
            uid = uuid.UUID(str(item["user_id"]))
        except (TypeError, ValueError):
            raise UserHttpUnavailable("auth_http batch malformed user_id") from None
        # 同单条信封：缺 data 键是畸形（抛 Unavailable 整块跳过），只有显式 null 才是
        # 「权威不存在」。.get 会把两者混成一个 None。
        if "data" not in item:
            raise UserHttpUnavailable("auth_http batch item missing data")
        data = item["data"]
        if data is None:
            out[uid] = (None, None)  # 权威不存在（与单条信封同义）
            continue
        out[uid] = (_to_fields_or_unavailable(data), _coerce_sv(item.get("sv")))
    return out


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
    """sv 字段：**真 int** → 原值；缺失/坏值 → None（调用方按无来源版本处理，仍可写缓存）。

    只认 ``int`` 而非任意 int-ish：``int(3.9)`` 会把浮点截断成 3、``int(True)`` 得 1，
    于是一个被写坏的 sv 会变成「看似合理但错误」的来源版本，被拿去给 write_if_newer 做
    CAS 判断，可能覆盖掉更新的缓存——本该按「无 sv」fail-open 处理。
    """
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
    user_id: uuid.UUID,
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
        "user_id": str(user_id),
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
    # fail-closed 面上只认真正的 JSON 布尔：{"ok": "false"}/{"ok": 1} 这类畸形体若被
    # bool() 强转成 truthy 就是放行，等于把服务端/网关的一次抖动变成越权。
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise UserHttpUnavailable("auth_http authz malformed ok flag")
    return {
        "ok": ok,
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
    user_id: uuid.UUID,
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
    body: dict[str, object] = {"kind": kind, "user_id": str(user_id)}
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


# —— bot 面板 SSO 铸票缝（面板并入社区后台）：票据签发原语在 auth 域（私钥只在此）——
#
# 社区后台进程（business）不持签发私钥，只能经本 client 把「代表某 admin 铸一张一次性票据」
# 交给 auth 进程。消费方是 ``app/modules/admin/bot_router`` 的 ``/admin/bot/sso-ticket``，
# 该端点已被 ``require_admin``（seam-only）裁决过管理员身份，此处再经 auth 独立复核。
#
# **fail-closed**：任一端不回 200／畸形 → 抛 ``UserHttpUnavailable``，调用方转 UNAVAILABLE 而
# 非"静默不发票"（发不出票只是需要手动登录，但必须让运维看见是缝坏了还是配置漏了）。

_BOT_TICKET_FIELDS: tuple[str, ...] = ("ticket", "expires_in")


async def mint_bot_sso_ticket(
    *, user_id: uuid.UUID, account_level: str = "admin"
) -> dict[str, object]:
    """经 AUTH internal 端点铸一次性 bot 面板 SSO 票据，返回 ``{"ticket", "expires_in"}``。"""
    url = f"{settings.auth_http_url}{settings.api_prefix}/auth/internal/bot-ticket"
    headers = {
        "Authorization": f"Bearer {reveal(settings.auth_http_token)}",
        "Accept": "application/json",
    }
    body = {"user_id": str(user_id), "account_level": account_level}
    try:
        async with _build_client() as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise UserHttpUnavailable(f"auth_http bot-ticket request failed: {exc}") from None

    if resp.status_code != 200:
        # 403 理论上不可达（调用方持 seam 裁决过的 admin），若出现说明两进程口径漂移，
        # 与网络故障同样按"缝不可用"上报，绝不静默降级。
        raise UserHttpUnavailable(
            f"auth_http bot-ticket unexpected status {resp.status_code}"
        )

    payload = _coerce_json(resp)
    for f in _BOT_TICKET_FIELDS:
        if f not in payload:
            raise UserHttpUnavailable(f"auth_http bot-ticket missing field {f}")
    ticket = payload["ticket"]
    # 必须显式校验：str(None) == "None" 是个非空字符串，原实现会把空票当有效票据返回，
    # 调用方再把它拼成 SSO iframe URL 发出去（违背「绝不返回空票」的 fail-closed 契约）。
    if not isinstance(ticket, str) or not ticket:
        raise UserHttpUnavailable(f"auth_http bot-ticket malformed ticket={ticket!r}")
    try:
        expires_in = int(payload["expires_in"])
    except (TypeError, ValueError):
        raise UserHttpUnavailable(
            f"auth_http bot-ticket malformed payload={payload!r}"
        ) from None
    return {"ticket": ticket, "expires_in": expires_in}
