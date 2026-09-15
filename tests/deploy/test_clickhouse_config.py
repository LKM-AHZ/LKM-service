"""ClickHouse 部署资产静态验收（M5 7.2.6）。

照 test_apisix_config 的模式：直接解析 compose / init.sql / vector.toml，锁死「启动即败或
静默丢数据」的关键契约——profile 隔离、卷挂载、表引擎/排序键/去重语义、vector 采集源与
sink 表名、三处服务的 CH 配置下发。每条断言都对应一个可复现的真实故障（见各注释）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_CH_DIR = _ROOT / "deploy" / "clickhouse"
_COMPOSE = _ROOT / "docker-compose.yml"


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _services() -> dict:
    return _compose()["services"]


def _init_sql() -> str:
    return (_CH_DIR / "init.sql").read_text(encoding="utf-8")


def _vector_toml() -> str:
    return (_CH_DIR / "vector.toml").read_text(encoding="utf-8")


# ── compose ──────────────────────────────────────────────────────────────────


def should_isolate_clickhouse_behind_profile() -> None:
    # 无 profile 隔离会让它随主栈默认拉起，拖慢冷启动并可能端口冲突
    ch = _services()["clickhouse"]
    assert ch["profiles"] == ["clickhouse"]


def should_mount_init_sql_and_data_volume() -> None:
    # 缺 init.sql 挂载 → 卷首启无表，导出全表不存在；缺数据卷 → 重启丢分析数据
    ch = _services()["clickhouse"]
    vols = " ".join(ch.get("volumes", []))
    assert "init.sql" in vols
    assert "/var/lib/clickhouse" in vols
    assert "clickhouse_data" in _compose()["volumes"]


def should_configure_vector_capture_volumes() -> None:
    # docker.sock 缺失 → docker_logs source 起不来；containers 目录缺失 → 读不到日志文件
    vector = _services()["vector"]
    assert vector["profiles"] == ["clickhouse"]
    assert vector["depends_on"]["clickhouse"]["condition"] == "service_healthy"
    vols = " ".join(vector.get("volumes", []))
    assert "/var/run/docker.sock" in vols
    assert "/var/lib/docker/containers" in vols
    assert "vector.toml" in vols


def should_route_clickhouse_env_to_three_services() -> None:
    # backend 做 admin 查询、worker 做回落直调、prefect-worker 跑 flow——漏一处即该路径不可用
    services = _services()
    configured = [
        name
        for name, spec in services.items()
        if "LKM_CLICKHOUSE_ENABLED" in (spec.get("environment") or {})
    ]
    assert len(configured) >= 3, configured


# ── init.sql ─────────────────────────────────────────────────────────────────


def should_declare_three_tables() -> None:
    sql = _init_sql()
    assert "CREATE DATABASE IF NOT EXISTS lkm" in sql
    for table in ("lkm.app_logs", "lkm.event_failures", "lkm.audit_logs"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def should_dedupe_exported_tables_by_id() -> None:
    # 导出表必须以 id 为排序/去重键，否则重跑导出会叠加重复行（水位失效时无兜底）
    sql = _init_sql()
    assert sql.count("ENGINE = ReplacingMergeTree(ingested_at)") == 2
    assert sql.count("ORDER BY id\n") == 2
    # 日志表由 vector 直写，无业务 id，按 service+ts 排序
    assert "ENGINE = MergeTree" in sql
    assert "ORDER BY (service, ts)" in sql


def should_apply_ttl_and_partitioning() -> None:
    # 缺 TTL/分区 → 分析库无限膨胀、按时间查询全表扫
    sql = _init_sql()
    assert sql.count("PARTITION BY toYYYYMM") == 3
    assert "TTL ts + INTERVAL 30 DAY" in sql
    assert "TTL folded_at + INTERVAL 180 DAY" in sql
    assert "TTL created_at + INTERVAL 365 DAY" in sql
    assert sql.count("DateTime64(3)") >= 6


# ── vector.toml ──────────────────────────────────────────────────────────────


def should_capture_docker_logs_into_app_logs() -> None:
    toml = _vector_toml()
    assert 'type = "docker_logs"' in toml
    assert 'type = "clickhouse"' in toml
    assert 'table = "app_logs"' in toml
    assert 'database = "lkm"' in toml
    # docker_logs 自带 container_id/image/stream 等非表列，必须忽略否则插入报未知列
    assert "skip_unknown_fields = true" in toml
    # 非 JSON 行不能丢：remap 失败分支保留原文并置 unknown
    assert '"unknown"' in toml
