"""compose 的**语义**校验：`docker compose config` 查不出来的那部分。

`docker compose config --quiet` 只能保证「YAML 合法 + 变量插值成功」。下面这几类错误它能
原样放行，而它们都会在部署时以难以定位的方式失败：

1. **bind 挂载源写错/改路径**：Docker 发现宿主路径不存在时会**静默创建一个空目录**并挂进去。
   于是 `./deploy/prometheus/promuthues.yml` 这种拼写错误不会报错，只是容器里读到空目录
   （Prometheus 起来后 "no config file"、或更糟：空配置跑起来但规则全无）。
2. **服务既无 image 也无 build**：compose 会报错，但如果镜像名被写进注释里就静默消失。
3. **引用了未声明的命名卷**：compose 其实会报错，这里再钉一遍是为了错误信息更早更明确。

判定口径（为什么用 "被 git 跟踪" 而不是 "文件存在"）：
    本地可能存在未跟踪的运行时产物（`deploy/apisix/rendered/`、`deploy/jwt/keys/`、
    子模块里的 `LKM-bot/data/`）。用 `exists()` 判断会在本地通过、在 CI 首次检出时失败。
    用「被 git 跟踪 或 落在已声明的运行时目录下」才能在两种环境下给出同样的结论。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"

#: 运行时才生成、因此**不**被 git 跟踪的挂载源目录。
#: 新增一项必须同时说明由谁生成——否则它就成了"随便写个路径也不会红"的后门。
_RUNTIME_CREATED: tuple[tuple[str, str], ...] = (
    ("deploy/apisix/rendered", "apisix-render 容器写入（网关配置渲染产物）"),
    (
        "deploy/alertmanager/rendered",
        "alertmanager-render 容器写入（告警配置渲染产物）",
    ),
    ("deploy/jwt/keys", "sh deploy/jwt/gen-keys.sh 生成（RS256 密钥，见 .gitignore）"),
    ("LKM-bot/data", "bot 面板运行时数据目录（子模块内，随容器写入）"),
)


def _compose() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _tracked_paths() -> set[str]:
    """根仓库当前跟踪的全部路径（gitlink 的子模块只列出目录本身）。

    判据依赖 git：在没有 .git 的环境（导出 tar 包、镜像内源码）无法判断"是否被跟踪"，
    这时**跳过**而不是硬失败——硬失败会把无关环境搞得满屏红，而这件事在 CI 里
    始终可用（actions/checkout 必然带 .git）。
    """
    proc = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"无法读取 git 跟踪清单（无 .git？）：{proc.stderr.strip()[:160]}")
    return {p for p in proc.stdout.split("\0") if p}


def _is_tracked(source: str, tracked: set[str]) -> bool:
    """source 本身被跟踪，或它是个含有被跟踪文件的目录。"""
    return source in tracked or any(p.startswith(source + "/") for p in tracked)


def _repo_bind_sources() -> list[tuple[str, str]]:
    """返回 [(service, 仓库内相对路径)]，只看 `./` / `../` 开头的 bind 挂载。"""
    result: list[tuple[str, str]] = []
    for name, service in _compose()["services"].items():
        for mount in service.get("volumes", []) or []:
            src = mount if isinstance(mount, str) else mount.get("source", "")
            src = src.split(":")[0]
            if not src.startswith("./"):
                continue
            result.append((name, src[2:]))
    return result


def _is_runtime_created(source: str) -> bool:
    return any(
        source == prefix or source.startswith(prefix + "/")
        for prefix, _ in _RUNTIME_CREATED
    )


def test_bind_sources_are_tracked_or_runtime_generated() -> None:
    """每个 bind 挂载源要么被 git 跟踪，要么是已声明的运行时目录。

    这是本文件最重要的一条：Docker 对不存在的 bind 源会**静默创建空目录**，
    路径写错不会报错，只会让容器读到空内容。
    """
    tracked = _tracked_paths()
    unknown = [
        f"{service}: ./{source}"
        for service, source in _repo_bind_sources()
        if not _is_tracked(source, tracked) and not _is_runtime_created(source)
    ]
    assert not unknown, (
        "这些 bind 挂载源既不存在于 git 跟踪清单，也不在运行时生成目录白名单里：\n  "
        + "\n  ".join(sorted(set(unknown)))
        + "\nDocker 对缺失的 bind 源不会报错，而是创建空目录——部署时表现为"
        "\n「容器起来了但读不到配置」，极难定位。拼写检查或补进 _RUNTIME_CREATED。"
    )


def test_runtime_allowlist_is_not_stale() -> None:
    """白名单也要防腐烂：已无人引用的条目会成为未来同类拼写错误的免检通道。"""
    used = {source for _, source in _repo_bind_sources()}
    stale = [
        prefix
        for prefix, _ in _RUNTIME_CREATED
        if not any(s == prefix or s.startswith(prefix + "/") for s in used)
    ]
    assert not stale, f"_RUNTIME_CREATED 里这些目录已无人挂载，应删除：{stale}"


def test_every_service_has_image_or_build() -> None:
    """服务必须能确定镜像来源（漏写 image 会让 compose 报错，但报错点离家很远）。"""
    bad = [
        name
        for name, service in _compose()["services"].items()
        if not service.get("image") and not service.get("build")
    ]
    assert not bad, f"这些服务既没有 image 也没有 build：{bad}"


def test_named_volumes_are_declared() -> None:
    """引用了命名卷就必须在顶层 `volumes:` 声明（compose 会报错，这里让定位更直接）。"""
    doc = _compose()
    declared = set(doc.get("volumes", {}) or {})
    referenced: set[str] = set()
    for service in doc["services"].values():
        for mount in service.get("volumes", []) or []:
            if not isinstance(mount, str):
                continue
            source = mount.split(":")[0]
            # 相对路径是 bind；其余形如 `name:/path` 的是命名卷
            if source.startswith((".", "/")):
                continue
            if ":" in mount:
                referenced.add(source)
    assert referenced - declared == set(), (
        f"这些命名卷被引用但未在顶层 volumes 声明：{sorted(referenced - declared)}"
    )
