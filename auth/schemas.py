import datetime
import uuid
from typing import Annotated, Any

from pydantic import BaseModel, Field

from auth.security import PASSWORD_MAX_LENGTH
from core.contracts import ContactLink, CurrentUser, RawEmail
from core.contracts import Password as BasePassword
from core.contracts import ProfileInfo as ProfileInfo
from core.contracts import ProfileRole as ProfileRole
from core.contracts import ProfileUpdate as ProfileUpdate

Password = Annotated[BasePassword, Field(max_length=PASSWORD_MAX_LENGTH)]


class MeInfo(CurrentUser):
    username: str
    nickname: str | None = None
    avatar: str | None = None
    contact_links: list[ContactLink] = Field(default_factory=list)


class UserRegLocal(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: Password = Field(...)


class UserRegNormal(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: Password = Field(...)
    email: RawEmail | None = None
    phone: str | None = Field(None, min_length=5, max_length=20)


class UserRegByPhone(BaseModel):
    phone: str = Field(..., min_length=5, max_length=20)


class UserRegByEmail(BaseModel):
    email: RawEmail


class UserLoginPassword(BaseModel):
    account: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)


class AuthTokenData(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    user_id: uuid.UUID
    account_level: str
    requires_2fa: bool = False
    setup_required: bool = False
    temp_token: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str


class TOTPSetupBeginData(BaseModel):
    secret: str
    qr_code_uri: str


class TOTPSetupCompleteRequest(BaseModel):
    code: str


class TOTPSetupCompleteData(BaseModel):
    recovery_codes: list[str]
    confirmed_saved_required: bool


class TOTPSetupCompleteTempData(BaseModel):
    """管理员强制设置的响应 — 包含认证令牌。"""

    recovery_codes: list[str]
    confirmed_saved_required: bool
    access_token: str | None = None
    refresh_token: str | None = None


class TOTPVerifyRequest(BaseModel):
    temp_token: str
    code: str | None = None
    recovery_code: str | None = None
    trust_device: bool = False


class TOTPDisableRequest(BaseModel):
    """关闭 2FA / step-up 第二次因子：TOTP 动态码或恢复码二选一。"""

    code: str | None = None
    recovery_code: str | None = None


# ── 通用消息响应 ──────────────────────────────────────────────


class MessageResponse(BaseModel):
    message: str


# ── 注册响应 ──────────────────────────────────────────────────


class RegNormalResponse(BaseModel):
    message: str
    txn_id: str
    email_sent: bool = False
    phone_sent: bool = False


class RegByPhoneResponse(BaseModel):
    phone: str
    message: str


class RegByEmailResponse(BaseModel):
    email: RawEmail
    message: str


# ── Recovery 响应 ─────────────────────────────────────────────


class RecoverCheckResponse(BaseModel):
    recoverable: bool


class RecoverRequires2FAResponse(BaseModel):
    message: str
    requires_2fa: bool | None = None
    txn_id: str | None = None
    temp_token: str | None = None


class AdminRecoverBeginResponse(BaseModel):
    message: str
    txn_id: str


class AdminRecoverVerifyContactResponse(BaseModel):
    message: str
    txn_id: str
    temp_token: str


class AdminRecoverVerifyTOTPResponse(BaseModel):
    message: str
    txn_id: str


# ── OAuth 响应 ────────────────────────────────────────────────


class OAuthRedirectResponse(BaseModel):
    url: str


# ── Passkey 响应 ──────────────────────────────────────────────


class PasskeyRegistrationOptionsResponse(BaseModel):
    challenge_id: str
    public_key: dict[str, Any]


class PasskeyLoginOptionsResponse(BaseModel):
    challenge_id: str
    public_key: dict[str, Any]


class PasskeyRegisterCompleteRequest(BaseModel):
    """WebAuthn 注册完成请求体。"""

    rawId: str
    challenge_id: str
    response: dict[str, Any] = Field(default_factory=dict)
    device_name: str | None = None


class PasskeyLoginCompleteRequest(BaseModel):
    """WebAuthn 登录完成请求体。"""

    rawId: str
    challenge_id: str
    response: dict[str, Any] = Field(default_factory=dict)


class PasskeyRegisterCompleteResponse(BaseModel):
    message: str
    device_name: str


class PasskeyCredentialItem(BaseModel):
    id: uuid.UUID
    credential_id: str
    device_name: str
    created_at: datetime.datetime  # UTC 时间


# ── TOTP 验证响应 ─────────────────────────────────────────────


# ── Settings 响应 ───────────────────────────────────────────────


class BindCodeRequestResponse(BaseModel):
    message: str
    record_id: uuid.UUID


class BindCodeVerifyResponse(BaseModel):
    message: str


class TOTPConfirmResponse(BaseModel):
    message: str


class TOTPVerifyResponse(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    user_id: uuid.UUID
    account_level: str
    trust_device: bool = False
    mfa_verified: bool | None = None
    message: str | None = None


class TOTPDisableResponse(BaseModel):
    message: str


class TOTPStatusData(BaseModel):
    """GET /auth/2fa/status —— 2FA 是否已开启。"""

    enabled: bool


class SettingsInfo(BaseModel):
    """GET /auth/settings —— 当前绑定状态。"""

    email: str | None = None
    phone: str | None = None
    github: str | None = None
    has_2fa: bool = False


class UnbindRequest(BaseModel):
    """DELETE /auth/settings/{type} —— 解绑请求体；已开启 2FA 时 code/recovery_code 二选一。"""

    code: str | None = Field(default=None, min_length=6, max_length=6)
    recovery_code: str | None = None


# ── Onboarding 引导向导 ──────────────────────────────────────────


class OnboardingState(BaseModel):
    """GET /auth/onboarding —— 当前用户引导进度（未开始时返回默认 step=1）。"""

    step: int = 1
    completed: bool = False
    data: dict[str, Any] | None = None


class OnboardingStepRequest(BaseModel):
    """PUT /auth/onboarding/steps/{step} —— 提交某一步的分步数据。"""

    data: dict[str, Any] = Field(default_factory=dict)
