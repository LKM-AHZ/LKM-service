"""LKM Bot 部署资产静态验收。

照 test_clickhouse_config / test_apisix_config 的模式：解析根 compose、网关路由模板与其
渲染产物、k8s 清单，锁死「启动即败 / 越权 / 上传被静默打死」的关键契约：

  1) **profile 隔离**：bot 与 shipyard 只能随 `--profile bot` 起，不得混进主栈；
  2) **越权面**：shipyard 挂 docker.sock（≈宿主 root），其 spawn 的沙箱容器必须落在
     **独立网络**——若落 `lkm`，沙箱里跑的模型生成代码就能直连 postgres/redis/minio；
  3) **上传上限独立**：bot 允许单文件 512MB，复用社群站的 `__MAX_BODY_SIZE__`(100MB)
     会把 bot 上传静默打死；两个数必须来自不同变量；
  4) **路径**：bot 面板挂在**社群域的子路径 `/bot/`**（不再有独立子域名）——其自带 API 与
     社群站同为绝对路径 /api/v1/*，同 host 下原样转发会与 backend 路由正面冲突，故网关用
     `proxy-rewrite` 剥掉 `/bot` 前缀后再送面板；路由 priority 必须高过 community-catchall，
     否则面板流量会被 Astro 接走；
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

_COMMUNITY = "lkm-ahz.ltd"
_COMMUNITY_HOSTS = [_COMMUNITY, f"www.{_COMMUNITY}"]
#: 面板子路径前缀（网关路由 uri 与 proxy-rewrite 的匹配对象）。
_BOT_PREFIX = "/bot"
#: 旧独立子域名：已下线，只在「不得再出现」类断言里用。
_RETIRED_BOT_DOMAIN = "bot.lkm-ahz.ltd"
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
    for domain in (_COMMUNITY, "lkm-ahz.icu"):
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


def should_route_bot_by_subpath() -> None:
    """面板挂在社群域子路径 /bot/*：同 host 下靠剥前缀避开与 backend 的 /api/v1/* 冲突。"""
    route = _routes()["bot-panel"]
    assert route["hosts"] == _COMMUNITY_HOSTS
    assert route["uri"] == f"{_BOT_PREFIX}/*"
    assert route["upstream"]["service_name"].startswith("lkmbot:")
    assert route["upstream"]["pass_host"] == "pass"
    # priority 必须高过 community-catchall(10)，否则面板流量被 Astro 接走
    assert route["priority"] > _routes()["community-catchall"]["priority"]


def should_rewrite_bot_prefix_off() -> None:
    """网关剥掉 /bot 前缀：面板进程仍以根路径服务其静态资源与 /api/v1/*（零应用侧改动）。"""
    routes = _routes()
    for route_id in ("bot-panel", "bot-auth-login"):
        rewrite = routes[route_id]["plugins"]["proxy-rewrite"]
        assert rewrite["regex_uri"] == [f"^{_BOT_PREFIX}/(.*)", "/$1"], route_id
    # 无尾斜杠的 /bot 由独立精确路由兜住（`/bot/*` 不匹配它）
    root = routes["bot-panel-root"]
    assert root["uri"] == _BOT_PREFIX
    assert root["plugins"]["proxy-rewrite"]["uri"] == "/"
    assert root["hosts"] == _COMMUNITY_HOSTS


def should_enable_websocket_on_bot_route() -> None:
    """面板实时通道（/api/v1/live-chat/ws 等）都落在 catchall，不开 upgrade 会被剥掉。"""
    assert _routes()["bot-panel"]["enable_websocket"] is True


def should_not_reuse_community_body_limit_for_bot() -> None:
    """bot 单文件上限 512MB，复用社群站的 100MB 会把 bot 上传静默打死。"""
    routes = _routes()
    bot_limit = routes["bot-panel"]["plugins"]["client-control"]["max_body_size"]
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
    assert route["hosts"] == _COMMUNITY_HOSTS
    assert route["uri"] == f"{_BOT_PREFIX}/api/v1/auth/login*"
    assert route["plugins"]["limit-count"]["rejected_code"] == 429
    assert route["upstream"]["service_name"].startswith("lkmbot:")
    # 不挂 cors：同源面板用不到，且 cors 插件在 allow_credential=true 下禁 `*`，
    # 多挂一处就多一处「整条路由校验失败不加载」的面
    assert "cors" not in route["plugins"]


def should_retire_bot_subdomain_from_acme_and_redirect_hosts() -> None:
    """子域已下线：ACME/跳转的 hosts 并集里不该再有 bot 域名（有则说明域名清单没清干净）。"""
    routes = _routes()
    assert _RETIRED_BOT_DOMAIN not in routes["acme-challenge"]["hosts"]
    assert _RETIRED_BOT_DOMAIN not in routes["http-redirect"]["hosts"]
    assert _COMMUNITY in routes["acme-challenge"]["hosts"]


def should_not_hardcode_bot_domain_in_template() -> None:
    """模板只放占位（单一来源由 render.sh 展开）；bot 域名与 __BOT_HOSTS__ 均已下线。"""
    raw = (_APISIX_DIR / "apisix.yaml").read_text(encoding="utf-8")
    assert _RETIRED_BOT_DOMAIN not in raw
    assert "__BOT_HOSTS__" not in raw
    # body 上限仍走占位（唯一消费者是 bot 路由，见 render.sh）
    assert "__BOT_MAX_BODY_SIZE__" in raw


def should_expand_bot_body_limit_but_not_domain() -> None:
    """上限经 APISIX_BOT_MAX_BODY_SIZE 展开；域名变量（APISIX_BOT_DOMAINS/BOT）必须已清空。"""
    render_sh = (_APISIX_DIR / "render.sh").read_text(encoding="utf-8")
    assert 'BOT_MAX_BODY_SIZE="${APISIX_BOT_MAX_BODY_SIZE:-' in render_sh
    assert "APISIX_BOT_DOMAINS" not in render_sh
    assert re.search(r"^BOT=", render_sh, flags=re.MULTILINE) is None
    # 证书 SNI 只覆盖社群/官网两域
    assert "DOMAINS=\"$COMMUNITY $OFFICIAL\"" in render_sh

    compose = _compose_raw()
    # 注：注释里可以提到这个名字（说明"为何没有"），故断言的是「没有赋值行」
    assert "APISIX_BOT_DOMAINS:" not in compose
    assert "APISIX_BOT_MAX_BODY_SIZE:" in compose


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


def should_not_publish_bot_cert_in_gateway_projected_volume() -> None:
    """子域下线后面板走社群域证书：网关证书卷里不该再有 bot 子域的键。"""
    text = (_K8S_BASE / "gateway" / "apisix.yaml").read_text(encoding="utf-8")
    assert f"{_RETIRED_BOT_DOMAIN}_fullchain.pem" not in text
    assert f"{_RETIRED_BOT_DOMAIN}_privkey.pem" not in text


def should_expose_bot_limit_but_not_domain_to_k8s_gateway() -> None:
    """上限仍要下发（少一处就渲染出占位残留）；域名变量已随子域下线清空。"""
    gw = (_K8S_BASE / "gateway" / "apisix.yaml").read_text(encoding="utf-8")
    assert gw.count("APISIX_BOT_MAX_BODY_SIZE") >= 2
    assert "APISIX_BOT_DOMAINS" not in gw
    cfg = (_K8S_BASE / "gateway" / "config.yaml").read_text(encoding="utf-8")
    # 上限放网关专属表：放进 lkm-config 会被 envFrom 灌进 backend/auth/worker（无消费方）
    assert 'APISIX_BOT_MAX_BODY_SIZE: "550000000"' in cfg
    assert "APISIX_BOT_DOMAINS" not in cfg
    for path in (
        _K8S_BASE / "gateway" / "config.yaml",
        _ROOT / "deploy" / "k8s" / "overlays" / "prod" / "gateway-domains.yaml",
        _ROOT / "deploy" / "k8s" / "gen-tls.sh",
    ):
        assert _RETIRED_BOT_DOMAIN not in path.read_text(encoding="utf-8"), path
    app_cfg = (_K8S_BASE / "app-config.yaml").read_text(encoding="utf-8")
    assert "BOT_MAX_UPLOAD" not in app_cfg


# ── 面板子路径与 SSO（并入社区后台）──────────────────────────────────────────


def should_pin_dashboard_base_path_to_subpath() -> None:
    """面板必须知道自己挂在 /bot 下：否则同域 cookie 会发给社区站全部路径，302 也会跳错地方。"""
    env = _services()["lkmbot"]["environment"]
    assert env["ASTRBOT_DASHBOARD_BASE_PATH"] == _BOT_PREFIX
    # 前端构建期 base 必须同一值（镜像内构建 dist，资源引用由它决定）
    assert _services()["lkmbot"]["build"]["args"]["VITE_BASE_PATH"] == f"{_BOT_PREFIX}/"
    k8s_env = {
        item["name"]: item.get("value")
        for item in next(
            d for d in _k8s_lkmbot() if d["kind"] == "Deployment"
        )["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert k8s_env["ASTRBOT_DASHBOARD_BASE_PATH"] == _BOT_PREFIX


def should_mount_sso_public_key_into_bot() -> None:
    """SSO 免登要 RS256 公钥（与网关同一份）；缺了只降级为「手动登录一次」，不是越权。"""
    svc = _services()["lkmbot"]
    mounts = [str(m) for m in svc["volumes"]]
    assert any("./deploy/jwt/keys:/etc/lkm/jwt:ro" in m for m in mounts), mounts
    assert (
        svc["environment"]["LKM_BOT_SSO_PUBLIC_KEY_FILE"]
        == "/etc/lkm/jwt/jwt-public.pem"
    )
    # 只给公钥目录，绝不给签发私钥
    assert not any("jwt-private" in m for m in mounts)

    container = next(d for d in _k8s_lkmbot() if d["kind"] == "Deployment")["spec"][
        "template"
    ]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["LKM_BOT_SSO_PUBLIC_KEY_FILE"] == "/etc/lkm/jwt/LKM_JWT_PUBLIC_KEY"
    volumes = next(d for d in _k8s_lkmbot() if d["kind"] == "Deployment")["spec"][
        "template"
    ]["spec"]["volumes"]
    jwt_volume = next(v for v in volumes if v["name"] == "jwtpub")
    # 只投影公钥这一个键（不 envFrom 整张 lkm-secrets）
    assert jwt_volume["secret"]["items"] == [
        {"key": "LKM_JWT_PUBLIC_KEY", "path": "LKM_JWT_PUBLIC_KEY"}
    ]
    assert jwt_volume["secret"]["optional"] is True


def should_ship_bundled_dashboard_dist_in_image() -> None:
    """dist 由镜像内构建并落到 bundled 位置：面板前端必须带 /bot base，不能靠运行期下载。"""
    dockerfile = (_ROOT / "LKM-bot" / "Dockerfile").read_text(encoding="utf-8")
    assert "pnpm build:subpath" in dockerfile
    assert "COPY --from=dashboard /dashboard/dist /LKMBot/astrbot/dashboard/dist" in (
        dockerfile
    )
    # 上游默认排除 dashboard/ 目录；构建既然要它，.dockerignore 必须放开源码（只排产物）
    dockerignore = (_ROOT / "LKM-bot" / ".dockerignore").read_text(encoding="utf-8")
    assert "dashboard/node_modules" in dockerignore
    assert re.search(r"^dashboard/$", dockerignore, flags=re.MULTILINE) is None
