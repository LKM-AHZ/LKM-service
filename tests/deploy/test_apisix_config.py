"""M5 7.2.4 APISIX 网关配置静态验收：路由/优先级/插件/TLS 渲染。

解析 `deploy/apisix/{config.yaml,apisix.yaml}` 并跑一次 render.sh（伪造证书），断言
nginx 全量替换后的关键契约。运行时连通性（DNS discovery、WS、预签名）由 smoke.sh + 人工清单守。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_APISIX_DIR = _ROOT / "deploy" / "apisix"

_DOMAINS = ["lkm-ahz.ltd", "lkm-ahz.icu"]
_ALL_HOSTS = [h for d in _DOMAINS for h in (d, f"www.{d}")]


def _load(name: str) -> dict:
    return yaml.safe_load((_APISIX_DIR / name).read_text())


def _routes() -> dict[str, dict]:
    return {r["id"]: r for r in _load("apisix.yaml")["routes"]}


def test_config_yaml_standalone() -> None:
    cfg = _load("config.yaml")
    assert cfg["deployment"]["role"] == "traditional"
    assert cfg["deployment"]["role_traditional"]["config_provider"] == "yaml"
    assert cfg["apisix"]["node_listen"] == 9080
    ssl_listen = cfg["apisix"]["ssl"]["listen"]
    assert any(item["port"] == 9443 for item in ssl_listen)
    assert cfg["apisix"]["dns_resolver"] == "127.0.0.11"


def test_dual_domain_hosts_covered() -> None:
    routes = _routes()
    hosted = {h for r in routes.values() for h in (r.get("hosts") or [])}
    assert set(_ALL_HOSTS) <= hosted


def test_exact_admin_me_beats_prefix() -> None:
    routes = _routes()
    me = routes["admin-auth-me"]
    prefix = routes["admin-auth-prefix"]
    assert me["uri"] == "/api/v1/admin/auth/me"
    assert prefix["uri"] == "/api/v1/admin/auth/*"
    assert me["priority"] > prefix["priority"]
    assert me["upstream"]["service_name"].startswith("backend:")
    assert prefix["upstream"]["service_name"].startswith("auth:")


def test_auth_prefix_split() -> None:
    routes = _routes()
    assert routes["auth-prefix"]["uri"] == "/api/v1/auth/*"
    assert routes["auth-prefix"]["upstream"]["service_name"].startswith("auth:")


def test_graphql_websocket_enabled() -> None:
    routes = _routes()
    assert routes["graphql-exact"]["enable_websocket"] is True
    assert routes["graphql-prefix"]["enable_websocket"] is True
    assert routes["graphql-exact"]["upstream"]["service_name"].startswith("backend:")


def test_minio_presign_host_rewrite() -> None:
    route = _routes()["minio"]
    assert route["uri"] == "/lkm/*"
    assert route["plugins"]["proxy-rewrite"]["host"] == "lkm-ahz.ltd"
    assert route["upstream"]["service_name"].startswith("minio:")


def test_upload_routes_have_body_limit() -> None:
    routes = _routes()
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


def test_global_gzip_and_login_rate_limit() -> None:
    data = _load("apisix.yaml")
    assert any("gzip" in rule["plugins"] for rule in data["global_rules"])
    limit = _routes()["auth-login"]["plugins"]["limit-count"]
    assert limit["policy"] == "local"
    assert limit["rejected_code"] == 429


def test_http_to_https_redirect_and_acme_precedence() -> None:
    routes = _routes()
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
