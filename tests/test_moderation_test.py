"""自动审校规则测试端点验证。

拆库(M3.B S5 dual 真 PG)：后台管理员用户身份迁 auth realm（business 无 users）。HTTP 用例经
``auth_seam_realm`` 把后台 cookie 裁决缝指到本测 auth_db(user 建此处,account_level=admin/
profile.role=super_admin)；``admin.moderation_manage`` 权限点(RolePermission)仍落业务 realm。

覆盖：
- mod_service.test_rules：命中/未命中、derank 累加 penalty、hide 触发 should_hide
- HTTP POST /admin/moderation/rules/test（require_admin_2fa）：无 2FA 信任报 MFA_REQUIRED，
  带 2FA 信任管理 cookie 后返回命中明细。
"""

import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.moderation import service as mod_service
from app.modules.admin.moderation.schemas import RuleCreate
from tests.conftest import DB, auth_user_uid


def _admin_cookie_tok(user_id: uuid.UUID, *, mfa_verified: bool) -> str:
    """给 auth realm 建好的 admin(id) 签发后台 access cookie(token_version=0,acct=admin)。"""
    fake = SimpleNamespace(id=user_id, account_level="admin", token_version=0)
    return create_admin_access_token(fake, mfa_verified=mfa_verified)


class TestTestRulesService:
    async def test_hits_derank(self, db: DB) -> None:
        await mod_service.create_rule(db, RuleCreate(pattern="敏感词", weight=0.8))
        result = await mod_service.test_rules(db, "内容里有敏感词")
        assert result.matched is True
        assert result.penalty == pytest.approx(0.8)
        assert result.should_hide is False
        assert [h.pattern for h in result.hits] == ["敏感词"]

    async def test_misses(self, db: DB) -> None:
        await mod_service.create_rule(db, RuleCreate(pattern="白名单词"))
        result = await mod_service.test_rules(db, "完全无关的内容")
        assert result.matched is False
        assert result.penalty == 0.0
        assert result.should_hide is False
        assert result.hits == []

    async def test_hide_sets_should_hide(self, db: DB) -> None:
        await mod_service.create_rule(db, RuleCreate(pattern="违禁", action="hide"))
        result = await mod_service.test_rules(db, "包含违禁内容")
        assert result.should_hide is True

    async def test_accumulates_penalty(self, db: DB) -> None:
        await mod_service.create_rule(db, RuleCreate(pattern="甲", weight=0.4))
        await mod_service.create_rule(db, RuleCreate(pattern="乙", weight=0.3))
        result = await mod_service.test_rules(db, "甲和乙都有")
        assert result.matched is True
        assert result.penalty == pytest.approx(0.7)
        assert len(result.hits) == 2


class TestTestRulesHttp:
    async def _mk_admin(
        self, db: DB, auth_db, username: str = "root"
    ) -> uuid.UUID:
        """auth realm 建 super_admin；业务 realm 授 moderation 权限点。返回其 uuid id。"""
        from app.modules.admin.models import RolePermission

        au = await auth_user_uid(
            auth_db,
            username=username,
            email=f"{username}@ex.com",
            nickname=username,
            account_level="admin",
            role="super_admin",
            with_token=False,
        )
        db.add(
            RolePermission(
                role_name="admin:super_admin",
                permission="admin.moderation_manage",
            )
        )
        await db.flush()
        return au.id

    async def test_requires_2fa_trusted_admin(
        self, db: DB, auth_db, auth_seam_realm: None, client: AsyncClient
    ) -> None:
        admin_id = await self._mk_admin(db, auth_db)
        tok = _admin_cookie_tok(admin_id, mfa_verified=False)
        client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)
        resp = await client.post(
            "/api/v1/admin/moderation/rules/test", json={"text": "hello"}
        )
        # 无 1h 2FA 信任 → MFA_REQUIRED（code=4，HTTP 401）
        assert resp.status_code == 401
        assert resp.json()["code"] == 4

    async def test_returns_hit_detail(
        self, db: DB, auth_db, auth_seam_realm: None, client: AsyncClient
    ) -> None:
        await mod_service.create_rule(db, RuleCreate(pattern="敏感", weight=0.5))
        admin_id = await self._mk_admin(db, auth_db)
        tok = _admin_cookie_tok(admin_id, mfa_verified=True)
        client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)
        resp = await client.post(
            "/api/v1/admin/moderation/rules/test", json={"text": "本段含敏感字"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["matched"] is True
        assert data["total_rules"] == 1
        assert data["penalty"] == pytest.approx(0.5)
        assert data["hits"][0]["pattern"] == "敏感"
