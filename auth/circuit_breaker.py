"""进程内轻量熔断器（蓝图 §5.4）：零新依赖，只为让「AUTH 进程不可用」失败得更快。

背景：本进程所有出站 HTTP 都打同一个 AUTH 进程（``auth.user_http``）。当它宕机/网络黑洞时，
每个请求都要白等一个连接超时（fail-closed 缝还要把这笔时延计进拒绝）。熔断器在连续失败达阈值
后 open，冷却期内**直接短路**（不再发出任何请求，省掉超时），冷却过半开放行**一次**试探；
试探成功即闭合复位，失败则重新 open。

语义边界（与 user_http 的 fail-open/fail-closed 契约一致）：熔断只让失败**更快**发生——open
时调用方照旧抛 ``UserHttpUnavailable``，fail-open 缝回落 DB、fail-closed 缝拒绝；它绝不缓存
成功，也绝不把失败变成功。粒度按进程内单一 AUTH 服务（本模块全部调用同源），故一个全局实例
足够，无需按端点分桶。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from core.config import settings

_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"


def _failure_threshold() -> int:
    """达阈值即 open；``max(1, …)`` 防止配 0/负数把熔断退化成「一失败即熔断」之外的怪状态。"""
    return max(1, int(settings.auth_http_circuit_failures))


def _reset_seconds() -> float:
    """冷却时长；``max(0.0, …)`` 允许配 0（测试/热修用，等价于「失败即立刻半开」）。"""
    return max(0.0, float(settings.auth_http_circuit_reset_s))


class CircuitBreaker:
    """连续失败达阈值即 open 的进程内熔断器（线程/协程安全）。

    ``allow()`` 是闸门：出站前先问是否放行；随后**必须**把本次结果喂回 ``record_success`` /
    ``record_failure``。阈值与冷却时长每次从 settings 现读，便于运行期调整与测试覆盖。
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._state = _CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False

    def reset(self) -> None:
        """复位为闭合、清零计数（应用收尾与测试隔离用）。"""
        with self._lock:
            self._state = _CLOSED
            self._failures = 0
            self._opened_at = 0.0
            self._probing = False

    def allow(self) -> bool:
        """是否放行本次出站：open 冷却中 → False（短路，不发请求）；冷却已过 → 半开放行一次。"""
        with self._lock:
            if self._state == _CLOSED:
                return True
            if self._state == _OPEN:
                if self._clock() - self._opened_at >= _reset_seconds():
                    self._state = _HALF_OPEN
                    self._probing = True
                    return True
                return False
            # HALF_OPEN：只放行一枚试探，其余请求继续短路，避免探针风暴压垮刚恢复的 AUTH。
            if self._probing:
                return False
            self._probing = True
            return True

    def record_success(self) -> None:
        """一次成功即完全复位（闭合、清计数、清探针标记）。"""
        with self._lock:
            self._state = _CLOSED
            self._failures = 0
            self._opened_at = 0.0
            self._probing = False

    def record_failure(self) -> None:
        """记一次失败：达阈值 → open；半开探针失败 → 重新 open 并重计冷却。"""
        with self._lock:
            if self._state == _HALF_OPEN:
                self._state = _OPEN
                self._probing = False
                self._opened_at = self._clock()
                return
            self._failures += 1
            if self._failures >= _failure_threshold():
                self._state = _OPEN
                self._opened_at = self._clock()


# 进程内单一 AUTH 服务熔断器（user_http 全部调用同源）。测试可 import 后 ``.reset()``。
auth_http_breaker = CircuitBreaker()
