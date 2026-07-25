"""专注引擎（F8）— 5s 滑动窗口方差分析判定专注/分心

订阅 imu:sample → 滑动窗口缓存 → 方差+过零率 → 三档判定 → 发布 focus:output
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from statistics import mean, variance

from ..config import (
    FOCUS_VARIANCE_DEEP,
    FOCUS_VARIANCE_LIGHT,
    FOCUS_WINDOW_SAMPLES,
    GROWTH_DECAY_PER_FRAME,
    GROWTH_PER_FRAME_DEEP,
    GROWTH_PER_FRAME_LIGHT,
)
from ..event_bus import EventBus
from ..models import FocusOutput, FocusState, IMUSample

logger = logging.getLogger(__name__)


class FocusEngine:
    """专注判定引擎。

    用加速度幅值的方差作为"运动强度"指标：
      - 方差 < DEEP 阈值 → 深度心流，生长快
      - 方差 < LIGHT 阈值 → 浅专注，生长慢
      - 方差 >= LIGHT → 分心，生长暂停并衰减

    生长进度 (growth_progress) 0.0~1.0，长到 1.0 = 植物完全开花。
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._window: deque[float] = deque(maxlen=FOCUS_WINDOW_SAMPLES)
        self._distractions: list[int] = []  # 分心事件时间戳列表
        self._current_state = FocusState.LIGHT_FOCUS
        self._growth = 0.0  # 0.0 ~ 1.0
        self._last_distraction = False
        self._running = False

    # ── 公开 ──

    @property
    def state(self) -> FocusState:
        return self._current_state

    @property
    def growth_progress(self) -> float:
        return self._growth

    @property
    def distraction_count(self) -> int:
        return len(self._distractions)

    async def start(self) -> None:
        self._running = True
        self._bus.subscribe("imu:sample", self._on_sample)

    async def stop(self) -> None:
        self._running = False
        self._bus.unsubscribe("imu:sample", self._on_sample)

    def mark_distraction(self) -> None:
        """外部（手势）触发分心标记"""
        self._distractions.append(0)  # 简化，只计数
        self._last_distraction = True

    # ── 内部 ──

    async def _on_sample(self, sample: IMUSample) -> None:
        # 计算加速度幅值（magnitude）
        mag = (sample.accel_x ** 2 + sample.accel_y ** 2 + sample.accel_z ** 2) ** 0.5
        self._window.append(mag)

        if len(self._window) < self._window.maxlen // 2:
            return  # 窗口未满，等待更多数据

        # 计算方差
        var = variance(self._window) if len(self._window) >= 2 else 0.0

        # 判定状态
        distraction_just_triggered = False
        if var < FOCUS_VARIANCE_DEEP:
            new_state = FocusState.DEEP_FLOW
            self._growth = min(1.0, self._growth + GROWTH_PER_FRAME_DEEP)
        elif var < FOCUS_VARIANCE_LIGHT:
            new_state = FocusState.LIGHT_FOCUS
            self._growth = min(1.0, self._growth + GROWTH_PER_FRAME_LIGHT)
        else:
            new_state = FocusState.DISTRACTED
            self._growth = max(0.0, self._growth - GROWTH_DECAY_PER_FRAME)
            if self._current_state != FocusState.DISTRACTED:
                distraction_just_triggered = True
                self._distractions.append(0)

        self._current_state = new_state

        # 计算静止分数（方差的倒数，归一化）
        max_var = max(FOCUS_VARIANCE_LIGHT * 2, var)
        stillness = max(0.0, 1.0 - var / max_var)

        # 发布引擎输出
        output = FocusOutput(
            state=new_state,
            growth_progress=round(self._growth, 4),
            stillness_score=round(stillness, 4),
            distraction=distraction_just_triggered or self._last_distraction,
        )
        self._last_distraction = False
        await self._bus.publish("focus:output", output)
