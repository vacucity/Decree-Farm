"""分身农庄 - 事件总线

统一的内部事件定义与发布/订阅，解耦各模块。
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any
import time
import asyncio
from loguru import logger


@dataclass
class FarmEvent:
    """系统事件"""
    type: str           # focus_start | rest | distracted | sleep | voice_cmd | tick | double_tap | ring_state
    ts: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)


class EventBus:
    """简单的发布/订阅事件总线"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}

    def subscribe(self, event_type: str, handler: Callable):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        logger.debug(f"订阅 {event_type} → {handler.__name__}")

    async def publish(self, event: FarmEvent):
        handlers = self._subscribers.get(event.type, [])
        logger.info(f"事件 {event.type} (handlers={len(handlers)})")
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"事件处理异常 {event.type} → {handler.__name__}: {e}")


# 全局单例
event_bus = EventBus()
