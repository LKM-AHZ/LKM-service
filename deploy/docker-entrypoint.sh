#!/bin/sh
# LKM 服务统一入口（M5 7.2.3）：
# 启动前按需从 Infisical 拉取密钥注入环境，再 exec 原 command（backend/auth/各 worker 通用）。
# LKM_INFISICAL_ENABLED 默认未设/false 时 bootstrap 直接返回 0，不触网、零副作用。
set -e

python -m app.core.secrets_bootstrap

exec "$@"
