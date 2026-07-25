"""WebSocket 上行客户端

将聚合帧、事件帧、系统状态帧通过 WebSocket 推送到云端 hub 或本地前端。
支持自动重连和断线缓冲。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import asdict
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .event_bus import EventBus
from .models import AggregatedFrame, RingEventFrame, SystemFrame

logger = logging.getLogger(__name__)

RECONNECT_DELAY = 3.0
MAX_BUFFER_SIZE = 500


class WSClient:
    """WebSocket 上行客户端"""

    def __init__(self, bus: EventBus, url: str):
        self._bus = bus
        self._url = url
        self._ws: Any = None
        self._running = False
        self._connected = False
        self._send_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

        # 待发送队列（断线时缓冲）
        self._send_queue: deque[dict] = deque(maxlen=MAX_BUFFER_SIZE)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._running = True

        # 订阅要上行的 topic
        self._bus.subscribe("aggregate:frame", self._on_frame)
        self._bus.subscribe("aggregate:event", self._on_event)
        self._bus.subscribe("system:status", self._on_system)
        self._bus.subscribe("classifier:prediction", self._on_classifier)
        self._bus.subscribe("focus:session", self._on_focus_session)

        # 开始连接循环
        self._reconnect_task = asyncio.create_task(self._connect_loop())
        logger.info("WS client starting (target=%s)", self._url)

    async def stop(self) -> None:
        self._running = False
        self._bus.unsubscribe("aggregate:frame", self._on_frame)
        self._bus.unsubscribe("aggregate:event", self._on_event)
        self._bus.unsubscribe("system:status", self._on_system)
        self._bus.unsubscribe("classifier:prediction", self._on_classifier)
        self._bus.unsubscribe("focus:session", self._on_focus_session)
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._send_task:
            self._send_task.cancel()
            self._send_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("WS client stopped")

    # ── 事件回调（入队） ──

    async def _on_frame(self, frame: AggregatedFrame) -> None:
        msg = {"type": "imu_aggregate", **asdict(frame)}
        self._enqueue(msg)

    async def _on_event(self, event: RingEventFrame) -> None:
        msg = {"type": "ring_event", **asdict(event)}
        self._enqueue(msg)

    async def _on_system(self, status: SystemFrame) -> None:
        msg = {"type": "system", **asdict(status)}
        self._enqueue(msg)

    async def _on_classifier(self, payload: dict) -> None:
        self._enqueue({"type": "focus_state", **payload})

    async def _on_focus_session(self, payload: dict) -> None:
        self._enqueue({"type": "focus_session", **payload})

    # ── 内部 ──

    def _enqueue(self, msg: dict) -> None:
        self._send_queue.append(msg)

    async def _connect_loop(self) -> None:
        """连接 + 重连循环"""
        while self._running:
            try:
                logger.info("Connecting to %s ...", self._url)
                async with websockets.connect(self._url, ping_interval=30) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("WS connected ✓")

                    # 先发送缓冲队列
                    while self._send_queue:
                        msg = self._send_queue.popleft()
                        try:
                            await ws.send(json.dumps(msg, ensure_ascii=False))
                        except Exception:
                            self._send_queue.appendleft(msg)
                            break

                    # 持续发送
                    while self._running:
                        await self._flush()

            except (ConnectionClosed, WebSocketException, OSError) as e:
                logger.warning("WS disconnected: %s", e)
            except Exception:
                logger.exception("WS unexpected error")

            self._connected = False
            self._ws = None

            if self._running:
                logger.info("Reconnecting in %ss ...", RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    async def _flush(self) -> None:
        """从队列取一条发送"""
        if not self._send_queue or not self._ws:
            await asyncio.sleep(0.05)
            return

        # 批量发送（一次最多 10 条）
        batch = []
        for _ in range(min(10, len(self._send_queue))):
            batch.append(self._send_queue.popleft())

        if batch:
            payload = "\n".join(json.dumps(m, ensure_ascii=False) for m in batch)
            try:
                await self._ws.send(payload)
            except Exception:
                # 放回队列
                for m in reversed(batch):
                    self._send_queue.appendleft(m)
                raise
