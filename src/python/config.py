"""分身农庄 - 配置管理

所有可调参数集中于此，环境变量覆盖默认值。
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # === 戒指 ===
    ring_mac: str = os.getenv("RING_MAC", "F1:C1:8A:35:40:FB")
    scan_timeout: float = 10.0
    reconnect_max: int = 5
    reconnect_backoff_base: float = 1.0

    # === 状态引擎 ===
    focus_timeout_sec: int = 300          # 专注超时（5分钟无操作→分心）
    distracted_threshold_sec: int = 180   # 分心累计阈值（触发负面后果）
    sleep_window_start: int = 22          # 睡眠窗口开始（22:00）
    sleep_window_end: int = 7             # 睡眠窗口结束（07:00）
    focus_boost_per_min: float = 0.002    # 每分钟专注增加的效率
    streak_max_bonus: float = 0.3         # 连续天数最大加成

    # === 戒指生物状态接入（自动源：Machine B 分类器 focus_state 流）===
    # 实测分类器(focus-rf-v1) confidence=当前状态概率，threshold=0.385，focused 帧低至 0.40；
    # 门槛设 0.35 只滤掉临界噪声，短时抖动由 dwell 滞回兑底。
    ring_state_min_conf: float = 0.35     # 采纳戒指分类的最低置信度
    ring_state_dwell_sec: int = 5         # 状态需稳定驻留多久才迁移（滞回防抖）
    manual_override_sec: int = 60         # 双击后多久内忽略戒指自动分类

    # === 映射 ===
    farm_tick_interval_sec: int = 15      # 状态检查间隔
    idle_action_interval_sec: int = 60    # 空闲时最低动作间隔

    # === 后果耦合（现实专注 ↔ 游戏后果，全部可调、demo 友好）===
    overwork_sec: int = 10800          # 真实专注超过3小时→精力耗尽晕倒（不逐步扣）
    min_focus_sec: int = 10            # 双击结束时低于此时长→判定"提前退出"施加惩罚
    penalty_money: int = 50            # 每次(提前退出/单次分心)扣的金币
    penalty_wither: int = 2            # 每次(提前退出)枯萎的作物株数
    penalty_money_cap: int = 300       # 分心累计扣钱上限
    penalty_wither_cap: int = 5        # 分心累计枯萎上限
    
    # === 奖励（专注时段正常完成 → 直接加金币/物资）===
    reward_base_money: int = 100       # 完成一次专注的基础奖励金币
    reward_per_minute: int = 10        # 每多专注一分钟额外加的金币
    reward_money_cap: int = 300        # 单次奖励上限
    reward_min_duration_sec: int = 20# 至少专注多久才有奖励（低于此只免惩罚不给奖）

    # === LLM ===
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.anthropic.com")
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.7

    # === STT ===
    stt_api_key: str = os.getenv("STT_API_KEY", "")
    stt_model: str = os.getenv("STT_MODEL", "whisper-1")

    # === MCP ===
    mcp_command: str = "node"
    mcp_args: list = field(default_factory=lambda: [
        "../../vendor/mcp-server/build/index.js"
    ])
    stardew_bridge_path: str = os.getenv(
        "STARDEW_BRIDGE_PATH",
        r"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\StardewMCPBridge"
    )
    stardew_action_dir: str = os.getenv(
        "STARDEW_ACTION_DIR",
        r"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\StardewMCPBridge\actions"
    )

    # === WebSocket ===
    ws_host: str = "localhost"
    ws_port: int = 8765

    # === 局域网中继（跨机对接：戒指+前端 ↔ 游戏+Agent）===
    ws_lan_host: str = "0.0.0.0"
    ws_lan_port: int = 8766
    state_broadcast_interval_sec: float = 2.0  # 状态广播给前端的频率（秒）

    # === TTS ===
    tts_enabled: bool = True
    tts_lang: str = "zh-CN"
    tts_rate: float = 1.0

    # === 演示兜底 ===
    demo_mode: bool = os.getenv("DEMO_MODE", "0") == "1"
    # LAN_ONLY=1 时不连本机戒指，仅通过 LAN 中继接收 Machine B 的数据
    lan_only: bool = os.getenv("LAN_ONLY", "1") == "1"

    # === 日志 ===
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


config = Config()
