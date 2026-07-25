"""数据聚合器 — 引擎输出聚合 + 前端帧生成 + SQLite 落库

订阅：
  - focus:output → 累积专注数据
  - sleep:output → 累积睡眠数据
  - imu:sample → 降频为 200ms 聚合帧
  - button:* / gesture:* → 透传为 ring_event
  - system:battery → 系统状态帧

发布：
  - aggregate:frame (AggregatedFrame) → 被 ws_client 推给前端
  - ring_event (RingEventFrame) → 被 ws_client 推给前端
  - system:status (SystemFrame) → 被 ws_client 推给前端
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from .config import AGGREGATE_INTERVAL_MS, GESTURE_MAP
from .event_bus import EventBus
from .models import (
    AggregatedFrame,
    ButtonEvent,
    DeviceMode,
    FocusOutput,
    GestureEvent,
    IMUSample,
    RingEventFrame,
    SleepOutput,
    SystemFrame,
    SystemStatus,
)

logger = logging.getLogger(__name__)


class DataAggregator:
    """将引擎输出和事件流聚合为前端可消费的帧格式"""

    def __init__(self, bus: EventBus, db_path: str | None = None):
        self._bus = bus
        self._db_path = Path(db_path) if db_path else None
        self._db: sqlite3.Connection | None = None

        # 当前状态
        self._mode = DeviceMode.REST
        self._last_focus: FocusOutput | None = None
        self._last_sleep: SleepOutput | None = None
        self._last_sample: IMUSample | None = None
        self._battery = SystemStatus(0, False, False)
        self._battery_known = False
        self._connected = False

        # 专注 session 统计（用于晚间复盘）
        self._focus_sessions: list[dict] = []
        self._current_session_start: float | None = None
        self._current_session_distractions: int = 0

        # 帧聚合器 — 每 200ms 产出一帧
        self._running = False
        self._frame_task: asyncio.Task | None = None
        self._pending_distraction = False

    # ── 公开 ──

    @property
    def mode(self) -> DeviceMode:
        return self._mode

    async def start(self) -> None:
        self._running = True

        # 初始化 SQLite
        if self._db_path:
            self._init_db()

        # 订阅事件
        self._bus.subscribe("focus:output", self._on_focus)
        self._bus.subscribe("sleep:output", self._on_sleep)
        self._bus.subscribe("imu:sample", self._on_imu_sample)
        self._bus.subscribe("button:*", self._on_button)
        self._bus.subscribe("gesture:*", self._on_gesture)
        self._bus.subscribe("system:battery", self._on_battery)
        self._bus.subscribe("system:connected", lambda _: self._on_connected(True))
        self._bus.subscribe("system:disconnected", lambda _: self._on_connected(False))

        # 启动帧生成循环
        self._frame_task = asyncio.create_task(self._frame_loop())
        logger.info("Aggregator started (db=%s)", self._db_path)

    async def stop(self) -> None:
        self._running = False
        if self._frame_task:
            self._frame_task.cancel()
            self._frame_task = None
        if self._db:
            self._db.close()
            self._db = None

    # ── 事件处理 ──

    async def _on_focus(self, output: FocusOutput) -> None:
        self._last_focus = output

        # 专注 session 追踪
        if output.state.value in ("deep_flow", "light_focus"):
            if self._current_session_start is None:
                self._current_session_start = time.time()
                self._current_session_distractions = 0
        else:
            # 分心 → 计数
            if self._current_session_start is not None:
                self._current_session_distractions += 1

        if output.distraction:
            self._pending_distraction = True

    async def _on_sleep(self, output: SleepOutput) -> None:
        self._last_sleep = output

    async def _on_imu_sample(self, sample: IMUSample) -> None:
        self._last_sample = sample

    async def _on_button(self, event: ButtonEvent) -> None:
        """按键事件 → 查手势映射表 → 可能切换模式"""

        # 先发布事件给前端
        await self._bus.publish("aggregate:event", RingEventFrame(
            event=event.event_type,
            ts=event.timestamp_ms,
        ))

        # 查映射表
        action = GESTURE_MAP.get(self._mode, {}).get(event.event_type)
        if action is None:
            return

        action_str = action.value if hasattr(action, "value") else str(action)

        # 模式切换
        if action_str == "enter_focus":
            self._mode = DeviceMode.FOCUS
            self._current_session_start = time.time()
            self._current_session_distractions = 0
            logger.info("Mode → FOCUS")
        elif action_str == "end_focus":
            # 结算当前专注 session
            if self._current_session_start is not None:
                duration = time.time() - self._current_session_start
                self._focus_sessions.append({
                    "start": self._current_session_start,
                    "duration_s": round(duration, 1),
                    "distractions": self._current_session_distractions,
                })
                self._current_session_start = None
                logger.info(
                    "Focus session ended: %.1fs, %s distractions",
                    duration, self._current_session_distractions,
                )
            self._mode = DeviceMode.REST
            logger.info("Mode → REST")
        elif action_str == "wake_up":
            self._mode = DeviceMode.REST
            logger.info("Mode → REST (wake up)")

        # 通知前端模式变化
        await self._send_system_frame()

    async def _on_gesture(self, event: GestureEvent) -> None:
        """手势事件 → 查映射表 → 发布对应动作"""

        await self._bus.publish("aggregate:event", RingEventFrame(
            event=event.gesture_name,
            ts=event.timestamp_ms,
        ))

        action = GESTURE_MAP.get(self._mode, {}).get(event.gesture_name)
        if action is None or action.value == "ignore":
            return

        logger.info("Garden action: %s (from %s)", action.value, event.gesture_name)

    async def _on_battery(self, status: SystemStatus) -> None:
        self._battery = status
        self._battery_known = True
        await self._send_system_frame()

    async def _on_connected(self, connected: bool) -> None:
        if connected != self._connected:
            self._battery_known = False
        self._connected = connected
        if not connected:
            # 连接断开，结算当前专注 session
            if self._current_session_start is not None:
                duration = time.time() - self._current_session_start
                self._focus_sessions.append({
                    "start": self._current_session_start,
                    "duration_s": round(duration, 1),
                    "distractions": self._current_session_distractions,
                })
                self._current_session_start = None
        await self._send_system_frame()

    # ── 帧生成 ──

    async def _frame_loop(self) -> None:
        """每 200ms 生成一个聚合帧推给前端"""
        while self._running:
            try:
                await asyncio.sleep(AGGREGATE_INTERVAL_MS / 1000.0)
                if not self._running:
                    break

                frame = AggregatedFrame(
                    ts=int(time.time() * 1000),
                    focus_state=self._last_focus.state.value if self._last_focus else "light_focus",
                    growth_progress=self._last_focus.growth_progress if self._last_focus else 0.0,
                    motion_intensity=self._last_sleep.motion_intensity if self._last_sleep else 0.0,
                    distraction=self._pending_distraction,
                    sleep_stage=self._last_sleep.stage.value if self._last_sleep else None,
                    toss_count=self._last_sleep.toss_count if self._last_sleep else 0,
                )
                self._pending_distraction = False

                await self._bus.publish("aggregate:frame", frame)

                # 落库
                if self._db:
                    self._save_frame(frame)

            except asyncio.CancelledError:
                break

    async def _send_system_frame(self) -> None:
        await self._bus.publish("system:status", SystemFrame(
            battery=self._battery.battery_percent if self._battery_known else None,
            charging=self._battery.battery_charging if self._battery_known else None,
            connected=self._connected,
            mode=self._mode.value,
        ))

    # ── SQLite ──

    def _init_db(self) -> None:
        if not self._db_path:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit prevents long frame transactions from blocking IMU labels.
        self._db = sqlite3.connect(
            str(self._db_path), timeout=5.0, isolation_level=None
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS focus_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts REAL, duration_s REAL, distractions INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS sleep_epochs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT, motion_intensity REAL, toss_count INTEGER,
                ts INTEGER, created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS aggregate_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER, focus_state TEXT, growth_progress REAL,
                motion_intensity REAL, distraction INTEGER,
                sleep_stage TEXT
            )
        """)
        self._db.commit()

    def _save_frame(self, frame: AggregatedFrame) -> None:
        if not self._db:
            return
        try:
            self._db.execute(
                "INSERT INTO aggregate_frames (ts, focus_state, growth_progress, motion_intensity, distraction, sleep_stage) VALUES (?,?,?,?,?,?)",
                (frame.ts, frame.focus_state, frame.growth_progress, frame.motion_intensity, int(frame.distraction), frame.sleep_stage),
            )
            # 每 300 帧 commit 一次（约 1 分钟）
            if frame.ts % 60000 < 200:
                self._db.commit()
        except Exception:
            pass
