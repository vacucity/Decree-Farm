"""分身农庄 - 编排器 + Agent (P3, player_* 驾驶版)

核心：现实状态/语音事件 → 映射策略 → 翻译成 player_* 桥接指令，直接驾驶主玩家 Game1.player。
不再生成 shadow farmer / companion——玩家本人的化身就是分身。
"""
import asyncio
import random
import time
from typing import Optional, Dict, Any

# 专注期间可随机选择的活动（farm=种地含除杂物, fish=钓鱼）及抽中权重：80% 种田 / 20% 钓鱼
FOCUS_ACTIVITIES = ["farm", "fish"]
FOCUS_ACTIVITY_WEIGHTS = [0.8, 0.2]
# 钓鱼始终去海边
FISH_SPOTS = ["Beach"]

from loguru import logger

from config import config
from event_bus import event_bus, FarmEvent
from state_engine import state_engine
from mapping import get_strategy, BehaviorStrategy, FARM, WANDER, IDLE, SLEEP
from agent import ActionBridge, IntentParser
from brain import Brain, HeuristicPolicy, LLMBrain, LOW_STAMINA_RATIO, NIGHT_TIME


class GamePilot:
    """把语义动作翻译成 player_* 桥接指令，直接驱动主玩家。"""

    # 已知安全落点（location, x, y）
    FARMHOUSE = ("FarmHouse", 3, 11)

    def __init__(self, bridge=None):
        # 复用外部传入的桥（与 Brain 共用 HybridBridge，保证 WS 优先且单一下发方）；
        # 未传入时自建文件桥（独立调试用）。
        self.bridge = bridge or ActionBridge()
        self.parser = IntentParser(use_llm=bool(config.llm_api_key))

    # --- 状态读取 ---
    def state(self) -> Dict[str, Any]:
        return self.bridge.read_state()

    def agent_player(self) -> Dict[str, Any]:
        return self.bridge.read_agent_player()

    def money(self) -> int:
        return (self.state().get("player") or {}).get("money", 0)

    def stamina(self) -> float:
        """当前体力（player.stamina 优先，回退 agentPlayer.stamina）。"""
        st = self.state()
        player = st.get("player") or {}
        ap = st.get("agentPlayer") or {}
        return float(player.get("stamina", ap.get("stamina", 0)))

    def _tile(self) -> tuple:
        t = (self.agent_player().get("tile") or {})
        return int(t.get("x", 64)), int(t.get("y", 15))

    # --- 语义动作 → player_* ---
    def farm(self):
        """自主务农。仅在化身未处于 farm 模式时重新 kick，避免打断正在进行的动作。"""
        if self.agent_player().get("mode") != "farm":
            self.bridge.send({"actionType": "player_farm"})

    def wander(self):
        """无目的闲逛：朝当前位置附近的随机格走动，不推进生产。"""
        x, y = self._tile()
        nx, ny = x + random.randint(-4, 4), y + random.randint(-4, 4)
        self.bridge.send({"actionType": "player_move_to", "x": nx, "y": ny})

    def idle(self):
        """停下待命 / 力竭站立（保留托管田）。"""
        self.bridge.send({"actionType": "player_stop"})

    def full_stop(self):
        """彻底停止、清除托管田（专注结束 / 休息时用）。"""
        self.bridge.send({"actionType": "player_idle"})

    def sleep(self):
        """回家睡觉：下发 player_sleep，MOD 会自动传送回农舍、入床并推进到第二天。
        （早期版本仅传送回家不入睡；现 MOD 已实现真正的结束当天）。"""
        self.bridge.send({"actionType": "player_sleep"})

    def voice(self, text: str):
        """语音指令：复用 Agent 的意图解析器，NL → player_* 动作序列。"""
        actions = self.parser.parse(text)
        for a in actions:
            self.bridge.send(a)
            time.sleep(0.05)
        return actions

    # 语义动作分发表
    def execute_move(self, move: str):
        {FARM: self.farm, WANDER: self.wander, IDLE: self.idle, SLEEP: self.sleep} \
            .get(move, self.idle)()


class Orchestrator:
    """编排器：现实事件 → 策略 → 主玩家动作"""

    def __init__(self):
        # 一个 Brain 持有自治大脑 + HybridBridge；GamePilot 复用同一桥。
        goal = "经营农场：高效耕种、浇水、收获、出货、买种、挖矿、复种。"
        backend = LLMBrain(goal) if config.llm_api_key else HeuristicPolicy()
        self.brain = Brain(backend)
        self.pilot = GamePilot(bridge=self.brain.bridge)
        self._current_strategy: Optional[BehaviorStrategy] = None
        self._focus_start_gold: int = 0
        self._last_action_ts: float = 0
        self._running = False
        self._action_task: Optional[asyncio.Task] = None
        self.focus_active = False
        self.fainted = False
        self._pending_sleep_walk = False  # 睡眠窗口：正在步行回家、到家才入睡
        self._wake_event = asyncio.Event()  # 唤醒 action_loop

    async def start(self):
        event_bus.subscribe("focus_start", self._on_focus_start)
        event_bus.subscribe("focus_resume", self._on_focus_resume)
        event_bus.subscribe("rest", self._on_rest)
        event_bus.subscribe("distracted", self._on_distracted)
        event_bus.subscribe("sleep", self._on_sleep)
        event_bus.subscribe("voice_cmd", self._on_voice_cmd)
        event_bus.subscribe("tick", self._on_tick)

        self._running = True
        self._action_task = asyncio.create_task(self._action_loop())
        logger.info("编排器已启动（纯玩家驾驶模式）")

    async def stop(self):
        self._running = False
        if self._action_task:
            self._action_task.cancel()

    # === 事件处理 ===

    async def _on_focus_start(self, event: FarmEvent):
        self.focus_active = True
        self.fainted = False
        self._pending_sleep_walk = False  # 深夜主动专注：取消回家入睡流程
        self._focus_start_gold = self.pilot.money()
        # 支持指定活动（测试用），否则随机
        forced = event.data.get("activity") if event.data else None
        if forced and forced in FOCUS_ACTIVITIES:
            activity = forced
        else:
            activity = random.choices(FOCUS_ACTIVITIES, weights=FOCUS_ACTIVITY_WEIGHTS, k=1)[0]
        self.brain.activity = activity
        # 种地或钓鱼二选一；钓鱼固定去海边
        if activity == "fish":
            self.brain.activity_target = "Beach"
        else:
            self.brain.activity_target = None
        logger.info(f"本次专注活动：{activity}" +
                    (f" → {self.brain.activity_target}" if self.brain.activity_target else ""))
        self._update_strategy()
        self._wake_event.set()  # 立即唤醒 action_loop（action_loop 内调 _brain_step）
        await event_bus.publish(FarmEvent(type="feedback", data={
            "msg": f"专注开始！化身选择了：{activity}",
            "positive": True,
        }))

    async def _on_focus_resume(self, event: FarmEvent):
        """戒指从 distracted 恢复专注：立即唤醒 action_loop 继续干活（不重新随机活动）。"""
        self._wake_event.set()

    async def _on_rest(self, event: FarmEvent):
        duration = event.data.get("duration", 0)
        self.focus_active = False
        self.fainted = False
        self._update_strategy()

        if duration < config.min_focus_sec:
            # --- 惩罚：提前退出（专注不足 min_focus_sec 就结束）---
            self.pilot.bridge.send({"actionType": "player_penalty",
                                    "money": config.penalty_money,
                                    "wither": config.penalty_wither})
            await event_bus.publish(FarmEvent(type="feedback", data={
                "msg": f"专注不足 {duration:.0f}s 就退出——扣 {config.penalty_money}g，"
                       f"{config.penalty_wither} 株作物枯萎。",
                "positive": False,
            }))
        elif duration >= config.reward_min_duration_sec:
            # --- 奖励：正常完成且达到最低奖励时长 ---
            minutes = duration / 60.0
            reward = min(
                config.reward_base_money + int(minutes * config.reward_per_minute),
                config.reward_money_cap
            )
            self.pilot.bridge.send({"actionType": "player_reward",
                                    "money": reward, "emote": 32})
            farm_delta = self.pilot.money() - self._focus_start_gold
            await event_bus.publish(FarmEvent(type="feedback", data={
                "msg": f"专注 {minutes:.0f} 分钟完成！奖励 +{reward}g，"
                       f"农场收益 +{farm_delta}g",
                "positive": True,
                "reward": reward,
                "gold_delta": farm_delta,
            }))
        else:
            # 中间地带：超过 min_focus_sec 但未达 reward_min_duration_sec——免罚不奖
            delta = self.pilot.money() - self._focus_start_gold
            await event_bus.publish(FarmEvent(type="feedback", data={
                "msg": f"专注 {duration/60:.0f} 分钟，农场收益 +{delta}g（再久一点就能拿奖励）",
                "positive": True,
                "gold_delta": delta,
            }))

        self.pilot.full_stop()  # 专注结束：彻底停止并清除托管田
        await event_bus.publish(FarmEvent(type="feedback", data={
            "msg": "专注结束，化身停下休息", "positive": True,
        }))

    async def _on_distracted(self, event: FarmEvent):
        self._update_strategy()
        count = event.data.get("distraction_count", 0)
        money = min(config.penalty_money * count, config.penalty_money_cap)
        wither = min(count, config.penalty_wither_cap)
        self.pilot.bridge.send({"actionType": "player_penalty",
                                "money": money, "wither": wither})
        await event_bus.publish(FarmEvent(type="feedback", data={
            "msg": f"你已分心 {count} 次——扣 {money}g，作物开始枯萎，回来专注吧。",
            "positive": False,
        }))
        self.pilot.idle()  # 分心：角色立即停手——运动→静止，视觉对比明显
        await event_bus.publish(FarmEvent(type="feedback", data={
            "msg": f"分心休假了 {count} 次，角色停下等你回来", "positive": False,
        }))

    async def _on_sleep(self, event: FarmEvent):
        """现实睡眠窗口：不许原地瞬移入睡——先步行回农舍，到家才上床。
        player_sleep 自带的 warp 只作为 MOD 侧兜底，这里不主动触发。"""
        self.focus_active = False
        self._pending_sleep_walk = True
        self._try_sleep_step()
        self._update_strategy()

    def _try_sleep_step(self):
        """睡眠窗口的推进步：在家→入睡；在外→步行回家（路上不重发指令）。"""
        if not self._pending_sleep_walk:
            return
        try:
            st = self.pilot.state()
            if not st:
                return
            ap = st.get("agentPlayer") or {}
            if st.get("location") == "FarmHouse":
                logger.info("睡眠窗口：已到家，上床睡觉")
                self._pending_sleep_walk = False
                self.pilot.sleep()
                return
            # 回家路上/过门中：别打断，也避免每轮重发
            if ap.get("moving") or ap.get("exiting") or ap.get("traveling"):
                return
            logger.info("睡眠窗口：步行回农舍睡觉")
            self.pilot.bridge.send({"actionType": "player_go_to", "target": "FarmHouse"})
        except Exception as e:
            logger.error(f"睡眠推进异常: {e}")

    async def _on_voice_cmd(self, event: FarmEvent):
        """语音指令 → Agent 意图解析 → player_* 指令。"""
        cmd = event.data.get("text", "")
        logger.info(f"语音指令: {cmd}")
        actions = self.pilot.voice(cmd)
        logger.info(f"语音 → {[a.get('actionType') for a in actions]}")

    async def _on_tick(self, event: FarmEvent):
        self._update_strategy()

    def _update_strategy(self):
        self._current_strategy = get_strategy(
            state_engine.state.mode,
            state_engine.state.efficiency,
            state_engine.state.distraction_count,
        )

    def _idle_guard(self):
        """非专注护栏：普通情况下体力见底/过午夜也要回家睡觉，不能干站着。
        专注中的护栏由 brain._safety_override 负责；这里只管非专注时段。"""
        try:
            st = self.pilot.state()
            if not st:
                return
            player = st.get("player") or {}
            ap = st.get("agentPlayer") or {}
            stamina = float(player.get("stamina", ap.get("stamina", 0)))
            max_st = float(ap.get("maxStamina") or 270)
            low = max_st > 0 and (stamina / max_st) <= LOW_STAMINA_RATIO
            night = int(st.get("time") or 0) >= NIGHT_TIME
            if not (low or night):
                return
            # 已在回家路上/过门中：别打断，也避免每轮重发指令
            if ap.get("moving") or ap.get("exiting") or ap.get("traveling"):
                return
            reason = "已过午夜" if night else "体力不足"
            if st.get("location") == "FarmHouse":
                logger.info(f"非专注护栏：{reason}，上床睡觉结束这一天")
                self.pilot.sleep()
            else:
                logger.info(f"非专注护栏：{reason}，步行回农舍睡觉")
                self.pilot.bridge.send({"actionType": "player_go_to", "target": "FarmHouse"})
        except Exception as e:
            logger.error(f"非专注护栏异常: {e}")

    # === 动作执行循环 ===

    async def _brain_step(self):
        """在线程池里跑 brain.step()，避免阻塞事件循环（LLM/发指令含阻塞 IO）。"""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.brain.step)
            self._last_action_ts = time.time()
        except Exception as e:
            logger.error(f"brain.step 异常: {e}")

    async def _action_loop(self):
        """门控循环：focus 且未过劳→放行 brain 自治；非专注→静默等待不发指令。"""
        while self._running:
            try:
                mode = state_engine.state.mode
                if mode == "focus" and self.focus_active:
                    dur = state_engine.state.focus_duration_sec
                    if dur <= config.overwork_sec:
                        # 未过劳：放行全流程自治，节奏随效率缩放
                        await self._brain_step()
                        wait = (self._current_strategy.action_interval_sec
                                if self._current_strategy else config.idle_action_interval_sec)
                    elif self.fainted:
                        # 已晕倒：静待，不再动作
                        wait = config.idle_action_interval_sec
                    else:
                        # 超过3小时：一次性耗尽精力 + 晕倒
                        self.pilot.bridge.send({"actionType": "player_faint"})
                        self.fainted = True
                        await event_bus.publish(FarmEvent(type="feedback", data={
                            "msg": "专注超过3小时，化身力竭晕倒——该歇歇了。",
                            "positive": False,
                        }))
                        wait = config.idle_action_interval_sec
                else:
                    # 非专注状态：静默等待，可被 focus_start 唤醒；
                    # 睡眠窗口回家流程要持续推进（跨图过门后重新发路径、到家入睡）
                    self._try_sleep_step()
                    # 但体力见底/过午夜时照样要回家睡觉（普通情况护栏）
                    self._idle_guard()
                    wait = config.idle_action_interval_sec

                # 使用 Event 等待：可被 focus_start 立即唤醒
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=max(wait, 1))
                except asyncio.TimeoutError:
                    pass  # 正常超时，继续循环
            except Exception as e:
                logger.error(f"动作循环异常: {e}")
                await asyncio.sleep(5)


# 全局单例
orchestrator = Orchestrator()
