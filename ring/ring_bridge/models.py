"""数据模型 — 所有模块共享的 dataclass 定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── 设备模式 ──


class DeviceMode(str, Enum):
    FOCUS = "focus"       # 专注模式（录音模式）
    REST = "rest"         # 休息/自由模式
    SLEEP = "sleep"       # 睡眠模式
    UNKNOWN = "unknown"


# ── 专注状态 ──


class FocusState(str, Enum):
    DEEP_FLOW = "deep_flow"    # 深度心流：静止，种子快速生长
    LIGHT_FOCUS = "light_focus"  # 浅专注：微动，生长缓慢
    DISTRACTED = "distracted"   # 分心：剧烈运动，暂停生长


# ── 睡眠阶段 ──


class SleepStage(str, Enum):
    DEEP = "deep"          # 深睡趋势
    LIGHT = "light"        # 浅睡趋势
    AWAKE = "awake"        # 醒/翻身


# ══════════════════════════════════════════
#  戒指原语事件
# ══════════════════════════════════════════


@dataclass
class IMUSample:
    """单个 IMU 采样点（16 字节解析结果）"""
    timestamp_ms: int
    sequence: int
    accel_x: int
    accel_y: int
    accel_z: int
    gyro_x: int
    gyro_y: int
    gyro_z: int


@dataclass
class IMUBatch:
    """批量 IMU 采样点"""
    sequence_start: int
    samples: list[IMUSample]


@dataclass
class GestureEvent:
    """手势识别事件（0x0702）"""
    timestamp_ms: int
    gesture_id: int          # 1=rotate_back, 2=rotate_front, 3=wave
    gesture_name: str        # "rotate_back" / "rotate_front" / "wave"


@dataclass
class ButtonEvent:
    """按键事件（0x0703 双击 / 0x0704 单击）"""
    timestamp_ms: int
    event_type: str          # "single_click" | "double_click"


@dataclass
class AudioRecording:
    """录音完成事件"""
    file_index: int
    raw_bytes: bytes


@dataclass
class SystemStatus:
    """系统状态事件"""
    battery_percent: int
    battery_charging: bool
    connected: bool


# ══════════════════════════════════════════
#  引擎输出
# ══════════════════════════════════════════


@dataclass
class FocusOutput:
    """专注引擎输出"""
    state: FocusState
    growth_progress: float      # 0.0 ~ 1.0，生长进度
    stillness_score: float      # 静止分数 0.0 ~ 1.0
    distraction: bool           # 是否刚触发分心标记


@dataclass
class SleepOutput:
    """睡眠引擎输出"""
    stage: SleepStage
    motion_intensity: float     # 0.0 ~ 1.0，体动强度（驱动月光）
    toss_count: int             # 当前 epoch 翻身次数


# ══════════════════════════════════════════
#  聚合帧（推送给前端的最终格式）
# ══════════════════════════════════════════


@dataclass
class AggregatedFrame:
    """每 200ms 推给前端的聚合帧"""
    ts: int                              # 毫秒时间戳
    focus_state: str = "light_focus"
    growth_progress: float = 0.0
    motion_intensity: float = 0.0
    distraction: bool = False
    # 可选：睡眠相关（仅在睡眠模式时有效）
    sleep_stage: str | None = None
    toss_count: int = 0


@dataclass
class RingEventFrame:
    """推给前端的手势/按键事件"""
    event: str                 # "single_click" | "double_click" | "wave" | ...
    ts: int


@dataclass
class SystemFrame:
    """推给前端的系统状态"""
    battery: int | None
    charging: bool | None
    connected: bool
    mode: str                   # "focus" | "rest" | "sleep" | "unknown"


# ══════════════════════════════════════════
#  手势映射动作
# ══════════════════════════════════════════


class GardenAction(str, Enum):
    """18 格手势映射表中的花园动作"""
    MARK_DISTRACTION = "mark_distraction"
    END_FOCUS = "end_focus"
    AVATAR_INTERACT = "avatar_interact"
    ENTER_FOCUS = "enter_focus"
    AVATAR_COME = "avatar_come"
    PLANT_NEW_SEED = "plant_new_seed"
    SWITCH_WEATHER = "switch_weather"
    WAKE_UP = "wake_up"
    IGNORE = "ignore"
