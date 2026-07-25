"""分身农庄 - 反馈闭环 (P4)

游戏状态回读 → TTS/HUD 正负反馈。
"""
import asyncio
import json
from pathlib import Path
from loguru import logger

from config import config
from event_bus import event_bus, FarmEvent
from state_engine import state_engine


class Feedback:
    """反馈引擎：游戏状态 → 用户可感知的反馈"""

    def __init__(self):
        self.bridge_path = Path(config.stardew_bridge_path) / "bridge_data.json"
        self._last_gold: int = 0
        self._last_stamina: int = 270
        self._running = False
        self._task = None

    async def start(self):
        event_bus.subscribe("feedback", self._on_feedback)
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("反馈引擎已启动")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _on_feedback(self, event: FarmEvent):
        """处理反馈事件 → TTS"""
        msg = event.data.get("msg", "")
        if not msg:
            return
        positive = event.data.get("positive", True)

        # 桌面 TTS
        if config.tts_enabled:
            await self._speak(msg)

        # 终端输出
        prefix = "✅" if positive else "⚠️"
        logger.info(f"反馈: {prefix} {msg}")

    async def _speak(self, text: str):
        """Windows TTS（使用 PowerShell）"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-Command",
                f'Add-Type -AssemblyName System.Speech; '
                f'$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                f'$s.Rate = {int(config.tts_rate)}; '
                f'$s.Speak("{text}")',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as e:
            logger.warning(f"TTS 失败: {e}")

    async def _monitor_loop(self):
        """定时监控游戏状态变化"""
        while self._running:
            try:
                st = self._read_game_state()
                if not st:
                    await asyncio.sleep(5)
                    continue

                gold = st.get("player", {}).get("money", 0)
                stamina = st.get("player", {}).get("stamina", 270)
                companions = st.get("companions", [])

                # 金币变化检测
                if self._last_gold > 0:
                    delta = gold - self._last_gold
                    if delta > 50:  # 收益显著时报告
                        mode = state_engine.state.mode
                        eff = state_engine.state.efficiency
                        logger.info(f"📊 金币 +{delta}g | 状态={mode} | 效率={eff:.2f}")

                # 体力告警
                if stamina < 50:
                    await event_bus.publish(FarmEvent(type="feedback", data={
                        "msg": f"农场分身体力不足（{stamina}/270），休息一下吧。",
                        "positive": False
                    }))

                self._last_gold = gold
                self._last_stamina = stamina
            except Exception as e:
                logger.error(f"监控异常: {e}")

            await asyncio.sleep(30)

    def _read_game_state(self) -> dict:
        if not self.bridge_path.exists():
            return {}
        try:
            return json.loads(self.bridge_path.read_text(encoding="utf-8"))
        except Exception:
            return {}


# 全局单例
feedback = Feedback()
