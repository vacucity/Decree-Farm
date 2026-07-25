"""分身农庄 - 戒指桥接 (P1)

监听戒指双击(0x0703) + 长按录音(0x0505)，
转换为 FarmEvent 发布到事件总线。
"""
import asyncio
import sys
import os

# 添加 ring_sound.py 路径
sys.path.insert(0, os.path.dirname(__file__))

from config import config
from event_bus import event_bus, FarmEvent
from loguru import logger


class RingBridge:
    """戒指桥接：BLE → FarmEvent"""

    def __init__(self):
        self.mac = config.ring_mac
        self._client = None
        self._running = False

    async def start(self):
        """启动桥接：连接戒指 + 并发监听"""
        if config.demo_mode:
            logger.info("DEMO_MODE=1，跳过真戒指连接")
            self._running = True
            return

        import ring_sound as sdk

        for attempt in range(config.reconnect_max):
            try:
                devices = await sdk.scan_rings(mac=self.mac)
                if not devices:
                    logger.warning(f"未扫描到戒指 {self.mac}")
                    raise ConnectionError("设备未找到")

                self._client = sdk.RingSoundClient(address=self.mac)
                await self._client.__aenter__()
                logger.info(f"戒指已连接 {self.mac}")

                # 并发监听
                self._running = True
                await asyncio.gather(
                    self._listen_double_press(),
                    self._listen_auto_audio(),
                )
                return

            except Exception as e:
                delay = config.reconnect_backoff_base * (2 ** attempt)
                logger.warning(f"连接失败 ({attempt+1}/{config.reconnect_max}): {e}，{delay}s 后重试")
                await asyncio.sleep(delay)

        logger.error("戒指连接彻底失败，请检查 BLE/电量")
        self._running = False

    async def stop(self):
        self._running = False
        if self._client:
            await self._client.__aexit__(None, None, None)

    async def _listen_double_press(self):
        """监听双击 0x0703: 专注开关"""
        import ring_sound as sdk

        while self._running:
            try:
                event = await sdk.wait_sensor_key_double_press_event(self._client)
                logger.info(f"双击事件")
                # 切换专注状态（由 state_engine 决定 start/end）
                await event_bus.publish(FarmEvent(type="double_tap", data={}))
            except Exception as e:
                if self._running:
                    logger.error(f"双击监听异常: {e}")
                await asyncio.sleep(1)

    async def _listen_auto_audio(self):
        """监听长按录音 0x0505: 语音指令"""
        import ring_sound as sdk

        while self._running:
            try:
                raw = await sdk.receive_auto_audio_file(self._client)
                if raw is None:
                    await asyncio.sleep(0.5)
                    continue

                bundle = sdk.save_audio_bundle(file_index=0, data=raw, output_dir="audio")
                logger.info(f"录音已保存: {bundle.play_path}")

                # 发布语音事件，等待 STT 处理
                await event_bus.publish(FarmEvent(
                    type="voice_recorded",
                    data={"wav_path": bundle.play_path}
                ))
            except Exception as e:
                if self._running:
                    logger.error(f"录音监听异常: {e}")
                await asyncio.sleep(1)


# 便捷启动
async def start_ring_bridge():
    bridge = RingBridge()
    await bridge.start()
