#!/bin/sh
# LKM 服务统一入口（M5 7.2.3）：
# 由同一个 Python 进程拉取密钥并 exec 原 command，确保注入的环境变量传给服务。
# LKM_INFISICAL_ENABLED 默认未设/false 时 bootstrap 直接返回 0，不触网、零副作用。
set -e

# 无参数时 exec 在 POSIX sh 里是 no-op（多数字典实现），容器会「健康地」立即退出 0；
# 这属于编排配置错误（CMD 被覆盖为空 / docker run 未带命令），应当响亮失败。
if [ "$#" -eq 0 ]; then
    echo "docker-entrypoint: no command supplied" >&2
    exit 1
fi

exec python -m core.secrets_bootstrap "$@"
