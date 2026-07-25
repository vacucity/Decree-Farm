"""分身农庄 - 状态引擎 (P2)

维护现实状态机：focus / rest / distracted / sleep，
输出当前状态 + 效率因子 efficiency ∈ [0,1]。
"""
import time
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from config import config
from event_bus import event_bus, FarmEvent


@dataclass
class RealityState:
    """现实状态快照"""
    mode: str = "rest"              # focus | rest | distracted | sleep
    focus_start_ts: float = 0.0
    focus_duration_sec: float = 0.0
    total_focus_today_sec: float = 0.0
    streak_days: int = 0
    distraction_count: int = 0
    last_activity_ts: float = field(default_factory=time.time)
    efficiency: float = 0.5         # [0,1]

    def reset_focus(self):
        self.mode = "rest"
        self.focus_start_ts = 0.0
        self.focus_duration_sec = 0.0


class StateEngine:
    """四态状态机 + 效率计算"""

    def __init__(self):
        self.state = RealityState()
        self._tick_task: Optional[asyncio.Task] = None
        # 戒指生物状态接入：手动优先锁 + 滞回防抖候选
        self._manual_override_until: float = 0.0
        self._pending_ring_state: Optional[str] = None
        self._pending_ring_since: float = 0.0

    async def start(self):
        # 订阅事件
        event_bus.subscribe("double_tap", self._on_double_tap)
        event_bus.subscribe("ring_state", self._on_ring_state)
        event_bus.subscribe("tick", self._on_tick)

        # 定时 tick
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("状态引擎已启动")

    async def stop(self):
        if self._tick_task:
            self._tick_task.cancel()

    async def _on_double_tap(self, event: FarmEvent):
        """双击切换专注/休息（手动源：优先级高于戒指自动分类）"""
        # 手动操作后锁定一段时间，忽略戒指自动分类，避免人机判定打架
        self._manual_override_until = time.time() + config.manual_override_sec
        self._pending_ring_state = None
        if self.state.mode in ("focus", "distracted"):
            await self._end_focus()
        else:
            await self._start_focus(event.data)

    async def _start_focus(self, extra_data: dict = None):
        """进入专注：重置计时并通知下游开工。"""
        self.state.mode = "focus"
        self.state.focus_start_ts = time.time()
        self.state.focus_duration_sec = 0.0
        self.state.distraction_count = 0
        logger.info("专注开始")
        focus_data = extra_data or {}
        await event_bus.publish(FarmEvent(type="focus_start", data=focus_data))

    async def _end_focus(self):
        """结束专注：累计时长并把 duration 带给 rest 事件（供退出结算）。"""
        duration = self.state.focus_duration_sec
        self.state.total_focus_today_sec += duration
        self.state.mode = "rest"
        logger.info(f"专注结束: 本次 {duration:.0f}s, "
                   f"今日累计 {self.state.total_focus_today_sec:.0f}s")
        await event_bus.publish(FarmEvent(type="rest", data={"duration": duration}))
        self.state.reset_focus()

    async def _on_ring_state(self, event: FarmEvent):
        """戒指生物状态接入（自动源）：置信度门槛 + 滞回防抖 + 手动优先。

        约定事件：data={"state": focus|distracted|rest|away, "confidence": 0~1,
        "source": "ring_bio", "metrics": {...}}。分类结果需稳定驻留
        config.ring_state_dwell_sec 才真正迁移，避免瞬时噪声频繁启停化身。
        """
        state = event.data.get("state")
        conf = float(event.data.get("confidence", 0.0))
        now = time.time()

        # 手动优先锁：用户刚双击过，忽略自动分类
        if now < self._manual_override_until:
            return
        # 置信度门槛
        if conf < config.ring_state_min_conf:
            return
        if state not in ("focus", "distracted", "rest", "away"):
            return

        # 滞回：候选状态需连续稳定驻留 dwell 秒才提交
        if state != self._pending_ring_state:
            self._pending_ring_state = state
            self._pending_ring_since = now
            return
        if now - self._pending_ring_since < config.ring_state_dwell_sec:
            return

        await self._apply_ring_state(state)

    async def _apply_ring_state(self, state: str):
        """把稳定后的戒指状态映射为状态机迁移（幂等，复用手动源同一套事件）。"""
        mode = self.state.mode
        if state == "focus":
            if mode in ("rest", "sleep"):  # 深夜窗口自动转的 sleep 也能被拉起专注
                await self._start_focus()
            elif mode == "distracted":
                self.state.mode = "focus"  # 从分心恢复，保留本次专注计时
                logger.info("戒指检测恢复专注")
                # 通知编排器立即恢复行动（不走 focus_start 避免重复随机活动）
                await event_bus.publish(FarmEvent(type="focus_resume", data={}))
        elif state == "distracted":
            if mode == "focus":
                self.state.mode = "distracted"
                self.state.distraction_count += 1
                await event_bus.publish(FarmEvent(type="distracted", data={
                    "distraction_count": self.state.distraction_count,
                    "source": "ring_bio",
                }))
        elif state in ("rest", "away"):
            if mode in ("focus", "distracted"):
                await self._end_focus()

    async def _on_tick(self, event: FarmEvent):
        """定时更新状态与效率"""
        now = time.time()

        # 睡眠检测（用户主动专注时不强制：深夜双击戒指开专注是明确意愿，
        # 否则 22 点后专注会在下一个 tick 被掐灭、化身被按去睡觉）
        hour = time.localtime(now).tm_hour
        if (hour >= config.sleep_window_start or hour < config.sleep_window_end):
            if self.state.mode not in ("sleep", "focus"):
                self.state.mode = "sleep"
                await event_bus.publish(FarmEvent(type="sleep", data={}))
                return

        # 专注中：检查是否分心
        if self.state.mode == "focus":
            self.state.focus_duration_sec = now - self.state.focus_start_ts
            idle = now - self.state.last_activity_ts
            if idle > config.focus_timeout_sec:
                self.state.mode = "distracted"
                self.state.distraction_count += 1
                await event_bus.publish(FarmEvent(
                    type="distracted",
                    data={"idle_sec": idle, "distraction_count": self.state.distraction_count}
                ))
                return

        # 效率计算
        self.state.efficiency = self._calc_efficiency()
        self.state.last_activity_ts = now

    def _calc_efficiency(self) -> float:
        """效率 ∈ [0,1] = f(专注时长, streak)"""
        if self.state.mode == "focus":
            base = min(self.state.focus_duration_sec / 3600, 1.0)  # 1h→1.0
            streak_bonus = min(self.state.streak_days / 7, 1.0) * config.streak_max_bonus
            return min(base + streak_bonus, 1.0)
        elif self.state.mode == "distracted":
            return 0.1
        elif self.state.mode == "sleep":
            return 0.0
        else:
            return 0.5

    async def _tick_loop(self):
        """每 15s 触发 tick"""
        while True:
            await asyncio.sleep(config.farm_tick_interval_sec)
            await event_bus.publish(FarmEvent(type="tick", data={
                "efficiency": self.state.efficiency,
                "mode": self.state.mode,
            }))


# 全局单例
state_engine = StateEngine()
