"""backend 的 admin 读取面依赖 auth 库 DSN（compose / k8s 两处都必须注入）。

背景
    ``/api/v1/admin/users``、``/api/v1/admin/stats`` 在 **backend** 进程内开 auth 库
    只读会话（``app/modules/admin/users_router.py`` → ``core.ports.audit.new_auth_session``
    → ``auth.db.session``），因为 HTTP 缝只提供按 id 的批量读，列表/聚合要直连
    auth authoritative。缺 ``LKM_AUTH_DB_*`` 时这两个端点连不上 auth 库。

    这是「代码齐了、部署少一行」的静默缺口：单测通过（fixture 覆盖了会话依赖），
    只有真机上打开后台用户列表才会 500。故用部署契约把它钉住。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website

_AUTH_DB_KEYS = {
    "LKM_AUTH_DB_HOST",
    "LKM_AUTH_DB_PORT",
    "LKM_AUTH_DB_NAME",
    "LKM_AUTH_DB_USER",
    "LKM_AUTH_DB_PASSWORD",
}


def _compose_env_keys(service: dict[str, Any]) -> set[str]:
    env = service.get("environment", {})
    entries = env if isinstance(env, list) else list(env)
    return {entry.split("=", 1)[0] for entry in entries}


def test_compose_backend_has_auth_db_dsn() -> None:
    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    missing = sorted(_AUTH_DB_KEYS - _compose_env_keys(compose["services"]["backend"]))
    assert not missing, (
        f"compose backend 缺 auth 库变量：{missing}"
        "（/admin/users、/admin/stats 会连不上 auth 库）"
    )


def test_k8s_backend_mounts_auth_db_configmap() -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(
            (_ROOT / "deploy/k8s/base/app/backend.yaml").read_text(encoding="utf-8")
        )
        if doc and doc.get("kind") == "Deployment"
    ]
    assert len(docs) == 1
    container = docs[0]["spec"]["template"]["spec"]["containers"][0]
    names = {
        item[key]["name"]
        for item in container["envFrom"]
        for key in ("configMapRef", "secretRef")
        if key in item
    }
    assert "lkm-config-authdb" in names, "backend 未挂 auth 库 ConfigMap"
    assert "lkm-secrets" in names, "backend 未挂 lkm-secrets（auth 库密码在其中）"
