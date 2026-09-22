"""登录限流：委托 Redis 滑动窗口限流器；Redis 不可用时拒绝（fail-close）。

密码登录是暴力破解关键路径，Redis 抖动时宁可拒绝登录也不能放开防爆破面，
故这里显式 fail_open=False。限流参数（IP/全局次数、窗口秒）统一读 settings
（LKM_LOGIN_* 可覆盖）。
"""

from app.core.config import settings
from app.core.err import (
    AuthErr,  # M3 peer: 并入共享 shared err
    BizError,
)
from app.core.redis_limiter import RedisRateLimiter
from app.core.secrets import reveal


async def check_password_login_rate_limit(ip_address: str) -> None:
    """对密码登录尝试应用 IP 和全局限流。

    Redis 已在栈上（``redis_url`` 非空）但运行期不可用时**拒绝（fail-close）**，
    不放开防爆破面；未配置 Redis 时无分布式限流依赖可失败，放行保持原语义。
    """
    # 未配置 Redis：无分布式限流可失败，放行（那类部署靠 DB 级账号锁定兜底）。
    if not reveal(settings.redis_url):
        return
    limiter = RedisRateLimiter()
    # 先查 IP 桶再查全局桶：限流器每查一次都会给自己的桶打点，若先查全局，单个 IP 即便
    # 早被 IP 桶拒绝也仍会持续累加 __global__，20 次/分以外的请求全用来烧全局额度，
    # 一个来源即可在窗口内把全局桶打满、拒绝所有用户的密码登录。
    if ip_address and not await limiter.check(
        f"ip:{ip_address}",
        settings.login_ip_max_per_min,
        settings.login_window_seconds,
        fail_open=False,
    ):
        raise BizError(AuthErr.ACCOUNT_LOCKED, "Too many login attempts from this IP")
    if not await limiter.check(
        "__global__",
        settings.login_global_max_per_min,
        settings.login_window_seconds,
        fail_open=False,
    ):
        raise BizError(
            AuthErr.ACCOUNT_LOCKED, "Too many login attempts, please try again later"
        )
