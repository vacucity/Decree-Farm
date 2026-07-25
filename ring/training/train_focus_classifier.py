"""Train and evaluate a session-isolated binary focus classifier."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.base import clone
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import GroupKFold

from ring_bridge.classifier.features import (
    DEFAULT_MAX_GAP_MS,
    DEFAULT_STEP_SAMPLES,
    DEFAULT_WINDOW_SAMPLES,
    FEATURE_NAMES,
    extract_feature_vector,
    iter_contiguous_windows,
)

LABEL_TO_INT = {"distracted": 0, "focused": 1}
INT_TO_LABEL = {value: key for key, value in LABEL_TO_INT.items()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data.db")
    parser.add_argument("--model", default="models/focus_classifier.joblib")
    parser.add_argument("--report", default="models/focus_classifier_report.json")
    parser.add_argument("--window-samples", type=int, default=500)
    parser.add_argument("--step-samples", type=int, default=DEFAULT_STEP_SAMPLES)
    parser.add_argument("--max-gap-ms", type=int, default=DEFAULT_MAX_GAP_MS)
    parser.add_argument("--seed", type=int, default=20260724)
    return parser.parse_args()


def load_windows(
    db_path: Path,
    *,
    window_samples: int,
    step_samples: int,
    max_gap_ms: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[dict[str, Any]]]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    sessions = connection.execute(
        """SELECT id,label,user_id,task_type,hand,orientation,sample_count
           FROM imu_capture_sessions
           WHERE label IN ('focused','distracted') AND ended_at_ms IS NOT NULL
           ORDER BY id"""
    ).fetchall()

    x_rows: list[np.ndarray] = []
    y_rows: list[int] = []
    session_groups: list[int] = []
    user_groups: list[str] = []
    session_summary: list[dict[str, Any]] = []
    for session in sessions:
        samples = connection.execute(
            """SELECT device_timestamp_ms,sequence,accel_x,accel_y,accel_z,
                      gyro_x,gyro_y,gyro_z
               FROM labeled_imu_samples WHERE session_id=? ORDER BY id""",
            (session["id"],),
        ).fetchall()
        timestamps = np.asarray([row[0] for row in samples], dtype=np.int64)
        sequences = np.asarray([row[1] for row in samples], dtype=np.int64)
        values = np.asarray([row[2:] for row in samples], dtype=np.float64)
        windows = list(
            iter_contiguous_windows(
                timestamps,
                values,
                sequences=sequences,
                window_samples=window_samples,
                step_samples=step_samples,
                max_gap_ms=max_gap_ms,
            )
        )
        for window in windows:
            x_rows.append(extract_feature_vector(window))
            y_rows.append(LABEL_TO_INT[session["label"]])
            session_groups.append(int(session["id"]))
            user_groups.append(str(session["user_id"]))
        session_summary.append(
            {
                "session_id": int(session["id"]),
                "label": str(session["label"]),
                "user_id": str(session["user_id"]),
                "task_type": str(session["task_type"]),
                "raw_samples": len(samples),
                "windows": len(windows),
            }
        )
    connection.close()
    if not x_rows:
        raise RuntimeError("no valid training windows found")
    return (
        np.vstack(x_rows),
        np.asarray(y_rows, dtype=np.int8),
        np.asarray(session_groups, dtype=np.int32),
        np.asarray(user_groups, dtype=object),
        session_summary,
    )


def stratified_session_split(
    y: np.ndarray, sessions: np.ndarray, *, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    session_label = {
        int(session): int(y[np.flatnonzero(sessions == session)[0]])
        for session in np.unique(sessions)
    }
    train_sessions: list[int] = []
    validation_sessions: list[int] = []
    test_sessions: list[int] = []
    for label in sorted(set(session_label.values())):
        ids = np.asarray(
            [session for session, value in session_label.items() if value == label]
        )
        rng.shuffle(ids)
        test_count = max(1, int(round(len(ids) * 0.20)))
        validation_count = max(1, int(round(len(ids) * 0.15)))
        test_sessions.extend(ids[:test_count].tolist())
        validation_sessions.extend(
            ids[test_count : test_count + validation_count].tolist()
        )
        train_sessions.extend(ids[test_count + validation_count :].tolist())
    return (
        np.isin(sessions, train_sessions),
        np.isin(sessions, validation_sessions),
        np.isin(sessions, test_sessions),
    )


def build_estimator(seed: int) -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=500,
        max_depth=14,
        min_samples_leaf=2,
        max_features="sqrt",
        class_weight="balanced_subsample",
        random_state=seed,
        # Single-process training is deterministic and also works inside the
        # restricted Windows runtime used by the local management panel.
        n_jobs=1,
    )


def metrics(
    estimator: Any,
    x: np.ndarray,
    y: np.ndarray,
    *,
    threshold: float = 0.5,
    sessions: np.ndarray | None = None,
    smoothing_predictions: int = 1,
) -> dict[str, Any]:
    probability = estimator.predict_proba(x)[:, LABEL_TO_INT["focused"]]
    if sessions is not None and smoothing_predictions > 1:
        smoothed = probability.copy()
        for session in np.unique(sessions):
            indices = np.flatnonzero(sessions == session)
            for offset, index in enumerate(indices):
                start = max(0, offset - smoothing_predictions + 1)
                smoothed[index] = np.median(probability[indices[start : offset + 1]])
        probability = smoothed
    predicted = probability >= threshold
    result = {
        "windows": int(len(y)),
        "accuracy": round(float(accuracy_score(y, predicted)), 4),
        "balanced_accuracy": round(float(balanced_accuracy_score(y, predicted)), 4),
        "precision_focused": round(
            float(precision_score(y, predicted, pos_label=1, zero_division=0)), 4
        ),
        "recall_focused": round(
            float(recall_score(y, predicted, pos_label=1, zero_division=0)), 4
        ),
        "recall_distracted": round(
            float(recall_score(y, predicted, pos_label=0, zero_division=0)), 4
        ),
        "f1_focused": round(
            float(f1_score(y, predicted, pos_label=1, zero_division=0)), 4
        ),
        "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
    }
    if len(np.unique(y)) == 2:
        result["roc_auc"] = round(float(roc_auc_score(y, probability)), 4)
    return result


def tune_threshold(
    estimator: Any,
    x: np.ndarray,
    y: np.ndarray,
    sessions: np.ndarray,
    *,
    smoothing_predictions: int,
) -> float:
    probability = estimator.predict_proba(x)[:, LABEL_TO_INT["focused"]]
    smoothed = probability.copy()
    if smoothing_predictions > 1:
        for session in np.unique(sessions):
            indices = np.flatnonzero(sessions == session)
            for offset, index in enumerate(indices):
                start = max(0, offset - smoothing_predictions + 1)
                smoothed[index] = np.median(probability[indices[start : offset + 1]])
    best_score, best_threshold = -1.0, 0.5
    for threshold in np.linspace(0.05, 0.95, 181):
        score = balanced_accuracy_score(y, smoothed >= threshold)
        if score > best_score:
            best_score, best_threshold = float(score), float(threshold)
    return best_threshold


def main() -> None:
    args = parse_args()
    db_path = Path(args.db).resolve()
    model_path = Path(args.model)
    report_path = Path(args.report)
    x, y, sessions, users, session_summary = load_windows(
        db_path,
        window_samples=args.window_samples,
        step_samples=args.step_samples,
        max_gap_ms=args.max_gap_ms,
    )
    dynamic_suffixes = (
        "_std",
        "_range",
        "_iqr",
        "_mean_abs_diff",
        "_std_diff",
        "_p95_abs_diff",
    )
    selected_feature_indices = np.asarray(
        [
            index
            for index, name in enumerate(FEATURE_NAMES)
            if name.endswith(dynamic_suffixes) or name.startswith("corr_")
        ],
        dtype=np.int32,
    )
    selected_feature_names = tuple(
        FEATURE_NAMES[index] for index in selected_feature_indices
    )
    x = x[:, selected_feature_indices]

    train_mask, validation_mask, test_mask = stratified_session_split(
        y, sessions, seed=args.seed
    )
    holdout_estimator = build_estimator(args.seed)
    holdout_estimator.fit(x[train_mask], y[train_mask])
    smoothing_predictions = 7
    decision_threshold = tune_threshold(
        holdout_estimator,
        x[validation_mask],
        y[validation_mask],
        sessions[validation_mask],
        smoothing_predictions=smoothing_predictions,
    )

    user_folds: list[dict[str, Any]] = []
    unique_users = np.unique(users)
    if len(unique_users) >= 2:
        splitter = GroupKFold(n_splits=len(unique_users))
        for fold, (train_indices, test_indices) in enumerate(
            splitter.split(x, y, groups=users), start=1
        ):
            estimator = clone(holdout_estimator)
            estimator.fit(x[train_indices], y[train_indices])
            fold_result = metrics(
                estimator,
                x[test_indices],
                y[test_indices],
                threshold=decision_threshold,
                sessions=sessions[test_indices],
                smoothing_predictions=smoothing_predictions,
            )
            fold_result["fold"] = fold
            fold_result["held_out_users"] = sorted(
                set(str(item) for item in users[test_indices])
            )
            user_folds.append(fold_result)

    final_estimator = build_estimator(args.seed)
    final_estimator.fit(x, y)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema_version": "1.0",
        "model_version": "focus-rf-v1-20260724",
        "model_type": "random_forest_window_features",
        "estimator": final_estimator,
        "label_to_int": LABEL_TO_INT,
        "int_to_label": INT_TO_LABEL,
        "feature_names": selected_feature_names,
        "feature_indices": selected_feature_indices.tolist(),
        "window_samples": args.window_samples,
        "step_samples": args.step_samples,
        "max_gap_ms": args.max_gap_ms,
        "sample_rate_hz": 100,
        "decision_threshold": decision_threshold,
        "smoothing_predictions": smoothing_predictions,
        "training_database": db_path.name,
        "training_windows": int(len(y)),
        "training_sessions": int(len(np.unique(sessions))),
        "training_users": int(len(unique_users)),
    }
    joblib.dump(artifact, model_path)

    report = {
        "model_type": artifact["model_type"],
        "database": str(db_path),
        "raw_stored_samples": int(
            sum(item["raw_samples"] for item in session_summary)
        ),
        "windows": int(len(y)),
        "features": int(x.shape[1]),
        "sessions": int(len(np.unique(sessions))),
        "users": sorted(str(user) for user in unique_users),
        "label_windows": {
            INT_TO_LABEL[int(label)]: int(count)
            for label, count in Counter(y.tolist()).items()
        },
        "split": {
            "train_sessions": sorted(
                int(item) for item in np.unique(sessions[train_mask])
            ),
            "validation_sessions": sorted(
                int(item) for item in np.unique(sessions[validation_mask])
            ),
            "test_sessions": sorted(
                int(item) for item in np.unique(sessions[test_mask])
            ),
        },
        "validation": metrics(
            holdout_estimator,
            x[validation_mask],
            y[validation_mask],
            threshold=decision_threshold,
            sessions=sessions[validation_mask],
            smoothing_predictions=smoothing_predictions,
        ),
        "test": metrics(
            holdout_estimator,
            x[test_mask],
            y[test_mask],
            threshold=decision_threshold,
            sessions=sessions[test_mask],
            smoothing_predictions=smoothing_predictions,
        ),
        "decision_threshold": decision_threshold,
        "smoothing_predictions": smoothing_predictions,
        "leave_one_user_out": user_folds,
        "session_summary": session_summary,
        "artifact": str(model_path.resolve()),
    }
    if user_folds:
        report["leave_one_user_out_average"] = {
            key: round(float(np.mean([fold[key] for fold in user_folds])), 4)
            for key in (
                "accuracy",
                "balanced_accuracy",
                "recall_focused",
                "recall_distracted",
                "f1_focused",
                "roc_auc",
            )
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
