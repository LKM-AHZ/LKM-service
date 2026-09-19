"""M5 7.2.4 APISIX 网关配置静态验收：路由/优先级/插件/TLS 渲染 + 单一来源展开。

解析 `deploy/apisix/{config.yaml,apisix.yaml}`（**模板**）与根 `docker-compose.yml`，并跑一次
render.sh（伪造证书）得到**渲染产物**，两者分别断言：模板只放占位（禁硬编码域名/上限），
产物里占位必须已展开为实值。运行时连通性（DNS discovery、WS、预签名）由 smoke.sh + 人工清单守。
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_APISIX_DIR = _ROOT / "deploy" / "apisix"
_COMPOSE = _ROOT / "docker-compose.yml"

_COMMUNITY = "lkm-ahz.ltd"
_OFFICIAL = "lkm-ahz.icu"
_DOMAINS = [_COMMUNITY, _OFFICIAL]
_ALL_HOSTS = [h for d in _DOMAINS for h in (d, f"www.{d}")]
# 模板里允许出现的占位（展开由 render.sh 负责）。
# 同时覆盖 apisix.yaml（路由模板）与 config.yaml（APISIX 自身配置模板）两个文件。
_PLACEHOLDERS = {
    "__SSL_SECTION__",
    "__COMMUNITY_DOMAIN__",
    "__COMMUNITY_HOSTS__",
    "__OFFICIAL_HOSTS__",
    "__ALL_HOSTS__",
    "__COMMUNITY_ORIGINS__",
    "__MAX_BODY_SIZE__",
    # upstream 服务名后缀：compose 空（Docker 内嵌 DNS 解析短名）/ k8s `.lkm.svc.cluster.local`
    "__UPSTREAM_SUFFIX__",
    # 上游 DNS：compose 127.0.0.11 / k8s CoreDNS ClusterIP
    "__DNS_RESOLVER__",
    # RS256 网关验签（批 5）：多行段占位，由 render.sh 从公钥文件展开
    "__JWT_CONSUMERS_SECTION__",
    "__JWT_ROUTE_SECTION__",
}


def _load(name: str) -> dict:
    return yaml.safe_load((_APISIX_DIR / name).read_text())


def _routes() -> dict[str, dict]:
    return {r["id"]: r for r in _load("apisix.yaml")["routes"]}


def _run_render(cert_root: Path, out_dir: Path, extra_env: dict | None = None) -> None:
    """在给定证书目录下跑一次 render.sh，产物落 out_dir/{apisix,config}.yaml。

    render.sh 现在渲染**两个**产物：路由（apisix.yaml）与 APISIX 自身配置（config.yaml），
    后者只有上游 DNS 一处随运行时变化。
    """
    env = {
        **os.environ,
        "APISIX_SRC": str(_APISIX_DIR / "apisix.yaml"),
        "APISIX_OUT": str(out_dir / "apisix.yaml"),
        "APISIX_SRC_CONFIG": str(_APISIX_DIR / "config.yaml"),
        "APISIX_OUT_CONFIG": str(out_dir / "config.yaml"),
        "APISIX_CERT_ROOT": str(cert_root),
        "APISIX_RENDER_ONCE": "1",
    }
    if extra_env:
        env.update(extra_env)
    subprocess.run(["sh", str(_APISIX_DIR / "render.sh")], env=env, check=True)


def _fake_certs(cert_root: Path) -> None:
    for domain in _DOMAINS:
        d = cert_root / domain
        d.mkdir(parents=True, exist_ok=True)
        (d / "fullchain.pem").write_text(
            "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
        )
        (d / "privkey.pem").write_text(
            "-----BEGIN PRIVATE KEY-----\nMIIEfake\n-----END PRIVATE KEY-----\n"
        )


@lru_cache(maxsize=1)
def _render_default() -> tuple[dict, dict]:
    """用**默认环境**（即 compose 口径：无 UPSTREAM_SUFFIX、DNS=127.0.0.11）渲染一次。

    模板里的 hosts/CORS 来源/请求体上限/upstream 服务名都是占位，故断言实值的用例
    必须看产物。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="apisix-render-"))
    cert_root = tmp / "live"
    _fake_certs(cert_root)
    out_dir = tmp / "out"
    out_dir.mkdir(parents=True)
    _run_render(cert_root, out_dir)
    return (
        yaml.safe_load((out_dir / "apisix.yaml").read_text()),
        yaml.safe_load((out_dir / "config.yaml").read_text()),
    )


def _rendered() -> dict:
    return _render_default()[0]


def _rendered_config() -> dict:
    return _render_default()[1]


def _rendered_routes() -> dict[str, dict]:
    return {r["id"]: r for r in _rendered()["routes"]}


def test_config_yaml_standalone() -> None:
    cfg = _load("config.yaml")
    # standalone(无 etcd)正确形态是 data_plane；role_traditional 只接受 etcd
    assert cfg["deployment"]["role"] == "data_plane"
    assert cfg["deployment"]["role_data_plane"]["config_provider"] == "yaml"
    assert cfg["apisix"]["node_listen"] == 9080
    ssl_listen = cfg["apisix"]["ssl"]["listen"]
    assert any(item["port"] == 9443 for item in ssl_listen)
    # 容器内 ssl 听 9443（非 root 不能听 443），对外重定向端口须显式 443，否则 Location 带 :9443
    assert cfg["plugin_attr"]["redirect"]["https_port"] == 443


def test_config_yaml_dns_is_placeholder_only() -> None:
    """config.yaml 现为**模板**：上游 DNS 两处只放占位，实值由 render.sh 展开。

    此前 config.yaml 是直接挂载的静态文件，DNS 硬编码 127.0.0.11（Docker 内嵌 DNS）；
    k8s 下必须换成 CoreDNS 的 ClusterIP，故改为占位 + 环境变量展开，避免出现第二份
    config.yaml 副本（第二真相源）。此断言即守「模板不得回退成硬编码」。
    """
    cfg = _load("config.yaml")
    # APISIX schema 要求数组；曾因写成裸字符串导致启动即校验失败、而旧断言同错故"假绿"
    assert cfg["apisix"]["dns_resolver"] == ["__DNS_RESOLVER__"]
    # discovery 模块须显式初始化，否则 discovery_type: dns 运行时 503
    assert cfg["discovery"]["dns"]["servers"] == ["__DNS_RESOLVER__"]


def test_dns_resolver_expands_per_runtime() -> None:
    """同一个模板要能在两种运行时展开出各自的 DNS（compose / k8s）。"""
    import tempfile

    # ① 默认（compose）：Docker 内嵌 DNS，行为与改造前完全一致
    assert _rendered_config()["apisix"]["dns_resolver"] == ["127.0.0.11"]
    assert _rendered_config()["discovery"]["dns"]["servers"] == ["127.0.0.11"]

    # ② k8s：显式给 CoreDNS ClusterIP
    tmp = Path(tempfile.mkdtemp(prefix="apisix-render-dns-"))
    cert_root = tmp / "live"
    _fake_certs(cert_root)
    out_dir = tmp / "out"
    out_dir.mkdir(parents=True)
    _run_render(cert_root, out_dir, {"APISIX_DNS_RESOLVER": "10.96.0.10"})
    cfg = yaml.safe_load((out_dir / "config.yaml").read_text())
    assert cfg["apisix"]["dns_resolver"] == ["10.96.0.10"]
    assert cfg["discovery"]["dns"]["servers"] == ["10.96.0.10"]


def test_upstream_suffix_expands_per_runtime() -> None:
    """upstream 服务名后缀：compose 空（短名）/ k8s 补全 FQDN。

    CoreDNS 不做 search domain 补全（lua-resty-dns 是裸查询），k8s 下若仍写短名
    `backend:8000` 会解析不到 → 网关全量 503。此处双向断言。
    """
    import tempfile

    # ① 默认（compose）：短名保持不变
    assert (
        _rendered_routes()["api-prefix"]["upstream"]["service_name"] == "backend:8000"
    )

    # ② k8s：补全 FQDN
    tmp = Path(tempfile.mkdtemp(prefix="apisix-render-suffix-"))
    cert_root = tmp / "live"
    _fake_certs(cert_root)
    out_dir = tmp / "out"
    out_dir.mkdir(parents=True)
    _run_render(
        cert_root, out_dir, {"APISIX_UPSTREAM_SUFFIX": ".lkm.svc.cluster.local"}
    )
    routes = {
        r["id"]: r
        for r in yaml.safe_load((out_dir / "apisix.yaml").read_text())["routes"]
    }
    assert (
        routes["api-prefix"]["upstream"]["service_name"]
        == "backend.lkm.svc.cluster.local:8000"
    )
    assert (
        routes["minio"]["upstream"]["service_name"]
        == "minio.lkm.svc.cluster.local:9000"
    )
    # 渲染产物里不得残留任何占位
    raw = (out_dir / "apisix.yaml").read_text()
    assert "__UPSTREAM_SUFFIX__" not in raw


def test_dual_domain_hosts_covered() -> None:
    """渲染产物里两个域名（含 www）都被路由覆盖。"""
    routes = _rendered_routes()
    hosted = {h for r in routes.values() for h in (r.get("hosts") or [])}
    assert set(_ALL_HOSTS) <= hosted


def test_template_has_no_hardcoded_domains_or_limits() -> None:
    """模板只放占位：域名与请求体上限均不得硬编码（单一来源由 render.sh 展开）。

    此前 hosts 硬编码 12 处、CORS 来源 8 处、max_body_size 8 处，改域名必漏。
    """
    raw = (_APISIX_DIR / "apisix.yaml").read_text()
    # 去掉占位后不应再出现任何真实域名或裸数字上限
    for host in _ALL_HOSTS:
        assert host not in raw, f"模板仍硬编码域名 {host}"
    assert "max_body_size: 104857600" not in raw
    assert "max_body_size: __MAX_BODY_SIZE__" in raw
    # 模板里出现的占位必须是已登记的那批（防拼错导致 render 后残留）。
    # config.yaml 也是模板（DNS 占位），同样纳入登记表校验。
    for name in ("apisix.yaml", "config.yaml"):
        text = (_APISIX_DIR / name).read_text()
        assert set(re.findall(r"__[A-Z_]+__", text)) <= _PLACEHOLDERS, name


def test_render_expands_all_placeholders() -> None:
    """渲染后不得残留任何未展开占位（render.sh 自身也会拒发含残留的配置）。"""
    rendered = _rendered()
    leftover = set(re.findall(r"__[A-Z_]+__", yaml.safe_dump(rendered)))
    assert leftover <= {"__SSL_SECTION__"}  # 仅模板注释里的说明文字会被带出


def test_render_expands_domains_and_body_limit() -> None:
    routes = _rendered_routes()
    assert routes["api-prefix"]["hosts"] == [
        _COMMUNITY,
        f"www.{_COMMUNITY}",
    ]
    assert routes["official-site"]["hosts"] == [_OFFICIAL, f"www.{_OFFICIAL}"]
    assert routes["acme-challenge"]["hosts"] == _ALL_HOSTS
    cors = routes["api-prefix"]["plugins"]["cors"]
    assert cors["allow_origins"] == f"https://{_COMMUNITY},https://www.{_COMMUNITY}"
    assert (
        routes["api-prefix"]["plugins"]["client-control"]["max_body_size"] == 104857600
    )


def test_exact_admin_me_beats_prefix() -> None:
    # 用渲染产物：upstream 服务名在模板里带 __UPSTREAM_SUFFIX__ 占位，
    # 在模板上断言 startswith("backend:") 会恒假（或反之恒真）——必须在产物上看实值。
    routes = _rendered_routes()
    me = routes["admin-auth-me"]
    prefix = routes["admin-auth-prefix"]
    assert me["uri"] == "/api/v1/admin/auth/me"
    assert prefix["uri"] == "/api/v1/admin/auth/*"
    assert me["priority"] > prefix["priority"]
    assert me["upstream"]["service_name"].startswith("backend:")
    assert prefix["upstream"]["service_name"].startswith("auth:")


def test_auth_prefix_split() -> None:
    routes = _rendered_routes()
    assert routes["auth-prefix"]["uri"] == "/api/v1/auth/*"
    assert routes["auth-prefix"]["upstream"]["service_name"].startswith("auth:")


def test_graphql_websocket_enabled() -> None:
    routes = _rendered_routes()
    assert routes["graphql-exact"]["enable_websocket"] is True
    assert routes["graphql-prefix"]["enable_websocket"] is True
    assert routes["graphql-exact"]["upstream"]["service_name"].startswith("backend:")


def test_realtime_ws_endpoint_upgrade_enabled() -> None:
    """/api/v1/ws/events 走 api-prefix，必须开 upgrade（网关未开→后端收普通 GET 404）。"""
    route = _rendered_routes()["api-prefix"]
    assert route["enable_websocket"] is True
    assert route["upstream"]["service_name"].startswith("backend:")


def test_minio_presign_host_rewrite() -> None:
    route = _rendered_routes()["minio"]
    assert route["uri"] == "/lkm/*"
    # pass_host=rewrite 的 Host 必须落 upstream.upstream_host；用 proxy-rewrite.host 会被
    # pass_host 逻辑以 nil 覆盖 → 空 Host → MinIO 400（真机验收暴露）
    assert "plugins" not in route
    assert route["upstream"]["pass_host"] == "rewrite"
    assert route["upstream"]["upstream_host"] == _COMMUNITY
    assert route["upstream"]["service_name"].startswith("minio:")


def test_upload_routes_have_body_limit() -> None:
    routes = _rendered_routes()
    for rid in (
        "auth-login",
        "admin-auth-me",
        "admin-auth-prefix",
        "auth-prefix",
        "api-exact",
        "api-prefix",
        "graphql-exact",
        "graphql-prefix",
    ):
        cc = routes[rid]["plugins"]["client-control"]
        assert cc["max_body_size"] == 104857600


def test_cache_headers_on_assets() -> None:
    routes = _routes()
    for rid in ("astro-assets", "avatars"):
        cache = routes[rid]["plugins"]["response-rewrite"]["headers"]["set"][
            "Cache-Control"
        ]
        assert "max-age=31536000" in cache


def test_cors_explicit_whitelist_no_empty_fallback() -> None:
    """M6.1：网关 CORS 须显式白名单，禁回退 `cors: {}`（默认等价 `*`，对全网开放）。"""
    routes = _rendered_routes()
    cors_routes = [
        rid for rid, r in routes.items() if "cors" in (r.get("plugins") or {})
    ]
    assert cors_routes, "至少承载 API/认证面的路由须配 cors"
    for rid in cors_routes:
        cors = routes[rid]["plugins"]["cors"]
        assert cors, f"{rid}.cors 不可为空兜底"
        origins = cors["allow_origins"]
        assert "*" not in origins, f"{rid}.cors 不得含通配来源"
        # 两个社区域名（含 www）均在白名单内（实值由 render.sh 从域名变量展开）
        assert f"https://{_COMMUNITY}" in origins
        assert f"https://www.{_COMMUNITY}" in origins
        assert cors["allow_credential"] is True
        # APISIX cors schema 硬规则：allow_credential=true 时四个字段**任一**为 `*` 即校验失败
        # → **整条路由不被加载**（限流/WS upgrade/CORS 静默失效，流量退化为经 astro 转发）。
        # 按规则本身断言，而非照抄实现取值（否则与实现同错 → 假绿，见 §8 #19/#26）。
        for field in (
            "allow_origins",
            "allow_methods",
            "allow_headers",
            "expose_headers",
        ):
            assert "*" not in str(cors.get(field, "")), f"{rid}.cors.{field} 不得为 *"


def test_every_api_route_carries_cors() -> None:
    """每条代理到 backend/auth 的路由都必须带 cors 插件。

    生产不挂应用层 CORS（见 LKM-service/app/core/middleware.py 的取舍），网关是唯一权威；
    新增对外 API 路由若漏配 cors，浏览器跨域会**完全没有**响应头且无兜底 —— 本断言即守此回归。

    必须在**渲染产物**上跑：模板里的 upstream 带 __UPSTREAM_SUFFIX__ 占位，
    在模板上判 startswith(("backend:","auth:")) 会一条都不匹配 → 断言空转、假绿。
    """
    for rid, route in _rendered_routes().items():
        upstream = str((route.get("upstream") or {}).get("service_name", ""))
        if not upstream.startswith(("backend:", "auth:")):
            continue
        cors = (route.get("plugins") or {}).get("cors")
        assert cors, f"{rid} 代理到 {upstream} 却未配 cors 插件（生产无应用层兜底）"


def test_global_gzip_and_login_rate_limit() -> None:
    data = _load("apisix.yaml")
    assert any("gzip" in rule["plugins"] for rule in data["global_rules"])
    limit = _routes()["auth-login"]["plugins"]["limit-count"]
    assert limit["policy"] == "local"
    assert limit["rejected_code"] == 429


def test_global_forwarded_headers_parity() -> None:
    """转发头契约：X-Real-IP/XFF/Proto 必须由 global rule 设（APISIX 默认不设）。"""
    data = _load("apisix.yaml")
    set_headers: dict[str, str] = {}
    for rule in data["global_rules"]:
        pr = rule["plugins"].get("proxy-rewrite", {})
        set_headers.update(pr.get("headers", {}).get("set", {}))
    assert set_headers["X-Real-IP"] == "$remote_addr"
    assert set_headers["X-Forwarded-For"] == "$remote_addr"
    assert set_headers["X-Forwarded-Proto"] == "$scheme"


def test_http_to_https_redirect_and_acme_precedence() -> None:
    routes = _rendered_routes()
    redirect = routes["http-redirect"]
    assert redirect["plugins"]["redirect"]["http_to_https"] is True
    assert redirect["vars"] == [["scheme", "==", "http"]]
    acme = routes["acme-challenge"]
    assert acme["priority"] > redirect["priority"]
    assert acme["upstream"]["service_name"].startswith("acme-webroot:")


def test_ssl_placeholder_and_end_marker() -> None:
    raw = (_APISIX_DIR / "apisix.yaml").read_text()
    assert "# __SSL_SECTION__" in raw
    assert raw.rstrip().endswith("#END")


def test_render_script_inlines_certs(tmp_path: Path) -> None:
    cert_root = tmp_path / "live"
    for domain in _DOMAINS:
        d = cert_root / domain
        d.mkdir(parents=True)
        (d / "fullchain.pem").write_text(
            "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
        )
        (d / "privkey.pem").write_text(
            "-----BEGIN PRIVATE KEY-----\nMIIEfake\n-----END PRIVATE KEY-----\n"
        )
    out = tmp_path / "out" / "apisix.yaml"
    out.parent.mkdir(parents=True)
    env = {
        **os.environ,
        "APISIX_SRC": str(_APISIX_DIR / "apisix.yaml"),
        "APISIX_OUT": str(out),
        "APISIX_CERT_ROOT": str(cert_root),
        "APISIX_RENDER_ONCE": "1",
    }
    subprocess.run(["sh", str(_APISIX_DIR / "render.sh")], env=env, check=True)

    rendered = yaml.safe_load(out.read_text())
    assert len(rendered["ssls"]) == 2
    for entry in rendered["ssls"]:
        assert "BEGIN CERTIFICATE" in entry["cert"]
        assert "BEGIN PRIVATE KEY" in entry["key"]
        assert any(
            s.endswith("lkm-ahz.ltd") or s.endswith("lkm-ahz.icu")
            for s in entry["snis"]
        )
    # 路由模板完整保留
    assert len(rendered["routes"]) == len(_load("apisix.yaml")["routes"])


# ── nginx 移除（防回潮）────────────────────────────────────────────────────


def _services() -> dict[str, dict]:
    return yaml.safe_load(_COMPOSE.read_text())["services"]


def test_nginx_gateway_fully_removed() -> None:
    """nginx 网关服务与 `nginx-gateway` 回退 profile 均已删除，配置目录亦不存在。

    回退路径由 git 承担（`git revert` 得到的是「当时一致」的整套配置）；保留一份与 APISIX
    路由分叉的 nginx 配置＝会腐烂的第二真相源，故以断言防其回潮。
    """
    assert "nginx" not in _services()
    assert "nginx-gateway" not in _COMPOSE.read_text()
    assert not (_ROOT / "deploy" / "nginx").exists()


def test_only_apisix_publishes_gateway_ports() -> None:
    """80/443 只由 APISIX 发布——全栈不存在第二个网关。"""
    owners = {
        name
        for name, svc in _services().items()
        for port in (svc.get("ports") or [])
        if str(port).split(":")[0] in {"80", "443"}
    }
    assert owners == {"apisix"}


def test_nginx_kept_only_as_static_file_servers() -> None:
    """仅存的 nginx 镜像是静态文件服务器角色（ACME responder / 官网源站），且不发布 80/443。"""
    nginx_services = {
        name
        for name, svc in _services().items()
        if "nginx" in str(svc.get("image", ""))
    }
    assert nginx_services <= {"acme-webroot", "static"}
    for name in nginx_services:
        assert not set(_services()[name].get("ports") or [])


def test_apisix_render_is_pure_shell_sidecar() -> None:
    """apisix-render 只借 shell 跑 render.sh，不应使用任何 Web 服务器镜像。"""
    image = _services()["apisix-render"]["image"]
    assert "nginx" not in image
    assert image.startswith("alpine:")


def test_certbot_entrypoint_relocated() -> None:
    """certbot 入口脚本随 nginx 目录移除迁到 deploy/certbot/，compose 挂载路径同步。"""
    assert (_ROOT / "deploy" / "certbot" / "entrypoint.sh").is_file()
    mounts = _services()["certbot"]["volumes"]
    assert any("./deploy/certbot/entrypoint.sh" in str(m) for m in mounts)


def test_apisix_mounts_rendered_config_not_repo_template() -> None:
    """APISIX 必须挂 apisix-render 的**渲染产物**，不能直接挂仓库里的 config.yaml 模板。

    config.yaml 自本次改造起是模板（`__DNS_RESOLVER__` 占位），直接挂原文件会让 APISIX
    读到字面量占位符当 DNS 地址 → 启动即校验失败。两个产物（config.yaml/apisix.yaml）
    都必须来自 apisix_conf 卷。
    """
    mounts = _services()["apisix"]["volumes"]
    as_str = [str(m) for m in mounts]
    assert not any("./deploy/apisix/config.yaml" in m for m in as_str), (
        "apisix 直接挂了模板文件；应改为挂 apisix_conf 卷里的渲染产物"
    )
    for target in ("config.yaml", "apisix.yaml"):
        assert any(target in m and "apisix_conf" in m for m in as_str), (
            f"apisix 未从 apisix_conf 卷挂载渲染后的 {target}"
        )
    # render sidecar 的健康检查必须同时覆盖两个产物，否则 apisix 可能在没有 config.yaml
    # 的情况下被 depends_on 放行
    hc = str(_services()["apisix-render"]["healthcheck"]["test"])
    assert "/out/config.yaml" in hc and "/out/apisix.yaml" in hc


# ─────────────────────── 批 5：RS256 网关验签 ───────────────────────
#
# 两条硬规则（都是真机踩出来的，写死为断言防回潮）：
#   ① APISIX consumer username 必须匹配 ^[a-zA-Z0-9_]+$（带 `-` 会让 consumer 配置校验失败）；
#   ② jwt-auth 的 RS256 consumer schema 要求 public_key **与** private_key 同时存在，而验签
#      路径只用 public_key —— 故网关只填公钥（两处同值），网关因此不持有签发能力。

_JWT_CONSUMERS_MARKER = "__JWT_CONSUMERS_SECTION__"
_JWT_ROUTE_MARKER = "__JWT_ROUTE_SECTION__"


def _fake_public_pem() -> str:
    return (
        "-----BEGIN PUBLIC KEY-----\n"
        "MIIBfake-public-key-material\n"
        "-----END PUBLIC KEY-----\n"
    )


def _render_with_jwt_key() -> dict:
    """带公钥文件渲染一次（网关验签开启路径）。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="apisix-jwt-"))
    cert_root = tmp / "live"
    _fake_certs(cert_root)
    out_dir = tmp / "out"
    out_dir.mkdir(parents=True)
    key_file = tmp / "jwt-public.pem"
    key_file.write_text(_fake_public_pem())
    _run_render(
        cert_root, out_dir, extra_env={"APISIX_JWT_PUBLIC_KEY_FILE": str(key_file)}
    )
    return yaml.safe_load((out_dir / "apisix.yaml").read_text())


def test_jwt_sections_are_multiline_placeholders_only() -> None:
    """模板里只有段占位，不得出现任何密钥材料。"""
    raw = (_APISIX_DIR / "apisix.yaml").read_text()
    assert _JWT_CONSUMERS_MARKER in raw and _JWT_ROUTE_MARKER in raw
    assert "BEGIN PUBLIC KEY" not in raw and "BEGIN PRIVATE KEY" not in raw


def test_gateway_jwt_disabled_without_key_file() -> None:
    """未配置公钥：不出现消费者，admin-me 路由也不挂 jwt-auth（应用层照常验签）。"""
    rendered = _rendered()
    assert not rendered.get("consumers")
    assert "jwt-auth" not in _rendered_routes()["admin-auth-me"]["plugins"]


def test_gateway_jwt_enabled_with_key_file() -> None:
    rendered = _render_with_jwt_key()
    consumers = rendered["consumers"]
    assert len(consumers) == 1
    consumer = consumers[0]
    # 规则①：username 必须匹配 APISIX 的 ^[a-zA-Z0-9_]+$，否则 consumer 校验失败
    assert re.fullmatch(r"[a-zA-Z0-9_]+", consumer["username"]), consumer["username"]

    auth_conf = consumer["plugins"]["jwt-auth"]
    assert auth_conf["algorithm"] == "RS256"
    assert auth_conf["key"] == "lkm"
    assert auth_conf["public_key"] == _fake_public_pem()
    # 规则②：schema 强制 private_key 字段，但验签只用 public_key → 网关只放公钥
    assert auth_conf["private_key"] == auth_conf["public_key"]
    assert "PRIVATE KEY" not in auth_conf["private_key"]

    routes = {r["id"]: r for r in rendered["routes"]}
    jwt_plugin = routes["admin-auth-me"]["plugins"]["jwt-auth"]
    # 后台会话走 cookie（admin_session），不是 Authorization 头
    assert jwt_plugin["cookie"] == "admin_session"
    # **只有**明确需要登录的路由挂验签：/api/* 前缀下大量公开只读接口必须保持匿名
    only_jwt = [rid for rid, r in routes.items() if "jwt-auth" in r.get("plugins", {})]
    assert only_jwt == ["admin-auth-me"], only_jwt


def test_gateway_consumer_key_matches_app_token_claim() -> None:
    """网关消费者 key 必须等于 app 侧写入 token 的 claim 值（跨仓一致性，改一边即红）。"""
    from auth import jwt_keys

    rendered = _render_with_jwt_key()
    assert (
        rendered["consumers"][0]["plugins"]["jwt-auth"]["key"] == jwt_keys.GATEWAY_KEY
    )


def _compose_services() -> dict[str, dict]:
    return yaml.safe_load(_COMPOSE.read_text())["services"]


def test_compose_mounts_jwt_keys_and_scopes_private_key() -> None:
    """私钥只下发到签发方 auth；backend / 网关渲染只拿公钥目录。"""
    services = _compose_services()
    assert "./deploy/jwt/keys:/etc/lkm/jwt:ro" in services["apisix-render"]["volumes"]

    def _env_of(name: str) -> dict:
        env = services[name].get("environment", {})
        # compose 支持 list 形式，本项目统一用 map 形式；这里容错取键名集合
        return env if isinstance(env, dict) else {k.split("=")[0]: "" for k in env}

    for name in ("apisix-render", "backend"):
        assert "LKM_JWT_PRIVATE_KEY_FILE" not in _env_of(name), name
    assert "LKM_JWT_PRIVATE_KEY_FILE" in _env_of("auth")
    # 三个验签方都要拿到公钥来源：auth 供 JWKS、backend 供本地验签、render 供网关消费者
    assert "LKM_JWT_PUBLIC_KEY_FILE" in _env_of("auth")
    assert "LKM_JWT_PUBLIC_KEY_FILE" in _env_of("backend")
    assert "APISIX_JWT_PUBLIC_KEY_FILE" in _env_of("apisix-render")


def test_k8s_private_key_secret_is_auth_only_and_optional() -> None:
    """k8s：私钥走独立 Secret 且 optional（未生成密钥时 Pod 仍能起）。"""
    gateway = (
        _ROOT / "deploy" / "k8s" / "base" / "gateway" / "apisix.yaml"
    ).read_text()
    auth = (_ROOT / "deploy" / "k8s" / "base" / "app" / "auth.yaml").read_text()
    assert "lkm-jwt-signing" in auth
    assert "optional: true" in auth.split("lkm-jwt-signing", 1)[1][:40]
    assert "lkm-jwt-signing" not in gateway  # 网关只拿公钥
    assert "APISIX_JWT_PUBLIC_KEY_FILE" in gateway
