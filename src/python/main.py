"""分身农庄 - 主入口

启动全链路：戒指桥接 + 状态引擎 + 编排器 + 反馈。
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "python"))

from loguru import logger

# 配置日志
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
    level="INFO"
)
logger.add("farm_log_{time:YYYY-MM-DD}.log", rotation="10 MB", level="DEBUG")

from config import config
from event_bus import event_bus, FarmEvent
from ring_bridge import RingBridge
from state_engine import state_engine
from orchestrator import orchestrator
from feedback import feedback
from ws_bridge import LanRelay


# ======================================================================
# 局域网中继：接收远程戒指事件 / 用户指令，广播游戏状态
# ======================================================================
_lan_relay: LanRelay = None
_schedule_tasks: list = []  # 当前日程列表（前端通过 user_command:schedule 下发）
_test_mode: bool = False     # 活动测试模式，阻止 Machine B 干扰
_main_loop = None            # 主事件循环引用（LAN 回调在后台线程触发，需明确投递目标）


def _publish_threadsafe(event_type: str, data: dict):
    """把事件从 LanRelay 后台线程安全投递到主事件循环（state_engine/orchestrator 所在）。"""
    import asyncio as _aio
    if _main_loop is None:
        logger.warning(f"主循环未就绪，丢弃 LAN 事件: {event_type}")
        return
    _aio.run_coroutine_threadsafe(
        event_bus.publish(FarmEvent(type=event_type, data=data)), _main_loop)


def _on_remote_ring_event(event: str, data: dict):
    """LAN 收到远程戒指事件 → 注入本地 event_bus（与本地 ring_bridge 等效）。"""
    # 测试模式下忽略 Machine B 的戒指事件，避免干扰活动验证
    if _test_mode:
        return
    if event == "double_tap":
        _publish_threadsafe("double_tap", data)
    elif event == "voice_recorded":
        _publish_threadsafe("voice_recorded", data)
    elif event == "ring_state":
        _publish_threadsafe("ring_state", data)


def _on_remote_user_command(command: str, data: dict):
    """LAN 收到用户前端指令 → 映射到 event_bus 事件。"""
    global _schedule_tasks
    # 测试模式下忽略 Machine B 的 focus_session，避免干扰
    if _test_mode and command in ("start_focus", "end_focus"):
        return
    if command == "start_focus":
        _publish_threadsafe("double_tap", data)
    elif command == "end_focus":
        _publish_threadsafe("double_tap", {})
    elif command == "schedule":
        # 日程规划指令：存储任务列表并立即广播给所有前端
        tasks = data.get("tasks", [])
        _schedule_tasks = tasks
        logger.info(f"收到日程规划指令: {tasks}")
        # 立即广播一次状态（包含新日程）让前端更新
        if _lan_relay and _lan_relay.connected:
            snapshot = _build_state_snapshot()
            _lan_relay.broadcast_state(snapshot)


def _build_state_snapshot() -> dict:
    """构建要广播给前端 HUD 的状态快照。"""
    from pathlib import Path
    import json as _json

    # 读取游戏状态
    bridge_path = Path(config.stardew_bridge_path) / "bridge_data.json"
    game_state = {}
    try:
        if bridge_path.exists():
            game_state = _json.loads(bridge_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    player = game_state.get("player") or {}
    ap = game_state.get("agentPlayer") or {}
    rs = state_engine.state

    return {
        "ring": {"connected": _lan_relay.connected if _lan_relay else False},
        "real": {
            "mode": rs.mode,
            "efficiency": round(rs.efficiency, 2),
            "focus_elapsed_sec": round(rs.focus_duration_sec, 0),
            "distraction_count": rs.distraction_count,
        },
        "game": {
            "gold": player.get("money", 0),
            "stamina": player.get("stamina", ap.get("stamina", 0)),
            "location": game_state.get("location", ""),
            "mode": ap.get("mode", "idle"),
            "day_time": game_state.get("time", 0),
            "day": game_state.get("day"),
            "season": game_state.get("season"),
        },
        "schedule": _schedule_tasks,  # 日程任务列表
        "feedback": {"msg": "", "positive": True},
    }


async def demo_loop():
    """演示兜底：无戒指时模拟事件序列，按“专注→过劳晕倒→提前退出惩罚”讲故事。"""
    logger.info("🎮 DEMO 模式：模拟事件序列")

    # ① 开始专注 → 化身自治干活
    await asyncio.sleep(2)
    logger.info("🎮 [1/3] 双击开始专注——观察化身自治耕作")
    await event_bus.publish(FarmEvent(type="double_tap"))

    # ② 专注超过 overwork_sec → 体力逐步下降直至晕倒
    watch = config.overwork_sec + config.farm_tick_interval_sec * 3
    logger.info(f"🎮 [2/3] 持续专注 ~{watch}s（>overwork_sec={config.overwork_sec}s）——看体力条下降直至晕倒")
    await asyncio.sleep(watch)
    await event_bus.publish(FarmEvent(type="double_tap"))  # 久专注后结束→正常完成

    # ③ 新一轮：开始专注后很快（<min_focus_sec）双击结束→提前退出惩罚
    await asyncio.sleep(5)
    logger.info(f"🎮 [3/3] 再开专注后 <min_focus_sec={config.min_focus_sec}s 立即结束——看提前退出扣钱+作物枯萎")
    await event_bus.publish(FarmEvent(type="double_tap"))  # 开始专注
    await asyncio.sleep(max(config.min_focus_sec // 3, 3))
    await event_bus.publish(FarmEvent(type="double_tap"))  # 提前结束
    await asyncio.sleep(3)
    logger.info("🎮 DEMO 循环结束")


async def demo_ring_loop():
    """演示戒指生物状态接入：模拟分类器持续吐 ring_state，驱动自动专注/分心/结束。"""
    logger.info("🎮 DEMO_RING 模式：模拟戒指生物状态信号流")

    async def stream(state: str, conf: float, seconds: float, period: float = 3.0):
        """按 period 持续推送同一状态，模拟分类器的信号流（驱动滞回提交）。"""
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            await event_bus.publish(FarmEvent(type="ring_state", data={
                "state": state, "confidence": conf, "source": "ring_bio",
            }))
            await asyncio.sleep(period)

    # ① 检测到专注（稳定驻留后进入 focus → 化身自动开工）
    await asyncio.sleep(2)
    logger.info(f"🎮 [1/3] 推送 focus 信号流——稳定 {config.ring_state_dwell_sec}s 后进入自动专注")
    await stream("focus", 0.9, seconds=config.ring_state_dwell_sec + 20)

    # ② 检测到分心（触发惩罚：扣钱 + 作物枯萎）
    logger.info("🎮 [2/3] 推送 distracted 信号流——触发分心惩罚")
    await stream("distracted", 0.85, seconds=config.ring_state_dwell_sec + 6)

    # ③ 检测到离开/休息（结束专注 → 退出结算）
    logger.info("🎮 [3/3] 推送 rest 信号流——结束专注并结算")
    await stream("rest", 0.9, seconds=config.ring_state_dwell_sec + 6)

    await asyncio.sleep(3)
    logger.info("🎮 DEMO_RING 循环结束")


# — 活动测试模式：farm → mine → fish → forage 顺序验证 —
TEST_ACTIVITIES = [("farm", "种地"), ("mine", "挖矿"), ("fish", "钓鱼"), ("forage", "采集")]
TEST_DURATION = 180
TEST_PAUSE = 30


async def test_activities_loop():
    """在 main.py 进程内直接发事件，绕过 LAN relay 避免 Machine B 干扰。"""
    logger.info("=" * 50)
    logger.info("🧪 活动测试模式: farm → mine → fish → forage")
    logger.info(f"   每项 {TEST_DURATION}s, 间隔 {TEST_PAUSE}s")
    logger.info("=" * 50)

    for i, (act, name) in enumerate(TEST_ACTIVITIES, 1):
        logger.info(f"🧪 [{i}/4] 开始 {act}({name})...")
        await event_bus.publish(FarmEvent(type="double_tap", data={"activity": act}))

        elapsed = 0
        while elapsed < TEST_DURATION:
            await asyncio.sleep(15)
            elapsed += 15

        await event_bus.publish(FarmEvent(type="double_tap", data={}))
        logger.info(f"🧪 [{i}/4] {act} 测试完成")

        if i < len(TEST_ACTIVITIES):
            await asyncio.sleep(TEST_PAUSE)

    logger.info("🧪 全部 4 项活动测试完成！")


async def main():
    global _lan_relay, _main_loop
    _main_loop = asyncio.get_running_loop()  # LAN 后台线程投递事件的目标循环

    logger.info("=" * 50)
    logger.info("🌾 分身农庄 Avatar Farm 启动中...")
    logger.info(f"   DEMO_MODE = {config.demo_mode}")
    logger.info(f"   戒指 MAC = {config.ring_mac}")
    logger.info(f"   LAN 中继 = ws://0.0.0.0:{config.ws_lan_port}")
    logger.info("=" * 50)

    # 启动引擎（必须先于 ring bridge）
    await state_engine.start()
    await orchestrator.start()
    await feedback.start()

    # 启动 LAN 中继（跨机对接）
    _lan_relay = LanRelay(config.ws_lan_host, config.ws_lan_port)
    _lan_relay.set_handlers(
        on_ring_event=_on_remote_ring_event,
        on_user_command=_on_remote_user_command,
    )
    _lan_relay.start()

    # 启动状态广播循环（定时推送游戏+现实状态给前端 HUD）
    broadcast_task = asyncio.create_task(_state_broadcast_loop())

    # — 测试模式：--test-activities 跳过 demo/ring/lan_only，直接跑活动验证 —
    if "--test-activities" in sys.argv:
        global _test_mode
        _test_mode = True
        # 等待 MOD 连入后再开始
        await asyncio.sleep(3)
        logger.info("🧪 等待 5 秒确保状态就绪...")
        await asyncio.sleep(5)
        await test_activities_loop()
        await asyncio.sleep(60)  # 等 admin 手动看结果
        broadcast_task.cancel()
        return

    if config.demo_mode:
        # Demo 模式：模拟事件（DEMO_RING=1 走戒指生物状态流演示）
        if os.getenv("DEMO_RING", "0") == "1":
            await demo_ring_loop()
        else:
            await demo_loop()
    elif config.lan_only:
        # LAN_ONLY 模式：不连本机戒指，仅等 Machine B 通过 LAN 中继推送
        logger.info("LAN_ONLY 模式：跳过本地戒指，等待 Machine B 连入 ws://0.0.0.0:{}", config.ws_lan_port)
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
    else:
        # 真戒指模式（本机连接 BLE 戒指）
        bridge = RingBridge()
        try:
            await bridge.start()
        except KeyboardInterrupt:
            pass
        finally:
            await bridge.stop()

    broadcast_task.cancel()
    # 等待一段时间让最后的动作执行完
    await asyncio.sleep(5)

    logger.info("🌾 分身农庄已停止")


async def _state_broadcast_loop():
    """定时向 LAN 中继广播状态快照，供前端 HUD 渲染。"""
    while True:
        try:
            if _lan_relay and _lan_relay.connected:
                snapshot = _build_state_snapshot()
                _lan_relay.broadcast_state(snapshot)
        except Exception as e:
            logger.error(f"状态广播异常: {e}")
        await asyncio.sleep(config.state_broadcast_interval_sec)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户中断")
