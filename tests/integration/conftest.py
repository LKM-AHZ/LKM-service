"""Testcontainers 集成环境（M6.12）：按需起真 PG / Redis / Pulsar 容器。

与 CI 的分工：
- CI 常规 job 用 **service containers**（快、无额外依赖）跑全套单测；
- integration job（本目录）用 **Testcontainers** 起真实中间件，自足且含 Pulsar。

默认**不启用**：只有显式设 ``LKM_IT_USE_TESTCONTAINERS=1`` 才起容器并注入
``LKM_REDIS_URL``/``LKM_PULSAR_URL``（避免本地跑 ``-m integration`` 时无谓拉起 Pulsar
——那是分钟级启动）。未启用时既有 skip 逻辑照旧（env 为空 → skip 而非红）。

本 fixture 是 **autouse**，因此环境不可用时**只打印警告、绝不 skip**——否则会把
「docker 不可用」放大成整套测试静默跳过。用例仍按原有「env 为空 → skip」判定。
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from typing import Any

import pytest


def _enabled() -> bool:
    return os.environ.get("LKM_IT_USE_TESTCONTAINERS") == "1"


@pytest.fixture(scope="session", autouse=True)
def _integration_containers() -> Iterator[None]:
    """按需拉起 PG/Redis/Pulsar 并注入连接 env；未启用或环境不可用则原样放行。"""
    if not _enabled():
        yield
        return

    containers: list[Any] = []
    try:
        import time

        from testcontainers.core.container import DockerContainer

        # 新版把预置模块挪到 testcontainers.community.*（旧路径会抛「已废弃」）；
        # 兼容两种布局，避免 CI 上因版本差异整体 skip。
        try:
            from testcontainers.community.postgres import PostgresContainer
            from testcontainers.community.redis import RedisContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer
            from testcontainers.redis import RedisContainer

        postgres = PostgresContainer(
            "postgres:16-alpine",
            username="postgres",
            password="postgres",
            dbname="lkm",
        )
        redis = RedisContainer("redis:7-alpine")
        pulsar = (
            DockerContainer("apachepulsar/pulsar:3.3.0")  # 与 compose/k8s 同版本
            .with_exposed_ports(6650)
            .with_command("bin/pulsar standalone")
        )
        for c in (postgres, redis, pulsar):
            # 先登记再 start：start() 中途抛错（拉镜像失败/端口占用）时该容器可能已经起来，
            # 只有先 append 才能被 finally 回收。
            containers.append(c)
            c.start()

        def _pulsar_admin(*args: str) -> int:
            """在 Pulsar 容器内跑 pulsar-admin（走 docker-py 原生 exec，跨版本稳定）。"""
            res = pulsar.get_wrapped_container().exec_run(
                ["bin/pulsar-admin", *args]
            )
            return int(res.exit_code)

        # Pulsar standalone 就绪：轮询 healthcheck（首次启动通常 30~60s）
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if _pulsar_admin("brokers", "healthcheck") == 0:
                break
            time.sleep(3)
        else:
            # 超时不得静默放行：否则会带着「一个没就绪的 broker」继续建租户/注入 URL，
            # 失败点散落到后续用例里，变成难查的 flaky，而不是明确的环境错误。
            raise RuntimeError("Pulsar 未在 180s 内就绪（integration fixture 中止）")

        # 租户/namespace（幂等；生产由 compose 的 pulsar entrypoint 建）
        for args in (
            ("tenants", "create", "lkm"),
            ("namespaces", "create", "lkm/biz"),
            ("namespaces", "create", "lkm/auth"),
            ("namespaces", "create", "lkm/system"),
        ):
            _pulsar_admin(*args)

        host = redis.get_container_host_ip()
        redis_url = f"redis://{host}:{redis.get_exposed_port(6379)}/0"
        phost = pulsar.get_container_host_ip()
        pulsar_url = f"pulsar://{phost}:{pulsar.get_exposed_port(6650)}"
        os.environ["LKM_REDIS_URL"] = redis_url
        os.environ["LKM_PULSAR_URL"] = pulsar_url

        # 同时改**已实例化**的 settings：部分用例（如 check_code_rate_limit）先看
        # settings.redis_url 是否为空再决定是否限流，只设 env 对它无效（Settings 在
        # 模块导入时就已读盘）。
        from app.core.config import settings as _settings

        _settings.redis_url = redis_url  # pydantic 自动转 SecretStr
        with contextlib.suppress(Exception):
            _settings.pulsar_url = pulsar_url

        os.environ.setdefault(
            "LKM_IT_PG_URL",
            postgres.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+asyncpg://"
            ),
        )
    except Exception as exc:
        print(f"[integration] Testcontainers 环境不可用，按 env 判定走 skip：{exc}")

    try:
        yield
    finally:
        # 不再用「全部就绪才算 started」做闸门：setup 中途失败（Pulsar 就绪超时、
        # get_exposed_port 抛错、settings 导入失败……）时同样要把已起的容器收掉，
        # 否则它们会一直留在机器上占资源。
        for c in reversed(containers):
            # stop() 之外再走底层 docker-py 强制 remove：实测仅 stop() 会留下
            # 运行中的容器（stop 卡住/异常被 suppress），残留会一直占资源。
            # 两级清理都失败时必须留痕：这正是「残留容器」场景，静默吞掉等于问题不可见。
            try:
                c.stop()
            except Exception as exc:
                print(f"[integration] 容器 stop 失败（尝试强制 remove）：{exc}")
            try:
                c.get_wrapped_container().remove(force=True)
            except Exception as exc:
                print(
                    f"[integration] ✗ 容器 remove 也失败，可能残留：{c!r}：{exc}",
                    file=sys.stderr,
                )
