import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import settings
from app.modules.blog import git_svc


@pytest.fixture
def repo_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    """自定义 test repo 根目录：tmp_path + 覆写 settings.blog_repo_dir（不 mock _run）。"""
    path = str(tmp_path / "blog_repos")
    monkeypatch.setattr(settings, "blog_repo_dir", path)
    yield path
    monkeypatch.setattr(settings, "blog_repo_dir", "blog_repos")


def _make_repo(repo_dir: str, repo_name: str, files: dict[str, str]) -> str:
    """用 git plumbing 建有个首提交的 bare repo，返回 HEAD SHA。

    先 ``git init --bare`` 保证对象库存在——Windows 上 git 的
    ``hash-object -w --git-dir <不存在目录>`` 会报 "not a git repository"
    （Linux git 会自动初始化，Windows 不会）。
    """
    bare = f"{repo_dir}/{repo_name}.git"
    # 显式 -b master：git init 的 HEAD 默认分支受全局 init.defaultBranch 影响，
    # 而下面 update-ref 固定写 refs/heads/master；不显式指定时在 main 默认的机器上
    # HEAD 会悬空、rev-parse HEAD 取到空。
    subprocess.run(
        ["git", "init", "--bare", "-b", "master", bare],
        capture_output=True,
        check=True,
    )
    blobs = {}
    for path, content in files.items():
        blob = (
            subprocess.run(
                ["git", "--git-dir", bare, "hash-object", "-w", "--stdin"],
                input=content.encode(),
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        blobs[path] = blob
    entries = [f"100644 blob {blobs[path]}\t{path.split('/')[-1]}" for path in files]
    tree = (
        subprocess.run(
            ["git", "--git-dir", bare, "mktree"],
            input="\n".join(entries).encode(),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    commit = (
        subprocess.run(
            ["git", "--git-dir", bare, "commit-tree", tree, "-m", "init"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "--git-dir", bare, "update-ref", "refs/heads/master", commit],
        check=True,
    )
    return commit


def test_revparse_or_none_empty(repo_dir: str):
    name = "empty-repo"
    git_svc.init_bare_repo(name)
    assert git_svc.revparse_or_none(name) is None


def test_revparse_or_none_after_commit(repo_dir: str):
    _make_repo(repo_dir, "abc", {"a.md": "x"})
    sha = git_svc.revparse_or_none("abc")
    assert sha is not None and len(sha) == 40


def test_diff_tree_names_new_file(repo_dir: str):
    _make_repo(repo_dir, "abc", {"a.md": "x"})
    # 第二个提交携带 a.md 并新增 b.md，diff 应只报 b.md。
    # （不用 write_file：其用临时空 index，新提交树里只含新文件，会丢掉 a.md。）
    first = git_svc.revparse_or_none("abc")
    _make_repo(repo_dir, "abc", {"a.md": "x", "b.md": "y"})
    second = git_svc.revparse_or_none("abc")
    assert first is not None and second is not None
    changed = git_svc.diff_tree_names("abc", first, second)
    assert changed == ["b.md"]
