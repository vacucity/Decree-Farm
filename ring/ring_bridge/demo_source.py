"""DemoSource — 预录数据回放

从 JSONL 文件读取预录的 IMU 数据，按原始时间戳回放。
支持本地模式和云端模式，前端不感知差异。

JSONL 格式（每行一个 JSON 对象）：
  {"ts": 12345678, "event": "imu_batch", "samples": [{"t":..., "ax":..., ...}]}
  {"ts": 12345900, "event": "double_click"}
  {"ts": 12346000, "event": "wave"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from .event_bus import EventBus
from .models import (
    ButtonEvent,
    GestureEvent,
    IMUBatch,
    IMUSample,
    SystemStatus,
)

logger = logging.getLogger(__name__)

# 内置的演示用预录数据（一小段模拟静止→微动→剧烈动的序列）
_BUILTIN_DEMO_SEQUENCE: list[dict] = []


def _generate_builtin_demo() -> list[dict]:
    """生成一段内置演示序列，用于 DEMO_MODE=true 但无文件时。"""
    events: list[dict] = []
    ts = int(time.time() * 1000)
    seq = 0

    # 模拟：静止 5 秒（深度心流）
    for _ in range(5 * 25):  # 5秒 × 25Hz
        events.append({
            "ts": ts, "event": "imu_sample",
            "ax": 100 + (seq % 10), "ay": 200, "az": 16000,
            "gx": 0, "gy": 0, "gz": 0,
            "seq": seq,
        })
        seq += 1
        ts += 40

    # 模拟：微动 3 秒（浅专注）
    for _ in range(3 * 25):
        events.append({
            "ts": ts, "event": "imu_sample",
            "ax": 100 + (seq % 50) * 10, "ay": 200 + (seq % 30) * 15,
            "az": 16000 + (seq % 20) * 20,
            "gx": (seq % 10) * 5, "gy": (seq % 8) * 3, "gz": 0,
            "seq": seq,
        })
        seq += 1
        ts += 40

    # 模拟：剧烈动 2 秒（分心）
    for _ in range(2 * 25):
        events.append({
            "ts": ts, "event": "imu_sample",
            "ax": 100 + (seq % 200) * 30, "ay": 200 + (seq % 250) * 20,
            "az": 16000 + (seq % 300) * 50,
            "gx": (seq % 100) * 20, "gy": (seq % 80) * 15,
            "gz": (seq % 50) * 10,
            "seq": seq,
        })
        seq += 1
        ts += 40

    # 加一个双击事件
    events.append({"ts": ts, "event": "double_click"})

    # 再静止 5 秒
    ts += 500
    for _ in range(5 * 25):
        events.append({
            "ts": ts, "event": "imu_sample",
            "ax": 100, "ay": 200, "az": 16000,
            "gx": 0, "gy": 0, "gz": 0,
            "seq": seq,
        })
        seq += 1
        ts += 40

    return events


_BUILTIN_DEMO_SEQUENCE = _generate_builtin_demo()


class DemoSource:
    """预录数据回放源——实现与 RingSource 相同的输出协议"""

    def __init__(self, bus: EventBus, file_path: str | None = None):
        self._bus = bus
        self._file_path = Path(file_path) if file_path else None
        self._running = False
        self._task: asyncio.Task | None = None
        self._events: list[dict] = []

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """加载数据并开始回放"""
        if self._running:
            return

        # 加载数据
        if self._file_path and self._file_path.exists():
            self._events = self._load_file(self._file_path)
            logger.info("Loaded %s events from %s", len(self._events), self._file_path)
        else:
            self._events = list(_BUILTIN_DEMO_SEQUENCE)
            logger.info("Using built-in demo sequence (%s events)", len(self._events))

        self._running = True

        await self._bus.publish("system:connected", None)
        await self._bus.publish("system:battery", SystemStatus(
            battery_percent=85,
            battery_charging=False,
            connected=True,
        ))

        self._task = asyncio.create_task(self._playback_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        await self._bus.publish("system:disconnected", None)

    # ── 内部 ──

    def _load_file(self, path: Path) -> list[dict]:
        events = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return events

    async def _playback_loop(self) -> None:
        """按时间戳回放事件"""
        if not self._events:
            logger.warning("No events to playback")
            return

        base_time = time.time() * 1000
        base_event_ts = self._events[0]["ts"]

        for evt in self._events:
            if not self._running:
                break

            # 计算回放延迟
            event_offset_ms = evt["ts"] - base_event_ts
            target_time = base_time + event_offset_ms
            now = time.time() * 1000
            delay = (target_time - now) / 1000.0

            if delay > 0:
                await asyncio.sleep(delay)

            # 分发事件
            event_type = evt.get("event", "")
            try:
                if event_type == "imu_sample":
                    sample = IMUSample(
                        timestamp_ms=int(evt["ts"]),
                        sequence=int(evt.get("seq", 0)),
                        accel_x=int(evt["ax"]),
                        accel_y=int(evt["ay"]),
                        accel_z=int(evt["az"]),
                        gyro_x=int(evt["gx"]),
                        gyro_y=int(evt["gy"]),
                        gyro_z=int(evt["gz"]),
                    )
                    await self._bus.publish("imu:sample", sample)

                elif event_type == "imu_batch":
                    # 批量样本
                    samples = [
                        IMUSample(
                            timestamp_ms=int(s.get("t", evt["ts"])),
                            sequence=int(evt.get("seq", 0)) + i,
                            accel_x=int(s.get("ax", 0)),
                            accel_y=int(s.get("ay", 0)),
                            accel_z=int(s.get("az", 0)),
                            gyro_x=int(s.get("gx", 0)),
                            gyro_y=int(s.get("gy", 0)),
                            gyro_z=int(s.get("gz", 0)),
                        )
                        for i, s in enumerate(evt.get("samples", []))
                    ]
                    batch = IMUBatch(sequence_start=0, samples=samples)
                    await self._bus.publish("imu:batch", batch)
                    for s in samples:
                        await self._bus.publish("imu:sample", s)

                elif event_type in ("double_click", "single_click"):
                    be = ButtonEvent(
                        timestamp_ms=int(evt["ts"]),
                        event_type=event_type,
                    )
                    await self._bus.publish(f"button:{event_type}", be)
                    await self._bus.publish("button:*", be)
                    logger.info("Demo: %s", event_type)

                elif event_type in ("wave", "rotate_front", "rotate_back"):
                    gesture_id = {"rotate_back": 1, "rotate_front": 2, "wave": 3}
                    ge = GestureEvent(
                        timestamp_ms=int(evt["ts"]),
                        gesture_id=gesture_id.get(event_type, 0),
                        gesture_name=event_type,
                    )
                    await self._bus.publish(f"gesture:{event_type}", ge)
                    await self._bus.publish("gesture:*", ge)
                    logger.info("Demo: %s", event_type)

            except Exception:
                logger.exception("Error playing demo event: %s", event_type)
