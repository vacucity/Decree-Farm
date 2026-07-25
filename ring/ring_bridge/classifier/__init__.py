"""Trainable focus-classification helpers shared by training and inference."""

from .features import (
    DEFAULT_MAX_GAP_MS,
    DEFAULT_SAMPLE_RATE_HZ,
    DEFAULT_STEP_SAMPLES,
    DEFAULT_WINDOW_SAMPLES,
    FEATURE_NAMES,
    extract_feature_vector,
    iter_contiguous_windows,
)

__all__ = [
    "DEFAULT_MAX_GAP_MS",
    "DEFAULT_SAMPLE_RATE_HZ",
    "DEFAULT_STEP_SAMPLES",
    "DEFAULT_WINDOW_SAMPLES",
    "FEATURE_NAMES",
    "extract_feature_vector",
    "iter_contiguous_windows",
]
