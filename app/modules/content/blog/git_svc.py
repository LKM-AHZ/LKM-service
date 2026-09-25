import os
import re
import shutil
import subprocess
from typing import Any, cast

import yaml

from app.core.config import settings
from app.core.err import BizError, CommonErr
from app.modules.content.blog.errors import BlogErr

# repo_name 来自用户输入（BlogSeriesCreate.repo_name 只限长度），拼进路径前必须收敛字符集：
# 不加限制时 "../../tmp/evil" 会让 init_bare_repo 在仓库根外建目录、delete_repo 直接
# rmtree 根外目录。首字符限字母数字，避免 ".hidden"/"-flag" 形态。
_SAFE_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _repo_path(repo_name: str) -> str:
    """repo_name → 裸仓库绝对路径（含路径穿越防护，所有 git 操作共用此入口）。"""
    if not _SAFE_REPO_NAME.match(repo_name) or ".." in repo_name:
        raise BizError(CommonErr.INVALID_INPUT, "Invalid repository name")
    base = os.path.abspath(settings.blog_repo_dir)
    os.makedirs(base, exist_ok=True)
    path = os.path.abspath(os.path.join(base, f"{repo_name}.git"))
    # 双保险：字符集已挡住穿越，仍校验落点在 base 下（防平台特有的归一化形式）
    if os.path.commonpath([base, path]) != base:
        raise BizError(CommonErr.INVALID_INPUT, "Invalid repository name")
    return path


class GitInfraError(BizError):
    """git 基础设施故障（缺可执行文件/超时/仓库目录缺失）。

    errcode 仍是 ``BlogErr.GIT_ERROR``（既有调用方的 ``except BizError`` 照旧生效），
    但把「git 正常运行并返回非零」与「根本没跑起来」区分开——例如 ``revparse_or_none``
    只有在后者才该向上抛，而不是把坏仓库当成空仓库静默跳过回填。
    """


def _run(
    repo_name: str,
    *args: str,
    input_data: bytes | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """跑裸仓库 git 命令，失败抛 BizError(GIT_ERROR)。

    默认返回原始（不 strip）stdout，需要去首尾空白的调用点自行 ``.strip()``。
    """
    path = _repo_path(repo_name)
    if not os.path.isdir(path):
        # 目录缺失/被删：不是「空仓库」，不能让上层把失败当无提交静默吞掉
        raise GitInfraError(BlogErr.GIT_ERROR, f"Repository missing: {repo_name}")
    cmd = ["git", "--git-dir", path, *list(args)]
    try:
        result = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            timeout=30,
            check=True,
            env=env,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        detail = e.stderr.decode("utf-8", errors="replace").strip() or str(e)
        raise BizError(BlogErr.GIT_ERROR, detail) from e
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired 属 SubprocessError 而非 CalledProcessError，不显式接住会绕过
        # 本函数的 GIT_ERROR 契约以裸异常冒给调用方
        raise GitInfraError(BlogErr.GIT_ERROR, "git command timed out") from e
    except FileNotFoundError:
        raise GitInfraError(BlogErr.GIT_ERROR, "git executable not found") from None


def init_bare_repo(repo_name: str) -> str:
    path = _repo_path(repo_name)
    if os.path.exists(path):
        raise BizError(BlogErr.GIT_ERROR, f"Repository '{repo_name}' already exists")
    try:
        subprocess.run(
            ["git", "init", "--bare", path],
            capture_output=True,
            timeout=10,
            check=True,
        )
        subprocess.run(
            ["git", "--git-dir", path, "config", "http.receivepack", "true"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        # 半初始化回滚：init 成功但 config 失败（或超时）时目录已存在，而入口的
        # exists 检查会让同名仓库永远无法重建，用户再也建不了这个系列
        shutil.rmtree(path, ignore_errors=True)
        detail = e.stderr.decode("utf-8", errors="replace").strip() or str(e)
        raise BizError(BlogErr.GIT_ERROR, detail) from e
    except subprocess.TimeoutExpired as e:
        shutil.rmtree(path, ignore_errors=True)
        raise GitInfraError(BlogErr.GIT_ERROR, "git init timed out") from e
    except FileNotFoundError:
        shutil.rmtree(path, ignore_errors=True)
        raise GitInfraError(BlogErr.GIT_ERROR, "git executable not found") from None
    return path


def delete_repo(repo_name: str) -> None:
    path = _repo_path(repo_name)
    if os.path.exists(path):
        shutil.rmtree(path)


def ensure_repo_has_commits(repo_name: str) -> bool:
    """仓库是否已有提交；空仓库 False，基础设施故障照旧抛出（见 revparse_or_none）。"""
    return revparse_or_none(repo_name) is not None


def read_file(repo_name: str, filepath: str) -> str:
    filepath = filepath.lstrip("/")
    if ".." in filepath.split("/"):
        raise BizError(CommonErr.INVALID_INPUT, "Invalid file path")
    return _run(repo_name, "show", f"HEAD:{filepath}")


def parse_frontmatter(content: str) -> dict[str, Any]:
    """从 MDX 首部 YAML 块提取元数据；无 frontmatter 返回 {}。

    结束分隔符必须是**独占一行**的 ``---``：按裸子串切会让 YAML 值里出现的 ``---``
    （标题、块标量）被当成块尾，元数据被截断后 yaml 解析失败而静默丢弃。
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next(
        (i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end is None:
        return {}
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
        if isinstance(data, dict):
            return cast("dict[str, Any]", data)
        return {}
    except yaml.YAMLError:
        return {}


def revparse_or_none(repo_name: str) -> str | None:
    """返回 HEAD 当前 SHA；仓库无提交时返回 None（基础设施故障照旧抛出）。

    单次 ``rev-parse`` 同时完成「有没有提交」与取值，省掉先 ensure 再 revparse 的
    两次起进程。只有「git 正常跑完但退出非零」（空仓库没有 HEAD）才算无提交；
    缺 git/超时/仓库目录缺失等经 ``GitInfraError`` 上抛，不被静默当成空仓库。
    """
    try:
        out = _run(repo_name, "rev-parse", "HEAD").strip()
    except GitInfraError:
        raise
    except BizError:
        return None
    return out or None


def diff_tree_names(repo_name: str, old_sha: str | None, new_sha: str) -> list[str]:
    """返回 old_sha..new_sha 之间变更文件的路径列表（重命名取新路径）。

    old_sha 为空（首 push/空仓库前置）时给出 new_sha 树里全部文件路径。

    调用方需注意并发：diff 与后续 read_file("HEAD:path") 之间若恰有另一 push 落库，
    HEAD:path 可能读到比本 diff 区间更新的内容；receive-pack 同步返回 + DB 权威的
    push_at/updated_at 规则可自愈，这里仅记并发窗口。
    """
    if old_sha:
        args = ["diff-tree", "--name-only", "-r", "--no-commit-id", old_sha, new_sha]
    else:
        args = ["ls-tree", "-r", "--name-only", new_sha]
    out = _run(repo_name, *args)
    return [line.strip() for line in out.splitlines() if line.strip()]
