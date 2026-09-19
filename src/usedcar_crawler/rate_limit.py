"""令牌桶限速器。

单一职责：在任何真实请求发出前调用 ``acquire()``，确保对目标站点的访问频率可控。
默认 QPS 0.5（即 2 秒 1 次），可被站点级配置覆盖，避免"爬虫把人家站点打挂"。
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """线程安全令牌桶。

    :param rate: 每秒补充的令牌数（QPS）
    :param capacity: 桶容量，允许短时突发
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate 必须大于 0")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> float:
        """阻塞直到取得令牌，返回实际等待秒数。

        :raises TimeoutError: 超过 ``timeout`` 仍未取得令牌
        """
        waited = 0.0
        if tokens > self.capacity:
            tokens = self.capacity
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate
            if timeout is not None and waited + sleep_for > timeout:
                raise TimeoutError(f"限速等待超时：需要 {waited + sleep_for:.2f}s，上限 {timeout}s")
            time.sleep(sleep_for)
            waited += sleep_for

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens
