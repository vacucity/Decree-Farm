"""Store manually labeled raw IMU segments and export them as CSV."""
from __future__ import annotations

import asyncio
import csv
import io
import sqlite3
import time
from pathlib import Path
from typing import Any

from .event_bus import EventBus
from .models import IMUSample

# The current product is a binary classifier. Keep the legacy label readable
# in old sessions/exports, but do not allow creating new uncertain samples.
VALID_LABELS = {"focused", "distracted"}
LEGACY_LABELS = {"uncertain"}
VALID_HANDS = {"left", "right"}
VALID_ORIENTATIONS = {"neutral", "inward", "outward", "unknown"}

class LabeledIMUCapture:
    """Persist labeled capture sessions and their six-axis samples."""

    def __init__(self, bus: EventBus, db_path: str | Path = "data.db"):
        self._bus = bus
        self._db_path = Path(db_path)
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._active: dict[str, Any] | None = None
        self._buffer: list[tuple[int, ...]] = []
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            str(self._db_path), timeout=5.0, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._bus.subscribe("imu:sample", self._on_sample)
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        async with self._lock:
            if self._active:
                self._stop_active_locked()
            self._flush_locked()
        self._bus.unsubscribe("imu:sample", self._on_sample)
        if self._db:
            self._db.close()
            self._db = None
        self._running = False

    async def start_capture(
        self, *, label: str, user_id: str, task_type: str, hand: str,
        orientation: str, notes: str = "",
    ) -> dict[str, Any]:
        label, hand = label.strip().lower(), hand.strip().lower()
        orientation = orientation.strip().lower()
        user_id, task_type, notes = user_id.strip(), task_type.strip(), notes.strip()
        if label not in VALID_LABELS:
            raise ValueError("label must be focused or distracted")
        if not user_id:
            raise ValueError("user_id is required")
        if not task_type:
            raise ValueError("task_type is required")
        if hand not in VALID_HANDS:
            raise ValueError("hand must be left or right")
        if orientation not in VALID_ORIENTATIONS:
            raise ValueError("invalid ring orientation")
        if len(user_id) > 80 or len(task_type) > 120 or len(notes) > 500:
            raise ValueError("capture metadata is too long")
        async with self._lock:
            self._require_db()
            if self._active:
                raise RuntimeError("a capture session is already active")
            started = int(time.time() * 1000)
            cursor = self._db.execute(
                """INSERT INTO imu_capture_sessions
                   (label,user_id,task_type,hand,orientation,notes,started_at_ms,sample_count)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (label, user_id, task_type, hand, orientation, notes, started),
            )
            self._active = {
                "id": int(cursor.lastrowid), "label": label, "user_id": user_id,
                "task_type": task_type, "hand": hand, "orientation": orientation,
                "notes": notes, "started_at_ms": started, "sample_count": 0,
            }
            return dict(self._active)

    async def stop_capture(self) -> dict[str, Any]:
        async with self._lock:
            if not self._active:
                raise RuntimeError("there is no active capture session")
            return self._stop_active_locked()

    async def delete_session(self, session_id: int) -> dict[str, int]:
        if session_id <= 0:
            raise ValueError("session_id must be a positive integer")
        async with self._lock:
            self._require_db()
            if self._active and self._active["id"] == session_id:
                raise RuntimeError("the active capture session cannot be deleted")
            exists = self._db.execute(
                "SELECT 1 FROM imu_capture_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not exists:
                raise LookupError("capture session not found")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                samples = self._db.execute(
                    "DELETE FROM labeled_imu_samples WHERE session_id=?",
                    (session_id,),
                ).rowcount
                sessions = self._db.execute(
                    "DELETE FROM imu_capture_sessions WHERE id=?",
                    (session_id,),
                ).rowcount
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return {
                "session_id": session_id,
                "deleted_sessions": int(sessions),
                "deleted_samples": int(samples),
            }

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            self._require_db()
            active = dict(self._active) if self._active else None
            if active:
                active["elapsed_ms"] = int(time.time() * 1000) - active["started_at_ms"]
            summary = {name: {"sessions": 0, "samples": 0} for name in VALID_LABELS}
            legacy_summary = {
                name: {"sessions": 0, "samples": 0} for name in LEGACY_LABELS
            }
            for row in self._db.execute(
                """SELECT label, COUNT(*) sessions, COALESCE(SUM(sample_count),0) samples
                   FROM imu_capture_sessions GROUP BY label"""
            ):
                if row["label"] in summary:
                    summary[row["label"]] = {
                        "sessions": int(row["sessions"]),
                        "samples": int(row["samples"]),
                    }
                elif row["label"] in legacy_summary:
                    legacy_summary[row["label"]] = {
                        "sessions": int(row["sessions"]),
                        "samples": int(row["samples"]),
                    }
            recent = [dict(row) for row in self._db.execute(
                """SELECT id,label,user_id,task_type,hand,orientation,notes,
                          started_at_ms,ended_at_ms,sample_count
                   FROM imu_capture_sessions ORDER BY id DESC LIMIT 20"""
            )]
            return {
                "active": active,
                "summary": summary,
                "legacy_summary": legacy_summary,
                "recent": recent,
                "db_path": str(self._db_path.resolve()),
            }

    async def export_csv(self) -> str:
        async with self._lock:
            self._require_db()
            self._flush_locked()
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow([
                "session_id","label","user_id","task_type","hand","orientation",
                "notes","session_started_at_ms","session_ended_at_ms",
                "device_timestamp_ms","sequence","accel_x","accel_y","accel_z",
                "gyro_x","gyro_y","gyro_z","received_at_ms",
            ])
            writer.writerows(self._db.execute(
                """SELECT s.id,s.label,s.user_id,s.task_type,s.hand,s.orientation,
                          s.notes,s.started_at_ms,s.ended_at_ms,
                          i.device_timestamp_ms,i.sequence,i.accel_x,i.accel_y,
                          i.accel_z,i.gyro_x,i.gyro_y,i.gyro_z,i.received_at_ms
                   FROM labeled_imu_samples i
                   JOIN imu_capture_sessions s ON s.id=i.session_id
                   ORDER BY i.id"""
            ))
            return output.getvalue()

    async def _on_sample(self, sample: IMUSample) -> None:
        if not self._active:
            return
        async with self._lock:
            if not self._active or not self._db:
                return
            self._buffer.append((
                self._active["id"], int(sample.timestamp_ms), int(sample.sequence),
                int(sample.accel_x), int(sample.accel_y), int(sample.accel_z),
                int(sample.gyro_x), int(sample.gyro_y), int(sample.gyro_z),
                int(time.time() * 1000),
            ))
            self._active["sample_count"] += 1
            if len(self._buffer) >= 25:
                self._flush_locked()

    def _stop_active_locked(self) -> dict[str, Any]:
        assert self._active is not None
        self._flush_locked()
        ended = int(time.time() * 1000)
        self._db.execute(
            "UPDATE imu_capture_sessions SET ended_at_ms=?,sample_count=? WHERE id=?",
            (ended, self._active["sample_count"], self._active["id"]),
        )
        result = {
            **self._active,
            "ended_at_ms": ended,
            "elapsed_ms": ended - self._active["started_at_ms"],
        }
        self._active = None
        return result

    def _flush_locked(self) -> None:
        if not self._buffer or not self._db:
            return
        self._db.executemany(
            """INSERT INTO labeled_imu_samples
               (session_id,device_timestamp_ms,sequence,accel_x,accel_y,accel_z,
                gyro_x,gyro_y,gyro_z,received_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            self._buffer,
        )
        if self._active:
            self._db.execute(
                "UPDATE imu_capture_sessions SET sample_count=? WHERE id=?",
                (self._active["sample_count"], self._active["id"]),
            )
        self._buffer.clear()

    def _init_schema(self) -> None:
        self._require_db()
        self._db.executescript(
            """CREATE TABLE IF NOT EXISTS imu_capture_sessions (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 label TEXT NOT NULL,
                 user_id TEXT NOT NULL,
                 task_type TEXT NOT NULL,
                 hand TEXT NOT NULL,
                 orientation TEXT NOT NULL,
                 notes TEXT NOT NULL DEFAULT '',
                 started_at_ms INTEGER NOT NULL,
                 ended_at_ms INTEGER,
                 sample_count INTEGER NOT NULL DEFAULT 0);
               CREATE TABLE IF NOT EXISTS labeled_imu_samples (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 session_id INTEGER NOT NULL,
                 device_timestamp_ms INTEGER NOT NULL,
                 sequence INTEGER NOT NULL,
                 accel_x INTEGER NOT NULL,
                 accel_y INTEGER NOT NULL,
                 accel_z INTEGER NOT NULL,
                 gyro_x INTEGER NOT NULL,
                 gyro_y INTEGER NOT NULL,
                 gyro_z INTEGER NOT NULL,
                 received_at_ms INTEGER NOT NULL,
                 FOREIGN KEY(session_id) REFERENCES imu_capture_sessions(id));
               CREATE INDEX IF NOT EXISTS idx_labeled_imu_session
                 ON labeled_imu_samples(session_id);"""
        )

    def _require_db(self) -> None:
        if self._db is None:
            raise RuntimeError("capture service is not running")
