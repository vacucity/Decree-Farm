"""分身农庄 - 现实→游戏映射表 (P2, player_* 驾驶版)

根据现实状态 + 效率因子，输出「行为策略」：一组语义动作 + 下发节奏。
语义动作由 orchestrator 翻译成 player_* 桥接指令驱动主玩家 Game1.player。

设计要点（对齐当前 MOD 能力）：
  - MOD 唯一的自主循环是 player_farm（自动扫描并浇水/收割/清杂物）。
  - "效率"不靠多种自主模式体现，而是靠【下发节奏 cadence】+【productive/idle 动作比例】缩放：
      专注越深 → 越高频保持 player_farm；分心 → 切成无目的闲逛，不推进生产；
      长期分心 → 停手，作物无人浇水自然干枯（可见负面）。
"""
import copy
from dataclasses import dataclass, field
from typing import List
from loguru import logger

# === 语义动作（orchestrator 负责翻译成 player_* 指令）===
FARM = "farm"        # 自主务农：浇水/收割/清杂物  → player_farm
WANDER = "wander"    # 无目的闲逛：随机近格走动    → player_move_to(随机)
IDLE = "idle"        # 停下待命/力竭站立           → player_stop
SLEEP = "sleep"      # 回家过夜                     → player_warp(FarmHouse) 序列

# 分心累计到该次数，升级为"长期分心"负面策略
DISTRACTION_NEG_COUNT = 3


@dataclass
class FarmAction:
    """单个语义动作"""
    move: str                   # FARM | WANDER | IDLE | SLEEP
    priority: int = 0           # 越高越先执行


@dataclass
class BehaviorStrategy:
    """行为策略"""
    mode: str                   # productive | passive | maintenance | negative
    moves: List[FarmAction] = field(default_factory=list)
    action_interval_sec: float = 15.0   # 下发节奏（秒/次），越小越"勤快"
    negative: bool = False              # 是否为负面后果状态
    description: str = ""


# === 映射表：现实状态 → 行为策略 ===
MAP = {
    # 专注：高频保持自主务农，效率越高节奏越快
    "focus": BehaviorStrategy(
        mode="productive",
        moves=[FarmAction(FARM, priority=5)],
        action_interval_sec=12,
        description="高效耕作：自主浇水→收割→清杂物，效率越高越勤快",
    ),
    # 分心：无目的闲逛，不浇水不收割 → 生产停滞
    "distracted": BehaviorStrategy(
        mode="passive",
        moves=[FarmAction(WANDER, priority=1)],
        action_interval_sec=30,
        description="分心：闲逛游走，不推进生产（作物无人打理）",
    ),
    # 休息：主动结束专注，停下待命，维持不衰退
    "rest": BehaviorStrategy(
        mode="maintenance",
        moves=[FarmAction(IDLE, priority=1)],
        action_interval_sec=45,
        description="休息：停下待命，不生产也不衰退",
    ),
    # 睡眠：回家过夜（一次性）
    "sleep": BehaviorStrategy(
        mode="maintenance",
        moves=[FarmAction(SLEEP, priority=1)],
        action_interval_sec=0,
        description="睡眠：回家过夜，世界推进到第二天",
    ),
    # 长期分心：负面后果——彻底停手，作物无人浇水而干枯，体力/金币停滞
    "negative": BehaviorStrategy(
        mode="negative",
        moves=[FarmAction(IDLE, priority=1)],
        action_interval_sec=60,
        negative=True,
        description="长期分心：停手站立，作物缺水枯萎、金币停滞",
    ),
}


def get_strategy(mode: str, efficiency: float, distraction_count: int = 0) -> BehaviorStrategy:
    """现实状态 + 效率 → 行为策略。

    - distracted 且分心累计 >= DISTRACTION_NEG_COUNT 时升级为 negative。
    - productive（专注）时按效率缩放下发节奏：效率越高越勤快。
    """
    if mode == "distracted" and distraction_count >= DISTRACTION_NEG_COUNT:
        base = MAP["negative"]
    else:
        base = MAP.get(mode, MAP["rest"])

    # 复制一份，避免缩放节奏时污染共享模板
    s = copy.copy(base)

    if s.mode == "productive":
        if efficiency > 0.7:
            s.action_interval_sec = max(6.0, base.action_interval_sec * 0.5)
        elif efficiency < 0.3:
            s.action_interval_sec = base.action_interval_sec * 1.5
        else:
            s.action_interval_sec = base.action_interval_sec

    logger.debug(f"策略 [{mode}] eff={efficiency:.2f} distract={distraction_count} → "
                 f"{s.description} ({s.action_interval_sec}s/次)")
    return s
