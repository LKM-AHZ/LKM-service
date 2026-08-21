import os
import shutil
import subprocess
from typing import Any, cast

import yaml

from app.core.config import settings
from app.core.err import BizError, CommonErr
from app.modules.blog.errors import BlogErr


def _repo_path(repo_name: str) -> str:
    base = os.path.abspath(settings.blog_repo_dir)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{repo_name}.git")


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
    except FileNotFoundError:
        raise BizError(BlogErr.GIT_ERROR, "git executable not found") from None


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
        detail = e.stderr.decode("utf-8", errors="replace").strip() or str(e)
        raise BizError(BlogErr.GIT_ERROR, detail) from e
    return path


def delete_repo(repo_name: str) -> None:
    path = _repo_path(repo_name)
    if os.path.exists(path):
        shutil.rmtree(path)


def ensure_repo_has_commits(repo_name: str) -> bool:
    try:
        _run(repo_name, "rev-parse", "HEAD")
        return True
    except BizError:
        return False


def read_file(repo_name: str, filepath: str) -> str:
    filepath = filepath.lstrip("/")
    if ".." in filepath.split("/"):
        raise BizError(CommonErr.INVALID_INPUT, "Invalid file path")
    return _run(repo_name, "show", f"HEAD:{filepath}")


def parse_frontmatter(content: str) -> dict[str, Any]:
    """从 MDX 首部 YAML 块提取元数据；无 frontmatter 返回 {}。"""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
        if isinstance(data, dict):
            return cast("dict[str, Any]", data)
        return {}
    except yaml.YAMLError:
        return {}


def revparse_or_none(repo_name: str) -> str | None:
    """返回 refs/heads/master 当前 SHA；仓库无提交时返回 None。"""
    if not ensure_repo_has_commits(repo_name):
        return None
    out = _run(repo_name, "rev-parse", "HEAD").strip()
    return out or None


def diff_tree_names(
    repo_name: str, old_sha: str | None, new_sha: str
) -> list[str]:
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
