"""审计写缝（P0a）：业务域经 ``/auth/internal/audit`` 落审计，禁止业务会话直写 auth 表。

背景：``audit_logs`` 属 ``AuthBase``（auth 独立库）。业务域把 ``get_session`` 直接传给
``log_audit`` 时，拆库后必 ``relation "audit_logs" does not exist``，并把业务事务一起
回滚（admin 删内容曾因此整条链路失败）。

覆盖：
- 客户端契约：POST 路径 / 内部 Bearer / 可选字段裁剪（``user_id=None`` 不发送）。
- 失败归一：非 200 或 ``ok != true`` → ``UserHttpUnavailable``（上层据此 fail-closed）。
- fail-closed：AUTH 不可达时 ``record_audit_from_business`` 抛 ``BizError(UNAVAILABLE)``，
  绝不静默丢审计。
- 单库回落：seam 未启用时写调用方会话（fused 装配下业务会话可见 ``audit_logs``）。
"""

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth.seams as seams
import auth.user_http as user_http
from auth.models import AuditLog
from core.config import settings
from core.err import BizError, CommonErr


@pytest.fixture(autouse=True)
def _reset_client_factory() -> Any:
    """制造/清理 seam 的 client 注入点，杜绝跨用例残留。"""
    user_http._client_factory = None
    yield
    user_http._client_factory = None


def _enable_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_http_url", "http://auth-proc")
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret")


def _inject_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        user_http,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://auth-proc"),
    )


class TestAuditClient:
    async def test_posts_all_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_seam(monkeypatch)
        captured: list[httpx.Request] = []

        async def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        _inject_transport(monkeypatch, _handler)
        uid = uuid.uuid4()

        await user_http.write_audit_via_seam(
            user_id=uid,
            action="review_file",
            detail='{"decision":"approved"}',
            ip_address="203.0.113.7",
        )

        req = captured[0]
        assert req.method == "POST"
        assert req.url.path == "/api/v1/auth/internal/audit"
        assert req.headers["authorization"] == "Bearer internal-secret"
        assert json.loads(req.content) == {
            "action": "review_file",
            "user_id": str(uid),
            "detail": '{"decision":"approved"}',
            "ip_address": "203.0.113.7",
        }

    async def test_omits_absent_optional_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_seam(monkeypatch)
        captured: list[httpx.Request] = []

        async def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        _inject_transport(monkeypatch, _handler)

        await user_http.write_audit_via_seam(user_id=None, action="system_cleanup")

        # user_id=None 是合法「无主体」动作：不发送该键，而不是发 null
        assert json.loads(captured[0].content) == {"action": "system_cleanup"}

    async def test_non_200_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_seam(monkeypatch)

        async def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        _inject_transport(monkeypatch, _handler)

        with pytest.raises(user_http.UserHttpUnavailable):
            await user_http.write_audit_via_seam(user_id=None, action="review_file")

    async def test_malformed_ok_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_seam(monkeypatch)

        async def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False})

        _inject_transport(monkeypatch, _handler)

        with pytest.raises(user_http.UserHttpUnavailable):
            await user_http.write_audit_via_seam(user_id=None, action="review_file")


class TestRecordAuditFromBusiness:
    async def test_seam_failure_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, fused_db_session: AsyncSession
    ) -> None:
        """AUTH 不可达 → 抛 UNAVAILABLE（调用方业务事务应回滚），不吞异常。"""
        _enable_seam(monkeypatch)

        async def _boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("auth unreachable")

        _inject_transport(monkeypatch, _boom)

        with pytest.raises(BizError) as exc:
            await seams.record_audit_from_business(
                fused_db_session, None, "review_file", detail="{}"
            )
        assert exc.value.errcode == CommonErr.UNAVAILABLE

    async def test_seam_off_writes_caller_session(
        self, monkeypatch: pytest.MonkeyPatch, fused_db_session: AsyncSession
    ) -> None:
        """单库/蓝绿（seam 未配置）回落同库直写，写的是调用方会话。"""
        monkeypatch.setattr(settings, "auth_http_url", "")
        monkeypatch.setattr(settings, "auth_http_token", "")
        assert user_http.enabled() is False

        await seams.record_audit_from_business(
            fused_db_session, None, "local_audit", detail="fallback"
        )

        row = (
            await fused_db_session.execute(
                select(AuditLog).where(AuditLog.action == "local_audit")
            )
        ).scalar_one()
        assert row.detail == "fallback"
