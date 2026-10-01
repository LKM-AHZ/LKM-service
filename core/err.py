import functools
import logging
from collections.abc import Callable, Coroutine
from enum import IntEnum
from typing import Any, cast

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from core.common import ApiResp, PageData
from core.logging import get_request_id

logger = logging.getLogger(__name__)


class Namespace:
    """Error-code namespace: encodes ns_id<<16 | local so modules stay collision-free."""

    ns_id: int
    prefix: str

    def __init__(self, ns_id: int, prefix: str) -> None:
        self.ns_id = ns_id
        self.prefix = prefix

    def err(self, local: int) -> int:
        return (self.ns_id << 16) | local


class ErrCode(IntEnum):
    """Base type for every module error-code enum."""


NS_COMMON = Namespace(0, "common")
NS_AUTH = Namespace(1, "auth")
NS_COLUMNS = Namespace(2, "columns")
NS_BLOG = Namespace(3, "blog")
NS_FORUM = Namespace(4, "forum")
NS_FILES = Namespace(5, "files")
NS_STARHOPE = Namespace(7, "starhope")
NS_ARTICLES = Namespace(8, "articles")
NS_STORAGE = Namespace(9, "storage")
NS_EXAM = Namespace(10, "exam")
NS_BOARDS = Namespace(11, "boards")
NS_POINTS = Namespace(12, "points")
NS_QA = Namespace(13, "qa")
NS_PROJECTS = Namespace(14, "projects")
NS_FOLLOW = Namespace(15, "follow")
NS_MODERATION = Namespace(16, "moderation")
NS_CONTENT = Namespace(17, "content")
NS_INTERACTION = Namespace(18, "interaction")
NS_NOTIFICATION = Namespace(19, "notification")
NS_SEARCH = Namespace(20, "search")


class CommonErr(ErrCode):
    OK = 0
    INVALID_INPUT = NS_COMMON.err(1)
    FORBIDDEN = NS_COMMON.err(2)
    INTERNAL_ERROR = NS_COMMON.err(3)
    MFA_REQUIRED = NS_COMMON.err(4)  # 危险操作需重新完成 2FA（step-up）
    UNAVAILABLE = NS_COMMON.err(5)  # 依赖的后端未启用/不可达（如分析库 ClickHouse）
    TIMEOUT = NS_COMMON.err(6)  # 请求超出执行预算被硬中断（GraphQL 查询级超时）
    VERSION_CONFLICT = NS_COMMON.err(7)
    UNAUTHORIZED = NS_COMMON.err(8)
    NOT_FOUND = NS_COMMON.err(9)
    BAD_REQUEST = NS_COMMON.err(10)
    CONFLICT = NS_COMMON.err(11)


ERRTABLE: dict[ErrCode, tuple[int, str]] = {}


def register(errors: dict[ErrCode, tuple[int, str]]) -> None:
    # 先整体校验再落表：逐个插入时遇到重复会在抛错前留下半更新的 ERRTABLE，
    # 测试/模块重载场景下会让后续断言看到一份不完整的注册表
    for code in errors:
        if code in ERRTABLE:
            raise ValueError(f"Duplicate error code: {code!r}")
    ERRTABLE.update(errors)


# —— 唯一约束名 → 语义化错误码 ——
UNIQUE_CONSTRAINT_ERRORS: dict[str, ErrCode] = {}


def register_unique_constraint(name_fragment: str, errcode: ErrCode) -> None:
    """登记「约束名含该片段 → 该错误码」。先注册者优先，重复登记不覆盖。"""
    UNIQUE_CONSTRAINT_ERRORS.setdefault(name_fragment.lower(), errcode)


def unique_constraint_errcode(constraint_name: str | None) -> ErrCode | None:
    """按约束名取语义化错误码；未命中/无名字返回 ``None``（调用方回落通用码）。"""
    if not constraint_name:
        return None
    lowered = constraint_name.lower()
    for fragment, code in UNIQUE_CONSTRAINT_ERRORS.items():
        if fragment in lowered:
            return code
    return None


register(
    {
        CommonErr.OK: (200, "OK"),
        CommonErr.INVALID_INPUT: (422, "Invalid input"),
        CommonErr.FORBIDDEN: (403, "Forbidden"),
        CommonErr.INTERNAL_ERROR: (500, "Internal server error"),
        CommonErr.MFA_REQUIRED: (401, "MFA required"),
        CommonErr.UNAVAILABLE: (503, "Service unavailable"),
        CommonErr.TIMEOUT: (504, "Request timed out"),
        CommonErr.VERSION_CONFLICT: (409, "Version conflict"),
        CommonErr.UNAUTHORIZED: (401, "Unauthorized"),
        CommonErr.NOT_FOUND: (404, "Not found"),
        CommonErr.BAD_REQUEST: (400, "Bad request"),
        CommonErr.CONFLICT: (409, "Resource conflict"),
    }
)


def err_info(errcode: ErrCode) -> tuple[int, str]:
    """
    查错误码对应的 ``(status, msg)``；未注册时回退 500 并告警。
    """
    info = ERRTABLE.get(errcode)
    if info is None:
        logger.error(
            "未注册的错误码 %r：检查对应模块 errors.py 是否被 app/modules/registry 导入",
            errcode,
        )
        return ERRTABLE[CommonErr.INTERNAL_ERROR]
    return info


class BizError(Exception):
    errcode: ErrCode
    detail: str

    def __init__(
        self,
        errcode: ErrCode,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.errcode = errcode
        self.detail = detail or err_info(errcode)[1]
        # 随错误一并下发的响应头
        self.headers = headers
        # 不调 super().__init__ 会让 exc.args 为空、str(exc) 变成空串：
        # logger.exception / 错误上报器这类按异常文本格式化的地方会丢掉消息与错误码
        super().__init__(self.detail)


def map_err(exc: Exception) -> tuple[int, ErrCode, str]:
    if isinstance(exc, BizError):
        status, _ = err_info(exc.errcode)
        return status, exc.errcode, exc.detail

    if isinstance(exc, RequestValidationError):
        msgs: list[str] = []
        for err in exc.errors():
            field = ".".join(str(loc) for loc in err.get("loc", []) if loc != "body")
            msgs.append(f"{field}: {err.get('msg', '')}")
        detail = "; ".join(msgs)
        status, _ = ERRTABLE[CommonErr.INVALID_INPUT]
        return status, CommonErr.INVALID_INPUT, detail

    logger.error("Unhandled exception", exc_info=exc)
    status, msg = ERRTABLE[CommonErr.INTERNAL_ERROR]
    return status, CommonErr.INTERNAL_ERROR, msg


_RETRY_AFTER_SECONDS = 60


def resp_json(
    errcode: ErrCode,
    *,
    data: Any = None,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    status, msg = err_info(errcode)
    out_headers = dict(headers) if headers else {}
    # 大小写不敏感地看调用方是否已给：HTTP 头名不区分大小写
    if status in (429, 503) and not any(
        k.lower() == "retry-after" for k in out_headers
    ):
        out_headers["Retry-After"] = str(_RETRY_AFTER_SECONDS)

    return JSONResponse(
        status_code=status,
        content=ApiResp(
            code=errcode,
            message=detail or msg,
            data=data,
            request_id=get_request_id(),
        ).model_dump(mode="json"),
        headers=out_headers or None,
    )


def respond[**P, R](
    func: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, Response]]:
    """
    装饰器：将返回值通过 ERRTABLE 包装。
    """

    @functools.wraps(func)
    async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Response:
        result = await func(*args, **kwargs)
        return _wrap_result(result)

    return async_wrapper


def _wrap_result(result: Any) -> Response:
    # 预编码响应透传（读热端点用 msgspec 直出，勿再包一层 JSONResponse）
    if isinstance(result, Response):
        return result
    if isinstance(result, tuple) and isinstance(result[0], ErrCode):
        # 只认 (errcode, payload) 这一种形状
        if len(cast(Any, result)) != 2:
            logger.error(
                "endpoint returned an (errcode, ...) tuple with %d elements, expected 2",
                len(cast(Any, result)),
            )
            return resp_json(CommonErr.INTERNAL_ERROR)
        # isinstance 已收窄 result[0] 为 ErrCode，无需再 cast
        errcode = result[0]
        payload = result[1]
        if isinstance(payload, str):
            return resp_json(errcode, detail=payload)
        return resp_json(errcode, data=payload)
    extra: dict[str, str] = {}
    if isinstance(result, PageData):
        extra["X-Total"] = str(result.total)
    return resp_json(CommonErr.OK, data=result, headers=extra)


class AuthErr(ErrCode):
    ALREADY_REGISTERED = NS_AUTH.err(1)
    INVALID_CREDENTIALS = NS_AUTH.err(2)
    USER_NOT_FOUND = NS_AUTH.err(3)
    ACCOUNT_LOCKED = NS_AUTH.err(4)
    ACCOUNT_LEVEL_INSUFFICIENT = NS_AUTH.err(5)
    VERIFICATION_CODE_INVALID = NS_AUTH.err(6)
    VERIFICATION_CODE_EXPIRED = NS_AUTH.err(7)
    VERIFICATION_CODE_RATE_LIMIT = NS_AUTH.err(8)
    TOKEN_EXPIRED = NS_AUTH.err(9)
    TOKEN_INVALID = NS_AUTH.err(10)
    REFRESH_TOKEN_REVOKED = NS_AUTH.err(11)
    TOTP_NOT_ENABLED = NS_AUTH.err(12)
    TOTP_ALREADY_ENABLED = NS_AUTH.err(13)
    TOTP_SETUP_REQUIRED = NS_AUTH.err(14)
    TOTP_CODE_INVALID = NS_AUTH.err(15)
    RECOVERY_CODE_INVALID = NS_AUTH.err(16)
    RECOVERY_CODE_USED = NS_AUTH.err(17)
    OAUTH_CANCELED = NS_AUTH.err(18)
    OAUTH_PROVIDER_ERROR = NS_AUTH.err(19)
    OAUTH_EMAIL_TAKEN = NS_AUTH.err(20)
    PASSKEY_REGISTRATION_FAILED = NS_AUTH.err(21)
    PASSKEY_VERIFICATION_FAILED = NS_AUTH.err(22)
    RECOVERY_NOT_SUPPORTED = NS_AUTH.err(23)
    RECOVERY_METHOD_UNAVAILABLE = NS_AUTH.err(24)
    OAUTH_EMAIL_ALREADY_REGISTERED = NS_AUTH.err(25)
    TOO_LARGE = NS_AUTH.err(26)
    AVATAR_NOT_FOUND = NS_AUTH.err(27)
    EMAIL_TAKEN = NS_AUTH.err(28)
    USERNAME_TAKEN = NS_AUTH.err(29)


register(
    {
        AuthErr.ALREADY_REGISTERED: (409, "Username or email already registered"),
        AuthErr.INVALID_CREDENTIALS: (401, "Invalid username or password"),
        AuthErr.USER_NOT_FOUND: (401, "User not found"),
        AuthErr.ACCOUNT_LOCKED: (423, "Account is locked"),
        AuthErr.ACCOUNT_LEVEL_INSUFFICIENT: (403, "Account level insufficient"),
        AuthErr.VERIFICATION_CODE_INVALID: (400, "Verification code invalid"),
        AuthErr.VERIFICATION_CODE_EXPIRED: (400, "Verification code expired"),
        AuthErr.VERIFICATION_CODE_RATE_LIMIT: (
            429,
            "Verification code rate limit exceeded",
        ),
        AuthErr.TOKEN_EXPIRED: (401, "Token expired"),
        AuthErr.TOKEN_INVALID: (401, "Token invalid"),
        AuthErr.REFRESH_TOKEN_REVOKED: (401, "Refresh token revoked"),
        AuthErr.TOTP_NOT_ENABLED: (400, "TOTP not enabled"),
        AuthErr.TOTP_ALREADY_ENABLED: (400, "TOTP already enabled"),
        AuthErr.TOTP_SETUP_REQUIRED: (400, "TOTP setup required"),
        AuthErr.TOTP_CODE_INVALID: (400, "TOTP code invalid"),
        AuthErr.RECOVERY_CODE_INVALID: (400, "Recovery code invalid"),
        AuthErr.RECOVERY_CODE_USED: (400, "Recovery code already used"),
        AuthErr.OAUTH_CANCELED: (400, "OAuth login canceled"),
        AuthErr.OAUTH_PROVIDER_ERROR: (502, "OAuth provider error"),
        AuthErr.OAUTH_EMAIL_TAKEN: (409, "OAuth email already taken"),
        AuthErr.PASSKEY_REGISTRATION_FAILED: (400, "Passkey registration failed"),
        AuthErr.PASSKEY_VERIFICATION_FAILED: (400, "Passkey verification failed"),
        AuthErr.RECOVERY_NOT_SUPPORTED: (400, "Recovery not supported"),
        AuthErr.RECOVERY_METHOD_UNAVAILABLE: (400, "Recovery method unavailable"),
        AuthErr.OAUTH_EMAIL_ALREADY_REGISTERED: (
            409,
            "OAuth email already registered",
        ),
        AuthErr.TOO_LARGE: (413, "Avatar exceeds upload size limit"),
        AuthErr.AVATAR_NOT_FOUND: (404, "Avatar not found"),
        AuthErr.EMAIL_TAKEN: (409, "Email already taken"),
        AuthErr.USERNAME_TAKEN: (409, "Username already taken"),
    }
)

register_unique_constraint("email", AuthErr.EMAIL_TAKEN)
register_unique_constraint("username", AuthErr.USERNAME_TAKEN)
