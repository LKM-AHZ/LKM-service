"""Pulsar「无状态化」静态验收（2026-09-17 根治，路线图 §8 #32）。

背景：standalone 的 embedded bookie 每次进程启动随机取端口、容器 IP 重启后也变，而 ledger 元数据
记的是 `IP:端口` → 旧 ledger 必然不可读 → broker 崩溃循环 → backend 与 9 个 worker 全卡 `Created`
（本机已复发三次）。三条「钉住 bookie 地址」的路（env 前缀 / CLI / standalone.conf）实测均无效，
故改为「每次启动都是干净 broker」+「租户/namespace 随启动自动幂等重建」。

本文件按**规则**断言（不照抄当前取值，见路线图 §8 #26 的教训）：只要有人把包装脚本拿掉、
把清空删掉、把初始化换回一次性服务、或把依赖条件退化为「服务已启动」，这里就变红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"
_ENTRYPOINT = _ROOT / "deploy" / "pulsar" / "entrypoint.sh"

_DATA_DIR = "/pulsar/data"
_NAMESPACES = ("biz", "auth", "system")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def script() -> str:
    return _ENTRYPOINT.read_text(encoding="utf-8")


def _no_comments(text: str) -> str:
    """去掉整行注释与行尾注释，避免注释里的说明文字被当成实现。"""
    lines = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        lines.append(stripped)
    return "\n".join(lines)


# ───────────────────────── 包装脚本：规则断言 ─────────────────────────


def test_entrypoint_clears_data_before_starting_broker(script: str) -> None:
    """启动 broker **之前**清空数据目录——这是「根治」的本体，删掉即故障回归。"""
    body = _no_comments(script)
    rm = body.find(_DATA_DIR)
    start = body.find("bin/pulsar standalone")
    assert rm != -1, f"entrypoint 未清空 {_DATA_DIR}（无状态化的本体）"
    assert start != -1, "entrypoint 未启动 `bin/pulsar standalone`"
    assert rm < start, "清空数据目录必须发生在启动 broker 之前"


def test_entrypoint_inits_namespaces_after_broker_is_ready(script: str) -> None:
    """broker 就绪**之后**才建租户/namespace，且三个 namespace 齐全。"""
    body = _no_comments(script)
    start = body.find("bin/pulsar standalone")
    create = body.find("tenants create")
    assert create > start, "建租户必须发生在启动 broker 之后（否则 broker 未就绪必失败）"
    # 就绪探针存在（用 tenants list 之类）——没有等待就没有「就绪后」
    assert re.search(r"until .*pulsar-admin", body), "缺少 broker 就绪探针（until ... pulsar-admin）"
    assert "namespaces create" in body, "缺少 namespace 创建步骤"
    for ns in _NAMESPACES:
        assert ns in body, f"缺 namespace {ns} 的创建"


def test_entrypoint_creates_are_idempotent(script: str) -> None:
    """每次容器启动都会重跑本脚本 → 创建命令必须可重入（失败不致命）。"""
    body = _no_comments(script)
    for line in body.splitlines():
        if "tenants create" in line or "namespaces create" in line:
            assert "|| true" in line or "|| :" in line, (
                f"创建命令必须幂等容错（`|| true`），否则重启即失败: {line.strip()}"
            )
    # `|| true` 只该对「已存在」宽容：create 之后必须各复查一次资源到底在不在，
    # 不在就非 0 退出 —— 否则鉴权失败/admin-url 写错也被吞掉，脚本照样打「已就绪」，
    # 而 compose healthcheck 正是校验 namespace 存在，依赖方会无限等待。
    assert "tenants list" in body, "创建租户后缺少存在性复查（tenants list）"
    assert "namespaces list" in body, "创建 namespace 后缺少存在性复查（namespaces list）"
    assert "exit 1" in body, "复查不通过必须非 0 退出，而不是继续打「已就绪」"


def test_entrypoint_forwards_stop_signal(script: str) -> None:
    """broker 后台跑 + 前台 wait 的形态必须转发 SIGTERM，否则 `docker stop` 只能等超时被 KILL。"""
    body = _no_comments(script)
    assert "trap" in body and "TERM" in body, "缺少 TERM 信号转发"
    assert re.search(r"wait\s+\"?\$", body), "缺少前台 wait（否则容器会立刻退出）"


# ───────────────────────── compose：接线与依赖语义 ─────────────────────────


def test_pulsar_service_wires_entrypoint_and_volume(compose: dict) -> None:
    """pulsar 服务必须挂载包装脚本并以它作 entrypoint，且不再直接跑 standalone。"""
    svc = compose["services"]["pulsar"]
    entry = svc["entrypoint"]
    assert any("entrypoint.sh" in str(x) for x in entry), entry
    binds = [v for v in svc.get("volumes", []) if isinstance(v, str) and ":" in v]
    assert any("deploy/pulsar/entrypoint.sh" in b for b in binds), binds
    assert any(b.startswith("pulsar_data:") or b.split(":")[0].endswith("pulsar_data") for b in binds), binds
    assert "command" not in svc, "command 不应再直接启动 standalone（会绕过清空+初始化）"


def test_pulsar_healthcheck_implies_namespace_initialized(compose: dict) -> None:
    """healthcheck 必须是「broker 就绪 **且** 初始化完成」的语义，否则依赖方会抢跑订阅。"""
    hc = compose["services"]["pulsar"]["healthcheck"]
    cmd = " ".join(str(x) for x in hc["test"])
    assert "namespaces list" in cmd and "lkm" in cmd, cmd
    assert "biz" in cmd, f"探针应验证业务 namespace 存在: {cmd}"
    # JVM 客户端单次调用实测 ~12s → 超时必须放宽，否则恒判超时（原 10s 就吃过这个亏）
    assert int(str(hc["timeout"]).rstrip("s")) >= 15, hc["timeout"]


def test_no_one_shot_pulsar_init_service(compose: dict) -> None:
    """一次性 pulsar-init 服务必须已删除：它只在首次 up 时跑，pulsar 重启后不会重跑。

    注：其它组件（如 prefect-init 之类）用 `service_completed_successfully` 是各自的合理形态，
    故此处只锁 pulsar 侧——不得有服务把「pulsar 初始化」当成一次性前置。
    """
    assert "pulsar-init" not in compose["services"]
    for name, svc in compose["services"].items():
        dep = svc.get("depends_on") or {}
        # compose 的 depends_on 两种形态并存（短列表 / 带 condition 的映射），都要能审
        for dep_name in dep if isinstance(dep, list) else list(dep):
            assert "pulsar" not in str(dep_name) or dep_name == "pulsar", (
                f"{name} 仍依赖 pulsar 的一次性初始化服务 {dep_name}"
            )


def test_app_services_depend_on_pulsar_being_healthy(compose: dict) -> None:
    """依赖消息总线的应用服务必须按 `service_healthy` 依赖 pulsar（即等初始化完成）。"""
    dependents = 0
    for name, svc in compose["services"].items():
        dep = svc.get("depends_on") or {}
        if isinstance(dep, dict) and "pulsar" in dep:
            dependents += 1
            assert dep["pulsar"]["condition"] == "service_healthy", (name, dep["pulsar"])
    assert dependents >= 10, f"应至少有 10 个服务依赖 pulsar，实际 {dependents}"
