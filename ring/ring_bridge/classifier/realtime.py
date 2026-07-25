"""Real-time focus inference, personal calibration, and stable state output."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from ..event_bus import EventBus
from ..models import IMUSample
from .features import extract_feature_vector

logger = logging.getLogger(__name__)

CALIBRATION_PHASES = (
    ("still", 10.0, "静止佩戴"),
    ("focused", 20.0, "正常工作或阅读"),
    ("distracted", 20.0, "模拟刷手机或离开任务"),
)


class RealtimeFocusClassifier:
    """Load a trained artifact and publish stable binary focus-state events."""

    def __init__(self, bus: EventBus, model_path: str | Path):
        self._bus = bus
        self._model_path = Path(model_path)
        self._artifact: dict[str, Any] | None = None
        self._samples: deque[IMUSample] = deque()
        self._probabilities: deque[float] = deque()
        self._samples_since_prediction = 0
        self._last_sequence: int | None = None
        self._last_timestamp_ms: int | None = None
        self._sequence = 0
        self._latest: dict[str, Any] | None = None
        self._running = False
        self._stable_state: str | None = None
        self._stable_since: float | None = None
        self._pending_state: str | None = None
        self._pending_count = 0
        self._minimum_state_duration_s = 8.0
        self._switch_confirmations = 3
        self._confidence_margin = 0.06
        self._calibration_path = self._model_path.with_suffix(".calibrations.json")
        self._profiles: dict[str, dict[str, Any]] = {}
        self._active_user_id: str | None = None
        self._session_id: str | None = None
        self._session_active = False
        self._calibration: dict[str, Any] = {
            "active": False,
            "status": "not_started",
            "user_id": None,
            "error": None,
        }

    @property
    def ready(self) -> bool:
        return self._artifact is not None

    @property
    def latest(self) -> dict[str, Any] | None:
        return dict(self._latest) if self._latest else None

    @property
    def calibration_status(self) -> dict[str, Any]:
        hidden = {
            "samples", "probabilities", "phase_started_at",
            "phase_active_elapsed_s", "last_device_timestamp_ms",
        }
        result = {k: v for k, v in self._calibration.items() if k not in hidden}
        if self._calibration.get("active"):
            index = int(self._calibration["phase_index"])
            phase, duration, label = CALIBRATION_PHASES[index]
            elapsed = float(self._calibration.get("phase_active_elapsed_s", 0.0))
            prior = sum(item[1] for item in CALIBRATION_PHASES[:index])
            total = sum(item[1] for item in CALIBRATION_PHASES)
            result.update({
                "phase": phase,
                "phase_label": label,
                "phase_duration_s": duration,
                "phase_elapsed_s": round(min(duration, elapsed), 1),
                "phase_remaining_s": round(max(0.0, duration - elapsed), 1),
                "progress": round(min(1.0, (prior + elapsed) / total), 4),
                "waiting_for_data": self._calibration.get("phase_started_at") is None,
                "sample_counts": {
                    key: len(self._calibration["samples"][key])
                    for key, _, _ in CALIBRATION_PHASES
                },
            })
        result["active_user_id"] = self._active_user_id
        result["available_users"] = sorted(self._profiles)
        result["profile"] = self._profiles.get(self._active_user_id)
        return result

    @property
    def status(self) -> dict[str, Any]:
        artifact = self._artifact or {}
        return {
            "ready": self.ready,
            "model_version": artifact.get("model_version"),
            "model_type": artifact.get("model_type"),
            "window_samples": artifact.get("window_samples"),
            "step_samples": artifact.get("step_samples"),
            "smoothing_predictions": artifact.get("smoothing_predictions"),
            "decision_threshold": artifact.get("decision_threshold"),
            "effective_threshold": round(self._effective_threshold(), 4),
            "stability": {
                "stable_state": self._stable_state,
                "minimum_state_duration_s": self._minimum_state_duration_s,
                "switch_confirmations": self._switch_confirmations,
                "confidence_margin": self._confidence_margin,
                "pending_state": self._pending_state,
                "pending_count": self._pending_count,
            },
            "calibration": self.calibration_status,
            "session_id": self._session_id,
            "session_active": self._session_active,
            "latest": self.latest,
        }

    async def start(self) -> None:
        self._artifact = joblib.load(self._model_path)
        required = {
            "estimator",
            "window_samples",
            "step_samples",
            "feature_indices",
            "decision_threshold",
        }
        missing = sorted(required.difference(self._artifact))
        if missing:
            raise RuntimeError(f"classifier artifact missing fields: {missing}")
        self._samples = deque(maxlen=int(self._artifact["window_samples"]))
        self._probabilities = deque(
            maxlen=int(self._artifact.get("smoothing_predictions", 1))
        )
        self._load_profiles()
        self._bus.subscribe("imu:sample", self._on_sample)
        self._bus.subscribe("system:disconnected", self._on_disconnected)
        self._bus.subscribe("focus:session", self._on_focus_session)
        self._running = True
        logger.info(
            "Focus classifier ready: %s (%s samples, %s calibrations)",
            self._artifact.get("model_version", self._model_path.name),
            self._artifact["window_samples"],
            len(self._profiles),
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._bus.unsubscribe("imu:sample", self._on_sample)
        self._bus.unsubscribe("system:disconnected", self._on_disconnected)
        self._bus.unsubscribe("focus:session", self._on_focus_session)
        self._reset_stream(reset_stable=True)
        self._running = False

    def _reset_stability(self) -> None:
        self._stable_state = None
        self._stable_since = None
        self._pending_state = None
        self._pending_count = 0

    def _reset_stream(self, *, reset_stable: bool = False) -> None:
        self._samples.clear()
        self._probabilities.clear()
        self._samples_since_prediction = 0
        self._last_sequence = None
        self._last_timestamp_ms = None
        if reset_stable:
            self._reset_stability()

    async def _on_disconnected(self, data: Any) -> None:
        self._reset_stream(reset_stable=True)
        if self._calibration.get("active"):
            reason = "ring disconnected during calibration"
            if isinstance(data, dict) and data.get("reason"):
                reason += f": {data['reason']}"
            self.cancel_calibration(reason)

    async def _on_focus_session(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        self._session_active = bool(payload.get("active", False))
        self._session_id = (
            str(payload.get("session_id"))
            if self._session_active and payload.get("session_id")
            else None
        )
        self._reset_stream(reset_stable=True)

    def start_calibration(self, user_id: str) -> dict[str, Any]:
        if not self.ready:
            raise RuntimeError("classifier model is not ready")
        user_id = str(user_id).strip()
        if not user_id:
            raise ValueError("user_id is required")
        if len(user_id) > 80:
            raise ValueError("user_id is too long")
        self._active_user_id = user_id
        self._calibration = {
            "active": True,
            "status": "running",
            "user_id": user_id,
            "phase_index": 0,
            "phase_started_at": None,
            "phase_active_elapsed_s": 0.0,
            "last_device_timestamp_ms": None,
            "started_at_ms": int(time.time() * 1000),
            "completed_at_ms": None,
            "error": None,
            "samples": {key: [] for key, _, _ in CALIBRATION_PHASES},
            "probabilities": {key: [] for key, _, _ in CALIBRATION_PHASES},
        }
        self._reset_stream(reset_stable=True)
        logger.info("Personal calibration started for user=%s", user_id)
        return self.calibration_status

    def cancel_calibration(self, reason: str = "cancelled") -> dict[str, Any]:
        if self._calibration.get("active"):
            self._calibration["active"] = False
            self._calibration["status"] = "cancelled"
            self._calibration["error"] = reason
            self._calibration["completed_at_ms"] = int(time.time() * 1000)
        return self.calibration_status

    def select_calibration(self, user_id: str | None) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        if not user_id:
            self._active_user_id = None
        elif user_id not in self._profiles:
            raise LookupError(f"no calibration found for user_id={user_id}")
        else:
            self._active_user_id = user_id
        self._reset_stability()
        self._save_profiles()
        return self.calibration_status

    def _is_contiguous(self, sample: IMUSample) -> bool:
        if self._last_sequence is None or self._last_timestamp_ms is None:
            return True
        sequence_delta = (int(sample.sequence) - self._last_sequence) & 0xFFFFFFFF
        timestamp_delta = int(sample.timestamp_ms) - self._last_timestamp_ms
        max_gap_ms = int(self._artifact.get("max_gap_ms", 50))
        return sequence_delta == 1 and 0 <= timestamp_delta <= max_gap_ms

    async def _on_sample(self, sample: IMUSample) -> None:
        if not self._artifact:
            return
        if not self._is_contiguous(sample):
            self._reset_stream(reset_stable=False)
        self._last_sequence = int(sample.sequence)
        self._last_timestamp_ms = int(sample.timestamp_ms)
        self._samples.append(sample)
        self._samples_since_prediction += 1
        self._collect_calibration_sample(sample)

        window_samples = int(self._artifact["window_samples"])
        step_samples = int(self._artifact["step_samples"])
        if len(self._samples) < window_samples:
            return
        if self._samples_since_prediction < step_samples:
            return
        self._samples_since_prediction = 0

        values = np.asarray(
            [
                (
                    item.accel_x,
                    item.accel_y,
                    item.accel_z,
                    item.gyro_x,
                    item.gyro_y,
                    item.gyro_z,
                )
                for item in self._samples
            ],
            dtype=np.float64,
        )
        all_features = extract_feature_vector(values)
        selected = all_features[
            np.asarray(self._artifact["feature_indices"], dtype=np.int32)
        ].reshape(1, -1)
        raw_probability = float(
            self._artifact["estimator"].predict_proba(selected)[0, 1]
        )
        self._collect_calibration_probability(raw_probability)
        self._probabilities.append(raw_probability)
        smoothed_probability = float(np.median(self._probabilities))

        # Do not publish calibration activity as normal customer state.
        if self._calibration.get("active"):
            return

        threshold = self._effective_threshold()
        state, held_reason = self._stable_decision(smoothed_probability, threshold)
        confidence = (
            smoothed_probability
            if state == "focused"
            else 1.0 - smoothed_probability
        )
        raw_candidate = "focused" if smoothed_probability >= threshold else "distracted"
        self._sequence += 1
        payload = {
            "schema_version": "1.1",
            "session_id": self._session_id,
            "session_active": self._session_active,
            "sequence": self._sequence,
            "timestamp_ms": int(time.time() * 1000),
            "state": state,
            "confidence": round(confidence, 4),
            "focused_probability": round(smoothed_probability, 4),
            "raw_focused_probability": round(raw_probability, 4),
            "raw_candidate": raw_candidate,
            "held_reason": held_reason,
            "state_age_s": round(
                max(0.0, time.monotonic() - self._stable_since)
                if self._stable_since else 0.0,
                1,
            ),
            "threshold": round(threshold, 4),
            "calibration_user_id": self._active_user_id,
            "source": "imu_model",
            "model_version": self._artifact.get("model_version", "focus-rf-v1"),
            "window_ms": int(
                1000 * window_samples / self._artifact.get("sample_rate_hz", 100)
            ),
            "data_quality": "good",
            "ring_connected": True,
        }
        self._latest = payload
        await self._bus.publish("classifier:prediction", payload)

    def _stable_decision(self, probability: float, threshold: float) -> tuple[str, str | None]:
        now = time.monotonic()
        raw = "focused" if probability >= threshold else "distracted"
        if self._stable_state is None:
            self._stable_state = raw
            self._stable_since = now
            return raw, "initial_state"

        if self._stable_state == "focused":
            candidate = "distracted" if probability < threshold - self._confidence_margin else "focused"
        else:
            candidate = "focused" if probability > threshold + self._confidence_margin else "distracted"

        if candidate == self._stable_state:
            self._pending_state = None
            self._pending_count = 0
            reason = "low_confidence_hold" if abs(probability - threshold) <= self._confidence_margin else None
            return self._stable_state, reason

        age_s = now - (self._stable_since or now)
        if age_s < self._minimum_state_duration_s:
            self._pending_state = None
            self._pending_count = 0
            return self._stable_state, "minimum_duration_hold"

        if candidate != self._pending_state:
            self._pending_state = candidate
            self._pending_count = 1
        else:
            self._pending_count += 1
        if self._pending_count < self._switch_confirmations:
            return self._stable_state, "consecutive_confirmation_hold"

        self._stable_state = candidate
        self._stable_since = now
        self._pending_state = None
        self._pending_count = 0
        return self._stable_state, "state_changed"

    def _collect_calibration_sample(self, sample: IMUSample) -> None:
        if not self._calibration.get("active"):
            return
        now = time.monotonic()
        if self._calibration["phase_started_at"] is None:
            self._calibration["phase_started_at"] = now
        last_device_ms = self._calibration.get("last_device_timestamp_ms")
        current_device_ms = int(sample.timestamp_ms)
        if last_device_ms is not None:
            delta_ms = current_device_ms - int(last_device_ms)
            if delta_ms <= 0 or delta_ms > 100:
                delta_ms = int(1000 / float(self._artifact.get("sample_rate_hz", 100)))
            self._calibration["phase_active_elapsed_s"] += delta_ms / 1000.0
        self._calibration["last_device_timestamp_ms"] = current_device_ms
        index = int(self._calibration["phase_index"])
        if self._calibration["phase_active_elapsed_s"] >= CALIBRATION_PHASES[index][1]:
            index += 1
            if index >= len(CALIBRATION_PHASES):
                self._finish_calibration()
                return
            self._calibration["phase_index"] = index
            self._calibration["phase_started_at"] = now
            self._calibration["phase_active_elapsed_s"] = 0.0
            self._calibration["last_device_timestamp_ms"] = current_device_ms
            self._reset_stream(reset_stable=False)
        key = CALIBRATION_PHASES[index][0]
        self._calibration["samples"][key].append((
            int(sample.accel_x), int(sample.accel_y), int(sample.accel_z),
            int(sample.gyro_x), int(sample.gyro_y), int(sample.gyro_z),
        ))

    def _collect_calibration_probability(self, probability: float) -> None:
        if not self._calibration.get("active"):
            return
        key = CALIBRATION_PHASES[int(self._calibration["phase_index"])][0]
        self._calibration["probabilities"][key].append(float(probability))

    def _finish_calibration(self) -> None:
        calibration = self._calibration
        if not calibration.get("active"):
            return
        try:
            arrays = {
                key: np.asarray(calibration["samples"][key], dtype=np.float64)
                for key, _, _ in CALIBRATION_PHASES
            }
            if any(len(values) < 20 for values in arrays.values()):
                raise RuntimeError("insufficient IMU samples in one or more phases")

            still = arrays["still"]
            gravity = np.median(still[:, :3], axis=0)
            gravity_norm = float(np.linalg.norm(gravity))
            unit = gravity / gravity_norm if gravity_norm > 1e-9 else np.zeros(3)
            dominant = int(np.argmax(np.abs(unit)))
            orientation = ("+" if unit[dominant] >= 0 else "-") + ("X", "Y", "Z")[dominant]

            focused_probs = calibration["probabilities"]["focused"]
            distracted_probs = calibration["probabilities"]["distracted"]
            global_threshold = float(self._artifact["decision_threshold"])
            threshold = global_threshold
            threshold_source = "global_model"
            focused_median = float(np.median(focused_probs)) if focused_probs else None
            distracted_median = float(np.median(distracted_probs)) if distracted_probs else None
            separation = (
                focused_median - distracted_median
                if focused_median is not None and distracted_median is not None
                else None
            )
            if separation is not None and separation >= 0.04:
                threshold = float(np.clip((focused_median + distracted_median) / 2.0, 0.15, 0.85))
                threshold_source = "personal_calibration"

            user_id = str(calibration["user_id"])
            profile = {
                "schema_version": "1.0",
                "user_id": user_id,
                "created_at_ms": int(time.time() * 1000),
                "model_version": self._artifact.get("model_version"),
                "sample_counts": {key: int(len(values)) for key, values in arrays.items()},
                "gravity_vector_raw": [round(float(value), 3) for value in gravity],
                "gravity_unit_vector": [round(float(value), 5) for value in unit],
                "orientation": orientation,
                "static_accel_magnitude_raw": round(gravity_norm, 3),
                "static_gyro_magnitude_raw": round(float(np.median(np.linalg.norm(still[:, 3:], axis=1))), 3),
                "motion": {key: self._motion_metrics(values) for key, values in arrays.items()},
                "focused_probability_median": round(focused_median, 5) if focused_median is not None else None,
                "distracted_probability_median": round(distracted_median, 5) if distracted_median is not None else None,
                "probability_separation": round(separation, 5) if separation is not None else None,
                "decision_threshold": round(threshold, 5),
                "threshold_source": threshold_source,
                "quality": "good" if separation is not None and separation >= 0.08 else "usable" if separation is not None and separation >= 0.04 else "weak",
            }
            self._profiles[user_id] = profile
            self._active_user_id = user_id
            self._save_profiles()
            self._calibration = {
                "active": False,
                "status": "completed",
                "user_id": user_id,
                "started_at_ms": calibration["started_at_ms"],
                "completed_at_ms": int(time.time() * 1000),
                "error": None,
            }
            self._reset_stream(reset_stable=True)
            logger.info("Personal calibration complete user=%s threshold=%.3f quality=%s", user_id, threshold, profile["quality"])
        except Exception as exc:
            logger.exception("Personal calibration failed")
            self._calibration = {
                "active": False,
                "status": "failed",
                "user_id": calibration.get("user_id"),
                "started_at_ms": calibration.get("started_at_ms"),
                "completed_at_ms": int(time.time() * 1000),
                "error": str(exc),
            }

    @staticmethod
    def _motion_metrics(values: np.ndarray) -> dict[str, float]:
        accel = np.linalg.norm(values[:, :3], axis=1)
        gyro = np.linalg.norm(values[:, 3:], axis=1)
        return {
            "accel_magnitude_median": round(float(np.median(accel)), 3),
            "accel_magnitude_std": round(float(np.std(accel)), 3),
            "accel_change_p95": round(float(np.percentile(np.abs(np.diff(accel)), 95)), 3),
            "gyro_magnitude_median": round(float(np.median(gyro)), 3),
            "gyro_magnitude_p95": round(float(np.percentile(gyro, 95)), 3),
            "gyro_change_p95": round(float(np.percentile(np.abs(np.diff(gyro)), 95)), 3),
        }

    def _effective_threshold(self) -> float:
        if not self._artifact:
            return 0.5
        profile = self._profiles.get(self._active_user_id) if self._active_user_id else None
        value = profile.get("decision_threshold") if profile else None
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        return float(self._artifact["decision_threshold"])

    def _load_profiles(self) -> None:
        self._profiles = {}
        if not self._calibration_path.exists():
            return
        try:
            payload = json.loads(self._calibration_path.read_text(encoding="utf-8"))
            profiles = payload.get("profiles", {})
            if isinstance(profiles, dict):
                self._profiles = {str(k): v for k, v in profiles.items() if isinstance(v, dict)}
            active = payload.get("active_user_id")
            if active in self._profiles:
                self._active_user_id = str(active)
        except Exception:
            logger.exception("Could not load calibration profiles from %s", self._calibration_path)

    def _save_profiles(self) -> None:
        payload = {
            "schema_version": "1.0",
            "active_user_id": self._active_user_id,
            "profiles": self._profiles,
        }
        self._calibration_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._calibration_path.with_suffix(self._calibration_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._calibration_path)
