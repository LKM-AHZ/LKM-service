"""Alertmanager 告警投递的部署侧验收：规则算出来之后，**必须真的有人能收到**。

背景（本文件守护的东西）：`deploy/prometheus/rules/` 的 12 条规则一直存在，但
`prometheus.yml` 没有 `alerting` 段、编排里也没有 Alertmanager —— 告警只在 Prometheus
UI 里可见。没人会去盯 UI，所以 `DBPoolExhausted` / `OutboxBacklogHigh` 这类规则
实际上等于不存在。本文件把「规则 → 投递」这条链上的每一段都钉住：

    rules/*.yml  --(rule_files)-->  prometheus  --(alerting)-->  alertmanager  --(webhook)-->  人

覆盖四段：
1. prometheus 的 alerting 指向 alertmanager 服务名；
2. compose 里 alertmanager 存在、且**必须等渲染完成**才启动（否则单文件 bind 会被
   Docker 建成目录 → 容器起不来，错误信息离成因很远）；
3. render.sh 的两个分支（配了地址 / 没配地址）都产出**合法 YAML**，且没配地址时
   是「降级成不投递」而不是「写坏配置」；
4. k8s 与 compose 跑的是**同一份** tpl + render.sh（两处不留副本，避免第二真相源）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_ALERT_DIR = _ROOT / "deploy" / "alertmanager"
_TPL = _ALERT_DIR / "alertmanager.yml.tpl"
_RENDER = _ALERT_DIR / "render.sh"
_PROMETHEUS_YML = _ROOT / "deploy" / "prometheus" / "prometheus.yml"
_COMPOSE = _ROOT / "docker-compose.yml"
_KUSTOMIZATION = _ROOT / "deploy" / "k8s" / "base" / "kustomization.yaml"
_K8S_ALERTMANAGER = _ROOT / "deploy" / "k8s" / "base" / "infra" / "alertmanager.yaml"
_ENV_EXAMPLE = _ROOT / ".env.example"
_GITIGNORE = _ROOT / ".gitignore"

_CONTAINER_CONFIG = "/etc/alertmanager/alertmanager.yml"


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _k8s_docs() -> list[dict]:
    """infra/alertmanager.yaml 是多文档（Service + Deployment），按 kind 取用。"""
    return [
        d
        for d in yaml.safe_load_all(_K8S_ALERTMANAGER.read_text(encoding="utf-8"))
        if d
    ]


def _k8s_deployment() -> dict:
    return next(d for d in _k8s_docs() if d["kind"] == "Deployment")


def _render(
    tmp_path: Path, *, webhook: str | None, tpl: Path | None = None
) -> tuple[int, str]:
    """跑一次 render.sh，返回 (退出码, 渲染产物文本或空串)。"""
    out = tmp_path / "alertmanager.yml"
    env = {
        **os.environ,
        "ALERTMANAGER_SRC": str(tpl or _TPL),
        "ALERTMANAGER_OUT": str(out),
    }
    # 显式清掉宿主可能存在的值，否则「未配置」分支在开发者机器上测不到
    env.pop("ALERT_WEBHOOK_URL", None)
    if webhook is not None:
        env["ALERT_WEBHOOK_URL"] = webhook
    proc = subprocess.run(
        ["sh", str(_RENDER)], env=env, capture_output=True, text=True, timeout=60
    )
    return proc.returncode, out.read_text(encoding="utf-8") if out.exists() else ""


def test_prometheus_delivers_alerts_to_alertmanager() -> None:
    """`alerting.alertmanagers` 必须指向 alertmanager —— 没有它，规则只在 UI 里可见。"""
    doc = yaml.safe_load(_PROMETHEUS_YML.read_text(encoding="utf-8"))
    assert "alerting" in doc, (
        "prometheus.yml 没有 alerting 段：规则会被算出来但不会交给 Alertmanager，"
        "等同于告警只在 Prometheus UI 可见、无人知晓"
    )
    targets = [
        target
        for am in doc["alerting"]["alertmanagers"]
        for sc in am.get("static_configs", [])
        for target in sc.get("targets", [])
    ]
    assert "alertmanager:9093" in targets, targets


def test_compose_waits_for_rendered_config_before_starting() -> None:
    """alertmanager 必须在渲染**完成**后才启动（否则单文件 bind 被建成目录）。"""
    services = _compose()["services"]
    assert "alertmanager" in services, "compose 里没有 alertmanager 服务"
    am = services["alertmanager"]
    assert am["profiles"] == ["monitoring"], am.get("profiles")

    depends = am.get("depends_on") or {}
    assert depends.get("alertmanager-render", {}).get("condition") == (
        "service_completed_successfully"
    ), depends

    mounts = am.get("volumes", [])
    # 配置来源必须是**渲染产物**，不能是模板（模板含未展开占位符，Alertmanager 会解析失败）
    assert (
        f"./deploy/alertmanager/rendered/alertmanager.yml:{_CONTAINER_CONFIG}:ro"
        in mounts
    ), mounts
    # silence/nflog 要持久化，否则每次重启都得重新静音
    assert any(str(m).startswith("alertmanager_data:") for m in mounts), mounts


def test_compose_declares_the_alertmanager_data_volume() -> None:
    """引用了命名卷就必须在顶层 volumes 里声明，否则 compose 直接报错。"""
    doc = _compose()
    assert "alertmanager_data" in doc.get("volumes", {}), doc.get("volumes")


def test_render_script_expands_webhook_and_escapes_quotes(tmp_path: Path) -> None:
    """配了地址：产物是合法 YAML，且含 webhook（含引号的 URL 必须能往返）。"""
    url = "https://hooks.example.com/lkm?a=1&b='2'"
    code, text = _render(tmp_path, webhook=url)
    assert code == 0, text

    doc = yaml.safe_load(text)
    assert doc["route"]["receiver"] == "lkm-alerts"
    receiver = next(r for r in doc["receivers"] if r["name"] == "lkm-alerts")
    webhook = receiver["webhook_configs"][0]
    # 单引号串里 ' 写成 ''，YAML 解析回来必须与原文一致（否则通知地址会被改坏）
    assert webhook["url"] == url, webhook
    assert webhook["send_resolved"] is True, webhook
    # 占位符必须被吃干净。按**整行**判断（与 render.sh 的匹配语义一致）：
    # 模板的注释里也提到这个占位符名，按子串判断会误报。
    assert not [
        ln for ln in text.splitlines() if ln.strip() == "__ALERT_RECEIVER_BODY__"
    ]


def test_render_script_degrades_instead_of_failing_without_webhook(
    tmp_path: Path,
) -> None:
    """没配地址：仍产出合法配置（空接收器），而不是写坏文件或让容器起不来。

    取舍写在 render.sh 头部：告警不投递是**降级**（UI 仍可见），容器起不来是**故障**。
    这里锁住「降级」这个选择，避免有人为了"更严格"改成 exit 1 —— 那会让整套
    monitoring profile 因为一个没填的可选变量而整体起不来。
    """
    code, text = _render(tmp_path, webhook=None)
    assert code == 0, (
        "未配置 webhook 时 render.sh 不应失败（否则 monitoring 整体起不来）"
    )

    doc = yaml.safe_load(text)
    assert [r["name"] for r in doc["receivers"]] == ["lkm-alerts"]
    assert doc["receivers"][0].get("webhook_configs") in (None, []), doc["receivers"][0]


def test_render_script_keeps_previous_config_when_template_is_broken(
    tmp_path: Path,
) -> None:
    """模板写坏（缺 receivers 段）时必须失败且**保留上一版**，不能留下半截配置。"""
    out = tmp_path / "alertmanager.yml"
    out.write_text("previous: good\n", encoding="utf-8")

    broken = tmp_path / "broken.tpl"
    broken.write_text("route:\n  receiver: x\n", encoding="utf-8")

    code, text = _render(tmp_path, webhook="https://x.example", tpl=broken)
    assert code != 0, "缺 receivers 段的模板应当渲染失败"
    assert "previous: good" in text, "失败时不应覆盖上一版配置"


def test_k8s_reuses_the_same_template_and_render_script() -> None:
    """k8s 必须引用**同一份** tpl + render.sh（不留副本，避免第二真相源漂移）。"""
    doc = yaml.safe_load(_KUSTOMIZATION.read_text(encoding="utf-8"))
    entry = next(
        (
            e
            for e in doc.get("configMapGenerator", [])
            if e.get("name") == "alertmanager-config"
        ),
        None,
    )
    assert entry is not None, "kustomization 里没有 alertmanager-config"
    files = {Path(f).as_posix() for f in entry["files"]}
    assert files == {
        "../../alertmanager/alertmanager.yml.tpl",
        "../../alertmanager/render.sh",
    }, files
    # 文件本身必须真实存在（kustomize 缺文件会构建失败，但这里给出更明确的报错位置）
    for f in entry["files"]:
        assert (_ROOT / "deploy" / "k8s" / "base" / f).resolve().is_file(), f


def test_k8s_renders_via_initcontainer_and_optional_secret() -> None:
    """k8s 用 initContainer 渲染，webhook 走**可选** Secret —— 缺 Secret 也不能卡住 Pod。"""
    pod = _k8s_deployment()["spec"]["template"]["spec"]

    init = pod["initContainers"][0]
    assert init["command"] == ["sh", "/src/render.sh"], init["command"]

    env = {e["name"]: e for e in init.get("env", [])}
    ref = env["ALERT_WEBHOOK_URL"]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == "lkm-alert-webhook", ref
    assert ref["optional"] is True, (
        "Secret 必须 optional：否则首次部署没建 Secret 时 Pod 会卡在 "
        "CreateContainerConfigError，整套监控起不来"
    )

    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["src"]["configMap"]["name"] == "alertmanager-config", volumes["src"]


def test_k8s_lists_alertmanager_as_optional_component() -> None:
    """可选组件用 0 副本表达（k8s 没有 compose 的 --profile 机制）。"""
    assert _k8s_deployment()["spec"]["replicas"] == 0

    kustomization = yaml.safe_load(_KUSTOMIZATION.read_text(encoding="utf-8"))
    assert "infra/alertmanager.yaml" in kustomization["resources"], kustomization[
        "resources"
    ]


def test_env_example_documents_the_webhook_variable() -> None:
    """可选项必须可发现：.env.example 里要能搜到变量名，否则运维不知道有这条。"""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "LKM_ALERT_WEBHOOK_URL" in text, (
        "环境变量契约文档没登记 LKM_ALERT_WEBHOOK_URL"
    )


def test_rendered_output_directory_is_gitignored() -> None:
    """渲染产物是运行时生成的，不能进 git（否则不同机器互相覆盖）。"""
    text = _GITIGNORE.read_text(encoding="utf-8")
    assert "deploy/alertmanager/rendered/" in text, text
