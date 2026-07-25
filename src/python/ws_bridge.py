"""分身农庄 - WebSocket 低延迟双向通道（P4）+ 局域网中继（P5 跨机对接）

架构（与 stardew-mcp 相反、更稳健的方向）：
- Python/brain 作为 WebSocket **服务端**（ws://localhost:8765）
- 游戏内 MOD 作为**客户端**主动连入（WsClient.cs，3 秒断线重连）

局域网中继（LanRelay）：
- 绑定 0.0.0.0:8766，接受 Machine B（戒指+前端）连接
- 接收 ring_event / user_command → 转发到本地 event_bus
- 定时广播 state_push → 前端 HUD 渲染
- HTTP GET / → 返回 hud.html（Machine B 浏览器直接访问即得 HUD）

优势：
- 下行命令即时到达（绕过 actions/*.json 的 0.5s 文件轮询）
- 上行 state 每 0.5s 推送（与 bridge_data.json 同构），无需读盘
- brain 重启只是断开重连，游戏不用动；游戏重启 MOD 自动重连
- 文件桥全程保留：WS 断开时 HybridBridge 自动回退，行为与之前完全一致
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

try:
    import websockets
    _WS_AVAILABLE = True
except ImportError:  # 库缺失时整体回退文件桥
    websockets = None
    _WS_AVAILABLE = False


class WsStateServer:
    """WebSocket 服务端：接收 MOD 推来的 state，向 MOD 下发 command。

    在后台守护线程里跑独立 asyncio 事件循环，主线程（Brain 循环）只通过
    线程安全的属性/方法交互。
    """

    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.clients: set = set()
        self.latest_state: Optional[Dict[str, Any]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False

    @property
    def connected(self) -> bool:
        return bool(self.clients)

    def start(self) -> bool:
        """启动后台服务端线程。返回是否可用（库缺失返回 False）。"""
        if not _WS_AVAILABLE:
            logger.warning("websockets 库缺失（pip install websockets）— 仅使用文件桥")
            return False
        if self._started:
            return True
        self._started = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ws-state-server")
        self._thread.start()
        return True

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            logger.warning(f"WS 服务端异常退出: {e}")

    async def _serve(self) -> None:
        async with websockets.serve(self._handler, self.host, self.port):
            logger.info(f"WS 服务端已监听 ws://{self.host}:{self.port}，等待 MOD 连入")
            await asyncio.Future()  # run forever

    async def _handler(self, ws, path=None) -> None:
        # path 形参兼容 websockets 新旧两版 handler 签名
        self.clients.add(ws)
        logger.success(f"MOD 已连入 WebSocket（{getattr(ws, 'remote_address', '?')}）— 低延迟通道在线")
        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("type") == "state":
                    self.latest_state = data.get("data")
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            logger.warning("MOD WebSocket 断开 — 自动回退文件桥")

    def send_command(self, action: Dict[str, Any]) -> bool:
        """向已连 MOD 即时下发动作 JSON（与 actions/*.json 同构）。无连接返回 False。"""
        if not self.clients or not self._loop:
            return False
        msg = json.dumps(action, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)
        return True

    async def _broadcast(self, msg: str) -> None:
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                self.clients.discard(ws)


class HybridBridge:
    """文件桥 + WebSocket 的复合桥。

    - send：WS 在线走 WS（低延迟），否则写 actions/*.json 文件
    - read_state：WS 在线用最新推送缓存，否则读 bridge_data.json
    - 接口与 agent.ActionBridge 完全一致，对 Brain 透明
    """

    def __init__(self, file_bridge, ws_server: Optional[WsStateServer] = None):
        self.file = file_bridge
        self.ws = ws_server
        # Brain 日志里引用 bridge_path，保持兼容
        self.bridge_path = getattr(file_bridge, "bridge_path", None)

    def send(self, action: Dict[str, Any]) -> str:
        if self.ws and self.ws.connected and self.ws.send_command(action):
            return "ws"
        return self.file.send(action)

    def read_state(self) -> Dict[str, Any]:
        if self.ws and self.ws.connected and self.ws.latest_state:
            return self.ws.latest_state
        return self.file.read_state()

    def read_agent_player(self) -> Dict[str, Any]:
        state = self.read_state() or {}
        return state.get("agentPlayer") or {}


# ======================================================================
# 局域网中继（LanRelay）：跨机对接枢纽
# ======================================================================

class LanRelay:
    """局域网中继服务：接收远程戒指事件 + 用户指令，广播游戏状态给前端 HUD。

    独立于 WsStateServer（MOD 通道），运行在单独的端口和线程上。
    Machine B 的 ring_remote.py 和前端 HUD 连接到此服务。
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8766):
        self.host = host
        self.port = port
        self.clients: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        # 事件回调：由 main.py 注入，收到远程事件时调用
        self._on_ring_event = None
        self._on_user_command = None

    @property
    def connected(self) -> bool:
        return bool(self.clients)

    def set_handlers(self, on_ring_event=None, on_user_command=None):
        """注入事件处理器（由 main.py 绑定到 event_bus）。"""
        self._on_ring_event = on_ring_event
        self._on_user_command = on_user_command

    def start(self) -> bool:
        """启动后台 LAN 中继线程。返回是否可用。"""
        if not _WS_AVAILABLE:
            logger.warning("websockets 库缺失 — LAN 中继不可用")
            return False
        if self._started:
            return True
        self._started = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="lan-relay-server")
        self._thread.start()
        return True

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            logger.warning(f"LAN 中继异常退出: {e}")

    async def _serve(self) -> None:
        # ping_interval=None：关闭服务端心跳踢人。Machine B 的 BLE 采样/推理会阻塞其
        # 事件循环，默认 20s ping/20s pong 超时会把它每分钟踢下线一次（断开→重连→重放指令）。
        async with websockets.serve(
            self._handler, self.host, self.port,
            process_request=self._http_handler,
            ping_interval=None, ping_timeout=None,
        ):
            logger.info(f"LAN 中继已监听 ws://{self.host}:{self.port}，等待 Machine B 连入")
            logger.info(f"  HUD 页面: http://<本机IP>:{self.port}/")
            await asyncio.Future()  # run forever

    async def _http_handler(self, path, request_headers):
        """处理 HTTP GET 请求，返回 HUD 静态页面；非 WS 升级时触发。"""
        # websockets 库: 返回 (status, headers, body) 则拦截为 HTTP 响应
        # 返回 None 则继续 WebSocket 握手
        if path == "/" or path == "/hud.html":
            hud_path = Path(__file__).resolve().parent.parent / "dashboard" / "hud.html"
            if hud_path.exists():
                body = hud_path.read_bytes()
                return (200, [("Content-Type", "text/html; charset=utf-8")], body)
            return (404, [], b"hud.html not found")
        # 其他路径 → 继续 WS 升级
        return None

    async def _handler(self, ws, path=None) -> None:
        self.clients.add(ws)
        remote = getattr(ws, 'remote_address', '?')
        logger.success(f"Machine B 已连入 LAN 中继（{remote}）")
        try:
            async for raw_msg in ws:
                # ring_bridge 的 ws_client 可能批量发送（换行分隔）
                for line in raw_msg.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    self._dispatch(data)
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            logger.warning(f"Machine B 断开 LAN 中继（{remote}）")

    def _dispatch(self, data: Dict[str, Any]) -> None:
        """统一消息路由：兼容 ring_bridge 原生格式 + 旧 ring_remote 格式。"""
        msg_type = data.get("type", "")

        # ── ring_bridge 原生：focus_state（分类器输出）──
        if msg_type == "focus_state":
            # {"type":"focus_state","state":"focused"/"distracted","confidence":0.85,...}
            raw_state = data.get("state", "")
            # 映射: focused→focus, distracted→distracted
            state = "focus" if raw_state == "focused" else raw_state
            confidence = data.get("confidence", 0.0)
            self._handle_ring_event({
                "type": "ring_event",
                "event": "ring_state",
                "data": {"state": state, "confidence": confidence,
                         "source": "ring_bridge_classifier"},
            })

        # ── ring_bridge 原生：ring_event（按键/手势）──
        elif msg_type == "ring_event":
            event_name = data.get("event", "")
            # ring_bridge 格式: {"type":"ring_event","event":"double_click","ts":...}
            if event_name == "double_click":
                self._handle_ring_event({
                    "type": "ring_event", "event": "double_tap", "data": {}})
            elif event_name == "single_click":
                # 单击在专注模式 = mark_distraction，直接当 ring_state:distracted
                self._handle_ring_event({
                    "type": "ring_event", "event": "ring_state",
                    "data": {"state": "distracted", "confidence": 1.0,
                             "source": "button_mark"},
                })
            elif event_name == "ring_state":
                # 旧 ring_remote.py 格式，直接透传
                self._handle_ring_event(data)
            else:
                # wave / rotate 等手势，记录但暂不映射
                logger.debug(f"LAN 收到手势: {event_name}")

        # ── ring_bridge 原生：focus_session（专注会话状态变化）──
        elif msg_type == "focus_session":
            active = data.get("active", False)
            if active:
                cmd_data = {}
                if "activity" in data:
                    cmd_data["activity"] = data["activity"]
                self._handle_user_command({"command": "start_focus", "data": cmd_data})
            else:
                self._handle_user_command({"command": "end_focus", "data": {}})

        # ── ring_bridge 原生：system（电池/连接状态）──
        elif msg_type == "system":
            connected = data.get("connected", False)
            battery = data.get("battery")
            logger.debug(f"Ring 系统状态: connected={connected}, battery={battery}")

        # ── ring_bridge 原生：imu_aggregate（高频聚合帧，仅日志不处理）──
        elif msg_type == "imu_aggregate":
            pass  # 5Hz 高频帧，不需要转发到游戏 Agent

        # ── 旧格式兼容：user_command ──
        elif msg_type == "user_command":
            self._handle_user_command(data)

    def _handle_ring_event(self, data: Dict[str, Any]) -> None:
        """处理远程戒指事件：double_tap / voice_recorded"""
        event = data.get("event", "")
        event_data = data.get("data") or {}
        logger.info(f"LAN 收到戒指事件: {event}")
        if self._on_ring_event:
            try:
                self._on_ring_event(event, event_data)
            except Exception as e:
                logger.error(f"处理远程戒指事件异常: {e}")

    def _handle_user_command(self, data: Dict[str, Any]) -> None:
        """处理远程用户指令：start_focus / end_focus / schedule"""
        command = data.get("command", "")
        cmd_data = data.get("data") or {}
        logger.info(f"LAN 收到用户指令: {command}")
        if self._on_user_command:
            try:
                self._on_user_command(command, cmd_data)
            except Exception as e:
                logger.error(f"处理远程用户指令异常: {e}")

    def broadcast_state(self, state_data: Dict[str, Any]) -> None:
        """向所有已连的 LAN 客户端广播状态 JSON。"""
        if not self.clients or not self._loop:
            return
        msg = json.dumps({"type": "state_push", "data": state_data}, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    async def _broadcast(self, msg: str) -> None:
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                self.clients.discard(ws)
