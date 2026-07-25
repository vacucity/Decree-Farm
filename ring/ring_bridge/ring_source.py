"""RingSource — 真实 BLE 戒指数据源

封装 ring_sound.py SDK，将原始 BLE 数据流转化为事件总线事件：
  - IMU 批量数据 → imu:sample / imu:batch
  - 手势事件 → gesture:rotate_back / gesture:rotate_front / gesture:wave
  - 按键事件 → button:single_click / button:double_click
  - 录音完成 → audio:recording
  - 系统状态 → system:battery / system:connected / system:disconnected
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
from collections import deque
from pathlib import Path

# 确保 ring_sound.py 在 path 中
SDK_DIR = Path(__file__).resolve().parent.parent
if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))

import ring_sound as sdk  # noqa: E402

from .event_bus import EventBus
from .models import (
    ButtonEvent,
    GestureEvent,
    IMUBatch,
    IMUSample,
    SystemStatus,
)

logger = logging.getLogger(__name__)


class RingSource:
    """真实 BLE 戒指数据源。

    连接到戒指 → 进入手势模式 → 启动 IMU 上报 → 持续推送事件到 EventBus。
    """

    def __init__(
        self,
        bus: EventBus,
        mac: str,
        command_timeout_s: float = 10.0,
    ):
        self._bus = bus
        self._mac = mac
        self._timeout = command_timeout_s
        self._client: sdk.RingSoundClient | None = None
        self._running = False
        self._connecting = False
        self._stopping = False
        self._connection_lock = asyncio.Lock()
        self._imu_retry_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._imu_task: asyncio.Task | None = None
        # Keep BLE control connection independent from the high-rate IMU
        # stream. 100 Hz reporting is requested only by capture/IMU actions.
        self._imu_requested = False
        self._reconnect_task: asyncio.Task | None = None
        self._last_imu_sample_at: float | None = None
        self._imu_sample_count = 0
        self._imu_report_enabled = False
        self._battery_known = False
        self._device_mode = "unknown"
        self._imu_state = "disconnected"
        self._imu_recovery_count = 0
        self._imu_last_error: str | None = None
        self._mode_prompt_shown = False
        self._imu_arrivals: deque[float] = deque(maxlen=2000)
        self._imu_recent_magnitudes: deque[float] = deque(maxlen=500)
        self._quality_received = 0
        self._quality_lost = 0
        self._quality_duplicates = 0
        self._quality_out_of_order = 0
        self._quality_last_sequence: int | None = None
        self._quality_contiguous = 0
        self._quality_max_contiguous = 0
        self._last_sample_values: tuple[int, ...] | None = None
        self._identical_sample_streak = 0
        self._sample_rate_hz: int | None = None
        self._accel_range_g: int | None = None
        self._gyro_range_dps: int | None = None
        self._last_disconnect_reason: str | None = None
        self._last_disconnect_at_ms: int | None = None
        self._last_connect_error: str | None = None

    # ── 公开接口 ──

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connecting(self) -> bool:
        return self._connecting

    @property
    def mac(self) -> str:
        return self._mac

    @property
    def client(self) -> sdk.RingSoundClient | None:
        return self._client

    @property
    def imu_streaming(self) -> bool:
        if not self._running or self._last_imu_sample_at is None:
            return False
        return time.monotonic() - self._last_imu_sample_at <= 8.0

    @property
    def imu_sample_count(self) -> int:
        return self._imu_sample_count

    @property
    def imu_last_sample_age_s(self) -> float | None:
        if self._last_imu_sample_at is None:
            return None
        return round(max(0.0, time.monotonic() - self._last_imu_sample_at), 1)

    @property
    def battery_known(self) -> bool:
        return self._battery_known

    @property
    def device_mode(self) -> str:
        return self._device_mode

    @property
    def imu_state(self) -> str:
        return self._imu_state

    @property
    def imu_recovery_count(self) -> int:
        return self._imu_recovery_count

    @property
    def imu_last_error(self) -> str | None:
        return self._imu_last_error

    @property
    def protocol_diagnostics(self) -> dict:
        if not self._client:
            return {}
        return getattr(self._client, "protocol_diagnostics", {})

    @property
    def data_quality(self) -> dict:
        now = time.monotonic()
        while self._imu_arrivals and now - self._imu_arrivals[0] > 5.0:
            self._imu_arrivals.popleft()
        span = self._imu_arrivals[-1] - self._imu_arrivals[0] if len(self._imu_arrivals) > 1 else 0.0
        actual_rate = (len(self._imu_arrivals) - 1) / span if span > 0 else 0.0
        total_expected = self._quality_received + self._quality_lost
        loss_rate = self._quality_lost / total_expected if total_expected else 0.0
        median_raw = None
        accel_g = None
        accel_reasonable = None
        if self._imu_recent_magnitudes:
            recent = list(self._imu_recent_magnitudes)[-100:]
            median_raw = sorted(recent)[len(recent) // 2]
            if self._accel_range_g:
                accel_g = median_raw * self._accel_range_g / 32768.0
                accel_reasonable = 0.4 <= accel_g <= 2.5
            else:
                accel_reasonable = 500.0 <= median_raw <= 50000.0
        expected_rate = float(self._sample_rate_hz or 100)
        frozen_limit = max(50, int(expected_rate * 2))
        frozen = self._identical_sample_streak >= frozen_limit
        stale = self.imu_last_sample_age_s
        status = "good"
        issues: list[str] = []
        if stale is not None and stale > 2.0:
            status = "bad"
            issues.append("imu_stale")
        if frozen:
            status = "bad"
            issues.append("sensor_frozen")
        if len(self._imu_arrivals) >= 10 and actual_rate < expected_rate * 0.5:
            status = "bad"
            issues.append("sample_rate_low")
        elif len(self._imu_arrivals) >= 10 and actual_rate < expected_rate * 0.8:
            if status == "good":
                status = "warning"
            issues.append("sample_rate_unstable")
        if loss_rate > 0.05:
            status = "bad"
            issues.append("packet_loss_high")
        elif loss_rate > 0.01:
            if status == "good":
                status = "warning"
            issues.append("packet_loss_detected")
        if accel_reasonable is False:
            if status == "good":
                status = "warning"
            issues.append("accel_magnitude_unusual")

        rssi = None
        rssi_age = None
        if self._client:
            transport = getattr(self._client, "transport", None)
            rssi = getattr(transport, "last_rssi", None)
            rssi_age = getattr(transport, "rssi_age_s", None)
        return {
            "status": status if self._running else "disconnected",
            "issues": issues,
            "expected_sample_rate_hz": self._sample_rate_hz,
            "actual_sample_rate_hz": round(actual_rate, 1),
            "recent_data_delay_s": stale,
            "received_samples": self._quality_received,
            "estimated_lost_samples": self._quality_lost,
            "packet_loss_rate": round(loss_rate, 5),
            "duplicate_samples": self._quality_duplicates,
            "out_of_order_samples": self._quality_out_of_order,
            "continuous_samples": self._quality_contiguous,
            "max_continuous_samples": self._quality_max_contiguous,
            "accel_magnitude_raw": round(median_raw, 2) if median_raw is not None else None,
            "accel_magnitude_g": round(accel_g, 3) if accel_g is not None else None,
            "accel_magnitude_reasonable": accel_reasonable,
            "sensor_frozen": frozen,
            "identical_sample_streak": self._identical_sample_streak,
            "rssi_dbm": rssi,
            "rssi_age_s": rssi_age,
            "last_disconnect_reason": self._last_disconnect_reason,
            "last_disconnect_at_ms": self._last_disconnect_at_ms,
            "last_connect_error": self._last_connect_error,
        }

    async def start(self, mac: str | None = None) -> None:
        """启动数据源：连接 → 校时 → 手势模式 → IMU 上报 → 事件监听"""
        async with self._connection_lock:
            if self._running:
                return
            self._stopping = False
            if mac:
                self._mac = mac.strip().upper()
            if not self._mac:
                raise ValueError("请提供戒指 MAC 地址")

            self._connecting = True
            logger.info("Connecting to %s ...", self._mac)
            print(f"[ring_source] Connecting to {self._mac}...", flush=True)

            try:
                # Some Windows BLE stacks can leave BleakClient.connect waiting
                # indefinitely. Bound the whole discovery/connect/notify phase
                # so the web panel can report failure and allow a retry.
                connect_timeout = max(20.0, min(45.0, self._timeout + 10.0))
                self._client = await asyncio.wait_for(
                    sdk.connect_ring(
                        address=self._mac,
                        command_timeout_s=self._timeout,
                    ),
                    timeout=connect_timeout,
                )
            except asyncio.TimeoutError as exc:
                self._client = None
                self._last_connect_error = f"BLE connection timed out after {connect_timeout:.0f}s"
                raise TimeoutError(
                    f"BLE connection timed out after {connect_timeout:.0f}s"
                ) from exc
            except Exception as exc:
                self._client = None
                self._last_connect_error = str(exc)
                raise
            finally:
                self._connecting = False
            print("[ring_source] Connected!", flush=True)

            self._last_imu_sample_at = None
            self._imu_sample_count = 0
            self._imu_report_enabled = False
            self._battery_known = False
            self._device_mode = "unknown"
            self._imu_state = "starting" if self._imu_requested else "idle"
            self._imu_recovery_count = 0
            self._imu_last_error = None
            self._mode_prompt_shown = False
            self._last_connect_error = None
            self._reset_quality_metrics()
            self._running = True
            await self._bus.publish("system:connected", None)

            sdk.enable_time_sync(self._client)

            self._tasks = [
                asyncio.create_task(self._gesture_loop()),
                asyncio.create_task(self._click_loop()),
                asyncio.create_task(self._double_click_loop()),
                asyncio.create_task(self._battery_loop()),
            ]
            if self._imu_requested:
                self._imu_task = asyncio.create_task(self._imu_loop())
                self._tasks.insert(0, self._imu_task)
            else:
                self._imu_task = None
                logger.info(
                    "BLE control connected; IMU remains idle until capture "
                    "or an explicit IMU request"
                )

    async def stop(self) -> None:
        """停止数据源"""
        async with self._connection_lock:
            self._stopping = True
            self._running = False
            self._imu_requested = False
            self._imu_report_enabled = False
            self._last_imu_sample_at = None
            self._imu_sample_count = 0
            self._battery_known = False
            self._device_mode = "unknown"
            self._imu_state = "disconnected"
            self._imu_last_error = None
            self._last_disconnect_reason = "user_requested"
            self._last_disconnect_at_ms = int(time.time() * 1000)

            if self._reconnect_task and self._reconnect_task is not asyncio.current_task():
                self._reconnect_task.cancel()
            self._reconnect_task = None

            for task in self._tasks:
                task.cancel()
            self._tasks.clear()
            self._imu_task = None

            if self._client and self._client.is_connected:
                try:
                    await sdk.stop_sensor_report(self._client)
                except Exception:
                    pass
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
            self._client = None

            await self._bus.publish(
                "system:disconnected",
                {"reason": self._last_disconnect_reason},
            )
            logger.info("Disconnected")

    # ── 内部协程 ──

    async def _handle_transport_loss(self, reason: str = "ble_transport_lost") -> None:
        """Mark a lost BLE session and start a non-blocking reconnect loop."""
        was_running = self._running
        self._last_disconnect_reason = reason
        self._last_disconnect_at_ms = int(time.time() * 1000)
        self._running = False
        self._imu_report_enabled = False
        self._last_imu_sample_at = None
        self._device_mode = "unknown"
        self._imu_state = "disconnected"
        current_task = asyncio.current_task()
        for task in self._tasks:
            if task is not current_task:
                task.cancel()
        self._tasks.clear()
        self._imu_task = None
        client = self._client
        self._client = None
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                pass
        if was_running:
            await self._bus.publish("system:disconnected", {"reason": reason})
        if self._stopping:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def retry_imu_report(self) -> None:
        """Re-request real IMU data without disconnecting the BLE session.

        The V2 hardware may require the user to long-press until the red LED
        lights before 0x0601. Restart only the IMU worker so that this physical
        activation is preserved in the current BLE connection.
        """
        async with self._imu_retry_lock:
            if not self._running or not self._client or not self._client.is_connected:
                raise RuntimeError("戒指蓝牙尚未连接")

            self._imu_requested = True
            old_task = self._imu_task
            if old_task and not old_task.done():
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass
            if old_task in self._tasks:
                self._tasks.remove(old_task)

            try:
                await sdk.stop_sensor_report(self._client, timeout_s=5.0)
            except sdk.TransportError:
                await self._handle_transport_loss("transport_lost_during_imu_retry")
                raise RuntimeError("重试时蓝牙连接已断开")
            except Exception as exc:
                logger.info("Manual IMU retry continuing after stop response: %s", exc)

            self._last_imu_sample_at = None
            self._imu_sample_count = 0
            self._imu_report_enabled = False
            self._imu_recovery_count = 0
            self._imu_last_error = None
            self._imu_state = "starting"
            self._imu_task = asyncio.create_task(self._imu_loop())
            self._tasks.insert(0, self._imu_task)
            logger.info("Manual in-session IMU retry requested")

    async def pause_imu_report(self) -> None:
        """Stop high-rate IMU reporting while keeping BLE control connected."""
        async with self._imu_retry_lock:
            self._imu_requested = False
            old_task = self._imu_task
            self._imu_task = None
            if old_task and not old_task.done():
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass
            if old_task in self._tasks:
                self._tasks.remove(old_task)

            self._imu_report_enabled = False
            self._last_imu_sample_at = None
            self._imu_state = "idle" if self._running else "disconnected"

            if not self._running or not self._client or not self._client.is_connected:
                return
            try:
                await sdk.stop_sensor_report(self._client, timeout_s=5.0)
                logger.info("IMU report paused; BLE control connection retained")
            except sdk.TransportError:
                await self._handle_transport_loss("transport_lost_while_stopping_imu")
                raise RuntimeError("停止 IMU 时蓝牙连接已断开")
            except Exception as exc:
                self._imu_last_error = str(exc)
                logger.warning("IMU stop response failed: %s", exc)
                raise RuntimeError(f"停止 IMU 失败: {exc}") from exc

    async def _reconnect_loop(self) -> None:
        attempt = 0
        while not self._stopping and not self._running:
            attempt += 1
            await asyncio.sleep(2.0 if attempt == 1 else 5.0)
            if self._stopping or self._running:
                return
            logger.info("BLE reconnect attempt %s for %s", attempt, self._mac)
            try:
                await self.start()
                logger.info("BLE reconnect succeeded")
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._last_connect_error = str(exc)
                logger.warning("BLE reconnect attempt %s failed: %s", attempt, exc)

    def _reset_quality_metrics(self) -> None:
        self._imu_arrivals.clear()
        self._imu_recent_magnitudes.clear()
        self._quality_received = 0
        self._quality_lost = 0
        self._quality_duplicates = 0
        self._quality_out_of_order = 0
        self._quality_last_sequence = None
        self._quality_contiguous = 0
        self._quality_max_contiguous = 0
        self._last_sample_values = None
        self._identical_sample_streak = 0
        self._sample_rate_hz = None
        self._accel_range_g = None
        self._gyro_range_dps = None

    def _record_quality_sample(self, sample: IMUSample) -> None:
        now = time.monotonic()
        self._imu_arrivals.append(now)
        self._quality_received += 1
        sequence = int(sample.sequence) & 0xFFFFFFFF
        if self._quality_last_sequence is None:
            self._quality_contiguous = 1
        else:
            delta = (sequence - self._quality_last_sequence) & 0xFFFFFFFF
            if delta == 1:
                self._quality_contiguous += 1
            elif delta == 0:
                self._quality_duplicates += 1
                self._quality_contiguous = 0
            elif 1 < delta < 1_000_000:
                self._quality_lost += delta - 1
                self._quality_contiguous = 1
            else:
                self._quality_out_of_order += 1
                self._quality_contiguous = 1
        self._quality_last_sequence = sequence
        self._quality_max_contiguous = max(
            self._quality_max_contiguous,
            self._quality_contiguous,
        )
        values = (
            int(sample.accel_x), int(sample.accel_y), int(sample.accel_z),
            int(sample.gyro_x), int(sample.gyro_y), int(sample.gyro_z),
        )
        if values == self._last_sample_values:
            self._identical_sample_streak += 1
        else:
            self._identical_sample_streak = 1
            self._last_sample_values = values
        self._imu_recent_magnitudes.append(
            math.sqrt(
                float(sample.accel_x) ** 2
                + float(sample.accel_y) ** 2
                + float(sample.accel_z) ** 2
            )
        )

    async def _ensure_gesture_mode(self) -> bool:
        """确保设备处于手势模式并开启 IMU 上报。

        设备启动默认是录音模式，需要单击切换到手势模式。
        不消费按键事件（留给 _click_loop 处理），只轮询重试。
        返回 True 表示成功。
        """
        if not self._client or not self._client.is_connected:
            return False

        # 轮询重试（不消费按键事件，避免与 _click_loop 冲突）
        if not self._mode_prompt_shown:
            print()
            print("=" * 50)
            print("  戒指当前在录音模式，请单击按键切换到手势模式")
            print("  然后程序会自动检测并启动 IMU 数据流")
            print("=" * 50)
            print()
            self._mode_prompt_shown = True

        for attempt in range(60):  # 最多等 60 秒
            if not self._running:
                return False
            await asyncio.sleep(1.0)
            try:
                self._imu_state = "starting"
                info = await sdk.start_sensor_report(self._client)
                self._imu_report_enabled = True
                self._sample_rate_hz = int(info.sample_rate_hz)
                self._accel_range_g = int(info.accel_range_g)
                self._gyro_range_dps = int(info.gyro_range_dps)
                self._device_mode = "gesture"
                self._imu_state = "awaiting_data"
                logger.info(
                    "IMU report started (attempt %s, rate=%sHz, accel=±%sg, gyro=±%sdps)",
                    attempt + 1,
                    info.sample_rate_hz,
                    info.accel_range_g,
                    info.gyro_range_dps,
                )
                print(">>> IMU 数据流已开启！")
                return True
            except sdk.DeviceError as e:
                if e.error_code == 2:
                    self._device_mode = "recording"
                    self._imu_state = "waiting_mode"
                else:
                    raise
            except sdk.TransportError:
                logger.warning("BLE transport error while starting IMU")
                await self._handle_transport_loss("transport_lost_while_starting_imu")
                return False
            except Exception as exc:
                self._imu_last_error = str(exc)
                logger.debug("IMU start attempt %s failed: %s", attempt + 1, exc)

        logger.error("Could not start IMU after 60s — ring stuck in recording mode")
        return False

    async def _imu_loop(self) -> None:
        """IMU 数据采集循环；数据中断后自动重新开启 0x0601 上报。"""
        while self._running:
            if not await self._ensure_gesture_mode():
                self._imu_report_enabled = False
                logger.error(
                    "Cannot start IMU — ring may still be in recording mode; retrying"
                )
                await asyncio.sleep(2.0)
                continue

            consecutive_timeouts = 0
            while self._running:
                try:
                    batch: sdk.SensorDataBatch = await sdk.wait_sensor_data(
                        self._client, timeout_s=5.0
                    )
                    consecutive_timeouts = 0
                    if not batch.samples:
                        continue

                    self._last_imu_sample_at = time.monotonic()
                    self._imu_sample_count += len(batch.samples)
                    self._imu_recovery_count = 0
                    self._imu_report_enabled = True
                    self._device_mode = "gesture"
                    self._imu_state = "streaming"
                    self._imu_last_error = None

                    # 转换为内部模型
                    samples = [
                        IMUSample(
                            timestamp_ms=s.timestamp_ms,
                            sequence=batch.sequence_start + i,
                            accel_x=s.accel_x,
                            accel_y=s.accel_y,
                            accel_z=s.accel_z,
                            gyro_x=s.gyro_x,
                            gyro_y=s.gyro_y,
                            gyro_z=s.gyro_z,
                        )
                        for i, s in enumerate(batch.samples)
                    ]

                    imu_batch = IMUBatch(
                        sequence_start=batch.sequence_start,
                        samples=samples,
                    )

                    for sample in samples:
                        self._record_quality_sample(sample)

                    # 发布批次 + 单个采样点
                    await self._bus.publish("imu:batch", imu_batch)
                    for sample in samples:
                        await self._bus.publish("imu:sample", sample)

                except sdk.TimeoutError:
                    consecutive_timeouts += 1
                    if consecutive_timeouts < 2:
                        continue
                    self._imu_report_enabled = False
                    self._imu_last_error = "启动命令成功，但 10 秒内未收到 0x0605 数据"
                    if self._imu_recovery_count >= 3:
                        self._imu_state = "device_no_data"
                        self._imu_last_error = (
                            "连续 3 次重置后仍无 0x0605，已停止自动重发命令；"
                            "请断开并重启戒指"
                        )
                        if consecutive_timeouts == 2:
                            logger.error(
                                "No 0x0605 after 3 recovery cycles; "
                                "automatic control-command retries are now paused"
                            )
                        # Keep listening so a late packet can recover the stream,
                        # but do not keep stressing the firmware with 0603/0601.
                        await asyncio.sleep(5.0)
                        continue
                    self._imu_state = "recovering"
                    self._imu_recovery_count += 1
                    logger.warning(
                        "No raw IMU packets for 10s; resetting 0x0603 -> 0x0601 "
                        "(recovery %s)",
                        self._imu_recovery_count,
                    )
                    try:
                        await sdk.stop_sensor_report(self._client, timeout_s=5.0)
                        logger.info("IMU report stopped before recovery")
                    except sdk.TransportError:
                        logger.warning("BLE transport error while stopping IMU")
                        await self._handle_transport_loss("transport_lost_during_imu_recovery")
                        return
                    except Exception as exc:
                        logger.warning(
                            "IMU stop before recovery failed; continuing with restart: %s",
                            exc,
                        )
                    await asyncio.sleep(0.5)
                    break
                except sdk.TransportError:
                    logger.warning("BLE transport error in IMU loop")
                    await self._handle_transport_loss("transport_lost_in_imu_stream")
                    return
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.exception("Unexpected error in IMU loop")
                    await asyncio.sleep(1.0)

            if self._running:
                await asyncio.sleep(0.2)

    async def _gesture_loop(self) -> None:
        """手势事件监听循环"""
        while self._running:
            try:
                event: sdk.SensorGestureEvent = await sdk.wait_sensor_gesture_event(
                    self._client, timeout_s=60.0
                )
                name = sdk.sensor_gesture_name(event.gesture_id)
                ge = GestureEvent(
                    timestamp_ms=event.timestamp_ms,
                    gesture_id=event.gesture_id,
                    gesture_name=name,
                )
                topic = f"gesture:{name}"
                await self._bus.publish(topic, ge)
                await self._bus.publish("gesture:*", ge)
                self._device_mode = "gesture"
                if not self._imu_requested:
                    self._imu_state = "idle"
                logger.info("Gesture: %s (id=%s)", name, event.gesture_id)

            except sdk.TimeoutError:
                continue
            except sdk.TransportError:
                logger.warning("BLE transport error in gesture loop")
                await self._handle_transport_loss("transport_lost_in_gesture_stream")
                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in gesture loop")

    async def _click_loop(self) -> None:
        """按键单击监听循环"""
        while self._running:
            try:
                event: sdk.SensorKeySinglePressEvent = (
                    await sdk.wait_sensor_key_single_press_event(
                        self._client, timeout_s=60.0
                    )
                )
                be = ButtonEvent(
                    timestamp_ms=event.timestamp_ms,
                    event_type="single_click",
                )
                await self._bus.publish("button:single_click", be)
                await self._bus.publish("button:*", be)
                self._imu_report_enabled = False
                self._device_mode = "switching"
                self._imu_state = "mode_switch"
                self._mode_prompt_shown = False
                logger.info(
                    "Button: single_click (device mode may have changed; IMU will be verified)"
                )

            except sdk.TimeoutError:
                continue
            except sdk.TransportError:
                await self._handle_transport_loss("transport_lost_in_click_stream")
                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in click loop")

    async def _double_click_loop(self) -> None:
        """按键双击监听循环"""
        while self._running:
            try:
                event: sdk.SensorKeyDoublePressEvent = (
                    await sdk.wait_sensor_key_double_press_event(
                        self._client, timeout_s=60.0
                    )
                )
                be = ButtonEvent(
                    timestamp_ms=event.timestamp_ms,
                    event_type="double_click",
                )
                await self._bus.publish("button:double_click", be)
                await self._bus.publish("button:*", be)
                logger.info("Button: double_click")

            except sdk.TimeoutError:
                continue
            except sdk.TransportError:
                await self._handle_transport_loss("transport_lost_in_double_click_stream")
                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in double-click loop")

    async def _battery_loop(self) -> None:
        """定期读取电量；未知时快速重试，成功后每 60 秒刷新。"""
        retry_delay_s = 1.0
        while self._running:
            try:
                await asyncio.sleep(retry_delay_s)
                if not self._client or not self._client.is_connected:
                    continue
                info = await sdk.get_system_info(self._client)
                self._battery_known = True
                await self._bus.publish("system:battery", SystemStatus(
                    battery_percent=info.battery_percent,
                    battery_charging=info.battery_charging,
                    connected=True,
                ))
                logger.info(
                    "System info: firmware=%s battery=%s%% charging=%s",
                    info.firmware_version,
                    info.battery_percent,
                    info.battery_charging,
                )
                retry_delay_s = 60.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._battery_known = False
                retry_delay_s = 15.0
                logger.warning("Could not read system info: %s", exc)
