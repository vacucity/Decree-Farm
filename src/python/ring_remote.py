"""分身农庄 - 戒指远程桥接客户端（Machine B 上运行）

职责：
  1. 通过 BLE 连接本地戒指（复用 ring_sound.py SDK）
  2. 通过 WebSocket 连接 Machine A 的 LAN 中继（ws://A_IP:8766）
  3. 戒指双击 → 远程推送 ring_event 给 Machine A
  4. 戒指录音 → WAV → base64 → 远程推送 ring_event 给 Machine A
  5. 接收 Machine A 广播的 state_push 并在终端打印（调试用）

用法：
  python ring_remote.py --server 192.168.1.100:8766
  python ring_remote.py --server 192.168.1.100:8766 --ring F1:C1:8A:35:40:FB
  python ring_remote.py --server 192.168.1.100:8766 --demo  # 无戒指模拟
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

# ring_sound SDK 放在同目录或 materials 下
sys.path.insert(0, str(Path(__file__).parent))

try:
    import websockets
except ImportError:
    print("[ERROR] 需要安装 websockets: pip install websockets")
    sys.exit(1)


class RingRemoteClient:
    """戒指远程桥接：BLE → WS → Machine A"""

    def __init__(self, server_url: str, ring_mac: str = "F1:C1:8A:35:40:FB",
                 demo: bool = False):
        self.server_url = server_url
        self.ring_mac = ring_mac
        self.demo = demo
        self._ws = None
        self._running = False

    async def start(self):
        """主循环：连 WS + 连戒指 + 并发监听"""
        self._running = True
        print(f"[RingRemote] 连接 Machine A: {self.server_url}")
        print(f"[RingRemote] 戒指 MAC: {self.ring_mac} (demo={self.demo})")

        while self._running:
            try:
                async with websockets.connect(self.server_url) as ws:
                    self._ws = ws
                    print("[RingRemote] WS 已连接 Machine A")
                    # 并发：监听戒指 + 接收 Machine A 广播
                    await asyncio.gather(
                        self._ring_loop(),
                        self._recv_loop(ws),
                    )
            except (ConnectionRefusedError, OSError) as e:
                print(f"[RingRemote] WS 连接失败: {e}，3s 后重试...")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"[RingRemote] 异常: {e}，3s 后重连...")
                await asyncio.sleep(3)

    async def _recv_loop(self, ws):
        """接收 Machine A 广播的 state_push（调试用）"""
        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    if data.get("type") == "state_push":
                        real = data.get("data", {}).get("real", {})
                        game = data.get("data", {}).get("game", {})
                        mode = real.get("mode", "?")
                        eff = real.get("efficiency", 0)
                        gold = game.get("gold", 0)
                        loc = game.get("location", "?")
                        print(f"  [STATE] mode={mode} eff={eff} gold={gold} loc={loc}")
                except Exception:
                    pass
        except Exception:
            pass

    async def _ring_loop(self):
        """戒指事件监听（或 demo 模拟）"""
        if self.demo:
            await self._demo_ring_loop()
        else:
            await self._real_ring_loop()

    async def _real_ring_loop(self):
        """真实戒指 BLE 连接 + 事件监听"""
        try:
            import ring_sound as sdk
        except ImportError:
            print("[ERROR] 找不到 ring_sound.py SDK，请确认路径")
            # 不退出，保持 WS 连接（前端仍可手动 start_focus）
            await asyncio.Future()
            return

        print(f"[RingRemote] 扫描戒指 {self.ring_mac}...")
        for attempt in range(5):
            try:
                devices = await sdk.scan_rings(mac=self.ring_mac)
                if not devices:
                    raise ConnectionError("未扫描到戒指")
                client = sdk.RingSoundClient(address=self.ring_mac)
                await client.__aenter__()
                print(f"[RingRemote] 戒指已连接")
                # 并发监听双击和录音
                await asyncio.gather(
                    self._listen_double_press(sdk, client),
                    self._listen_audio(sdk, client),
                )
                return
            except Exception as e:
                delay = 2 ** attempt
                print(f"[RingRemote] 戒指连接失败 ({attempt+1}/5): {e}，{delay}s 后重试")
                await asyncio.sleep(delay)
        print("[RingRemote] 戒指连接彻底失败，仅保持 WS（可用前端按钮控制）")
        await asyncio.Future()

    async def _listen_double_press(self, sdk, client):
        """监听按键双击"""
        while self._running:
            try:
                await sdk.wait_sensor_key_double_press_event(client)
                print("[RingRemote] >> 双击事件")
                await self._send_ring_event("double_tap", {})
            except Exception as e:
                if self._running:
                    print(f"[RingRemote] 双击监听异常: {e}")
                await asyncio.sleep(1)

    async def _listen_audio(self, sdk, client):
        """监听长按录音"""
        while self._running:
            try:
                raw = await sdk.receive_auto_audio_file(client)
                if raw is None:
                    await asyncio.sleep(0.5)
                    continue
                bundle = sdk.save_audio_bundle(file_index=0, data=raw, output_dir="audio")
                print(f"[RingRemote] >> 录音: {bundle.play_path}")
                # 读取 WAV 并 base64 编码发送
                wav_bytes = Path(bundle.play_path).read_bytes()
                wav_b64 = base64.b64encode(wav_bytes).decode("ascii")
                await self._send_ring_event("voice_recorded", {
                    "wav_base64": wav_b64,
                    "duration_sec": bundle.play_size / (16000 * 2),  # 估算时长
                })
            except Exception as e:
                if self._running:
                    print(f"[RingRemote] 录音监听异常: {e}")
                await asyncio.sleep(1)

    async def _demo_ring_loop(self):
        """Demo 模式：定时模拟双击事件"""
        print("[RingRemote] DEMO 模式：按 Enter 模拟双击，输入 'q' 退出")
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                line = line.strip()
                if line.lower() in ("q", "quit", "exit"):
                    self._running = False
                    break
                # 任意输入（包括空行）= 模拟双击
                print("[RingRemote] >> 模拟双击事件")
                await self._send_ring_event("double_tap", {})
            except Exception:
                await asyncio.sleep(1)

    async def _send_ring_event(self, event: str, data: dict):
        """通过 WS 发送戒指事件给 Machine A"""
        if self._ws:
            msg = json.dumps({
                "type": "ring_event",
                "event": event,
                "data": data,
                "ts": time.time(),
            }, ensure_ascii=False)
            try:
                await self._ws.send(msg)
            except Exception as e:
                print(f"[RingRemote] 发送失败: {e}")

    async def send_user_command(self, command: str, data: dict = None):
        """发送用户指令给 Machine A（供外部调用或前端按钮集成）"""
        if self._ws:
            msg = json.dumps({
                "type": "user_command",
                "command": command,
                "data": data or {},
                "ts": time.time(),
            }, ensure_ascii=False)
            try:
                await self._ws.send(msg)
            except Exception as e:
                print(f"[RingRemote] 发送指令失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="分身农庄 - 戒指远程桥接客户端")
    parser.add_argument("--server", required=True,
                        help="Machine A 的 LAN 中继地址（如 192.168.1.100:8766）")
    parser.add_argument("--ring", default="F1:C1:8A:35:40:FB",
                        help="戒指 MAC 地址")
    parser.add_argument("--demo", action="store_true",
                        help="Demo 模式（无需真戒指，按 Enter 模拟双击）")
    args = parser.parse_args()

    # 补全 ws:// 协议头
    url = args.server
    if not url.startswith("ws://") and not url.startswith("wss://"):
        url = f"ws://{url}"

    client = RingRemoteClient(server_url=url, ring_mac=args.ring, demo=args.demo)
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        print("\n[RingRemote] 已停止")


if __name__ == "__main__":
    main()
