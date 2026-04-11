#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SharedState: Thread-safe state container replacing Qt Signals.

Workers write via set_*() methods (which trigger Events).
Server/Display read via get_*() methods + Event.wait(timeout).
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np


class Mode(str, Enum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    DETECTING = "DETECTING"
    TRAINING = "TRAINING"
    EVALUATING = "EVALUATING"


@dataclass
class DetectionResult:
    a: float = float('nan')
    b: float = float('nan')
    fps: float = 0.0
    infer_time_ms: float = 0.0
    serial_status: str = "disabled"
    serial_count: int = 0


@dataclass
class TrainingStatus:
    running: bool = False
    epoch: int = 0
    total_epochs: int = 0
    loss: float = 0.0
    phase: str = ""
    new_model_path: str = ""


@dataclass
class EvaluationStatus:
    running: bool = False
    current_frame: int = 0
    total_frames: int = 0
    phase: str = ""
    model_name: str = ""
    avg_iou: float = 0.0


class SharedState:
    """Thread-safe state container. Single lock to avoid deadlocks."""

    def __init__(self, save_dir: str = "~/zed_records"):
        self._lock = threading.Lock()

        # Mode
        self._mode: Mode = Mode.IDLE

        # Frames (BGR numpy arrays)
        self._preview_frame: Optional[np.ndarray] = None
        self._annotated_frame: Optional[np.ndarray] = None

        # Detection
        self._detection = DetectionResult()

        # Recording
        self._recording_start: Optional[float] = None
        self._recording_session_dir: str = ""

        # Test image detection
        self._test_image_path: Optional[str] = None

        # Training
        self._training = TrainingStatus()

        # Evaluation
        self._evaluation = EvaluationStatus()

        # Log ring buffer
        self._log_entries: deque = deque(maxlen=200)

        # Save dir
        self.save_dir = save_dir

        # Camera info
        self._camera_opened: bool = False
        self._imu_available: bool = False

        # Client time sync (offset = client_epoch - time.time())
        # None until at least one client has called /api/time/sync.
        self._time_offset: Optional[float] = None
        self._time_offset_updated_at: Optional[float] = None

        # Events for change notification
        self.mode_changed = threading.Event()
        self.detection_updated = threading.Event()
        self.training_updated = threading.Event()
        self.evaluation_updated = threading.Event()
        self.frame_updated = threading.Event()
        self.log_updated = threading.Event()

    # ----------------------------------------------------------------
    # Mode
    # ----------------------------------------------------------------
    def get_mode(self) -> Mode:
        with self._lock:
            return self._mode

    def set_mode(self, mode: Mode):
        with self._lock:
            self._mode = mode
        self.mode_changed.set()

    # ----------------------------------------------------------------
    # Preview frame (from camera)
    # ----------------------------------------------------------------
    def get_preview_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._preview_frame is None:
                return None
            return self._preview_frame.copy()

    def set_preview_frame(self, frame: np.ndarray):
        with self._lock:
            self._preview_frame = frame
        self.frame_updated.set()

    # ----------------------------------------------------------------
    # Annotated frame (from inference)
    # ----------------------------------------------------------------
    def get_annotated_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._annotated_frame is None:
                return None
            return self._annotated_frame.copy()

    def set_annotated_frame(self, frame: np.ndarray):
        with self._lock:
            self._annotated_frame = frame
        self.frame_updated.set()

    # ----------------------------------------------------------------
    # Display frame (annotated if detecting, else preview)
    # ----------------------------------------------------------------
    def get_display_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._mode == Mode.DETECTING and self._annotated_frame is not None:
                return self._annotated_frame.copy()
            if self._preview_frame is not None:
                return self._preview_frame.copy()
            return None

    # ----------------------------------------------------------------
    # Detection result
    # ----------------------------------------------------------------
    def get_detection(self) -> DetectionResult:
        with self._lock:
            return DetectionResult(
                a=self._detection.a,
                b=self._detection.b,
                fps=self._detection.fps,
                infer_time_ms=self._detection.infer_time_ms,
                serial_status=self._detection.serial_status,
                serial_count=self._detection.serial_count,
            )

    def set_detection(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._detection, k):
                    setattr(self._detection, k, v)
        self.detection_updated.set()

    # ----------------------------------------------------------------
    # Recording
    # ----------------------------------------------------------------
    def get_recording_info(self) -> tuple:
        """Returns (start_monotonic, session_dir)."""
        with self._lock:
            return self._recording_start, self._recording_session_dir

    def set_recording_info(self, start: Optional[float], session_dir: str = ""):
        with self._lock:
            self._recording_start = start
            self._recording_session_dir = session_dir

    # ----------------------------------------------------------------
    # Test Image Detection
    # ----------------------------------------------------------------
    def get_test_image_path(self) -> Optional[str]:
        with self._lock:
            return self._test_image_path

    def set_test_image_path(self, path: Optional[str]):
        with self._lock:
            self._test_image_path = path

    # ----------------------------------------------------------------
    # Training
    # ----------------------------------------------------------------
    def get_training(self) -> TrainingStatus:
        with self._lock:
            return TrainingStatus(
                running=self._training.running,
                epoch=self._training.epoch,
                total_epochs=self._training.total_epochs,
                loss=self._training.loss,
                phase=self._training.phase,
                new_model_path=self._training.new_model_path,
            )

    def set_training(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._training, k):
                    setattr(self._training, k, v)
        self.training_updated.set()

    # ----------------------------------------------------------------
    # Evaluation
    # ----------------------------------------------------------------
    def get_evaluation(self) -> EvaluationStatus:
        with self._lock:
            return EvaluationStatus(
                running=self._evaluation.running,
                current_frame=self._evaluation.current_frame,
                total_frames=self._evaluation.total_frames,
                phase=self._evaluation.phase,
                model_name=self._evaluation.model_name,
                avg_iou=self._evaluation.avg_iou,
            )

    def set_evaluation(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._evaluation, k):
                    setattr(self._evaluation, k, v)
        self.evaluation_updated.set()

    # ----------------------------------------------------------------
    # Camera info
    # ----------------------------------------------------------------
    def get_camera_info(self) -> dict:
        with self._lock:
            return {
                "opened": self._camera_opened,
                "imu_available": self._imu_available,
            }

    def set_camera_opened(self, opened: bool):
        with self._lock:
            self._camera_opened = opened

    def set_imu_available(self, available: bool):
        with self._lock:
            self._imu_available = available

    # ----------------------------------------------------------------
    # Client time sync
    # ----------------------------------------------------------------
    def set_time_offset(self, client_epoch: float) -> float:
        """Store offset such that corrected_now() ≈ client wall-clock time.

        Last-write-wins across multiple clients.
        Returns the computed offset.
        """
        offset = client_epoch - time.time()
        with self._lock:
            self._time_offset = offset
            self._time_offset_updated_at = time.time()
        return offset

    def get_time_offset(self) -> Optional[float]:
        with self._lock:
            return self._time_offset

    def corrected_now(self) -> Optional[float]:
        """Returns current epoch corrected by client offset, or None if unsynced."""
        with self._lock:
            if self._time_offset is None:
                return None
            return time.time() + self._time_offset

    def get_time_sync_info(self) -> dict:
        with self._lock:
            return {
                "synced": self._time_offset is not None,
                "offset": self._time_offset,
                "updated_at": self._time_offset_updated_at,
            }

    # ----------------------------------------------------------------
    # Log
    # ----------------------------------------------------------------
    def append_log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self._log_entries.append(f"[{ts}] {message}")
        self.log_updated.set()

    def get_logs(self, last_n: int = 50) -> list:
        with self._lock:
            entries = list(self._log_entries)
        return entries[-last_n:]

    # ----------------------------------------------------------------
    # Snapshot (for API /status)
    # ----------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            rec_start = self._recording_start
            rec_elapsed = 0.0
            if rec_start is not None:
                rec_elapsed = time.monotonic() - rec_start

            return {
                "mode": self._mode.value,
                "camera_opened": self._camera_opened,
                "imu_available": self._imu_available,
                "detection": {
                    "a": self._detection.a,
                    "b": self._detection.b,
                    "fps": self._detection.fps,
                    "infer_time_ms": self._detection.infer_time_ms,
                    "serial_status": self._detection.serial_status,
                    "serial_count": self._detection.serial_count,
                },
                "recording": {
                    "active": rec_start is not None,
                    "elapsed_s": rec_elapsed,
                    "session_dir": self._recording_session_dir,
                },
                "test_image_path": self._test_image_path,
                "time_sync": {
                    "synced": self._time_offset is not None,
                    "offset": self._time_offset,
                },
                "training": {
                    "running": self._training.running,
                    "epoch": self._training.epoch,
                    "total_epochs": self._training.total_epochs,
                    "loss": self._training.loss,
                    "phase": self._training.phase,
                },
                "evaluation": {
                    "running": self._evaluation.running,
                    "current_frame": self._evaluation.current_frame,
                    "total_frames": self._evaluation.total_frames,
                    "phase": self._evaluation.phase,
                    "model_name": self._evaluation.model_name,
                    "avg_iou": self._evaluation.avg_iou,
                },
            }
