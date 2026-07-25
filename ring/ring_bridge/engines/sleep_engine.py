"""睡眠引擎（F7，降级为趋势指标）— 60s epoch 体动分析

订阅 imu:sample → 60s epoch 聚合 → 体动强度 → 深睡/浅睡/醒趋势
发布 sleep:output → 驱动月光亮度起伏 + 睡眠阶段展示
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from statistics import variance

from ..config import (
    MOTION_THRESHOLD_AWAKE,
    MOTION_THRESHOLD_DEEP,
    SLEEP_EPOCH_SAMPLES,
)
from ..event_bus import EventBus
from ..models import IMUSample, SleepOutput, SleepStage

logger = logging.getLogger(__name__)


class SleepEngine:
    """睡眠趋势引擎（消费级，不宣称医疗精度）。

    每 60 秒 epoch 计算体动强度：
      - motion_intensity < 0.05 → 深睡趋势
      - 0.05 ~ 0.30 → 浅睡趋势
      - > 0.30 → 醒/翻身

    motion_intensity (0.0~1.0) 也用来驱动前端月光亮度。
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._accel_mags: deque[float] = deque()
        self._epoch_samples: list[float] = []
        self._toss_count: list[int] = []  # 翻身次数（每 epoch）
        self._current_stage = SleepStage.LIGHT
        self._motion_intensity = 0.05
        self._running = False
        self._sample_count = 0

    # ── 公开 ──

    @property
    def stage(self) -> SleepStage:
        return self._current_stage

    @property
    def motion_intensity(self) -> float:
        return self._motion_intensity

    @property
    def total_toss_count(self) -> int:
        return sum(self._toss_count)

    async def start(self) -> None:
        self._running = True
        self._bus.subscribe("imu:sample", self._on_sample)

    async def stop(self) -> None:
        self._running = False
        self._bus.unsubscribe("imu:sample", self._on_sample)

    # ── 内部 ──

    async def _on_sample(self, sample: IMUSample) -> None:
        # 计算加速度幅值
        mag = (sample.accel_x ** 2 + sample.accel_y ** 2 + sample.accel_z ** 2) ** 0.5
        self._epoch_samples.append(mag)
        self._sample_count += 1

        # 达到 60 秒 epoch → 计算
        if self._sample_count >= SLEEP_EPOCH_SAMPLES:
            await self._process_epoch()
            self._epoch_samples.clear()
            self._sample_count = 0

    async def _process_epoch(self) -> None:
        if len(self._epoch_samples) < 10:
            return

        # 体动强度 = 加速度幅值方差 / 归一化
        var = variance(self._epoch_samples)
        # 归一化到 0~1（经验值，最大方差约 500000）
        intensity = min(1.0, var / 100000.0)

        # 判定阶段
        if intensity < MOTION_THRESHOLD_DEEP:
            stage = SleepStage.DEEP
        elif intensity < MOTION_THRESHOLD_AWAKE:
            stage = SleepStage.LIGHT
        else:
            stage = SleepStage.AWAKE

        # 翻身检测：高体动事件计数
        high_motion = sum(1 for v in self._epoch_samples if v > 300)
        toss_this_epoch = max(0, high_motion // 10)  # 简化
        self._toss_count.append(toss_this_epoch)

        self._current_stage = stage
        self._motion_intensity = round(intensity, 4)

        output = SleepOutput(
            stage=stage,
            motion_intensity=round(intensity, 4),
            toss_count=toss_this_epoch,
        )

        await self._bus.publish("sleep:output", output)
        logger.debug(
            "Sleep epoch: stage=%s intensity=%.3f toss=%s",
            stage.value, intensity, toss_this_epoch,
        )
