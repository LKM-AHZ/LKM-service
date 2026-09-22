#!/bin/sh
# LKM 服务统一入口（M5 7.2.3）：
# 启动前按需从 Infisical 拉取密钥注入环境，再 exec 原 command（backend/auth/各 worker 通用）。
# LKM_INFISICAL_ENABLED 默认未设/false 时 bootstrap 直接返回 0，不触网、零副作用。
set -e

# 显式处理退出码：python 不在 PATH(127)/app 不可 import 等「与拉密钥无关」的失败，若只靠
# set -e 直接终止，日志里看不出失败发生在 secrets bootstrap 这一步（容器表现为无因 crash-loop）。
python -m app.core.secrets_bootstrap || {
    rc=$?
    echo "secrets bootstrap failed (rc=$rc)" >&2
    exit "$rc"
}

# 无参数时 exec 在 POSIX sh 里是 no-op（多数字典实现），容器会「健康地」立即退出 0；
# 这属于编排配置错误（CMD 被覆盖为空 / docker run 未带命令），应当响亮失败。
if [ "$#" -eq 0 ]; then
    echo "docker-entrypoint: no command supplied" >&2
    exit 1
fi

exec "$@"
