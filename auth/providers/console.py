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
        masked = code[:2] + "****"
        logger.info("[SMS] To: %s | Code: %s", phone, masked)

    async def send_alert(self, phone: str, message: str) -> None:
        logger.info("[SMS] To: %s | Alert: %s", phone, message)


class ConsoleEmailProvider(EmailProvider):
    """仅用于测试环境的邮件提供商 — 打印脱敏后的消息。

    绝不以明文形式记录密钥、令牌或验证码。
    """

    async def send_code(self, email: str, code: str) -> None:
        masked = code[:2] + "****"
        logger.info("[EMAIL] To: %s | Code: %s", email, masked)

    async def send_magic_link(self, email: str, link: str) -> None:
        # 仅记录域名，不记录完整的带令牌链接
        try:
            parsed = urlparse(link)
            safe = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except Exception:
            safe = "[redacted]"
        logger.info("[EMAIL] To: %s | Magic Link sent: %s", email, safe)

    async def send_alert(self, email: str, message: str) -> None:
        logger.info("[EMAIL] To: %s | Alert: %s", email, message)
