import uuid
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jobs
from app.core.client_ip import client_ip
from app.core.common import ApiResp
from app.core.config import settings
from app.core.err import BizError, CommonErr, respond
from auth import service_auth
from auth.channels import (
    EMAIL_CHANNEL,
    PHONE_CHANNEL,
    ContactChannel,
    channel_for,
)
from auth.db.session import get_auth_session
from auth.deps import (
    CurrentUser,
    get_current_user,
    get_email_provider,
)
from auth.limits import (
    GLOBAL_VERIFY_MAX_PER_WINDOW,
    GLOBAL_VERIFY_WINDOW_SECONDS,
    REFRESH_MAX_PER_WINDOW,
    REFRESH_WINDOW_SECONDS,
)
from auth.providers.base import EmailProvider
from auth.schemas import (
    AuthTokenData,
    MessageResponse,
    ProfileInfo,
    ProfileUpdate,
    RefreshRequest,
    RegByEmailResponse,
    RegByPhoneResponse,
    RegNormalResponse,
    TokenPair,
    UserLoginPassword,
    UserRegByEmail,
    UserRegByPhone,
    UserRegLocal,
    UserRegNormal,
)
from auth.service import (
    get_profile,
    get_profile_by_username,
    serve_avatar,
    update_avatar,
    update_profile,
)
from auth.service_auth import (
    consume_pending_normal_registration,
    store_pending_normal_registration,
)
from auth.service_verify import check_code_rate_limit

router = APIRouter(prefix="/auth", tags=["auth"])


async def _send_reg_code(
    channel: ContactChannel,
    contact: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> None:
    await check_code_rate_limit(
        f"reg:{channel.name}:{contact}", max_count=5, window=3600
    )
    code, _ = await channel.create_verification(db, contact, "register")
    # 延后到响应之后发送（与本文件 resend 路径同款）：原先这里 await，参数被白挂，
    # SMTP/短信往返被算进注册请求时延，两条路径语义也不一致
    background_tasks.add_task(jobs.send_code, channel.name, contact, code)


async def _complete_reg_verify(
    db: AsyncSession, channel: ContactChannel, contact: str, code: str
) -> dict[str, Any]:
    await check_code_rate_limit(
        f"reg:{channel.name}:verify:{contact}", max_count=5, window=3600
    )
    await channel.consume_code(db, contact, code, "register")
    return await service_auth.register_by_verify(db, channel.name, contact)


@router.get("/me", response_model=ApiResp[CurrentUser])
@respond
async def get_me(cur: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return cur


@router.get("/user/by-username/{username}", response_model=ApiResp[ProfileInfo])
@respond
async def get_user_by_username(
    username: str,
    db: AsyncSession = Depends(get_auth_session),
) -> ProfileInfo:
    """公开：按唯一 username 查基础资料，供他人主页浏览（无需登录）。"""
    return await get_profile_by_username(db, username)


@router.get("/{user_id:uuid}", response_model=ApiResp[ProfileInfo])
@respond
async def get_user(
    user_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> ProfileInfo:
    return await get_profile(db, user_id)


@router.put("/{user_id:uuid}/profile", response_model=ApiResp[ProfileInfo])
@respond
async def edit_profile(
    user_id: uuid.UUID,
    info: ProfileUpdate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> ProfileInfo:
    if cur.id != user_id:
        raise BizError(CommonErr.FORBIDDEN)
    await update_profile(db, user_id, info)
    return await get_profile(db, user_id)


class _AvatarUploadResp(BaseModel):
    """头像上传响应：MessageResponse 只有 message，返回的 avatar key 会被 FastAPI 过滤掉。"""

    message: str
    avatar: str


@router.post("/avatar", response_model=ApiResp[_AvatarUploadResp])
@respond
async def upload_avatar(
    file: UploadFile = File(...),
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """上传当前用户头像。multipart ``file``，<=2MB，返回新头像 storage key。"""
    key = await update_avatar(db, cur.id, file.file)
    return {"message": "Avatar uploaded", "avatar": key}


@router.get("/avatar/{user_id}")
async def get_avatar(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_auth_session),
) -> StreamingResponse:
    """代理回读某用户头像字节（immutable 长缓存）；无头像 → 404。"""
    return await serve_avatar(db, user_id)


@router.post("/reg/local", response_model=ApiResp[AuthTokenData])
@respond
async def register_local(
    info: UserRegLocal, db: AsyncSession = Depends(get_auth_session)
) -> dict[str, Any]:
    return await service_auth.register_local(db, info)


@router.post("/reg/normal", response_model=ApiResp[RegNormalResponse])
@respond
async def register_normal_with_password_route(
    info: UserRegNormal,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """发起普通注册"""
    if not info.email and not info.phone:
        raise BizError(
            CommonErr.INVALID_INPUT,
            "At least one of email or phone is required for normal registration",
        )

    # 存储待处理的注册数据
    txn_id = await store_pending_normal_registration(
        db, info.username, info.password, info.email, info.phone
    )

    result: dict[str, Any] = {
        "message": "Verification code(s) sent",
        "txn_id": txn_id,
        "email_sent": False,
        "phone_sent": False,
    }

    if info.email:
        await _send_reg_code(EMAIL_CHANNEL, info.email, background_tasks, db)
        result["email_sent"] = True

    if info.phone:
        await _send_reg_code(PHONE_CHANNEL, info.phone, background_tasks, db)
        result["phone_sent"] = True

    return result


@router.post("/reg/normal/verify", response_model=ApiResp[AuthTokenData])
@respond
async def register_normal_verify(
    txn_id: str,
    email_code: str | None = None,
    phone_code: str | None = None,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用用户名+密码+联系方式完成普通注册。"""
    return await consume_pending_normal_registration(
        db, txn_id, email_code=email_code, phone_code=phone_code
    )


@router.post("/reg/phone", response_model=ApiResp[RegByPhoneResponse])
@respond
async def register_phone(
    info: UserRegByPhone,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """发起仅手机号的注册。发送短信验证码。"""
    await _send_reg_code(PHONE_CHANNEL, info.phone, background_tasks, db)
    return {"phone": info.phone, "message": "SMS verification code sent"}


@router.post("/reg/phone/verify", response_model=ApiResp[AuthTokenData])
@respond
async def register_phone_verify(
    phone: str, code: str, db: AsyncSession = Depends(get_auth_session)
) -> dict[str, Any]:
    """完成仅手机号的注册 — 创建一个普通账号（无密码）。"""
    return await _complete_reg_verify(db, PHONE_CHANNEL, phone, code)


@router.post("/reg/email", response_model=ApiResp[RegByEmailResponse])
@respond
async def register_email(
    info: UserRegByEmail,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """发起仅邮箱的注册。发送邮箱验证码。"""
    await _send_reg_code(EMAIL_CHANNEL, info.email, background_tasks, db)
    return {"email": info.email, "message": "Email verification code sent"}


@router.post("/reg/email/verify", response_model=ApiResp[AuthTokenData])
@respond
async def register_email_verify(
    email: str, code: str, db: AsyncSession = Depends(get_auth_session)
) -> dict[str, Any]:
    """完成仅邮箱的注册 — 创建一个普通账号（无密码）。"""
    return await _complete_reg_verify(db, EMAIL_CHANNEL, email, code)


@router.post("/login/code/request", response_model=ApiResp[MessageResponse])
@respond
async def login_code_request(
    contact: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """请求登录验证码。自动检测邮箱还是手机号。"""

    # 无论用户是否存在，都进行速率限制
    await check_code_rate_limit(f"login:code:{contact}", max_count=5, window=3600)

    # 检查用户是否存在且符合条件
    channel = channel_for(contact)
    user = await channel.find_user(db, contact)

    if not user or user.account_level == "local":
        # 统一响应 — 不泄露用户是否存在
        return {"message": "If account exists, verification code sent"}

    # 用户存在 — 创建并发送验证码
    code, _ = await channel.create_verification(db, contact, "login")
    background_tasks.add_task(channel.send_code, contact, code)

    # 与不存在分支返回**同一**文案：文案不同即账号存在性预言机（调用方直接比对响应即可枚举）
    return {"message": "If account exists, verification code sent"}


@router.post("/login/code", response_model=ApiResp[AuthTokenData])
@respond
async def login_code(
    contact: str,
    code: str,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """使用验证码登录。仅限普通/管理员用户。"""
    await check_code_rate_limit(
        f"login:code:verify:{contact}", max_count=5, window=3600
    )
    return await service_auth.login_code(db, contact, code)


@router.post("/login/password", response_model=ApiResp[AuthTokenData])
@respond
async def login_password_route(
    info: UserLoginPassword,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    # 传 IP 才会触发 service 层的密码登录限流（IP 桶 + 全局桶，fail-close）；不传则整条
    # 防爆破路径被静默跳过。IP 取法与 refresh 同源（网关后读 X-Real-IP）。
    return await service_auth.login_password(db, info, ip_address=client_ip(request))


@router.post("/refresh", response_model=ApiResp[TokenPair])
@respond
async def refresh_access_token_route(
    info: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    # 按 IP 限流而非全局，避免单用户频繁刷新拖垮/阻塞全站其他用户；
    # 刷新签发新 token 属安全敏感路径，Redis 故障时 fail-close（拒绝）。
    # IP 经 core.client_ip 取（网关后读 X-Real-IP），不可用 request.client.host
    # ——那是 apisix 容器的地址，会让本限流退化成全站共享单桶。
    ip = client_ip(request)
    await check_code_rate_limit(
        f"token:refresh:ip:{ip}",
        max_count=REFRESH_MAX_PER_WINDOW,
        window=REFRESH_WINDOW_SECONDS,
    )
    return await service_auth.refresh_access_token(db, info.refresh_token)


@router.post("/logout", response_model=ApiResp[MessageResponse])
@respond
async def logout_route(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    await service_auth.revoke_all_refresh_tokens(db, cur.id)
    return {"message": "Logged out successfully"}


@router.post("/login/magic-link/request", response_model=ApiResp[MessageResponse])
@respond
async def magic_link_request(
    background_tasks: BackgroundTasks,
    email: str = Query(...),
    email_provider: EmailProvider = Depends(get_email_provider),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    await service_auth.request_magic_link(
        db,
        email,
        email_provider,
        purpose="login",
        frontend_url=settings.frontend_callback,
        background_tasks=background_tasks,
    )
    return {"message": "If email exists, magic link sent"}


@router.get("/login/magic-link/verify", response_model=ApiResp[AuthTokenData])
@respond
async def magic_link_verify(
    token: str,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    await check_code_rate_limit(
        "magic-link:verify:global",
        max_count=GLOBAL_VERIFY_MAX_PER_WINDOW,
        window=GLOBAL_VERIFY_WINDOW_SECONDS,
    )
    return await service_auth.verify_magic_link(db, token, purpose="login")
