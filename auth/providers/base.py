"""短信和邮件发送的抽象提供商接口。"""

from abc import ABC, abstractmethod


class SmsProvider(ABC):
    """抽象短信提供商。"""

    @abstractmethod
    async def send_code(self, phone: str, code: str) -> None:
        """向给定的手机号码发送验证码。"""
        ...

    @abstractmethod
    async def send_alert(self, phone: str, message: str) -> None:
        """向给定的手机号码发送告警消息。"""
        ...


class EmailProvider(ABC):
    """抽象邮件提供商。"""

    @abstractmethod
    async def send_code(self, email: str, code: str) -> None:
        """向给定的邮箱地址发送验证码。"""
        ...

    @abstractmethod
    async def send_magic_link(self, email: str, link: str) -> None:
        """向给定的邮箱地址发送魔法登录链接。"""
        ...

    @abstractmethod
    async def send_alert(self, email: str, message: str) -> None:
        """向给定的邮箱地址发送告警消息。"""
        ...
