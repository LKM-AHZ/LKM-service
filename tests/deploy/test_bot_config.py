"""LKM Bot 部署资产静态验收。

照 test_clickhouse_config / test_apisix_config 的模式：解析根 compose、网关路由模板与其
渲染产物、k8s 清单，锁死「启动即败 / 越权 / 上传被静默打死」的关键契约：

  1) **profile 隔离**：bot 与 shipyard 只能随 `--profile bot` 起，不得混进主栈；
  2) **越权面**：shipyard 挂 docker.sock（≈宿主 root），其 spawn 的沙箱容器必须落在
     **独立网络**——若落 `lkm`，沙箱里跑的模型生成代码就能直连 postgres/redis/minio；
  3) **上传上限独立**：bot 允许单文件 512MB，复用社群站的 `__MAX_BODY_SIZE__`(100MB)
     会把 bot 上传静默打死；两个数必须来自不同变量；
  4) **域名**：bot 是独立 host（其自带 API 与社群站同为绝对路径 /api/v1/*，路径前缀方案
     会与 backend 路由正面冲突），且必须并入 ACME/跳转的 hosts 并集；
  5) **k8s**：bot 是可选组件（`replicas: 0`），data 走 PVC + `strategy: Recreate`
     （RWO 单写者：滚动更新会双挂载并让 SQLite 双写）。
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"
_APISIX_DIR = _ROOT / "deploy" / "apisix"
_K8S_BASE = _ROOT / "deploy" / "k8s" / "base"

_BOT = "bot.lkm-ahz.ltd"
_COMMUNITY_BODY_LIMIT = 104857600  # LKM_MAX_UPLOAD_BYTES 默认值
_BOT_BODY_LIMIT = 550000000  # LKM_BOT_MAX_UPLOAD_BYTES 默认值


def _services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def _compose_raw() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _render() -> dict:
    """跑一次 render.sh（伪造证书）取**渲染产物**——路由实值只在产物上可见。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="apisix-bot-"))
    cert_root = tmp / "live"
    for domain in ("lkm-ahz.ltd", "lkm-ahz.icu", _BOT):
        d = cert_root / domain
        d.mkdir(parents=True)
        (d / "fullchain.pem").write_text(
            "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
        )
        (d / "privkey.pem").write_text(
            "-----BEGIN PRIVATE KEY-----\nMIIEfake\n-----END PRIVATE KEY-----\n"
        )
    out_dir = tmp / "out"
    out_dir.mkdir(parents=True)
    subprocess.run(
        ["sh", str(_APISIX_DIR / "render.sh")],
        check=True,
        env={
            **os.environ,
            "APISIX_SRC": str(_APISIX_DIR / "apisix.yaml"),
            "APISIX_OUT": str(out_dir / "apisix.yaml"),
            "APISIX_SRC_CONFIG": str(_APISIX_DIR / "config.yaml"),
            "APISIX_OUT_CONFIG": str(out_dir / "config.yaml"),
            "APISIX_CERT_ROOT": str(cert_root),
            "APISIX_RENDER_ONCE": "1",
        },
    )
    return yaml.safe_load((out_dir / "apisix.yaml").read_text())


def _routes() -> dict[str, dict]:
    return {r["id"]: r for r in _render()["routes"]}


def _k8s_lkmbot() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(
            (_K8S_BASE / "app" / "lkmbot.yaml").read_text(encoding="utf-8")
        )
        if doc
    ]


# ── compose：profile 隔离与端口面 ─────────────────────────────────────────────


def should_isolate_bot_behind_profile() -> None:
    # 无 profile 隔离会让 bot（大镜像、含 nodejs/ffmpeg）随主栈默认拉起
    assert _services()["lkmbot"]["profiles"] == ["bot"]


def should_isolate_shipyard_behind_profile() -> None:
    # 沙箱要挂 docker.sock（≈宿主 root），绝不能随主栈默认起
    assert _services()["shipyard"]["profiles"] == ["bot"]


def should_publish_no_host_ports_for_bot() -> None:
    """面板与 OneBot 端口都不得发布到宿主：对外只经网关，多一个端口就多一条绕过限流的入口。"""
    svc = _services()["lkmbot"]
    assert not svc.get("ports"), f"lkmbot 不应发布宿主端口: {svc.get('ports')}"
    assert not svc.get("expose")


def should_never_mount_docker_sock_into_bot() -> None:
    """docker.sock 只给 shipyard；bot 本体拿到了就等于把宿主 root 交给模型插件。"""
    mounts = [str(m) for m in _services()["lkmbot"]["volumes"]]
    assert not any("docker.sock" in m for m in mounts)


def should_bind_bot_data_to_host_path() -> None:
    """data 必须是宿主机 bind（非命名卷）：shipyard 要把宿主路径 bind 进它 spawn 的沙箱容器。"""
    mounts = [str(m) for m in _services()["lkmbot"]["volumes"]]
    assert any("./LKM-bot/data:/LKMBot/data" in m for m in mounts), mounts


def should_pin_dashboard_host_and_data_root() -> None:
    """DASHBOARD_HOST 必须 0.0.0.0（绑回环会让面板「默认密码免校验」判定成立且网关连不上）；
    ASTRBOT_ROOT 显式固定 data 路径（默认取 cwd，入口一改就漂移）。"""
    env = _services()["lkmbot"]["environment"]
    assert env["DASHBOARD_HOST"] == "0.0.0.0"
    assert env["ASTRBOT_ROOT"] == "/LKMBot"


# ── compose：沙箱网络隔离 ────────────────────────────────────────────────────


def should_put_sandbox_on_its_own_network() -> None:
    """沙箱容器必须落在独立网络：落 lkm 就能直连 postgres/redis/minio（真实越权面）。

    shipyard 自身要在两个网络上——只在 bot_sandbox 上，面板连不到它；只在 lkm 上，
    它管不到船。`DOCKER_NETWORK` 走字面网络名，故 bot_sandbox 必须显式 name。
    """
    shipyard = _services()["shipyard"]
    env = shipyard["environment"]
    assert env["DOCKER_NETWORK"] == "lkm-bot-sandbox"
    assert set(shipyard["networks"]) == {"lkm", "bot_sandbox"}
    assert _compose_raw().count("name: lkm-bot-sandbox") == 1
    # 该网络名不得等于 bot 主网络（防「顺手改成 lkm」回潮）
    assert env["DOCKER_NETWORK"] != _services()["lkmbot"]["networks"][0]


def should_share_temp_dir_between_bot_and_sandbox() -> None:
    """面板落盘的上传件要让沙箱读到，两者必须共享同一宿主 temp 目录。"""
    mounts = [str(m) for m in _services()["shipyard"]["volumes"]]
    assert any("./LKM-bot/data/temp:/LKMBot/data/temp" in m for m in mounts), mounts


def should_pass_ship_data_dir_as_host_absolute_path() -> None:
    """SHIP_DATA_DIR 由 bay 原样交给 Docker API 做 bind，必须是宿主机绝对路径。

    静态只能看 compose 里的插值式：要求可被 LKM_BOT_SHIP_DATA_DIR 覆盖，且默认值派生自
    绝对路径 ${PWD}。写成相对路径（`./data/...`）会被 bay 原样交给 Docker API →
    bind 源解析不到 → 沙箱容器起不来（且失败发生在运行时，不在 compose 校验期）。
    """
    raw = _services()["shipyard"]["environment"]["SHIP_DATA_DIR"]
    assert raw.startswith("${LKM_BOT_SHIP_DATA_DIR:-${PWD}/"), raw


# ── 网关：路由、WS、独立上传上限 ──────────────────────────────────────────────


def should_route_bot_by_dedicated_host() -> None:
    """bot 用独立 host——其自带 API 与社群站同为绝对路径 /api/v1/*，同 host 必冲突。"""
    route = _routes()["bot-dashboard"]
    assert route["hosts"] == [_BOT]
    assert f"www.{_BOT}" not in route["hosts"]  # 面板没有 www 变体
    assert route["upstream"]["service_name"].startswith("lkmbot:")
    assert route["upstream"]["pass_host"] == "pass"


def should_enable_websocket_on_bot_route() -> None:
    """面板实时通道（/api/v1/live-chat/ws 等）都落在 catchall，不开 upgrade 会被剥掉。"""
    assert _routes()["bot-dashboard"]["enable_websocket"] is True


def should_not_reuse_community_body_limit_for_bot() -> None:
    """bot 单文件上限 512MB，复用社群站的 100MB 会把 bot 上传静默打死。"""
    routes = _routes()
    bot_limit = routes["bot-dashboard"]["plugins"]["client-control"]["max_body_size"]
    assert bot_limit == _BOT_BODY_LIMIT
    assert bot_limit != _COMMUNITY_BODY_LIMIT
    # 社群站各路由仍必须是 100MB（防「改共用变量」把社群站上限一起抬高）
    assert (
        routes["api-prefix"]["plugins"]["client-control"]["max_body_size"]
        == _COMMUNITY_BODY_LIMIT
    )


def should_rate_limit_bot_login_at_gateway() -> None:
    """面板是管理员面：网关层要有粗粒度登录限流（应用层另有令牌桶，分工同社群站）。"""
    route = _routes()["bot-auth-login"]
    assert route["hosts"] == [_BOT]
    assert route["plugins"]["limit-count"]["rejected_code"] == 429
    assert route["upstream"]["service_name"].startswith("lkmbot:")
    # 不挂 cors：同源面板用不到，且 cors 插件在 allow_credential=true 下禁 `*`，
    # 多挂一处就多一处「整条路由校验失败不加载」的面
    assert "cors" not in route["plugins"]


def should_include_bot_in_acme_and_redirect_hosts() -> None:
    """漏掉 bot 的表现：http://bot.* 不跳转，且 bot 域名的 ACME 挑战没有上游。"""
    routes = _routes()
    assert _BOT in routes["acme-challenge"]["hosts"]
    assert _BOT in routes["http-redirect"]["hosts"]


def should_not_hardcode_bot_domain_in_template() -> None:
    """模板只放占位（单一来源由 render.sh 展开），bot 域名同理。"""
    raw = (_APISIX_DIR / "apisix.yaml").read_text(encoding="utf-8")
    assert _BOT not in raw
    assert "__BOT_HOSTS__" in raw and "__BOT_MAX_BODY_SIZE__" in raw


def should_expand_bot_domain_per_runtime_env() -> None:
    """域名经 APISIX_BOT_DOMAINS 展开：compose/k8s 各自下发，模板不认死默认值。"""
    raw = (_APISIX_DIR / "render.sh").read_text(encoding="utf-8")
    assert 'BOT="${APISIX_BOT_DOMAINS:-' in raw
    assert 'BOT_MAX_BODY_SIZE="${APISIX_BOT_MAX_BODY_SIZE:-' in raw
    # 证书/SNI 也要跟随（漏了 DOMAINS 的并集，bot 会退回自签占位）
    assert "$DOMAINS" in raw and " $BOT" in raw


# ── 交付面：清单与 kustomize 挂载 ─────────────────────────────────────────────


def should_register_bot_in_kustomization() -> None:
    text = (_K8S_BASE / "kustomization.yaml").read_text(encoding="utf-8")
    assert "app/lkmbot.yaml" in text


def should_keep_bot_disabled_by_default_in_k8s() -> None:
    """compose 用 profile 表达可选，k8s 无此机制 → 用 replicas: 0。"""
    dep = next(d for d in _k8s_lkmbot() if d["kind"] == "Deployment")
    assert dep["spec"]["replicas"] == 0
    # RWO + SQLite 单写者：滚动更新会双挂载同一卷并双写
    assert dep["spec"]["strategy"]["type"] == "Recreate"


def should_back_bot_with_pvc_and_tcp_probe() -> None:
    docs = _k8s_lkmbot()
    assert any(d["kind"] == "PersistentVolumeClaim" for d in docs)
    container = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"][
        "spec"
    ]["containers"][0]
    # bot 没有公开健康端点，/api/v1/* 又绕过面板鉴权中间件 → 「端口在听」才是真语义
    assert container["readinessProbe"]["tcpSocket"]["port"] == "http"


def should_publish_bot_cert_in_gateway_projected_volume() -> None:
    """k8s 证书经 Secret 扁平键名还原成 render.sh 期望的 <域名>/fullchain.pem 结构。"""
    text = (_K8S_BASE / "gateway" / "apisix.yaml").read_text(encoding="utf-8")
    assert f"{_BOT}_fullchain.pem" in text and f"{_BOT}_privkey.pem" in text
    assert re.search(rf"path: {re.escape(_BOT)}/fullchain\.pem", text)


def should_expose_bot_domain_and_limit_to_k8s_gateway() -> None:
    """网关 initContainer 与常驻 sidecar 两处都要拿到 bot 域名/上限（少一处就渲染出占位残留）。"""
    gw = (_K8S_BASE / "gateway" / "apisix.yaml").read_text(encoding="utf-8")
    assert gw.count("APISIX_BOT_DOMAINS") >= 2
    assert gw.count("APISIX_BOT_MAX_BODY_SIZE") >= 2
    cfg = (_K8S_BASE / "gateway" / "config.yaml").read_text(encoding="utf-8")
    assert f"APISIX_BOT_DOMAINS: {_BOT}" in cfg
    # 上限放网关专属表：放进 lkm-config 会被 envFrom 灌进 backend/auth/worker（无消费方）
    assert 'APISIX_BOT_MAX_BODY_SIZE: "550000000"' in cfg
    app_cfg = (_K8S_BASE / "app-config.yaml").read_text(encoding="utf-8")
    assert "BOT_MAX_UPLOAD" not in app_cfg
