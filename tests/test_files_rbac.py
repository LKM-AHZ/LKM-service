"""files 迁移 RBAC：上传/下载/审核/属主（M3.B S5 拆库双真 PG 迁移样板）。

拆库后业务库(Base=53,无 users)不再有 User/Profile 表；users/profiles 迁 auth 库
(AuthBase=18)。业务文件行的 uploader_id / 登录身份的裁决与展示名一律走 auth realm：
- 测试先经 ``auth_user_uid(auth_db,...)`` 在“本测 auth schema”写真实 User(+Profile)，
  取其稳定 uuid ``.id`` / ``.token`` 作为登录身份。
- relevant 涉及 HTTP 鉴权 / 展示读的用例注入 ``auth_db`` + ``auth_seam_realm`` fixture：
  seam(替身直读本测 auth_db)裁决 current user 权威 account_level/role——业务端绝不摸
  users。RBAC 权限点 RolePermission 仍落在业务 realm(Base, 符合生产) 由 db 直插。
- 纯“(无身份)拒绝”用例可仅 db/client(seam 无关)。

真双 PG(lkm / lkm_auth)各建 schema 可跑。
"""

import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.auth.models import User
from tests.conftest import DB, AuthUser, Client, auth_user_uid


async def _mk_au(
    auth_db: AsyncSession,
    uname: str,
    level: str = "normal",
    role: str = "member",
) -> AuthUser:
    """在 auth realm 建一线用户并返回其稳定 AuthUser(id/token/account_level)。"""
    return await auth_user_uid(
        auth_db, username=uname, nickname=uname, account_level=level, role=role
    )


def _h(au: AuthUser) -> dict[str, str]:
    # AuthUser.token 已在 auth realm 以该用户的 (account_level, role) mint 的 Web Bearer。
    return {"Authorization": f"Bearer {au.token}"}


async def _grant(db: DB, role_name: str, *perms: str) -> None:
    for p in perms:
        db.add(RolePermission(role_name=role_name, permission=p))
    await db.flush()


async def _upload(
    client: Client, headers: dict[str, str], original_name: str = "讲义.pdf"
):
    return await client.post(
        "/api/v1/files",
        headers=headers,
        files={
            "file": (
                original_name,
                io.BytesIO(b"%PDF-1.4 content"),
                "application/pdf",
            )
        },
        data={"category_id": "math", "description": "rbac 测试", "tags": "[]"},
    )


async def _mk_file(
    db: DB,
    client: Client,
    auth_db: AsyncSession,
    tmp_path,
    monkeypatch,
    uploader: AuthUser,
    approved: bool = False,
) -> str:
    """造一个文件：先以 uploader 上传，再（可选）由 super_admin 审核通过。

    需把 files_store_dir 指到 tmp_path 使落盘可用。上传者与审核者都建在 auth realm，
    HTTP 经 auth_seam_realm 跨 realm 裁决。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    await _grant(db, "normal:member", "files.upload")
    r = await _upload(client, _h(uploader))
    assert r.status_code == 200, r.text
    fid = r.json()["data"]["id"]
    if approved:
        sa = await _mk_au(auth_db, "sa_review", level="admin", role="super_admin")
        await _grant(db, "admin:super_admin", "files.review")
        tok = create_admin_access_token(
            (await auth_db.execute(select(User).where(User.id == sa.id))).scalar_one(),
            mfa_verified=True,
        )
        client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)
        rr = await client.post(
            f"/api/v1/files/{fid}/review",
            data={"status": "approved"},
        )
        assert rr.status_code == 200, rr.text
    return fid


# ---- 上传 ----


async def test_upload_without_auth_is_403(db: DB, client: Client) -> None:
    r = await client.post(
        "/api/v1/files",
        files={"file": ("a.pdf", io.BytesIO(b"content"), "application/pdf")},
    )
    # get_current_user 必选 → 无 Authorization 头 403
    assert r.status_code == 403


async def test_member_without_upload_perm_is_403(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None, tmp_path, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    # member 未授 files.upload（默认 normal:member 不授）
    u = await _mk_au(auth_db, "u_noperm", level="normal", role="member")
    r = await _upload(client, _h(u))
    # 迁移后：RequirePermission(files.upload) → 403
    assert r.status_code == 403


async def test_member_with_upload_perm_can_upload(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None, tmp_path, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    await _grant(db, "normal:member", "files.upload")
    u = await _mk_au(auth_db, "u_ok", level="normal", role="member")
    r = await _upload(client, _h(u))
    assert r.status_code == 200
    assert r.json()["data"]["uploader_id"] == str(u.id)


# ---- 下载 ----


async def test_download_without_perm_is_403(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None, tmp_path, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    uploader = await _mk_au(auth_db, "dl_owner", level="normal", role="member")
    fid = await _mk_file(db, client, auth_db, tmp_path, monkeypatch, uploader, approved=True)
    # 下载者未授 files.download（不设权限 / 只授 upload）
    actor = await _mk_au(auth_db, "dl_noperm", level="normal", role="member")
    r = await client.post(f"/api/v1/files/{fid}/download", headers=_h(actor))
    assert r.status_code == 403


async def test_download_with_perm_is_200(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None, tmp_path, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    uploader = await _mk_au(auth_db, "dl2_owner", level="normal", role="member")
    fid = await _mk_file(db, client, auth_db, tmp_path, monkeypatch, uploader, approved=True)
    await _grant(db, "normal:member", "files.download")
    actor = await _mk_au(auth_db, "dl2_ok", level="normal", role="member")
    r = await client.post(f"/api/v1/files/{fid}/download", headers=_h(actor))
    assert r.status_code == 200


# ---- 删除（属主 / 代管） ----


async def test_delete_others_file_is_403(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None, tmp_path, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    owner = await _mk_au(auth_db, "del_owner", level="normal", role="member")
    fid = await _mk_file(db, client, auth_db, tmp_path, monkeypatch, owner)
    other = await _mk_au(auth_db, "del_other", level="normal", role="member")
    r = await client.post(f"/api/v1/files/{fid}/delete", headers=_h(other))
    assert r.status_code == 403


async def test_delete_own_file_is_200(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None, tmp_path, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    owner = await _mk_au(auth_db, "del_self", level="normal", role="member")
    fid = await _mk_file(db, client, auth_db, tmp_path, monkeypatch, owner)
    r = await client.post(f"/api/v1/files/{fid}/delete", headers=_h(owner))
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "deleted"


async def test_super_admin_can_delete_others_file(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None, tmp_path, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
    owner = await _mk_au(auth_db, "del_sa_owner", level="normal", role="member")
    fid = await _mk_file(db, client, auth_db, tmp_path, monkeypatch, owner)
    sa = await _mk_au(auth_db, "del_sa", level="admin", role="super_admin")
    # super_admin 授 file.owner_delete → check_owner 凭 owner 权限点代管放行
    await _grant(db, "admin:super_admin", "file.owner_delete")
    r = await client.post(f"/api/v1/files/{fid}/delete", headers=_h(sa))
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "deleted"
