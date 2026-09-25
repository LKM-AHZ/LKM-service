"""Prometheus 告警规则的部署侧验收：**同一目录，两条引入路径必须都覆盖到**。

- compose：`deploy/prometheus/rules` 整目录挂进 `/etc/prometheus/rules` —— 加文件即生效。
- k8s：`kustomization.yaml` 的 `prometheus-rules` ConfigMap 是**显式文件清单** —— 只加
  `.yml` 不往清单里补一行，规则在 k8s 上**静默不加载**（2026-09-26 真机核查时正是这么漏的：
  `lkm-audit.yml` 写好了、compose 能挂，k8s 那份清单里没有它）。

故这里用「目录里的每个规则文件都必须出现在 k8s 清单里」把这条不变量钉住；另有
`prometheus.yml` 的 `rule_files` 必须覆盖该目录（否则挂载了也不被加载）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_RULES_DIR = _ROOT / "deploy" / "prometheus" / "rules"
_PROMETHEUS_YML = _ROOT / "deploy" / "prometheus" / "prometheus.yml"
_KUSTOMIZATION = _ROOT / "deploy" / "k8s" / "base" / "kustomization.yaml"
_COMPOSE = _ROOT / "docker-compose.yml"

_CONTAINER_RULES_DIR = "/etc/prometheus/rules"


def _rule_files() -> list[Path]:
    return sorted(_RULES_DIR.glob("*.yml"))


def _k8s_rule_configmap() -> dict:
    doc = yaml.safe_load(_KUSTOMIZATION.read_text(encoding="utf-8"))
    for entry in doc.get("configMapGenerator", []):
        if entry.get("name") == "prometheus-rules":
            return entry
    raise AssertionError("kustomization.yaml 里没有 prometheus-rules ConfigMap")


def test_every_rule_file_is_mounted_into_k8s_configmap() -> None:
    """目录里的每个规则文件都必须在 k8s 清单里显式列出（否则 k8s 上静默不加载）。"""
    listed = {Path(f).name for f in _k8s_rule_configmap()["files"]}
    on_disk = {p.name for p in _rule_files()}

    assert on_disk, "规则目录为空，本测试失去意义"
    assert on_disk - listed == set(), (
        f"这些规则文件没进 k8s 的 prometheus-rules ConfigMap：{sorted(on_disk - listed)}"
    )
    assert listed - on_disk == set(), (
        f"清单里列了不存在的规则文件：{sorted(listed - on_disk)}"
    )


def test_k8s_lists_rules_from_the_same_dir_as_compose() -> None:
    """两处引入的必须是**同一份**文件（compose 挂目录、k8s 列文件，来源要一致）。"""
    for f in _k8s_rule_configmap()["files"]:
        assert Path(f).parent.name == "rules", f
        assert (_ROOT / "deploy" / "prometheus" / "rules" / Path(f).name).exists(), f


def test_prometheus_scrape_config_globs_the_rules_dir() -> None:
    """`prometheus.yml` 的 rule_files 必须覆盖该目录——挂载了但没被 glob 等于没挂。"""
    doc = yaml.safe_load(_PROMETHEUS_YML.read_text(encoding="utf-8"))
    assert f"{_CONTAINER_RULES_DIR}/*.yml" in doc["rule_files"], doc["rule_files"]


def test_compose_mounts_the_rules_dir_readonly() -> None:
    """compose 侧的挂载点：整目录只读挂进同一容器路径（与 k8s 的 target 一致）。"""
    doc = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    mounts = doc["services"]["prometheus"].get("volumes", [])
    assert f"./deploy/prometheus/rules:{_CONTAINER_RULES_DIR}:ro" in mounts, mounts
