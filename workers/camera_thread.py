#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CameraThread: Continuously grabs frames from ZED camera.
Converted from QThread to threading.Thread.

Modes (read from SharedState):
  - IDLE: grab + preview
  - RECORDING: grab + SVO2 + IMU CSV + random frame capture + preview
  - DETECTING: grab + push to inference queue + preview
"""

import csv
import os
import queue
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    import pyzed.sl as sl
except Exception:
    print("Failed to import pyzed.sl. Ensure ZED SDK + Python API are installed.")
    raise

from state.shared_state import SharedState, Mode


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def expand_user(path_str: str) -> str:
    return os.path.expanduser(path_str)


def ensure_dir(path_str: str) -> None:
    Path(path_str).mkdir(parents=True, exist_ok=True)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sl_time_to_ns(t) -> int:
    try:
        return int(t.get_nanoseconds())
    except Exception:
        return int(t.get_milliseconds() * 1_000_000)


def get_resolution_enum(resolution_str: str):
    resolution_map = {
        'VGA': sl.RESOLUTION.VGA,
        'HD720': sl.RESOLUTION.HD720,
        'HD1080': sl.RESOLUTION.HD1080,
        'HD2K': sl.RESOLUTION.HD2K,
    }
    return resolution_map.get(resolution_str, sl.RESOLUTION.HD720)


# ---------------------------------------------------------------------------
# CameraThread
# ---------------------------------------------------------------------------
class CameraThread(threading.Thread):
    """Continuously grabs frames from the ZED camera."""

    def __init__(
        self,
        state: SharedState,
        save_dir: str = "~/zed_records",
        camera_fps: int = 30,
        camera_resolution: str = "HD720",
        preview_fps: int = 15,
        process_width: int = 640,
        inference_queue: Optional[queue.Queue] = None,
        capture_probability: float = 0.02,
    ):
        super().__init__(daemon=True)
        self._state = state
        self.save_dir = expand_user(save_dir)
        self.camera_fps = camera_fps
        self.camera_resolution = camera_resolution
        self.preview_fps = max(1, int(preview_fps))
        self.process_width = process_width
        self.inference_queue = inference_queue
        self._capture_probability = capture_probability

        self._stop_flag = False

        # ZED objects
        self.zed = sl.Camera()
        self.runtime = sl.RuntimeParameters()
        self.image_left = sl.Mat()

        # Recording state
        self._recording = False
        self._recording_start_monotonic: Optional[float] = None
        self._session_dir: Optional[str] = None
        self._csv_file = None
        self._csv_writer = None
        self._imu_available = False
        self._frame_count = 0

        # Detecting state
        self._detecting = False

        # Preview timing
        self._last_preview_monotonic = 0.0

        # Camera info
        self.orig_w = 0
        self.orig_h = 0
        self.process_h = 0
        self.scale_factor = 1.0

    # ----------------------------------------------------------------
    # Public controls (called from ModeManager callbacks)
    # ----------------------------------------------------------------
    def request_stop(self):
        self._stop_flag = True

    def start_recording(self):
        if self._recording:
            self._state.append_log("Already recording.")
            return

        records_dir = os.path.join(self.save_dir, "records")
        ensure_dir(records_dir)
        stamp = now_stamp()
        session_dir = os.path.join(records_dir, stamp)
        ensure_dir(session_dir)
        frames_dir = os.path.join(session_dir, "frames")
        ensure_dir(frames_dir)
        labels_dir = os.path.join(session_dir, "labels")
        ensure_dir(labels_dir)

        svo_path = os.path.join(session_dir, "recording.svo2")

        rec_params = sl.RecordingParameters()
        rec_params.video_filename = svo_path
        rec_params.compression_mode = sl.SVO_COMPRESSION_MODE.LOSSLESS

        err = self.zed.enable_recording(rec_params)
        if err != sl.ERROR_CODE.SUCCESS:
            self._state.append_log(f"ERROR: enable_recording failed: {repr(err)}")
            return

        self._session_dir = session_dir
        self._recording = True
        self._recording_start_monotonic = time.monotonic()
        self._frame_count = 0
        self._state.set_recording_info(self._recording_start_monotonic, session_dir)
        self._state.append_log(f"Recording started: {svo_path}")

        # IMU CSV
        if self._imu_available:
            csv_path = os.path.join(session_dir, "imu.csv")
            try:
                self._csv_file = open(csv_path, "w", newline="")
                self._csv_writer = csv.writer(self._csv_file)
                self._csv_writer.writerow([
                    "ts_ns",
                    "accel_x", "accel_y", "accel_z",
                    "gyro_x", "gyro_y", "gyro_z",
                    "temp_c",
                ])
                self._state.append_log(f"IMU CSV enabled: {csv_path}")
            except Exception as e:
                self._state.append_log(f"ERROR: Failed to open IMU CSV: {e}")
                self._csv_file = None
                self._csv_writer = None

    def stop_recording(self):
        if not self._recording:
            self._state.append_log("Not recording.")
            return

        try:
            self.zed.disable_recording()
        except Exception as e:
            self._state.append_log(f"ERROR: disable_recording exception: {e}")

        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
        self._csv_file = None
        self._csv_writer = None

        self._recording = False
        self._recording_start_monotonic = None
        self._session_dir = None
        self._frame_count = 0
        self._state.set_recording_info(None, "")
        self._state.append_log("Recording stopped.")

    def start_detecting(self):
        if self._detecting:
            self._state.append_log("Already detecting.")
            return
        self._detecting = True
        self._state.append_log("Camera: detection mode started.")

    def stop_detecting(self):
        if not self._detecting:
            self._state.append_log("Not detecting.")
            return
        self._detecting = False
        # Drain the inference queue
        if self.inference_queue is not None:
            while not self.inference_queue.empty():
                try:
                    self.inference_queue.get_nowait()
                except queue.Empty:
                    break
        self._state.append_log("Camera: detection mode stopped.")

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------
    def _detect_imu(self) -> bool:
        sensors_data = sl.SensorsData()
        err = self.zed.get_sensors_data(sensors_data, sl.TIME_REFERENCE.CURRENT)
        if err != sl.ERROR_CODE.SUCCESS:
            return False
        try:
            imu = sensors_data.get_imu_data()
            ts_ns = sl_time_to_ns(imu.timestamp)
            return ts_ns > 0
        except Exception:
            return False

    def _write_imu_row_if_available(self):
        if not (self._recording and self._csv_writer and self._imu_available):
            return

        sensors_data = sl.SensorsData()
        err = self.zed.get_sensors_data(sensors_data, sl.TIME_REFERENCE.CURRENT)
        if err != sl.ERROR_CODE.SUCCESS:
            return

        try:
            imu = sensors_data.get_imu_data()
            ts_ns = sl_time_to_ns(imu.timestamp)
            acc = imu.linear_acceleration
            gyr = imu.angular_velocity

            temp = float("nan")
            try:
                temp = float(imu.temperature)
            except Exception:
                pass

            self._csv_writer.writerow([
                ts_ns,
                float(acc[0]), float(acc[1]), float(acc[2]),
                float(gyr[0]), float(gyr[1]), float(gyr[2]),
                temp,
            ])
        except Exception:
            self._state.append_log("IMU read failed during recording; disabling IMU CSV.")
            try:
                if self._csv_file:
                    self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

    def _get_left_image_bgra(self) -> Optional[np.ndarray]:
        self.zed.retrieve_image(self.image_left, sl.VIEW.LEFT)
        arr = self.image_left.get_data()
        if arr is None:
            return None
        return np.ascontiguousarray(arr)

    def _push_frame_to_inference(self, bgra: np.ndarray):
        """Push a resized BGR frame to inference queue (non-blocking)."""
        if self.inference_queue is None:
            return
        bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        if self.process_width > 0 and self.orig_w > 0:
            bgr = cv2.resize(bgr, (self.process_width, self.process_h))

        # Drop old frames if queue is full
        if self.inference_queue.full():
            try:
                self.inference_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.inference_queue.put_nowait(bgr)
        except queue.Full:
            pass

    def _save_random_frame(self, bgra: np.ndarray):
        """Save frame as JPEG with capture_probability during recording."""
        if not self._recording or self._session_dir is None:
            return
        if random.random() >= self._capture_probability:
            return
        bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        frame_path = os.path.join(
            self._session_dir, "frames", f"frame_{self._frame_count:06d}.jpg"
        )
        cv2.imwrite(frame_path, bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])

    def _update_preview(self, bgra: np.ndarray):
        """Store BGR preview frame in SharedState (decimated by timestamp)."""
        bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        self._state.set_preview_frame(bgr)

    # ----------------------------------------------------------------
    # Thread main loop
    # ----------------------------------------------------------------
    def run(self):
        init = sl.InitParameters()
        init.camera_resolution = get_resolution_enum(self.camera_resolution)
        init.camera_fps = self.camera_fps
        init.depth_mode = sl.DEPTH_MODE.NONE
        init.coordinate_units = sl.UNIT.METER

        self._state.append_log(f"Python: {sys.executable}")
        self._state.append_log("Opening ZED camera...")
        err = self.zed.open(init)
        if err != sl.ERROR_CODE.SUCCESS:
            self._state.append_log(f"ERROR: Camera open failed: {repr(err)}")
            return

        # Camera info
        camera_info = self.zed.get_camera_information()
        self.orig_w = camera_info.camera_configuration.resolution.width
        self.orig_h = camera_info.camera_configuration.resolution.height
        if self.process_width > 0 and self.orig_w > 0:
            self.scale_factor = self.process_width / self.orig_w
            self.process_h = int(self.orig_h * self.scale_factor)
        else:
            self.process_h = self.orig_h

        self._imu_available = self._detect_imu()
        self._state.set_camera_opened(True)
        self._state.set_imu_available(self._imu_available)
        if self._imu_available:
            self._state.append_log("IMU detected.")
        else:
            self._state.append_log("IMU not detected.")

        self._state.append_log(
            f"Camera opened: {self.orig_w}x{self.orig_h} @ {self.camera_fps}fps. Ready."
        )

        preview_interval = 1.0 / float(self.preview_fps)

        while not self._stop_flag:
            err = self.zed.grab(self.runtime)
            if err != sl.ERROR_CODE.SUCCESS:
                time.sleep(0.01)
                continue

            # IMU recording
            if self._recording:
                self._write_imu_row_if_available()
                self._frame_count += 1

            # Get image
            bgra = self._get_left_image_bgra()
            if bgra is None:
                continue

            # Random frame capture during recording
            if self._recording:
                self._save_random_frame(bgra)

            # Push to inference queue in detecting mode
            if self._detecting:
                self._push_frame_to_inference(bgra)

            # Preview (decimated by timestamp)
            now_m = time.monotonic()
            if (now_m - self._last_preview_monotonic) >= preview_interval:
                self._last_preview_monotonic = now_m
                self._update_preview(bgra)

        # Cleanup
        if self._recording:
            self.stop_recording()

        try:
            self.zed.close()
        except Exception:
            pass

        self._state.set_camera_opened(False)
        self._state.append_log("Camera closed. Bye.")
