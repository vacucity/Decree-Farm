"""事件总线 — 所有模块通过 topic 订阅/发布事件

用法:
    bus = EventBus()
    bus.subscribe("imu:sample", handler_func)
    bus.subscribe("gesture:*", handler_func)   # 通配符
    await bus.publish("imu:sample", sample)
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    """轻量级异步发布-订阅总线"""

    def __init__(self):
        self._exact: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[tuple[str, Handler]] = []

    # ── 订阅 ──

    def subscribe(self, topic: str, handler: Handler) -> None:
        """订阅 topic。支持通配符 `*`（如 `gesture:*`）。"""
        if "*" in topic:
            self._wildcard.append((topic, handler))
        else:
            self._exact[topic].append(handler)
        logger.debug("Subscribed to %s", topic)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        """取消订阅。"""
        if "*" in topic:
            self._wildcard = [(t, h) for t, h in self._wildcard
                              if not (t == topic and h == handler)]
        else:
            handlers = self._exact.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

    # ── 发布 ──

    async def publish(self, topic: str, data: Any = None) -> None:
        """发布事件到指定 topic，并发调用所有匹配的 handler。"""
        tasks: list[asyncio.Task] = []

        # 精确匹配
        for handler in self._exact.get(topic, []):
            tasks.append(asyncio.create_task(self._safe_call(handler, data, topic)))

        # 通配符匹配
        for pattern, handler in self._wildcard:
            if fnmatch.fnmatch(topic, pattern):
                tasks.append(asyncio.create_task(self._safe_call(handler, data, topic)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── 批量发布 ──

    async def publish_batch(self, items: list[tuple[str, Any]]) -> None:
        """批量发布 [(topic, data), ...]"""
        await asyncio.gather(*(self.publish(t, d) for t, d in items))

    # ── 内部 ──

    async def _safe_call(self, handler: Handler, data: Any, topic: str) -> None:
        try:
            await handler(data)
        except Exception:
            logger.exception("Event handler for '%s' raised exception", topic)

    def subscriber_count(self, topic: str) -> int:
        """返回某 topic 的订阅数（用于调试）"""
        count = len(self._exact.get(topic, []))
        for pattern, _ in self._wildcard:
            if fnmatch.fnmatch(topic, pattern):
                count += 1
        return count
