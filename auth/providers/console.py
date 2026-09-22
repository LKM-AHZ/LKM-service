"""
基于控制台的短信和邮件提供商的模拟实现。
这些提供商通过标准 logging 模块记录消息，而不是
通过真实的短信/邮件网关发送。适用于开发和集成测试。
"""

import logging
from urllib.parse import urlparse

from auth.providers.base import EmailProvider, SmsProvider

logger = logging.getLogger(__name__)


class ConsoleSmsProvider(SmsProvider):
    """仅用于测试环境的短信提供商 — 打印脱敏后的消息。

    绝不在生产日志中记录完整的验证码、令牌或密钥。
    """

    async def send_code(self, phone: str, code: str) -> None:
        # 整体遮蔽：保留前两位会把 6 位码缩到 10^4 种可能，len<=2 时更是原样打出
        masked = "*" * len(code)
        logger.info("[SMS] To: %s | Code: %s", phone, masked)

    async def send_alert(self, phone: str, message: str) -> None:
        logger.info("[SMS] To: %s | Alert: %s", phone, message)


class ConsoleEmailProvider(EmailProvider):
    """仅用于测试环境的邮件提供商 — 打印脱敏后的消息。

    绝不以明文形式记录密钥、令牌或验证码。
    """

    async def send_code(self, email: str, code: str) -> None:
        # 整体遮蔽，理由同 ConsoleSmsProvider.send_code
        masked = "*" * len(code)
        logger.info("[EMAIL] To: %s | Code: %s", email, masked)

    async def send_magic_link(self, email: str, link: str) -> None:
        # 仅记录站点（scheme+host），连 path 也不记：令牌现在在 query 里，但链接形态一旦
        # 改成 /auth/magic/<token> 就会连明文令牌一起打进日志——这层脱敏本就该与格式无关。
        try:
            parsed = urlparse(link)
            safe = f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            # urlparse 对畸形输入只抛 ValueError：收窄到它并留一条日志，别把解析问题
            # 静默吞成一句写死的 [redacted]。
            logger.warning("[EMAIL] 无法解析 magic link（只记录站点名，不回显链接）")
            safe = "[redacted]"
        logger.info("[EMAIL] To: %s | Magic Link sent: %s", email, safe)

    async def send_alert(self, email: str, message: str) -> None:
        logger.info("[EMAIL] To: %s | Alert: %s", email, message)
