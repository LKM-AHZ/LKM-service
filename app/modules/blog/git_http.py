import asyncio
import base64
import binascii
import contextlib
import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_read_session, new_session
from app.modules.blog import backfill, git_svc
from app.modules.blog.models import BlogSeries
from auth.entities import User
from auth.seams import verifypwd

logger = logging.getLogger(__name__)

_session_factory = new_session  # 缝：测试可替换为注入会话

git_router = APIRouter(prefix="/blog/git", tags=["blog-git"])


def _decode_basic_auth(header: str) -> tuple[str, str] | None:
    """解析 Basic Authorization 头为 (username, password)。

    仅当 header 为合法 Basic 凭据时返回元组；base64 解码失败、非 UTF-8、
    缺少冒号等格式错误返回 None，由调用方回退匿名读。
    """
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return username, password


def _parse_git_response(stdout: bytes) -> Response:
    """解析 git http-backend 的 CGI 输出为 HTTP Response。"""
    header_end = stdout.find(b"\r\n\r\n")
    if header_end != -1:
        header_section = stdout[:header_end].decode("utf-8", errors="replace")
        response_body = stdout[header_end + 4 :]
    else:
        header_section = ""
        response_body = stdout

    status_code = 200
    content_type = "application/octet-stream"
    response_headers: dict[str, str] = {}

    for line in header_section.split("\r\n"):
        if line.lower().startswith("status:"):
            with contextlib.suppress(ValueError, IndexError):
                status_code = int(line.split(":", 1)[1].strip().split()[0])
        elif ":" in line:
            key, value = line.split(":", 1)
            response_headers[key.strip()] = value.strip()

    content_type = response_headers.pop("Content-Type", content_type)

    return Response(
        content=response_body,
        status_code=status_code,
        media_type=content_type,
        headers=response_headers,
    )


def _is_receive_pack(request: Request) -> bool:
    """仅 POST git-receive-pack 视为写（push）；读/其他不触发回填。"""
    if request.method.lower() != "post":
        return False
    path = request.url.path
    return "git-receive-pack" in path


async def _resolve_series(db: AsyncSession, repo_name: str) -> BlogSeries | None:
    """repo_name → blog_series；无记录（孤儿仓库）返回 None。"""
    return (
        (await db.execute(select(BlogSeries).where(BlogSeries.repo_name == repo_name)))
        .scalars()
        .first()
    )


async def _resolve_series_id(db: AsyncSession, repo_name: str) -> int | None:
    """repo_name → blog_series.id；无记录（孤儿仓库）返回 None。"""
    series = await _resolve_series(db, repo_name)
    return series.id if series is not None else None


async def _require_owner_for_push(
    db: AsyncSession, repo_name: str, request: Request
) -> User:
    """写路径(push)授权：校验 Basic Auth 身份，且该用户须为 repo 对应 blog_series 的属主。

    任一环节失败抛 HTTPException(401/403)，git http-backend 不会被调用，refs 不会被更新。
    - 无有效身份 → 401（git push 将其视为认证失败并提示）。
    - 仓库无归属（孤儿）或无属主匹配 → 403，防止越权写入他人/无人认领的仓库。
    """
    auth = request.headers.get("Authorization", "")
    creds = _decode_basic_auth(auth)
    if creds is None:
        # 缺失或格式非法的 Basic 凭据都算未认证
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="lkm-git"'},
        )
    username, password = creds
    # —— A5 凭证例外（合法直接认证，非展示）——
    # 此处按 username 直查 auth.User 并校验 hashed_password，是 git HTTP-Basic 写路径
    # 的**凭证直读**（须拿 auth 主模型 User 的 hashed_password 列交给 verifypwd 做口令
    # 比对），无法改经展示只读缝 auth.snapshot——后者刻意不含凭证列。故判定为合法
    # 直接认证例外，排除在缝迁移范围外；由 A5 在 import-linter 白名单把
    # `blog.git_http -> auth.models` 记为凭证例外即可。勿重构此读取、勿改其 User/select
    # 单行解析与 hashed_password 的 parse/serialize。
    user = (
        (await db.execute(select(User).where(User.username == username)))
        .scalars()
        .first()
    )
    if user is None or not await verifypwd(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    series = await _resolve_series(db, repo_name)
    if series is None or series.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not repository owner")
    return user


async def maybe_backfill_after_push(repo_name: str, old_sha: str | None) -> None:
    """http-backend 成功后调用；push 内容回填 blog_content，失败只记日志不阻塞。

    自建独立写会话并 commit（get_read_session 不 commit 会丢弃 flush）；隔离此写路径，
    http-backend 代理本身保持只读。孤儿仓库(无 blog_series)跳过。
    """
    db = await _session_factory()
    try:
        series_id = await _resolve_series_id(db, repo_name)
        if series_id is None:
            logger.warning(
                "blog push 命中孤儿仓库(无 blog_series), 跳过回填: %s", repo_name
            )
            return
        await backfill.backfill_series_from_git(
            db,
            repo_name,
            series_id,
            old_sha,
            push_at=datetime.now(UTC),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("blog push 回填失败(不阻塞): repo=%s", repo_name)
    finally:
        await db.close()


async def _stream_to_stdin(proc: asyncio.subprocess.Process, request: Request) -> None:
    """把请求体流式写入 git http-backend 的 stdin，写完关闭 stdin。"""
    assert proc.stdin is not None
    try:
        async for chunk in request.stream():
            proc.stdin.write(chunk)
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # 进程可能已提前退出（如 push 被拒），写失败可忽略
        pass
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()


# 同一 handler 同时服务 smart HTTP 的 GET(拉取)与 POST(推送)，
# 拆成两个路由并给独立 operation_id，避免 FastAPI 生成重复 Operation ID。
# 内部按 request.method 区分 is_push。
@git_router.post(
    "/{repo_name}.git/{rest:path}",
    operation_id="git_http_backend_post",
)
@git_router.get(
    "/{repo_name}.git/{rest:path}",
    operation_id="git_http_backend_get",
)
async def git_http_backend(
    repo_name: str,
    rest: str,
    request: Request,
    db: AsyncSession = Depends(get_read_session),
) -> Response:
    root = str(await asyncio.to_thread(os.path.abspath, settings.blog_repo_dir))
    repo_path = os.path.join(root, f"{repo_name}.git")

    if not await asyncio.to_thread(os.path.isdir, repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    is_push = _is_receive_pack(request)

    # 写路径(push)必须先确认身份 + 属主，未通过直接 401/403，绝不让 git http-backend 处理。
    # 读路径维持原语义（Basic 通过即设 REMOTE_USER，匿名回退公开读）。
    auth_user: User | None = None
    if is_push:
        auth_user = await _require_owner_for_push(db, repo_name, request)
    old_sha = None
    if is_push:
        old_sha = await asyncio.to_thread(git_svc.revparse_or_none, repo_name)

    env = os.environ.copy()
    env["GIT_PROJECT_ROOT"] = root
    env["GIT_HTTP_EXPORT_ALL"] = "1"
    env["PATH_INFO"] = f"/{repo_name}.git/{rest}"
    env["REQUEST_METHOD"] = request.method
    env["CONTENT_TYPE"] = request.headers.get("Content-Type", "")
    env["CONTENT_LENGTH"] = request.headers.get("Content-Length", "0")
    qs = str(request.url.query) if request.url.query else ""
    env["QUERY_STRING"] = qs

    # Basic Auth 校验（读权限门槛；会话由 FastAPI 依赖注入统一管理）
    if is_push:
        # push 鉴权已在 _require_owner_for_push 完成，落库用户名供 http-backend 标记远程用户
        assert auth_user is not None
        env["REMOTE_USER"] = auth_user.username
    else:
        auth = request.headers.get("Authorization", "")
        creds = _decode_basic_auth(auth)
        if creds is not None:
            username, password = creds
            user = (
                (await db.execute(select(User).where(User.username == username)))
                .scalars()
                .first()
            )
            if user and await verifypwd(password, user.hashed_password):
                env["REMOTE_USER"] = username
        elif auth.startswith("Basic "):
            # 仅格式错误（非 base64/缺冒号/非 UTF-8）回退匿名读；DB/内部错误不在此捕获，正常传播
            logger.warning("git Basic Auth 格式无效，回退为匿名（读公开）")

    # 用 asyncio 子进程 + request.stream() 流式喂入请求体，避免 request.body() 全量缓冲
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "http-backend",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500, detail="git executable not found"
        ) from None

    try:
        async with asyncio.timeout(120):
            await _stream_to_stdin(proc, request)
            stdout, _ = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=504, detail="Git operation timed out") from None

    resp = _parse_git_response(stdout)
    if is_push and resp.status_code < 400:
        await maybe_backfill_after_push(repo_name, old_sha)
    return resp
