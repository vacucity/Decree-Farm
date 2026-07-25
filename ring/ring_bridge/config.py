"""配置常量、可编辑手势映射和引擎阈值。"""

import json
from pathlib import Path
from typing import Any

from .models import DeviceMode, FocusState, GardenAction

# ══════════════════════════════════════════
#  手势映射表（PRD §3.2，D1 冻结）
# ══════════════════════════════════════════

# 格式: GESTURE_MAP[mode][event_name] = GardenAction
GESTURE_MAP: dict[DeviceMode, dict[str, GardenAction]] = {
    DeviceMode.FOCUS: {
        "single_click": GardenAction.MARK_DISTRACTION,
        "double_click": GardenAction.END_FOCUS,
        "wave":         GardenAction.IGNORE,
        "rotate_front": GardenAction.IGNORE,
        "rotate_back":  GardenAction.IGNORE,
    },
    DeviceMode.REST: {
        "single_click": GardenAction.AVATAR_INTERACT,
        "double_click": GardenAction.ENTER_FOCUS,
        "wave":         GardenAction.AVATAR_COME,
        "rotate_front": GardenAction.PLANT_NEW_SEED,
        "rotate_back":  GardenAction.SWITCH_WEATHER,
    },
    DeviceMode.SLEEP: {
        "single_click": GardenAction.IGNORE,
        "double_click": GardenAction.WAKE_UP,
        "wave":         GardenAction.IGNORE,
        "rotate_front": GardenAction.IGNORE,
        "rotate_back":  GardenAction.IGNORE,
    },
}

GESTURE_EVENTS = (
    "single_click",
    "double_click",
    "wave",
    "rotate_front",
    "rotate_back",
)

GESTURE_EVENT_LABELS = {
    "single_click": "单击",
    "double_click": "双击",
    "wave": "挥手",
    "rotate_front": "向前旋转",
    "rotate_back": "向后旋转",
}

GARDEN_ACTION_LABELS = {
    GardenAction.MARK_DISTRACTION.value: "标记分心",
    GardenAction.END_FOCUS.value: "结束专注",
    GardenAction.AVATAR_INTERACT.value: "互动",
    GardenAction.ENTER_FOCUS.value: "进入专注",
    GardenAction.AVATAR_COME.value: "角色靠近",
    GardenAction.PLANT_NEW_SEED.value: "种下种子",
    GardenAction.SWITCH_WEATHER.value: "切换天气",
    GardenAction.WAKE_UP.value: "唤醒",
    GardenAction.IGNORE.value: "不执行动作",
}

GESTURE_MAPPING_FILE = Path(__file__).resolve().parent.parent / "gesture_mappings.json"


def _default_gesture_map() -> dict[DeviceMode, dict[str, GardenAction]]:
    return {
        DeviceMode.FOCUS: {
            "single_click": GardenAction.MARK_DISTRACTION,
            "double_click": GardenAction.END_FOCUS,
            "wave": GardenAction.IGNORE,
            "rotate_front": GardenAction.IGNORE,
            "rotate_back": GardenAction.IGNORE,
        },
        DeviceMode.REST: {
            "single_click": GardenAction.AVATAR_INTERACT,
            "double_click": GardenAction.ENTER_FOCUS,
            "wave": GardenAction.AVATAR_COME,
            "rotate_front": GardenAction.PLANT_NEW_SEED,
            "rotate_back": GardenAction.SWITCH_WEATHER,
        },
        DeviceMode.SLEEP: {
            "single_click": GardenAction.IGNORE,
            "double_click": GardenAction.WAKE_UP,
            "wave": GardenAction.IGNORE,
            "rotate_front": GardenAction.IGNORE,
            "rotate_back": GardenAction.IGNORE,
        },
    }


def serialize_gesture_map() -> dict[str, dict[str, str]]:
    """返回适合 REST API/JSON 保存的映射表。"""
    return {
        mode.value: {
            event: GESTURE_MAP[mode][event].value
            for event in GESTURE_EVENTS
        }
        for mode in (DeviceMode.FOCUS, DeviceMode.REST, DeviceMode.SLEEP)
    }


def gesture_mapping_payload() -> dict[str, Any]:
    """返回前端渲染映射编辑器所需的完整元数据。"""
    return {
        "mappings": serialize_gesture_map(),
        "modes": [
            {"value": DeviceMode.REST.value, "label": "休息"},
            {"value": DeviceMode.FOCUS.value, "label": "专注"},
        ],
        "events": [
            {"value": event, "label": GESTURE_EVENT_LABELS[event]}
            for event in GESTURE_EVENTS
        ],
        "actions": [
            {"value": action.value, "label": GARDEN_ACTION_LABELS[action.value]}
            for action in GardenAction
            if action is not GardenAction.WAKE_UP
        ],
        "storage": str(GESTURE_MAPPING_FILE),
        "device_model_writable": False,
        "device_model_note": (
            "当前公开协议只上报 0701/0702/0703/0704 事件，"
            "没有写入戒指 HMM 模型的命令；这里编辑的是事件到应用动作的映射。"
        ),
    }


def update_gesture_map(
    mappings: dict[str, dict[str, str]],
    *,
    persist: bool = True,
) -> dict[str, dict[str, str]]:
    """校验并原位更新映射，让已运行的聚合器立即生效。"""
    if not isinstance(mappings, dict):
        raise ValueError("mappings 必须是对象")

    updated: dict[DeviceMode, dict[str, GardenAction]] = {}
    modes_to_update = [DeviceMode.FOCUS, DeviceMode.REST]
    if DeviceMode.SLEEP.value in mappings:
        modes_to_update.append(DeviceMode.SLEEP)

    for mode in modes_to_update:
        mode_map = mappings.get(mode.value)
        if not isinstance(mode_map, dict):
            raise ValueError(f"缺少 {mode.value} 模式映射")
        updated[mode] = {}
        for event in GESTURE_EVENTS:
            raw_action = mode_map.get(event)
            try:
                updated[mode][event] = GardenAction(raw_action)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{mode.value}.{event} 的动作无效: {raw_action!r}"
                ) from exc

    if DeviceMode.SLEEP not in updated:
        updated[DeviceMode.SLEEP] = dict(
            GESTURE_MAP.get(
                DeviceMode.SLEEP,
                _default_gesture_map()[DeviceMode.SLEEP],
            )
        )

    GESTURE_MAP.clear()
    GESTURE_MAP.update(updated)
    if persist:
        GESTURE_MAPPING_FILE.write_text(
            json.dumps(serialize_gesture_map(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return serialize_gesture_map()


def reset_gesture_map(*, persist: bool = True) -> dict[str, dict[str, str]]:
    defaults = _default_gesture_map()
    GESTURE_MAP.clear()
    GESTURE_MAP.update(defaults)
    if persist:
        GESTURE_MAPPING_FILE.write_text(
            json.dumps(serialize_gesture_map(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return serialize_gesture_map()


def load_gesture_map() -> None:
    """加载用户保存的映射；损坏文件不会阻止服务启动。"""
    if not GESTURE_MAPPING_FILE.exists():
        return
    try:
        payload = json.loads(GESTURE_MAPPING_FILE.read_text(encoding="utf-8"))
        update_gesture_map(payload, persist=False)
    except (OSError, ValueError, json.JSONDecodeError):
        return


load_gesture_map()

# ══════════════════════════════════════════
#  模式切换逻辑
# ══════════════════════════════════════════

# 双击在某些模式下触发模式切换
DOUBLE_CLICK_MODE_TRANSITION: dict[DeviceMode, DeviceMode] = {
    DeviceMode.REST:  DeviceMode.FOCUS,    # 休息→双击→进入专注
    DeviceMode.SLEEP: DeviceMode.REST,     # 睡眠→双击→起床
}

# 单击也可触发模式切换（PRD：单击在rest模式下不切换）
# 由上层根据业务逻辑处理

# ══════════════════════════════════════════
#  专注引擎阈值（F8）
# ══════════════════════════════════════════

FOCUS_WINDOW_SECONDS = 5            # 滑动窗口大小
FOCUS_WINDOW_SAMPLES = 5 * 25       # 5秒 × 25Hz = 125 采样点

# 加速度方差阈值（原始 ADC 值，需根据实际数据调参）
FOCUS_VARIANCE_DEEP = 5000     # 方差 < 此值 → 深度心流
FOCUS_VARIANCE_LIGHT = 20000   # 方差 < 此值 → 浅专注（>=5000 且 <20000）
                               # 方差 >= 20000 → 分心

# 生长速度（每 200ms 推一帧，每帧增长量）
GROWTH_PER_FRAME_DEEP = 0.008    # 深度心流：125秒长满
GROWTH_PER_FRAME_LIGHT = 0.003   # 浅专注：333秒长满
GROWTH_DECAY_PER_FRAME = 0.005   # 分心时衰减速度

# ══════════════════════════════════════════
#  睡眠引擎阈值（F7，降级趋势指标）
# ══════════════════════════════════════════

SLEEP_EPOCH_SECONDS = 60           # 60 秒一个 epoch
SLEEP_EPOCH_SAMPLES = 60 * 25      # 1500 采样点

# 体动强度阈值
MOTION_THRESHOLD_DEEP = 0.05    # 体动 < 此值 → 深睡趋势
MOTION_THRESHOLD_AWAKE = 0.30   # 体动 > 此值 → 醒/翻身

# ══════════════════════════════════════════
#  聚合帧推送频率
# ══════════════════════════════════════════

AGGREGATE_INTERVAL_MS = 200    # 每 200ms 推一帧（5Hz）

# ══════════════════════════════════════════
#  连接重试
# ══════════════════════════════════════════

RECONNECT_MAX_RETRIES = 5
RECONNECT_BASE_DELAY_S = 2     # 指数退避基础延迟

# ══════════════════════════════════════════
#  BLE 默认参数
# ══════════════════════════════════════════

# 不再把某一枚戒指写死为默认设备。管理面板可手动输入或扫描选择。
DEFAULT_MAC = ""
DEFAULT_COMMAND_TIMEOUT_S = 10.0
