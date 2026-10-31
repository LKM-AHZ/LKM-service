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


def _clamav_dependents() -> dict[str, str]:
    """所有把 clamav 写进 depends_on 的服务 → 其依赖条件。"""
    deps: dict[str, str] = {}
    for name, service in _compose()["services"].items():
        depends = service.get("depends_on") or {}
        if isinstance(
            depends, list
        ):  # 短语法 `depends_on: [a, b]` 等价 service_started
            if "clamav" in depends:
                deps[name] = "service_started"
            continue
        for dep, spec in depends.items():
            if dep == "clamav":
                deps[name] = (spec or {}).get("condition", "")
    return deps


def test_clamav_dependency_is_not_gated_on_unverifiable_probe() -> None:
    """clamav 的依赖条件必须是 `service_started`，且它自己声明了 healthcheck。

    这是评审阻断项 R1 的回归锁，两侧都说清楚为什么要这样组合：

    - **没有 healthcheck 的服务不能被 `service_healthy` 依赖**：Compose v2 在**装配期**直接
      失败（`dependency failed to start: ... has no healthcheck configured`），整栈起不来，
      而报错文本与「病毒扫描」业务毫无关联，排查成本极高。
    - **反过来，也不拿探针当启动闸门**：镜像 `clamav/clamav:stable` 是浮动 tag，探针二进制
      在仓库内无法锁定；一旦上游镜像改名/挪路径，「整栈能不能起来」就取决于一个外部文件。
      扫描未就绪由应用层 fail-closed 兜底（上传返回 `FileErr.SCAN_UNAVAILABLE`）。
    - 但探针本身要**声明**：`docker compose ps` / 监控靠它区分「进程还在」与「clamd 真在应答」。

    想收紧成 `service_healthy` 时，请先在目标机确认 `docker compose ps clamav` 达到 healthy，
    再连同本测试一起改——否则等于把一条未验证的外部假设写进启动路径。
    """
    services = _compose()["services"]
    assert "healthcheck" in services["clamav"], (
        "clamav 必须显式声明 healthcheck：其它服务若用 service_healthy 依赖它，"
        "没有 healthcheck 会在装配期直接失败"
    )
    dependents = _clamav_dependents()
    assert dependents, "没有任何服务依赖 clamav？上传路径的扫描依赖应当是真实存在的"
    gated = {
        name: cond for name, cond in dependents.items() if cond != "service_started"
    }
    assert not gated, (
        "这些服务把 clamav 当启动闸门，但探针路径在仓库内未锁定（闭包 R1）："
        f"{gated}；请改为 service_started，扫描不可用交给应用层 fail-closed"
    )


def test_minio_init_creates_the_configured_bucket() -> None:
    """全新主机首启必须自动建桶（R6）：MinIO 不自建桶，后端也没有建桶逻辑。

    缺桶时文件库与所有头像 404，而 DEPLOYMENT 把「手工 mc mb」写成「必做」，说明真踩过。
    `minio-init` 是**一次性 job**（`restart: 'no'`），用 `mc mb --ignore-existing` 做幂等，
    桶名与 backend 的 `LKM_S3_BUCKET` 同源——两处各自写字面量就会再次漂移。
    """
    services = _compose()["services"]
    assert "minio-init" in services, (
        "缺少 minio-init 一次性服务：新部署首启时桶不存在，上传与头像会全部 404"
    )
    job = services["minio-init"]
    assert str(job.get("restart")) == "no", (
        "minio-init 是一次性 job，restart 必须是 'no'"
    )
    assert (job.get("depends_on") or {}).get("minio", {}).get("condition") == (
        "service_healthy"
    ), "minio-init 必须等 minio 健康后再建桶（否则连接被拒、退出码非 0）"

    script = "\n".join(str(part) for part in job.get("entrypoint", []))
    assert "mc mb" in script and "--ignore-existing" in script, (
        "minio-init 必须用 `mc mb --ignore-existing` 幂等建桶，重复运行不能失败"
    )

    job_bucket = (job.get("environment") or {}).get("LKM_S3_BUCKET")
    backend_bucket = (services["backend"].get("environment") or {}).get("LKM_S3_BUCKET")
    assert job_bucket == backend_bucket, (
        "minio-init 与 backend 的桶名必须同源（同一个 ${LKM_S3_BUCKET:-...} 表达式），"
        f"当前 job={job_bucket!r} backend={backend_bucket!r}"
    )
