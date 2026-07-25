"""Focus Agent - ring_bridge 主进程

启动方式:
  # 真实戒指模式
  python -m ring_bridge.server --mac DA:2A:F8:9B:FE:44

  # DEMO 回放模式（无需戒指）
  python -m ring_bridge.server --demo

  # 指定 WS 目标
  python -m ring_bridge.server --mac DA:2A:F8:9B:FE:44 --ws ws://localhost:8000/ws/bridge
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .aggregator import DataAggregator
from .classifier.realtime import RealtimeFocusClassifier
from .config import DEFAULT_MAC
from .demo_source import DemoSource
from .engines.focus_engine import FocusEngine
from .engines.sleep_engine import SleepEngine
from .event_bus import EventBus
from .labeled_capture import LabeledIMUCapture
from .ring_source import RingSource
from .web_server import WebServer
from .ws_client import WSClient

# ── 日志 ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ring_bridge")


# ══════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════


class RingBridge:
    """戒指与专注 Agent 边缘层主控"""

    def __init__(self, args: argparse.Namespace):
        self._args = args
        self._bus = EventBus()
        self._source: RingSource | DemoSource | None = None
        self._focus_engine = FocusEngine(self._bus)
        self._sleep_engine = SleepEngine(self._bus)
        self._aggregator = DataAggregator(
            self._bus,
            db_path=args.db,
        )
        self._capture = LabeledIMUCapture(self._bus, db_path=args.db)
        self._classifier = RealtimeFocusClassifier(self._bus, args.model)
        self._ws: WSClient | None = None
        self._web: WebServer | None = None
        self._source_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        args = self._args
        logger.info("=" * 50)
        logger.info("Focus Agent - ring_bridge starting")
        logger.info("  DEMO mode : %s", args.demo)
        logger.info("  Cloud mode: %s", args.cloud)
        logger.info("  WS target : %s", args.ws)
        logger.info("=" * 50)

        # 1. 创建数据源
        if args.demo:
            demo_file = args.demo_file or None
            logger.info("Using DemoSource (file=%s)", demo_file or "built-in")
            self._source = DemoSource(self._bus, file_path=demo_file)
        else:
            mac = args.mac or DEFAULT_MAC
            logger.info("Using RingSource (mac=%s)", mac)
            self._source = RingSource(
                self._bus,
                mac=mac,
                command_timeout_s=args.timeout,
            )

        # 2. 启动引擎（在 IMU 数据到达后才开始消费）
        await self._focus_engine.start()
        await self._sleep_engine.start()
        await self._classifier.start()

        # 3. 启动聚合器
        await self._aggregator.start()
        await self._capture.start()

        # 4. 先开放 Web 管理面板。即使戒指暂时不在附近，也能扫描和修改配置。
        self._web = WebServer(
            self._bus,
            self._aggregator,
            capture=self._capture,
            port=getattr(args, "port", 8520),
        )
        if self._source:
            self._web.set_ring_source(self._source)
        self._web.set_classifier(self._classifier)
        await self._web.start()

        # 5. 后台自动连接默认戒指；失败不会关闭网页端口，可在网页重试。
        if getattr(args, "no_auto_connect", False):
            logger.info("Automatic ring connection disabled; waiting for web panel")
        else:
            self._source_task = asyncio.create_task(self._start_source_safely())

        # 6. 启动 WS 客户端（如果指定了额外 URL）
        if args.ws:
            self._ws = WSClient(self._bus, args.ws)
            await self._ws.start()

        # 6. 订阅系统事件用于日志
        self._bus.subscribe("system:connected", self._log_connected)
        self._bus.subscribe("system:disconnected", self._log_disconnected)
        self._bus.subscribe("gesture:*", self._log_gesture)
        self._bus.subscribe("button:*", self._log_button)

        # 7. 等待关闭信号
        await self._shutdown_event.wait()

    async def _start_source_safely(self) -> None:
        if not self._source:
            return
        try:
            logger.info("Starting data source...")
            await self._source.start()
            logger.info("Data source started")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Initial data-source connection failed; web panel remains available"
            )

    async def stop(self) -> None:
        logger.info("Shutting down...")
        if self._source_task and not self._source_task.done():
            self._source_task.cancel()
            try:
                await self._source_task
            except asyncio.CancelledError:
                pass
        self._source_task = None
        if self._web:
            await self._web.stop()
        if self._ws:
            await self._ws.stop()
        await self._capture.stop()
        await self._aggregator.stop()
        await self._classifier.stop()
        await self._focus_engine.stop()
        await self._sleep_engine.stop()
        if self._source:
            await self._source.stop()
        logger.info("Shutdown complete")

    # ── 事件日志 ──

    async def _log_connected(self, _data) -> None:
        logger.info("✓ Ring connected")

    async def _log_disconnected(self, _data) -> None:
        logger.warning("✗ Ring disconnected")

    async def _log_gesture(self, event) -> None:
        from .models import GestureEvent
        if isinstance(event, GestureEvent):
            logger.info("👋 Gesture: %s", event.gesture_name)

    async def _log_button(self, event) -> None:
        from .models import ButtonEvent
        if isinstance(event, ButtonEvent):
            logger.info("🔘 Button: %s", event.event_type)


# ══════════════════════════════════════════════════
#  CLI 入口
# ══════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Focus Agent - 戒指边缘层 bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m ring_bridge.server --mac DA:2A:F8:9B:FE:44
  python -m ring_bridge.server --demo
  python -m ring_bridge.server --mac DA:2A:F8:9B:FE:44 --ws ws://localhost:8000/ws/bridge
        """,
    )
    p.add_argument("--mac", default=DEFAULT_MAC, help="戒指 MAC 地址")
    p.add_argument("--demo", action="store_true", help="DEMO 回放模式（无需戒指）")
    p.add_argument("--demo-file", default=None, help="DEMO 模式的 JSONL 数据文件路径")
    p.add_argument("--cloud", action="store_true", help="云模式（连接 Zeabur hub）")
    p.add_argument("--ws", default="", help="WebSocket 目标 URL（如 ws://localhost:8000/ws/bridge）")
    p.add_argument("--db", default="data.db", help="本地 SQLite 数据库路径")
    p.add_argument(
        "--model",
        default="models/focus_classifier.joblib",
        help="实时专注分类模型文件",
    )
    p.add_argument("--timeout", type=float, default=10.0, help="BLE 命令超时秒数")
    p.add_argument("--port", type=int, default=8520, help="Web 管理面板端口 (默认 8520)")
    p.add_argument(
        "--no-auto-connect",
        action="store_true",
        help="只启动管理面板，等待用户手动连接戒指",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="显示 DEBUG 日志")
    return p.parse_args()


async def main_async() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    bridge = RingBridge(args)

    # 优雅关闭
    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("Received shutdown signal")
        bridge._shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    try:
        await bridge.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Fatal error")
    finally:
        await bridge.stop()


def main() -> None:
    """CLI 入口"""
    # Windows 下用 asyncio.run() 的信号处理更好
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
