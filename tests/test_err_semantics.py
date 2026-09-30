"""错误语义单测（蓝图 §6.1）：新增错误码取值、唯一约束语义化映射、429/503 的 Retry-After。

唯一约束的取数是**真实驱动形状**：SQLAlchemy 的 asyncpg 方言把约束名藏在
``exc.orig.driver_exception``（``.orig`` / ``__cause__`` 同一对象）上，``exc.orig`` 本身只透出
sqlstate；这里用复刻该形状的替身把映射钉住，不依赖真库（真库探针见实现注释）。
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from app.modules.content.blog.errors import BlogErr
from app.modules.content.boards.errors import BoardErr
from app.modules.content.errors import ContentErr
from app.modules.interaction.errors import FollowErr, InteractionErr
from app.modules.notification.errors import NotificationErr
from app.modules.projects.errors import ProjectErr
from core.db.session import (
    _is_unique_violation,
    _unique_constraint_name,
    unique_violation_errcode,
)
from core.err import (
    _RETRY_AFTER_SECONDS,
    NS_AUTH,
    NS_COMMON,
    NS_INTERACTION,
    AuthErr,
    BizError,
    CommonErr,
    err_info,
    resp_json,
)


class _AsyncpgUniqueViolation(Exception):
    """asyncpg 原生异常：约束名在自身属性上，无 pgcode（只有 sqlstate）。"""

    def __init__(self, constraint_name: str | None, sqlstate: str = "23505") -> None:
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


class _SqlalchemyAsyncpgWrapper(Exception):
    """SQLAlchemy asyncpg 适配层异常：约束名只在被包的驱动异常上。"""

    def __init__(self, inner: _AsyncpgUniqueViolation) -> None:
        self.sqlstate = "23505"
        self.pgcode = "23505"
        self.driver_exception = inner
        self.orig = inner
        self.__cause__ = inner


class _PsycopgDiag:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _PsycopgError(Exception):
    """psycopg 形状：约束名在 ``.diag.constraint_name``。"""

    def __init__(self, constraint_name: str | None, sqlstate: str = "23505") -> None:
        self.pgcode = sqlstate
        self.diag = _PsycopgDiag(constraint_name)


def _ie(orig: Exception | None) -> IntegrityError:
    return IntegrityError("INSERT", {}, orig)  # ty: ignore[invalid-argument-type]


class TestNewErrorCodes:
    """值即线上契约：数值与 HTTP 状态都按蓝图规划固定。"""

    def should_expose_version_conflict(self) -> None:
        assert NS_COMMON.err(7) == CommonErr.VERSION_CONFLICT
        assert err_info(CommonErr.VERSION_CONFLICT) == (409, "Version conflict")

    def should_expose_auth_email_and_username_taken(self) -> None:
        assert NS_AUTH.err(28) == AuthErr.EMAIL_TAKEN
        assert NS_AUTH.err(29) == AuthErr.USERNAME_TAKEN
        assert err_info(AuthErr.EMAIL_TAKEN) == (409, "Email already taken")
        assert err_info(AuthErr.USERNAME_TAKEN) == (409, "Username already taken")

    def should_expose_duplicate_like(self) -> None:
        assert NS_INTERACTION.err(2) == InteractionErr.DUPLICATE_LIKE
        assert err_info(InteractionErr.DUPLICATE_LIKE) == (409, "Duplicate like")

    def should_expose_protocol_and_conflict_codes(self) -> None:
        """协议型端点与唯一冲突兜底的通用码（蓝图 §6.1：所有端点走统一信封、冲突 409）。"""
        assert err_info(CommonErr.UNAUTHORIZED) == (401, "Unauthorized")
        assert err_info(CommonErr.NOT_FOUND) == (404, "Not found")
        assert err_info(CommonErr.BAD_REQUEST) == (400, "Bad request")
        assert err_info(CommonErr.CONFLICT) == (409, "Resource conflict")


class TestConstraintNameExtraction:
    def should_read_name_from_sqlalchemy_asyncpg_wrapper(self) -> None:
        exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("uq_users_email")))
        assert _unique_constraint_name(exc) == "uq_users_email"

    def should_read_name_from_raw_asyncpg_error(self) -> None:
        exc = _ie(_AsyncpgUniqueViolation("content_likes_pkey"))
        assert _unique_constraint_name(exc) == "content_likes_pkey"

    def should_read_name_from_psycopg_diag(self) -> None:
        exc = _ie(_PsycopgError("uq_users_username"))
        assert _unique_constraint_name(exc) == "uq_users_username"

    def should_return_none_without_orig(self) -> None:
        assert _unique_constraint_name(_ie(None)) is None


class TestUniqueViolationErrcode:
    def should_map_email(self) -> None:
        exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("_probe_uq_email_key")))
        assert _is_unique_violation(exc) is True
        assert unique_violation_errcode(exc) == AuthErr.EMAIL_TAKEN

    def should_map_username(self) -> None:
        exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("uq_users_username")))
        assert unique_violation_errcode(exc) == AuthErr.USERNAME_TAKEN

    def should_map_like_by_substring(self) -> None:
        # content_likes 是复合主键，create_all 下隐式名形如 content_likes_pkey，含 "like"
        exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("content_likes_pkey")))
        assert unique_violation_errcode(exc) == InteractionErr.DUPLICATE_LIKE

    def should_map_slug(self) -> None:
        exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("ix_content_slug")))
        assert unique_violation_errcode(exc) == ContentErr.SLUG_TAKEN

    def should_map_follow_pairs(self) -> None:
        """关注两表的复合唯一键 → 「已关注」，而不是笼统的注册冲突。"""
        for name in ("uq_user_follows_pair", "uq_board_follows_pair"):
            exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation(name)))
            assert unique_violation_errcode(exc) == FollowErr.DUPLICATE_FOLLOW
        assert err_info(FollowErr.DUPLICATE_FOLLOW)[0] == 409

    def should_map_notification_token(self) -> None:
        exc = _ie(
            _SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("uq_notification_token"))
        )
        assert unique_violation_errcode(exc) == NotificationErr.DUPLICATE_TOKEN

    def should_map_pending_project_application(self) -> None:
        exc = _ie(
            _SqlalchemyAsyncpgWrapper(
                _AsyncpgUniqueViolation("uq_project_applications_pending")
            )
        )
        assert unique_violation_errcode(exc) == ProjectErr.DUPLICATE_APPLICATION

    def should_map_blog_repo_name(self) -> None:
        exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("repo_name")))
        assert unique_violation_errcode(exc) == BlogErr.REPO_NAME_TAKEN

    def should_fall_back_to_neutral_conflict(self) -> None:
        """未登记的业务约束回落**中性** 409，而不是 auth 域的「用户名或邮箱已注册」。"""
        exc = _ie(_SqlalchemyAsyncpgWrapper(_AsyncpgUniqueViolation("uq_unknown_thing")))
        assert unique_violation_errcode(exc) == CommonErr.CONFLICT

    def should_fall_back_when_name_missing(self) -> None:
        assert unique_violation_errcode(_ie(None)) == CommonErr.CONFLICT


class TestBizErrorHeaders:
    """``BizError`` 可携带响应头（协议端点的挑战头，如 git smart-HTTP 的 WWW-Authenticate）。"""

    def should_keep_headers_on_error_origin(self) -> None:
        err = BizError(
            CommonErr.UNAUTHORIZED,
            "Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="lkm-git"'},
        )
        assert err.headers == {"WWW-Authenticate": 'Basic realm="lkm-git"'}

    def should_default_headers_to_none(self) -> None:
        assert BizError(CommonErr.FORBIDDEN, "nope").headers is None

    def should_forward_headers_through_resp_json(self) -> None:
        """全局 handler 的落点：``resp_json`` 必须把调用方给的头带出去。"""
        resp = resp_json(
            CommonErr.UNAUTHORIZED, headers={"WWW-Authenticate": 'Basic realm="x"'}
        )
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == 'Basic realm="x"'


class TestRetryAfterHeader:
    def should_attach_retry_after_on_429(self) -> None:
        resp = resp_json(AuthErr.VERIFICATION_CODE_RATE_LIMIT)
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == str(_RETRY_AFTER_SECONDS)

    def should_attach_retry_after_on_board_daily_limit(self) -> None:
        resp = resp_json(BoardErr.DAILY_POST_LIMIT_REACHED)
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == str(_RETRY_AFTER_SECONDS)

    def should_attach_retry_after_on_503(self) -> None:
        resp = resp_json(CommonErr.UNAVAILABLE)
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == str(_RETRY_AFTER_SECONDS)

    def should_let_caller_header_win(self) -> None:
        resp = resp_json(
            AuthErr.VERIFICATION_CODE_RATE_LIMIT, headers={"Retry-After": "120"}
        )
        assert resp.headers["retry-after"] == "120"

    def should_be_case_insensitive_about_caller_header(self) -> None:
        resp = resp_json(
            AuthErr.VERIFICATION_CODE_RATE_LIMIT, headers={"retry-after": "5"}
        )
        assert resp.headers["retry-after"] == "5"

    def should_not_attach_on_409(self) -> None:
        resp = resp_json(AuthErr.EMAIL_TAKEN)
        assert resp.status_code == 409
        assert "retry-after" not in resp.headers
