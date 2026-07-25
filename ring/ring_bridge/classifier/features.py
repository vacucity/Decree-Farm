"""Windowing and orientation-tolerant features for six-axis ring IMU data."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

DEFAULT_SAMPLE_RATE_HZ = 100
DEFAULT_WINDOW_SAMPLES = 3 * DEFAULT_SAMPLE_RATE_HZ
DEFAULT_STEP_SAMPLES = DEFAULT_SAMPLE_RATE_HZ
DEFAULT_MAX_GAP_MS = 50

AXIS_NAMES = ("ax", "ay", "az", "gx", "gy", "gz")
SIGNAL_NAMES = AXIS_NAMES + ("accel_mag", "gyro_mag")
STAT_NAMES = (
    "mean",
    "std",
    "min",
    "max",
    "range",
    "median",
    "iqr",
    "rms",
    "mean_abs_diff",
    "std_diff",
    "p95_abs",
    "p95_abs_diff",
)
CORRELATION_NAMES = tuple(
    f"corr_{AXIS_NAMES[left]}_{AXIS_NAMES[right]}"
    for left in range(len(AXIS_NAMES))
    for right in range(left + 1, len(AXIS_NAMES))
)
FEATURE_NAMES = tuple(
    f"{signal}_{stat}" for signal in SIGNAL_NAMES for stat in STAT_NAMES
) + CORRELATION_NAMES


def iter_contiguous_windows(
    timestamps_ms: np.ndarray,
    values: np.ndarray,
    *,
    sequences: np.ndarray | None = None,
    window_samples: int = DEFAULT_WINDOW_SAMPLES,
    step_samples: int = DEFAULT_STEP_SAMPLES,
    max_gap_ms: int = DEFAULT_MAX_GAP_MS,
) -> Iterator[np.ndarray]:
    """Yield fixed-size windows without crossing packet gaps or clock resets."""
    timestamps_ms = np.asarray(timestamps_ms, dtype=np.int64)
    values = np.asarray(values, dtype=np.float64)
    if timestamps_ms.ndim != 1 or values.ndim != 2 or values.shape[1] != 6:
        raise ValueError("expected timestamps [n] and six-axis values [n, 6]")
    if len(timestamps_ms) != len(values):
        raise ValueError("timestamp and value lengths differ")
    if sequences is not None:
        sequences = np.asarray(sequences, dtype=np.int64)
        if sequences.shape != timestamps_ms.shape:
            raise ValueError("sequence and timestamp lengths differ")
    if window_samples < 2 or step_samples < 1:
        raise ValueError("invalid window configuration")
    if len(values) < window_samples:
        return

    diffs = np.diff(timestamps_ms)
    # V2 batches can contain two adjacent samples with the same device
    # timestamp even though their sequence numbers remain continuous. A zero
    # timestamp delta is therefore valid; clock rollback and large gaps are not.
    discontinuity = (diffs < 0) | (diffs > max_gap_ms)
    if sequences is not None:
        discontinuity |= np.diff(sequences) != 1
    boundaries = np.flatnonzero(discontinuity) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [len(values)]))
    for chunk_start, chunk_end in zip(starts, ends):
        for start in range(
            int(chunk_start),
            int(chunk_end) - window_samples + 1,
            step_samples,
        ):
            yield values[start : start + window_samples]


def _safe_correlation(left: np.ndarray, right: np.ndarray) -> float:
    if np.std(left) < 1e-9 or np.std(right) < 1e-9:
        return 0.0
    value = float(np.corrcoef(left, right)[0, 1])
    return value if np.isfinite(value) else 0.0


def extract_feature_vector(window: np.ndarray) -> np.ndarray:
    """Convert one [samples, 6] raw IMU window to a stable feature vector."""
    window = np.asarray(window, dtype=np.float64)
    if window.ndim != 2 or window.shape[1] != 6 or len(window) < 2:
        raise ValueError("expected at least two six-axis samples")

    accel_mag = np.linalg.norm(window[:, :3], axis=1)
    gyro_mag = np.linalg.norm(window[:, 3:], axis=1)
    signals = [window[:, index] for index in range(6)] + [accel_mag, gyro_mag]
    features: list[float] = []
    for signal in signals:
        diff = np.diff(signal)
        q25, q75 = np.percentile(signal, [25, 75])
        features.extend(
            [
                float(np.mean(signal)),
                float(np.std(signal)),
                float(np.min(signal)),
                float(np.max(signal)),
                float(np.ptp(signal)),
                float(np.median(signal)),
                float(q75 - q25),
                float(np.sqrt(np.mean(np.square(signal)))),
                float(np.mean(np.abs(diff))),
                float(np.std(diff)),
                float(np.percentile(np.abs(signal), 95)),
                float(np.percentile(np.abs(diff), 95)),
            ]
        )

    for left in range(6):
        for right in range(left + 1, 6):
            features.append(_safe_correlation(window[:, left], window[:, right]))

    result = np.asarray(features, dtype=np.float32)
    if result.shape != (len(FEATURE_NAMES),):
        raise RuntimeError("feature schema mismatch")
    return result
