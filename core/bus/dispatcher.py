"""
L2 进程内命令/查询总线（骨架）。

**为什么需要**：模块之间的同步、同事务协作（如「内容发布时记积分」「文件上传校验项目归属」）
目前靠直接 import 对方的 service 或 models。import 级耦合让「模块边界」只剩一张不断变长的
import-linter 白名单，而且无法注入假实现做单测。本层把这类协作收敛成**显式契约**：

- 提供方用 :func:`register` 声明 ``Command``，消费方只依赖 ``Command`` 对象与名称；
- 分发是**进程内、同步、同事务**的：``db`` 必填，调用方与被调方共用同一 session。

**为什么 ``db`` 必填**：若被调方自己开会话，跨域写就不再原子（内容发布回滚、积分却已入账）。
把它设成必填参数而不是「可选、默认自己开」，是为了让违反者编译期就写不出来。

**边界**：本层不做重试、不做跨进程、不做最终一致——那些是 L3（``core.bus.runtime`` +
outbox）的职责。把 L2 的调用改走 L3 是**行为改变**（强一致 → 最终一致），必须单独评审。

当前是骨架：注册表为空、没有调用方，接入生产代码在后续提交里逐条进行。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession


class CommandError(RuntimeError):
    """命令总线错误基类。"""


class CommandNotRegistered(CommandError):
    """调用了没人注册的命令：装配漏了提供方模块，或命令名写错。"""


class DuplicateCommand(CommandError):
    """同一命令名被两个不同 handler 注册：装配期即冲突，不允许后者静默覆盖。"""


class CommandResultMismatch(CommandError):
    """handler 返回类型与 ``Command.response`` 不符：接线错误，尽早暴露。"""


@dataclass(frozen=True)
class Command[Req, Resp]:
    """一条命令的契约：名称 + 请求类型 + 响应类型。

    ``request`` / ``response`` 只用于运行期校验，不参与分发（分发只按 ``name``）。
    """

    name: str
    request: type[Req]
    response: type[Resp]


Handler = Callable[..., Awaitable[Any]]

_handlers: dict[str, Handler] = {}


def register(command: Command[Any, Any]) -> Callable[[Handler], Handler]:
    """注册 handler 的装饰器。

    同一函数重复注册是幂等的（测试里模块重载常见）；**不同**函数抢同一命令名则抛
    :class:`DuplicateCommand`——后者静默覆盖会让「谁真正处理了这条命令」随导入顺序变化。
    """

    def _decorator(handler: Handler) -> Handler:
        existing = _handlers.get(command.name)
        if existing is not None and existing is not handler:
            raise DuplicateCommand(
                f"命令 {command.name!r} 已被 {existing!r} 注册，拒绝 {handler!r} 覆盖"
            )
        _handlers[command.name] = handler
        return handler

    return _decorator


def is_registered(command: Command[Any, Any]) -> bool:
    return command.name in _handlers


async def call[Req, Resp](
    command: Command[Req, Resp], request: Req, *, db: AsyncSession
) -> Resp:
    """分发一条命令。

    ``db`` 由调用方传入并与被调方共用——这是「同事务」的唯一保证方式。
    """
    handler = _handlers.get(command.name)
    if handler is None:
        raise CommandNotRegistered(
            f"命令 {command.name!r} 未注册：该进程没有装配它的提供方模块"
        )
    if not isinstance(request, command.request):
        raise TypeError(
            f"命令 {command.name!r} 的请求类型应为 {command.request!r}，"
            f"实际收到 {type(request)!r}"
        )
    result = await handler(request, db=db)
    if command.response is not Any and not isinstance(result, command.response):
        raise CommandResultMismatch(
            f"命令 {command.name!r} 的响应类型应为 {command.response!r}，"
            f"实际返回 {type(result)!r}"
        )
    return cast(Resp, result)


def reset() -> None:
    """清空注册表（仅供测试隔离）。"""
    _handlers.clear()
