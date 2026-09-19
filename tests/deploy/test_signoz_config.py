"""批 6 SigNoz 自托管静态验收：compose profile / 配置单一来源 / 两处真机踩过的坑。

解析根 `docker-compose.yml` 与 `deploy/signoz/*`（vendor 自 SigNoz 官方 v0.128.0），
断言部署口径与「改一边即红」的耦合点。运行时连通性由手记的验收步骤守（见路线图 §8）。

**两条断言是拿真机调试换来的，不是形式检查**：

1. `signoz-clickhouse` 必须在专用网络上带别名 `clickhouse`——SigNoz 官方配置把主机名
   写死为 `clickhouse`（otel-collector-config.yaml 的 DSN、cluster.xml 的 distributed DDL）。
   缺别名时 ClickHouse 起得来、migrator 却卡在 `Creating databases`（DNS 解析不到），
   表现为「容器 healthy 但整栈永久起不来」。
2. `signoz-init-clickhouse` 拉 UDF 失败必须**不阻断**——否则受限网络下 init 非零退出，
   `depends_on: service_completed_successfully` 会把整个 SigNoz 卡死（ClickHouse 根本不启动）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"
_SIGNOZ = _ROOT / "deploy" / "signoz"
_K8S_SIGNOZ = _ROOT / "deploy" / "k8s" / "signoz"

# 本 profile 的六个服务：四个长驻 + 两个一次性
_SIGNOZ_SERVICES = {
    "signoz-init-clickhouse",
    "signoz-zookeeper",
    "signoz-clickhouse",
    "signoz-migrator",
    "signoz-otel-collector",
    "signoz",
}
_ONESHOT = {"signoz-init-clickhouse", "signoz-migrator"}


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def _services() -> dict[str, dict]:
    return _compose()["services"]


# ─────────────────────── profile 与生命周期 ───────────────────────


def test_all_signoz_services_are_in_the_signoz_profile() -> None:
    services = _services()
    for name in _SIGNOZ_SERVICES:
        assert name in services, name
        assert services[name].get("profiles") == ["signoz"], name
    # 不在 signoz profile 里的服务不应被误加进来
    others = [
        n
        for n, s in services.items()
        if n not in _SIGNOZ_SERVICES and "signoz" in (s.get("profiles") or [])
    ]
    assert others == [], others


def test_restart_policy_follows_repo_convention() -> None:
    """长驻服务走 LKM_RESTART_POLICY（默认 no=不自动拉起）；一次性服务恒 no。"""
    services = _services()
    for name in _SIGNOZ_SERVICES:
        restart = str(services[name].get("restart", ""))
        if name in _ONESHOT:
            assert restart == "no", f"{name} 是一次性服务，不该自动重启：{restart}"
        else:
            assert "LKM_RESTART_POLICY" in restart, f"{name} 未接入统一开关：{restart}"


def test_signoz_profile_is_independent_from_clickhouse_and_otel_profiles() -> None:
    """三套 profile 互不共用实例：日志分析 CH ≠ SigNoz CH；两个 collector 也各是各的。"""
    services = _services()
    logging_ch = services["clickhouse"]
    assert logging_ch.get("profiles") == ["clickhouse"]
    assert logging_ch["image"] != services["signoz-clickhouse"]["image"]
    # 应用侧 collector 保持 otel profile 且仍只 debug+otlphttp（由下面的配置断言细查）
    assert services["otel-collector"].get("profiles") == ["otel"]


# ─────────────────────── 网络隔离与别名（真机坑 1） ───────────────────────


def test_signoz_has_own_network_and_clickhouse_alias() -> None:
    services = _services()
    compose = _compose()
    assert "signoz" in compose["networks"], "缺少 SigNoz 专用网络"
    # 专用网络上必须能解析出官方配置写死的主机名
    ch_net = services["signoz-clickhouse"]["networks"]
    assert isinstance(ch_net, dict), "signoz-clickhouse 的 networks 需为映射以挂别名"
    assert "clickhouse" in (ch_net["signoz"].get("aliases") or []), (
        "signoz-clickhouse 缺 `clickhouse` 网络别名：migrator 会卡在 Creating databases"
    )
    zk_net = services["signoz-zookeeper"]["networks"]
    assert "zookeeper-1" in (zk_net["signoz"].get("aliases") or []), (
        "signoz-zookeeper 缺 `zookeeper-1` 别名：cluster.xml 里的 zookeeper 主机名解析不到"
    )


def test_signoz_clickhouse_is_not_on_lkm_network() -> None:
    """SigNoz 的 CH 不得接入 lkm：否则与日志分析 CH 争 `clickhouse` 这个 DNS 名。"""
    networks = _services()["signoz-clickhouse"]["networks"]
    assert set(networks) == {"signoz"}


def test_app_collector_can_reach_signoz_collector() -> None:
    """应用侧 collector 在 lkm 上，故 signoz-otel-collector 必须双挂。"""
    networks = _services()["signoz-otel-collector"]["networks"]
    assert set(networks) == {"lkm", "signoz"}


# ─────────────────────── 配置单一来源 ───────────────────────


def _mount_sources(service: dict) -> list[str]:
    out = []
    for vol in service.get("volumes", []):
        text = vol if isinstance(vol, str) else ""
        if text.startswith("./deploy/signoz/"):
            out.append(text.split(":")[0])
    return out


def test_mounts_reference_vendored_configs() -> None:
    services = _services()
    ch_mounts = _mount_sources(services["signoz-clickhouse"])
    for f in ("config.xml", "users.xml", "custom-function.xml", "cluster.xml"):
        assert f"./deploy/signoz/clickhouse/{f}" in ch_mounts, f
        assert (_SIGNOZ / "clickhouse" / f).is_file(), f
    otel_mounts = _mount_sources(services["signoz-otel-collector"])
    assert "./deploy/signoz/otel-collector-config.yaml" in otel_mounts
    assert "./deploy/signoz/otel-collector-opamp-config.yaml" in otel_mounts
    assert (_SIGNOZ / "otel-collector-config.yaml").is_file()


def test_vendored_configs_keep_upstream_hostnames() -> None:
    """配置保持与上游逐字节同形（只靠别名适配），防止有人「顺手改配置」脱钩上游。"""
    cluster = (_SIGNOZ / "clickhouse" / "cluster.xml").read_text()
    assert "<host>zookeeper-1</host>" in cluster
    otel = (_SIGNOZ / "otel-collector-config.yaml").read_text()
    assert "tcp://clickhouse:9000/signoz_traces" in otel


def test_udf_fetch_is_non_fatal() -> None:
    """真机坑 2：UDF 拉取失败不得让 init 非零退出（否则整栈卡在 depends_on）。"""
    cmd = _services()["signoz-init-clickhouse"]["command"]
    script = "\n".join(cmd) if isinstance(cmd, list) else str(cmd)
    assert "histogramQuantile" in script
    assert "|| echo" in script, "UDF 拉取失败必须有兜底分支，不能直接退出"


def test_ui_published_on_loopback_only() -> None:
    ports = _services()["signoz"]["ports"]
    assert ports == ["127.0.0.1:8080:8080"], ports


# ─────────────────────── 应用侧 collector 接线 ───────────────────────


def test_app_otel_collector_exports_to_signoz_and_keeps_debug() -> None:
    cfg = yaml.safe_load(
        (_ROOT / "deploy" / "otel" / "otel-collector.yaml").read_text()
    )
    assert (
        cfg["exporters"]["otlphttp"]["endpoint"] == "http://signoz-otel-collector:4318"
    )
    # debug 保留为本地兜底：signoz profile 未起时仍能看 span
    assert "debug" in cfg["service"]["pipelines"]["traces"]["exporters"]
    assert "otlphttp" in cfg["service"]["pipelines"]["traces"]["exporters"]


# ─────────────────────── k8s ───────────────────────


def test_k8s_signoz_kustomization_builds_and_is_isolated() -> None:
    out = subprocess.run(
        [
            "kubectl",
            "kustomize",
            "--load-restrictor",
            "LoadRestrictionsNone",
            str(_K8S_SIGNOZ),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    kinds = {d["kind"] for d in docs}
    assert {"Namespace", "StatefulSet", "Deployment", "Job", "Service"} <= kinds

    namespaces = {d["metadata"].get("namespace", "signoz") for d in docs}
    assert namespaces == {"signoz"}, namespaces
    svc_names = {d["metadata"]["name"] for d in docs if d["kind"] == "Service"}
    # Service 名必须与官方配置写死的主机名一致（namespace 隔离让同名可用）
    assert {"clickhouse", "zookeeper-1", "signoz-otel-collector", "signoz"} <= svc_names

    # 默认不启用：长驻工作负载副本数 0、Job suspend
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet"):
            assert d["spec"]["replicas"] == 0, d["metadata"]["name"]
        if d["kind"] == "Job":
            assert d["spec"]["suspend"] is True


def test_k8s_base_aliases_signoz_collector_into_lkm() -> None:
    """应用侧 collector 读的是短名；跨 namespace 靠 ExternalName 在 lkm 内补齐。"""
    text = (
        _ROOT / "deploy" / "k8s" / "base" / "infra" / "otel-collector.yaml"
    ).read_text()
    assert "kind: Service" in text
    assert re.search(r"type:\s*ExternalName", text), "缺少 ExternalName 别名"
    assert "signoz-otel-collector.signoz.svc.cluster.local" in text
