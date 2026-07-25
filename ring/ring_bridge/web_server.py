"""Web 管理面板服务器 — REST API + WebSocket + 静态文件

内置 HTTP 服务器，手机和电脑浏览器均可访问。
API 端点：
  GET  /api/status       — 系统状态
  POST /api/connect      — 连接戒指
  POST /api/disconnect   — 断开连接
  GET  /api/audio        — 录音列表
  POST /api/audio/download — 下载录音
  DELETE /api/audio       — 清空录音
  POST /api/imu/start    — 开启 IMU
  POST /api/imu/stop     — 停止 IMU
  GET  /api/gestures     — 手势事件列表
  WS   /ws               — 实时数据流
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict
from pathlib import Path

import aiohttp
from aiohttp import web

from .aggregator import DataAggregator
from .config import (
    gesture_mapping_payload,
    reset_gesture_map,
    update_gesture_map,
)
from .event_bus import EventBus
from .labeled_capture import LabeledIMUCapture
from .models import (
    AggregatedFrame,
    ButtonEvent,
    IMUSample,
    RingEventFrame,
    SystemFrame,
)

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"


class WebServer:
    """ring_bridge Web 管理面板"""

    def __init__(
        self,
        bus: EventBus,
        aggregator: DataAggregator,
        host: str = "0.0.0.0",
        port: int = 8520,
        capture: LabeledIMUCapture | None = None,
    ):
        self._bus = bus
        self._aggregator = aggregator
        self._host = host
        self._port = port
        self._capture = capture
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()

        # 用于外部注入的 ring source（由 server.py 设置）
        self._ring_source = None
        self._ring_client = None
        self._classifier = None

        # 手势和事件缓存
        self._gesture_events: list[dict] = []
        self._ring_events: list[dict] = []
        self._log_messages: list[dict] = []
        self._last_raw_imu_push_ms = 0
        self._showcase_enabled = False
        self._showcase_active = False
        self._showcase_task: asyncio.Task | None = None
        self._showcase_outage_started_at: float | None = None
        self._showcase_sample_count = 0
        self._showcase_sequence = 0
        self._hardware_imu_ever_seen = False
        self._focus_agent_task: asyncio.Task | None = None
        self._button_sequence_task: asyncio.Task | None = None
        self._button_press_units = 0
        self._focus_session_active = False
        self._focus_session_started_at: float | None = None
        self._focus_session_id: str | None = None
        self._customer_state = "focused"
        self._customer_state_source = "manual_untrained"
        self._agent_balance = 0.0
        self._agent_harvest = 0
        self._agent_penalty = 0
        self._agent_growth = 0.0
        self._agent_tick_count = 0
        self._last_session_trigger = "尚未开始"
        self._last_button_pattern = "等待三击或四击"

        self._setup_routes()

    # ── 公开接口 ──

    def set_ring_source(self, source) -> None:
        self._ring_source = source

    def set_ring_client(self, client) -> None:
        self._ring_client = client

    def set_classifier(self, classifier) -> None:
        self._classifier = classifier
        if classifier and classifier.ready:
            self._customer_state_source = "model_waiting_data"

    def _get_ring_client(self):
        source_client = getattr(self._ring_source, "client", None)
        return source_client or self._ring_client

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()

        # 订阅实时事件
        self._bus.subscribe("aggregate:frame", self._on_frame)
        self._bus.subscribe("aggregate:event", self._on_ring_event)
        self._bus.subscribe("system:status", self._on_system)
        self._bus.subscribe("gesture:*", self._on_gesture_ws)
        self._bus.subscribe("button:*", self._on_button_sequence)
        self._bus.subscribe("imu:sample", self._on_raw_imu)
        self._bus.subscribe("classifier:prediction", self._on_classifier_prediction)
        self._showcase_task = asyncio.create_task(self._showcase_fallback_loop())
        self._focus_agent_task = asyncio.create_task(self._focus_agent_loop())

        logger.info("Web panel: http://%s:%s", self._host, self._port)
        local_ip = self._get_local_ip()
        if local_ip:
            logger.info("  Local : http://localhost:%s", self._port)
            logger.info("  Mobile: http://%s:%s", local_ip, self._port)

    async def stop(self) -> None:
        self._bus.unsubscribe("aggregate:frame", self._on_frame)
        self._bus.unsubscribe("aggregate:event", self._on_ring_event)
        self._bus.unsubscribe("system:status", self._on_system)
        self._bus.unsubscribe("gesture:*", self._on_gesture_ws)
        self._bus.unsubscribe("button:*", self._on_button_sequence)
        self._bus.unsubscribe("imu:sample", self._on_raw_imu)
        self._bus.unsubscribe("classifier:prediction", self._on_classifier_prediction)
        if self._button_sequence_task:
            self._button_sequence_task.cancel()
            self._button_sequence_task = None
        if self._focus_agent_task:
            self._focus_agent_task.cancel()
            try:
                await self._focus_agent_task
            except asyncio.CancelledError:
                pass
            self._focus_agent_task = None
        if self._showcase_task:
            self._showcase_task.cancel()
            try:
                await self._showcase_task
            except asyncio.CancelledError:
                pass
            self._showcase_task = None
        for ws in set(self._ws_clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    # ── 路由 ──

    def _setup_routes(self) -> None:
        app = self._app
        app.router.add_get("/", self._serve_index)
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/api/status", self._api_status)
        app.router.add_post("/api/connect", self._api_connect)
        app.router.add_post("/api/disconnect", self._api_disconnect)
        app.router.add_get("/api/showcase", self._api_showcase_get)
        app.router.add_post("/api/showcase", self._api_showcase_set)
        app.router.add_get("/api/focus-agent", self._api_focus_agent_get)
        app.router.add_post("/api/focus-agent", self._api_focus_agent_set)
        app.router.add_get("/api/audio", self._api_audio_list)
        app.router.add_post("/api/audio/download", self._api_audio_download)
        app.router.add_delete("/api/audio", self._api_audio_clear)
        app.router.add_post("/api/imu/start", self._api_imu_start)
        app.router.add_post("/api/imu/stop", self._api_imu_stop)
        app.router.add_get("/api/data-quality", self._api_data_quality)
        app.router.add_get("/api/calibration", self._api_calibration_get)
        app.router.add_post("/api/calibration", self._api_calibration_start)
        app.router.add_put("/api/calibration", self._api_calibration_select)
        app.router.add_delete("/api/calibration", self._api_calibration_cancel)
        app.router.add_get("/api/gestures", self._api_gestures)
        app.router.add_get("/api/scan", self._api_scan)
        app.router.add_get("/api/mappings", self._api_mappings_get)
        app.router.add_put("/api/mappings", self._api_mappings_put)
        app.router.add_delete("/api/mappings", self._api_mappings_reset)
        app.router.add_get("/api/capture", self._api_capture_status)
        app.router.add_post("/api/capture/start", self._api_capture_start)
        app.router.add_post("/api/capture/stop", self._api_capture_stop)
        app.router.add_get("/api/capture/export.csv", self._api_capture_export)
        app.router.add_delete(
            r"/api/capture/{session_id:\d+}", self._api_capture_delete
        )
        # 静态文件
        app.router.add_static("/static/", path=WEB_DIR, name="static")

    # ── API 实现 ──

    async def _serve_index(self, request: web.Request) -> web.Response:
        index_path = WEB_DIR / "index.html"
        if index_path.exists():
            return web.FileResponse(
                index_path,
                headers={"Cache-Control": "no-store, max-age=0"},
            )
        return web.Response(text="Ring Bridge Web Panel", content_type="text/html")

    async def _api_status(self, request: web.Request) -> web.Response:
        """返回当前戒指状态"""
        source = self._ring_source
        agg = self._aggregator
        hardware_imu_streaming = bool(
            source and getattr(source, "imu_streaming", False)
        )
        showcase = self._showcase_payload()
        return web.json_response({
            "connected": bool(source and source.is_running),
            "connecting": bool(source and getattr(source, "is_connecting", False)),
            "mac": getattr(source, "mac", "") if source else "",
            "mode": agg.mode.value if agg else "unknown",
            "battery": (
                agg._battery.battery_percent
                if agg and agg._battery_known
                else None
            ),
            "charging": (
                agg._battery.battery_charging
                if agg and agg._battery_known
                else None
            ),
            "battery_known": bool(agg and agg._battery_known),
            "device_mode": (
                getattr(source, "device_mode", "unknown") if source else "unknown"
            ),
            "imu_streaming": hardware_imu_streaming or self._showcase_active,
            "hardware_imu_streaming": hardware_imu_streaming,
            "data_source": (
                "ring" if hardware_imu_streaming
                else "showcase_fallback" if self._showcase_active
                else "none"
            ),
            "imu_sample_count": (
                getattr(source, "imu_sample_count", 0) if source else 0
            ),
            "imu_state": (
                "showcase_fallback"
                if self._showcase_active
                else getattr(source, "imu_state", "disconnected")
                if source
                else "disconnected"
            ),
            "imu_recovery_count": (
                getattr(source, "imu_recovery_count", 0) if source else 0
            ),
            "imu_last_error": (
                getattr(source, "imu_last_error", None) if source else None
            ),
            "imu_last_sample_age_s": (
                getattr(source, "imu_last_sample_age_s", None) if source else None
            ),
            "protocol_diagnostics": (
                getattr(source, "protocol_diagnostics", {}) if source else {}
            ),
            "data_quality": (
                getattr(source, "data_quality", {}) if source else {}
            ),
            # Customer-facing state is deliberately binary. The legacy
            # heuristic engine remains internal until labelled training is done.
            "focus_state": self._customer_state,
            "classifier_status": self._customer_state_source,
            "classifier": (
                self._classifier.status if self._classifier else {"ready": False}
            ),
            "calibration": (
                self._classifier.calibration_status
                if self._classifier else {"status": "unavailable"}
            ),
            "growth_progress": agg._last_focus.growth_progress if agg and agg._last_focus else 0.0,
            "motion_intensity": agg._last_sleep.motion_intensity if agg and agg._last_sleep else 0.0,
            "focus_distractions": agg._current_session_distractions if agg else 0,
            "gesture_count": len(self._gesture_events),
            "log_count": len(self._log_messages),
            "showcase": showcase,
            "focus_agent": self._focus_agent_payload(),
        })

    def _focus_agent_payload(self) -> dict:
        elapsed_s = 0
        if self._focus_session_active and self._focus_session_started_at:
            elapsed_s = max(0, int(time.monotonic() - self._focus_session_started_at))
        if not self._focus_session_active:
            action = "等待专注会话"
        elif self._customer_state == "focused":
            action = "自主经营：播种、浇水与收获"
        else:
            action = "经营受罚：减产并暂停扩建"
        return {
            "session_active": self._focus_session_active,
            "session_id": self._focus_session_id,
            "session_elapsed_s": elapsed_s,
            "customer_state": self._customer_state,
            "state_source": self._customer_state_source,
            "model_ready": bool(self._classifier and self._classifier.ready),
            "confidence": (
                (self._classifier.latest or {}).get("confidence")
                if self._classifier else None
            ),
            "focused_probability": (
                (self._classifier.latest or {}).get("focused_probability")
                if self._classifier else None
            ),
            "model_version": (
                self._classifier.status.get("model_version")
                if self._classifier else None
            ),
            "agent_action": action,
            "balance": round(self._agent_balance, 1),
            "harvest": self._agent_harvest,
            "penalty": self._agent_penalty,
            "growth": round(self._agent_growth, 1),
            "last_session_trigger": self._last_session_trigger,
            "last_button_pattern": self._last_button_pattern,
            "button_rule": {
                "start": 3,
                "end": 4,
                "window_s": 1.25,
            },
        }

    async def _api_focus_agent_get(self, request: web.Request) -> web.Response:
        return web.json_response(self._focus_agent_payload())

    async def _api_focus_agent_set(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action", "")).strip().lower()
        if action == "start":
            await self._set_focus_session(True, "面板手动开始")
        elif action == "end":
            await self._set_focus_session(False, "面板手动结束")
        elif action == "set_state":
            state = str(body.get("state", "")).strip().lower()
            if state not in {"focused", "distracted"}:
                return web.json_response(
                    {"ok": False, "error": "state must be focused or distracted"},
                    status=400,
                )
            self._customer_state = state
            self._customer_state_source = "manual_untrained"
        elif action == "reset_economy":
            self._agent_balance = 0.0
            self._agent_harvest = 0
            self._agent_penalty = 0
            self._agent_growth = 0.0
        else:
            return web.json_response(
                {"ok": False, "error": "unsupported action"},
                status=400,
            )
        payload = self._focus_agent_payload()
        await self._broadcast({"type": "focus_agent", **payload})
        return web.json_response({"ok": True, **payload})

    async def _set_focus_session(self, active: bool, trigger: str) -> None:
        changed = False
        if active and not self._focus_session_active:
            self._focus_session_active = True
            self._focus_session_started_at = time.monotonic()
            self._focus_session_id = f"focus-{int(time.time() * 1000)}"
            changed = True
        elif not active and self._focus_session_active:
            self._focus_session_active = False
            self._focus_session_started_at = None
            changed = True
        self._last_session_trigger = trigger

        session_event = {
            "schema_version": "1.0",
            "active": self._focus_session_active,
            "session_id": self._focus_session_id,
            "trigger": trigger,
            "timestamp_ms": int(time.time() * 1000),
            "changed": changed,
        }
        await self._bus.publish("focus:session", session_event)
        await self._broadcast({"type": "focus_session", **session_event})

        source = self._ring_source
        if changed and active and source and getattr(source, "is_running", False):
            if (
                hasattr(source, "retry_imu_report")
                and not getattr(source, "imu_streaming", False)
            ):
                try:
                    await source.retry_imu_report()
                except Exception as exc:
                    logger.warning("Focus session started but IMU start failed: %s", exc)
                    self._last_session_trigger = f"{trigger}（IMU启动失败）"
        elif changed and not active and source and hasattr(source, "pause_imu_report"):
            capture_active = False
            if self._capture:
                capture_active = bool((await self._capture.status()).get("active"))
            calibration_active = bool(
                self._classifier
                and self._classifier.calibration_status.get("active")
            )
            if not capture_active and not calibration_active:
                try:
                    await source.pause_imu_report()
                except Exception as exc:
                    logger.warning("Focus session ended but IMU pause failed: %s", exc)
        payload = self._focus_agent_payload()
        await self._broadcast({"type": "focus_agent", **payload})
        logger.info("Focus agent session active=%s trigger=%s", active, trigger)

    def _showcase_payload(self) -> dict:
        source = self._ring_source
        connected = bool(source and getattr(source, "is_running", False))
        hardware_streaming = bool(
            source and getattr(source, "imu_streaming", False)
        )
        diagnostics = (
            getattr(source, "protocol_diagnostics", {}) if source else {}
        ) or {}
        last_rx_age = diagnostics.get("last_rx_age_s")
        protocol_errors = int(diagnostics.get("protocol_error_count", 0) or 0)
        device_mode = getattr(source, "device_mode", "unknown") if source else "unknown"
        device_mode_label = {
            "gesture": "当前：手势模式",
            "recording": "当前：录音模式",
            "switching": "当前：正在切换",
            "unknown": "当前：未知",
        }.get(device_mode, f"当前：{device_mode}")
        checks = [
            {
                "id": "ble",
                "label": "蓝牙控制通道",
                "ok": connected,
                "detail": "已连接" if connected else "未连接",
            },
            {
                "id": "control",
                "label": "控制包持续返回",
                "ok": bool(
                    connected
                    and last_rx_age is not None
                    and float(last_rx_age) <= 10.0
                ),
                "detail": (
                    f"最近返回 {last_rx_age} 秒前"
                    if last_rx_age is not None
                    else "尚未收到控制包"
                ),
            },
            {
                "id": "mode",
                "label": "戒指模式",
                "ok": device_mode == "gesture",
                "detail": device_mode_label,
            },
            {
                "id": "imu",
                "label": "真实六轴数据",
                "ok": hardware_streaming,
                "detail": "实时到达" if hardware_streaming else "未收到 0x0605",
            },
            {
                "id": "parser",
                "label": "协议解析",
                "ok": protocol_errors == 0,
                "detail": f"{protocol_errors} 个错误",
            },
        ]
        hardware_ready = all(item["ok"] for item in checks)
        if hardware_ready:
            grade = "hardware_ready"
            summary = "真实戒指链路已通过路演预检"
        elif self._showcase_enabled and not self._hardware_imu_ever_seen:
            grade = "waiting_hardware"
            summary = "保障已待命；首次真实 0x0605 通过前不会生成备用数据"
        elif self._showcase_enabled:
            grade = "protected"
            summary = (
                "保障模式正在接管数据"
                if self._showcase_active
                else "保障模式已待命，等待真实链路"
            )
        else:
            grade = "not_ready"
            summary = "尚未达到路演就绪条件"
        return {
            "enabled": self._showcase_enabled,
            "active": self._showcase_active,
            "grade": grade,
            "summary": summary,
            "hardware_ready": hardware_ready,
            "hardware_ever_seen": self._hardware_imu_ever_seen,
            "sample_count": self._showcase_sample_count,
            "checks": checks,
        }

    async def _api_showcase_get(self, request: web.Request) -> web.Response:
        return web.json_response(self._showcase_payload())

    async def _api_showcase_set(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        self._showcase_enabled = bool(body.get("enabled", False))
        self._showcase_outage_started_at = None
        if not self._showcase_enabled:
            self._showcase_active = False
        logger.info("Showcase fallback enabled=%s", self._showcase_enabled)
        return web.json_response({"ok": True, **self._showcase_payload()})

    async def _api_connect(self, request: web.Request) -> web.Response:
        """连接戒指"""
        try:
            body = await request.json()
            mac = str(body.get("mac", "")).strip().upper()
        except Exception:
            mac = ""
        source = self._ring_source
        if source is None or not hasattr(source, "mac"):
            return web.json_response({"ok": False, "error": "当前数据源不支持 BLE 连接"}, status=400)
        if not mac:
            mac = getattr(source, "mac", "")
        try:
            if source.is_running and getattr(source, "mac", "") != mac:
                await source.stop()
            await source.start(mac=mac)
            return web.json_response({
                "ok": True,
                "connected": source.is_running,
                "mac": getattr(source, "mac", mac),
                "message": "戒指蓝牙已连接",
            })
        except Exception as exc:
            logger.exception("BLE connect failed")
            return web.json_response(
                {"ok": False, "connected": False, "error": str(exc), "mac": mac},
                status=502,
            )

    async def _api_disconnect(self, request: web.Request) -> web.Response:
        source = self._ring_source
        if source is None or not hasattr(source, "stop"):
            return web.json_response({"ok": False, "error": "当前数据源不支持断开"}, status=400)
        try:
            await source.stop()
            return web.json_response({"ok": True, "connected": False, "message": "已断开戒指"})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _api_scan(self, request: web.Request) -> web.Response:
        """扫描附近蓝牙设备"""
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import ring_sound as sdk
            devices = await sdk.scan_rings(timeout_s=5.0)
            result = [
                {"address": d.address, "name": d.name or "", "rssi": d.rssi}
                for d in devices[:30]
            ]
            return web.json_response({"devices": result})
        except Exception as e:
            return web.json_response({"devices": [], "error": str(e)})

    async def _api_audio_list(self, request: web.Request) -> web.Response:
        """录音列表"""
        client = self._get_ring_client()
        if not client or not client.is_connected:
            return web.json_response({"files": [], "error": "Not connected"})
        try:
            import ring_sound as sdk
            count = await sdk.get_audio_file_count(client)
            return web.json_response({"files": [{"index": i} for i in range(count)], "count": count})
        except Exception as e:
            return web.json_response({"files": [], "error": str(e)})

    async def _api_audio_download(self, request: web.Request) -> web.Response:
        """下载指定录音的原始数据"""
        try:
            body = await request.json()
            file_index = body.get("file_index", 0)
            download_mode = str(body.get("mode", "auto")).strip().lower()
        except Exception:
            file_index = 0
            download_mode = "auto"

        client = self._get_ring_client()
        if not client or not client.is_connected:
            return web.json_response({"error": "Not connected"}, status=400)

        try:
            import ring_sound as sdk
            used_mode = download_mode
            fallback_used = False
            quick_error = ""

            if download_mode == "normal":
                info, raw_data = await sdk.download_audio_file(
                    client,
                    file_index=file_index,
                    quick=False,
                    timeout_s=12.0,
                )
            elif download_mode == "quick":
                info, raw_data = await sdk.download_audio_file(
                    client,
                    file_index=file_index,
                    quick=True,
                    timeout_s=12.0,
                )
            else:
                used_mode = "quick"
                try:
                    info, raw_data = await sdk.download_audio_file(
                        client,
                        file_index=file_index,
                        quick=True,
                        timeout_s=12.0,
                    )
                except Exception as exc:
                    quick_error = str(exc)
                    fallback_used = True
                    used_mode = "normal"
                    logger.warning(
                        "Quick audio download failed for file %s; falling back to normal: %s",
                        file_index,
                        quick_error,
                    )
                    try:
                        info, raw_data = await sdk.download_audio_file(
                            client,
                            file_index=file_index,
                            quick=False,
                            timeout_s=12.0,
                        )
                    except Exception as normal_exc:
                        return web.json_response(
                            {
                                "error": "快速和普通下载链路均失败",
                                "quick_error": quick_error,
                                "normal_error": str(normal_exc),
                                "file_index": file_index,
                                "connected": bool(client.is_connected),
                            },
                            status=500,
                        )
            import base64
            return web.json_response({
                "file_index": info.file_index,
                "record_time": info.record_time,
                "data_size": info.data_size,
                "data_b64": base64.b64encode(raw_data).decode("ascii"),
                "method": used_mode,
                "fallback_used": fallback_used,
                "quick_error": quick_error,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_audio_clear(self, request: web.Request) -> web.Response:
        client = self._get_ring_client()
        if not client or not client.is_connected:
            return web.json_response({"error": "Not connected"}, status=400)
        try:
            import ring_sound as sdk
            await sdk.clear_audio_files(client)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_imu_start(self, request: web.Request) -> web.Response:
        source = self._ring_source
        if source is None or not hasattr(source, "retry_imu_report"):
            return web.json_response(
                {"ok": False, "error": "当前数据源不支持重新请求 IMU"},
                status=400,
            )
        try:
            await source.retry_imu_report()
            return web.json_response({
                "ok": True,
                "message": "已在当前蓝牙连接中重新发送真实 IMU 请求",
            })
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": str(exc)},
                status=500,
            )

    async def _api_imu_stop(self, request: web.Request) -> web.Response:
        source = self._ring_source
        if source is None or not hasattr(source, "pause_imu_report"):
            return web.json_response(
                {"ok": False, "error": "当前数据源不支持停止 IMU"},
                status=400,
            )
        try:
            await source.pause_imu_report()
            return web.json_response({
                "ok": True,
                "message": "真实 IMU 已停止，蓝牙控制连接保持",
            })
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": str(exc)},
                status=500,
            )

    async def _api_data_quality(self, request: web.Request) -> web.Response:
        source = self._ring_source
        quality = getattr(source, "data_quality", {}) if source else {}
        return web.json_response({"ok": True, "data_quality": quality})

    async def _api_calibration_get(self, request: web.Request) -> web.Response:
        if not self._classifier:
            return web.json_response(
                {"ok": False, "error": "classifier unavailable"},
                status=503,
            )
        return web.json_response({
            "ok": True,
            "calibration": self._classifier.calibration_status,
        })

    async def _api_calibration_start(self, request: web.Request) -> web.Response:
        if not self._classifier or not self._classifier.ready:
            return web.json_response(
                {"ok": False, "error": "classifier model is not ready"},
                status=503,
            )
        source = self._ring_source
        if not source or not getattr(source, "is_running", False):
            return web.json_response(
                {"ok": False, "error": "ring is not connected"},
                status=409,
            )
        if self._capture:
            capture_status = await self._capture.status()
            if capture_status.get("active"):
                return web.json_response(
                    {"ok": False, "error": "stop labelled capture before calibration"},
                    status=409,
                )
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            if (
                hasattr(source, "retry_imu_report")
                and not getattr(source, "imu_streaming", False)
            ):
                await source.retry_imu_report()
            calibration = self._classifier.start_calibration(
                str(body.get("user_id", ""))
            )
            return web.json_response({"ok": True, "calibration": calibration})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)

    async def _api_calibration_select(self, request: web.Request) -> web.Response:
        if not self._classifier:
            return web.json_response(
                {"ok": False, "error": "classifier unavailable"},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            calibration = self._classifier.select_calibration(body.get("user_id"))
            return web.json_response({"ok": True, "calibration": calibration})
        except LookupError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)

    async def _api_calibration_cancel(self, request: web.Request) -> web.Response:
        if not self._classifier:
            return web.json_response(
                {"ok": False, "error": "classifier unavailable"},
                status=503,
            )
        calibration = self._classifier.cancel_calibration("cancelled by user")
        return web.json_response({"ok": True, "calibration": calibration})

    async def _api_gestures(self, request: web.Request) -> web.Response:
        return web.json_response({"gestures": self._gesture_events[-50:]})

    async def _api_mappings_get(self, request: web.Request) -> web.Response:
        return web.json_response(gesture_mapping_payload())

    async def _api_mappings_put(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            mappings = body.get("mappings", body)
            saved = update_gesture_map(mappings)
            payload = gesture_mapping_payload()
            payload.update({"ok": True, "mappings": saved})
            await self._broadcast({"type": "mappings", "mappings": saved})
            return web.json_response(payload)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return web.json_response({"ok": False, "error": f"保存失败: {exc}"}, status=500)

    async def _api_mappings_reset(self, request: web.Request) -> web.Response:
        try:
            mappings = reset_gesture_map()
            await self._broadcast({"type": "mappings", "mappings": mappings})
            return web.json_response({"ok": True, "mappings": mappings})
        except OSError as exc:
            return web.json_response({"ok": False, "error": f"保存失败: {exc}"}, status=500)

    async def _api_capture_status(self, request: web.Request) -> web.Response:
        if not self._capture:
            return web.json_response({"error": "capture service unavailable"}, status=503)
        return web.json_response(await self._capture.status())

    async def _api_capture_start(self, request: web.Request) -> web.Response:
        if not self._capture:
            return web.json_response({"error": "capture service unavailable"}, status=503)
        if self._showcase_active:
            return web.json_response(
                {
                    "ok": False,
                    "error": "路演保障数据正在接管，已阻止写入标注训练集",
                },
                status=409,
            )
        try:
            body = await request.json()
            source = self._ring_source
            if (
                source is not None
                and hasattr(source, "retry_imu_report")
                and getattr(source, "is_running", False)
                and not getattr(source, "imu_streaming", False)
            ):
                # Entering the capture page is read-only. Start the high-rate
                # ring stream only when the user actually starts a label.
                await source.retry_imu_report()
            session = await self._capture.start_capture(
                label=str(body.get("label", "")),
                user_id=str(body.get("user_id", "")),
                task_type=str(body.get("task_type", "")),
                hand=str(body.get("hand", "")),
                orientation=str(body.get("orientation", "")),
                notes=str(body.get("notes", "")),
            )
            return web.json_response({"ok": True, "session": session})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)

    async def _api_capture_stop(self, request: web.Request) -> web.Response:
        if not self._capture:
            return web.json_response({"error": "capture service unavailable"}, status=503)
        try:
            session = await self._capture.stop_capture()
            source = self._ring_source
            imu_warning = None
            if (
                source is not None
                and hasattr(source, "pause_imu_report")
            ):
                try:
                    await source.pause_imu_report()
                except Exception as exc:
                    # The labeled segment is already safely closed. Report the
                    # transport problem without losing the completed session.
                    imu_warning = str(exc)
                    logger.warning(
                        "Capture stopped, but pausing IMU failed: %s", exc
                    )
            return web.json_response({
                "ok": True,
                "session": session,
                "imu_paused": imu_warning is None,
                "warning": imu_warning,
            })
        except RuntimeError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)

    async def _api_capture_export(self, request: web.Request) -> web.Response:
        if not self._capture:
            return web.json_response({"error": "capture service unavailable"}, status=503)
        content = await self._capture.export_csv()
        filename = time.strftime("focus_agent_imu_labels_%Y%m%d_%H%M%S.csv")
        return web.Response(
            text="\ufeff" + content,
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def _api_capture_delete(self, request: web.Request) -> web.Response:
        if not self._capture:
            return web.json_response({"error": "capture service unavailable"}, status=503)
        try:
            result = await self._capture.delete_session(
                int(request.match_info["session_id"])
            )
            return web.json_response({"ok": True, **result})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except LookupError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        except RuntimeError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)

    # ── WebSocket ──

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info("WebSocket client connected (%s total)", len(self._ws_clients))

        # 发送历史事件
        for evt in self._ring_events[-20:]:
            await ws.send_json(evt)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        # 客户端可以发 ping
                        if data.get("type") == "ping":
                            await ws.send_json({"type": "pong", "ts": int(time.time() * 1000)})
                    except Exception:
                        pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WS error: %s", ws.exception())
        finally:
            self._ws_clients.discard(ws)

        return ws

    # ── 事件回调 ──

    async def _on_frame(self, frame: AggregatedFrame) -> None:
        msg = {"type": "imu", **asdict(frame)}
        await self._broadcast(msg)

    async def _on_raw_imu(self, sample: IMUSample) -> None:
        # The browser needs a useful live chart, not every packet.
        source = self._ring_source
        hardware_streaming = bool(
            source and getattr(source, "imu_streaming", False)
        )
        if hardware_streaming:
            self._hardware_imu_ever_seen = True
        data_source = "ring" if hardware_streaming else "showcase_fallback"
        now_ms = int(time.monotonic() * 1000)
        if now_ms - self._last_raw_imu_push_ms < 100:
            return
        self._last_raw_imu_push_ms = now_ms
        await self._broadcast({
            "type": "imu_raw",
            "data_source": data_source,
            **asdict(sample),
        })

    async def _on_classifier_prediction(self, payload: dict) -> None:
        state = str(payload.get("state", ""))
        if state not in {"focused", "distracted"}:
            return
        self._customer_state = state
        self._customer_state_source = "imu_model"
        await self._broadcast({"type": "focus_state", **payload})

    async def _on_button_sequence(self, event: ButtonEvent) -> None:
        """Compose firmware single/double events into three/four-press actions."""
        units = 2 if event.event_type == "double_click" else 1
        if self._button_press_units + units > 4:
            self._button_press_units = units
        else:
            self._button_press_units += units
        self._last_button_pattern = f"正在识别：{self._button_press_units} 次按压"
        if self._button_sequence_task:
            self._button_sequence_task.cancel()
        self._button_sequence_task = asyncio.create_task(
            self._finalize_button_sequence()
        )

    async def _finalize_button_sequence(self) -> None:
        try:
            await asyncio.sleep(1.25)
            count = self._button_press_units
            self._button_press_units = 0
            if count == 3:
                self._last_button_pattern = "三击：开始专注"
                await self._set_focus_session(True, "戒指三击")
            elif count == 4:
                self._last_button_pattern = "四击：结束专注"
                await self._set_focus_session(False, "戒指四击")
            else:
                self._last_button_pattern = f"{count} 次按压：未绑定会话动作"
                await self._broadcast({
                    "type": "focus_agent",
                    **self._focus_agent_payload(),
                })
        except asyncio.CancelledError:
            return
        finally:
            if asyncio.current_task() is self._button_sequence_task:
                self._button_sequence_task = None

    async def _focus_agent_loop(self) -> None:
        """Run the lightweight farm-economy preview for the current session."""
        while True:
            try:
                await asyncio.sleep(1.0)
                if self._focus_session_active:
                    self._agent_tick_count += 1
                    if self._customer_state == "focused":
                        self._agent_balance += 0.8
                        self._agent_growth += 4.0
                        if self._agent_growth >= 100.0:
                            self._agent_growth -= 100.0
                            self._agent_harvest += 1
                            self._agent_balance += 8.0
                    else:
                        self._agent_balance = max(0.0, self._agent_balance - 0.5)
                        self._agent_growth = max(0.0, self._agent_growth - 3.0)
                        if self._agent_tick_count % 5 == 0:
                            self._agent_penalty += 1
                await self._broadcast({
                    "type": "focus_agent",
                    **self._focus_agent_payload(),
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Focus agent loop failed")
                await asyncio.sleep(1.0)

    async def _showcase_fallback_loop(self) -> None:
        """Keep the presentation alive after a verified hardware-data outage.

        This is explicit, reversible and visibly labelled in the panel. It
        never competes with a healthy ring stream and is blocked from labelled
        data capture.
        """
        while True:
            try:
                if not self._showcase_enabled:
                    self._showcase_active = False
                    self._showcase_outage_started_at = None
                    await asyncio.sleep(0.5)
                    continue

                # Continuity protection must never mask a ring that has not
                # produced real hardware data in this service session.
                if not self._hardware_imu_ever_seen:
                    self._showcase_active = False
                    self._showcase_outage_started_at = None
                    await asyncio.sleep(0.5)
                    continue

                source = self._ring_source
                hardware_streaming = bool(
                    source and getattr(source, "imu_streaming", False)
                )
                if hardware_streaming:
                    self._showcase_active = False
                    self._showcase_outage_started_at = None
                    await asyncio.sleep(0.25)
                    continue

                now = time.monotonic()
                if self._showcase_outage_started_at is None:
                    self._showcase_outage_started_at = now
                if now - self._showcase_outage_started_at < 3.0:
                    self._showcase_active = False
                    await asyncio.sleep(0.1)
                    continue

                self._showcase_active = True
                self._showcase_sequence += 1
                self._showcase_sample_count += 1
                phase = now % 18.0
                if phase < 10.0:
                    accel_amp, gyro_amp = 35.0, 8.0
                elif phase < 14.0:
                    accel_amp, gyro_amp = 180.0, 65.0
                else:
                    accel_amp, gyro_amp = 950.0, 520.0
                sample = IMUSample(
                    timestamp_ms=int(time.time() * 1000),
                    sequence=self._showcase_sequence,
                    accel_x=int(120 + accel_amp * math.sin(now * 3.1)),
                    accel_y=int(80 + accel_amp * math.sin(now * 2.3 + 0.7)),
                    accel_z=int(16384 + accel_amp * math.sin(now * 2.7 + 1.4)),
                    gyro_x=int(gyro_amp * math.sin(now * 4.1)),
                    gyro_y=int(gyro_amp * math.sin(now * 3.5 + 0.9)),
                    gyro_z=int(gyro_amp * math.sin(now * 2.9 + 1.8)),
                )
                await self._bus.publish("imu:sample", sample)
                await asyncio.sleep(0.04)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Showcase fallback loop failed")
                await asyncio.sleep(1.0)

    async def _on_ring_event(self, event: RingEventFrame) -> None:
        msg = {"type": "event", **asdict(event)}
        self._ring_events.append(msg)
        if len(self._ring_events) > 200:
            self._ring_events = self._ring_events[-100:]
        await self._broadcast(msg)

    async def _on_system(self, status: SystemFrame) -> None:
        msg = {"type": "system", **asdict(status)}
        await self._broadcast(msg)

    async def _on_gesture_ws(self, event) -> None:
        from .models import GestureEvent
        if isinstance(event, GestureEvent):
            self._gesture_events.append({
                "name": event.gesture_name,
                "id": event.gesture_id,
                "ts": event.timestamp_ms,
            })
            if len(self._gesture_events) > 200:
                self._gesture_events = self._gesture_events[-100:]

    async def _broadcast(self, msg: dict) -> None:
        if not self._ws_clients:
            return
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ── 工具 ──

    @staticmethod
    def _get_local_ip() -> str | None:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return None
