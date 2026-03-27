#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CameraThread: Continuously grabs frames from ZED camera.
- IDLE: grab only (preview)
- RECORDING: grab + SVO2 recording + IMU CSV
- DETECTING: grab + push frames to inference queue
"""

import os
import sys
import csv
import time
import queue
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

from PySide6 import QtCore, QtGui


# ---------------------------------------------------------------------------
# Utilities (from zed_recoder_gui.py)
# ---------------------------------------------------------------------------
def expand_user(path_str: str) -> str:
    return os.path.expanduser(path_str)


def ensure_dir(path_str: str) -> None:
    Path(path_str).mkdir(parents=True, exist_ok=True)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sl_time_to_ns(t: sl.Timestamp) -> int:
    try:
        return int(t.get_nanoseconds())
    except Exception:
        return int(t.get_milliseconds() * 1_000_000)


def get_resolution_enum(resolution_str: str):
    """Convert resolution string to sl.RESOLUTION enum."""
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
class CameraThread(QtCore.QThread):
    """Continuously grabs frames from the ZED camera.

    Modes:
      - IDLE: grab + optional preview
      - RECORDING: grab + SVO2 recording + IMU CSV + preview
      - DETECTING: grab + push frames to inference queue + preview
    """

    # Signals
    sig_frame = QtCore.Signal(QtGui.QImage)        # preview frame
    sig_status = QtCore.Signal(str)                 # log message
    sig_error = QtCore.Signal(str)                  # error message
    sig_recording_state = QtCore.Signal(bool)       # recording started/stopped
    sig_detecting_state = QtCore.Signal(bool)       # detecting started/stopped
    sig_imu_available = QtCore.Signal(bool)         # IMU availability

    def __init__(
        self,
        parent=None,
        save_dir: str = "~/zed_records",
        camera_fps: int = 30,
        camera_resolution: str = "HD720",
        preview_fps: int = 15,
        preview_enabled: bool = True,
        process_width: int = 640,
        inference_queue: Optional[queue.Queue] = None,
    ):
        super().__init__(parent)
        self.save_dir = expand_user(save_dir)
        self.camera_fps = camera_fps
        self.camera_resolution = camera_resolution
        self.preview_fps = max(1, int(preview_fps))
        self.preview_enabled = bool(preview_enabled)
        self.process_width = process_width
        self.inference_queue = inference_queue  # shared queue to InferenceThread

        self._stop_flag = False

        # ZED objects
        self.zed = sl.Camera()
        self.runtime = sl.RuntimeParameters()
        self.image_left = sl.Mat()

        # Recording state
        self._recording = False
        self._recording_start_monotonic: Optional[float] = None
        self._current_base: Optional[str] = None
        self._csv_file = None
        self._csv_writer = None
        self._imu_available = False

        # Detecting state
        self._detecting = False

        # Preview timing
        self._last_preview_monotonic = 0.0

        # Camera info (populated after open)
        self.orig_w = 0
        self.orig_h = 0
        self.process_h = 0
        self.scale_factor = 1.0

    # ----------------------------------------------------------------
    # Public controls (called from UI thread via signal/slot)
    # ----------------------------------------------------------------
    @QtCore.Slot()
    def request_stop(self):
        self._stop_flag = True

    @QtCore.Slot(bool)
    def set_preview_enabled(self, enabled: bool):
        self.preview_enabled = bool(enabled)

    @QtCore.Slot()
    def start_recording(self):
        if self._recording:
            self.sig_status.emit("Already recording.")
            return

        ensure_dir(self.save_dir)
        base = now_stamp()
        svo_path = os.path.join(self.save_dir, f"{base}.svo2")

        rec_params = sl.RecordingParameters()
        rec_params.video_filename = svo_path
        rec_params.compression_mode = sl.SVO_COMPRESSION_MODE.LOSSLESS

        err = self.zed.enable_recording(rec_params)
        if err != sl.ERROR_CODE.SUCCESS:
            self.sig_error.emit(f"enable_recording failed: {repr(err)}")
            return

        self._current_base = base
        self._recording = True
        self._recording_start_monotonic = time.monotonic()
        self.sig_recording_state.emit(True)
        self.sig_status.emit(f"Recording started: {svo_path}")

        # IMU CSV (best effort)
        if self._imu_available:
            csv_path = os.path.join(self.save_dir, f"{base}_imu.csv")
            try:
                self._csv_file = open(csv_path, "w", newline="")
                self._csv_writer = csv.writer(self._csv_file)
                self._csv_writer.writerow([
                    "ts_ns",
                    "accel_x", "accel_y", "accel_z",
                    "gyro_x", "gyro_y", "gyro_z",
                    "temp_c",
                ])
                self.sig_status.emit(f"IMU CSV enabled: {csv_path}")
            except Exception as e:
                self.sig_error.emit(f"Failed to open IMU CSV: {e}")
                self._csv_file = None
                self._csv_writer = None

    @QtCore.Slot()
    def stop_recording(self):
        if not self._recording:
            self.sig_status.emit("Not recording.")
            return

        try:
            self.zed.disable_recording()
        except Exception as e:
            self.sig_error.emit(f"disable_recording exception: {e}")

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
        self._current_base = None
        self.sig_recording_state.emit(False)
        self.sig_status.emit("Recording stopped.")

    @QtCore.Slot()
    def start_detecting(self):
        if self._detecting:
            self.sig_status.emit("Already detecting.")
            return
        self._detecting = True
        self.sig_detecting_state.emit(True)
        self.sig_status.emit("Detection mode started.")

    @QtCore.Slot()
    def stop_detecting(self):
        if not self._detecting:
            self.sig_status.emit("Not detecting.")
            return
        self._detecting = False
        # Drain the inference queue
        if self.inference_queue is not None:
            while not self.inference_queue.empty():
                try:
                    self.inference_queue.get_nowait()
                except queue.Empty:
                    break
        self.sig_detecting_state.emit(False)
        self.sig_status.emit("Detection mode stopped.")

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
            self.sig_status.emit("IMU read failed during recording; disabling IMU CSV.")
            try:
                if self._csv_file:
                    self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

    def _get_left_image_bgra(self) -> Optional[np.ndarray]:
        self.zed.retrieve_image(self.image_left, sl.VIEW.LEFT)
        arr = self.image_left.get_data()  # BGRA format
        if arr is None:
            return None
        return np.ascontiguousarray(arr)

    def _emit_preview(self, image_np_bgra: np.ndarray):
        rgb = cv2.cvtColor(image_np_bgra, cv2.COLOR_BGRA2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QtGui.QImage(
            rgb.data,
            w,
            h,
            bytes_per_line,
            QtGui.QImage.Format.Format_RGB888,
        )
        self.sig_frame.emit(qimg.copy())

    def _push_frame_to_inference(self, bgra: np.ndarray):
        """Push a resized BGR frame to inference queue (non-blocking)."""
        if self.inference_queue is None:
            return
        # Convert BGRA -> BGR and resize for inference
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

    # ----------------------------------------------------------------
    # Thread main loop
    # ----------------------------------------------------------------
    def run(self):
        init = sl.InitParameters()
        init.camera_resolution = get_resolution_enum(self.camera_resolution)
        init.camera_fps = self.camera_fps
        init.depth_mode = sl.DEPTH_MODE.NONE
        init.coordinate_units = sl.UNIT.METER

        self.sig_status.emit(sys.executable)
        self.sig_status.emit("Opening ZED camera...")
        err = self.zed.open(init)
        if err != sl.ERROR_CODE.SUCCESS:
            self.sig_error.emit(f"Camera open failed: {repr(err)}")
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
        self.sig_imu_available.emit(self._imu_available)
        if self._imu_available:
            self.sig_status.emit("IMU detected.")
        else:
            self.sig_status.emit("IMU not detected.")

        self.sig_status.emit(
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

            # Get image for preview / inference
            bgra = self._get_left_image_bgra()
            if bgra is None:
                continue

            # Push to inference queue in detecting mode
            if self._detecting:
                self._push_frame_to_inference(bgra)

            # Preview (decimated) - always emit regardless of mode
            if self.preview_enabled:
                now_m = time.monotonic()
                if (now_m - self._last_preview_monotonic) >= preview_interval:
                    self._last_preview_monotonic = now_m
                    self._emit_preview(bgra)

        # Cleanup
        if self._recording:
            self.stop_recording()

        try:
            self.zed.close()
        except Exception:
            pass

        self.sig_status.emit("Camera closed. Bye.")
