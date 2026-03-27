#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
InferenceThread: YOLO-seg inference + serial sending.
Receives frames from CameraThread via queue.Queue.
Emits results (a, b, annotated image) to UI via Qt signals.
"""

import queue
import time
from typing import Optional

import cv2
import numpy as np

from PySide6 import QtCore, QtGui

from core.ridge_detection import process_image
from core.ubx_protocol import build_ubx_nav_relposned
from core.visualization import visualize_result

# Serial (optional)
try:
    import serial as pyserial
    serial_available = True
except ImportError:
    serial_available = False


class InferenceThread(QtCore.QThread):
    """Runs YOLO-seg inference on frames received via queue.

    When detecting mode is ON, reads frames from the queue,
    runs inference, computes a/b, sends UBX via serial, and
    emits annotated frames + parameters to the UI.
    """

    # Signals
    sig_frame = QtCore.Signal(QtGui.QImage)          # annotated preview
    sig_result = QtCore.Signal(object)                # dict with a, b, infer_fps, etc.
    sig_status = QtCore.Signal(str)                   # log message
    sig_error = QtCore.Signal(str)                    # error message
    sig_serial_status = QtCore.Signal(str, int)       # (status_str, send_count)

    def __init__(
        self,
        parent=None,
        inference_queue: Optional[queue.Queue] = None,
        model_path: str = "yolo11s-seg.pt",
        conf: float = 0.25,
        half: bool = True,
        target_class: Optional[int] = None,
        fitting_mode: str = "ransac",
        num_lines: int = 20,
        inference_fps: int = 10,
        serial_port: Optional[str] = None,
        serial_baud: int = 115200,
        mask_alpha: float = 0.4,
        y_margin: float = 0.1,
        min_run: int = 5,
        ema_alpha: float = 0.3,
    ):
        super().__init__(parent)
        self.inference_queue = inference_queue
        self.model_path = model_path
        self.conf = conf
        self.half = half
        self.target_class = target_class
        self.fitting_mode = fitting_mode
        self.num_lines = num_lines
        self.inference_fps = max(1, inference_fps)
        self.serial_port = serial_port
        self.serial_baud = serial_baud
        self.mask_alpha = mask_alpha
        self.y_margin = y_margin
        self.min_run = min_run
        self.ema_alpha = max(0.0, min(1.0, ema_alpha))

        self._stop_flag = False
        self._detecting = False

        # EMA filter state
        self._filtered_a: Optional[float] = None
        self._filtered_b: Optional[float] = None

        # Serial
        self._serial_conn: Optional[object] = None
        self._serial_send_count = 0
        self._serial_error_count = 0

        # Model (loaded in thread)
        self._model = None

        # Timing
        self._start_time = 0.0

    # ----------------------------------------------------------------
    # Public controls
    # ----------------------------------------------------------------
    @QtCore.Slot()
    def request_stop(self):
        self._stop_flag = True

    @QtCore.Slot()
    def start_detecting(self):
        self._detecting = True

    @QtCore.Slot()
    def stop_detecting(self):
        self._detecting = False
        # Reset EMA filter state
        self._filtered_a = None
        self._filtered_b = None
        # Drain queue
        if self.inference_queue is not None:
            while not self.inference_queue.empty():
                try:
                    self.inference_queue.get_nowait()
                except queue.Empty:
                    break

    # ----------------------------------------------------------------
    # Serial helpers
    # ----------------------------------------------------------------
    def _open_serial(self):
        if not serial_available or self.serial_port is None:
            if self.serial_port is not None and not serial_available:
                self.sig_status.emit("pyserial not installed. Serial disabled.")
            return
        try:
            self._serial_conn = pyserial.Serial(
                port=self.serial_port,
                baudrate=self.serial_baud,
                timeout=0.01,
            )
            self.sig_status.emit(f"Serial opened: {self.serial_port} @ {self.serial_baud}bps")
        except Exception as e:
            self.sig_error.emit(f"Serial open failed: {e}")
            self._serial_conn = None

    def _close_serial(self):
        if self._serial_conn is not None:
            try:
                self._serial_conn.close()
            except Exception:
                pass
            self.sig_status.emit(
                f"Serial closed. Sent: {self._serial_send_count}, Errors: {self._serial_error_count}"
            )
            self._serial_conn = None

    def _send_serial(self, msg: bytes):
        if self._serial_conn is None:
            return
        try:
            self._serial_conn.write(msg)
            self._serial_send_count += 1
        except Exception as e:
            self._serial_error_count += 1
            if self._serial_error_count % 10 == 1:
                self.sig_status.emit(f"Serial error: {e}")

    # ----------------------------------------------------------------
    # Preview helper
    # ----------------------------------------------------------------
    def _emit_preview(self, bgr_frame: np.ndarray):
        """Convert BGR frame to QImage and emit."""
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QtGui.QImage(
            rgb.data, w, h, bytes_per_line,
            QtGui.QImage.Format.Format_RGB888,
        )
        self.sig_frame.emit(qimg.copy())

    # ----------------------------------------------------------------
    # Thread main loop
    # ----------------------------------------------------------------
    def run(self):
        # Load YOLO model
        self.sig_status.emit(f"Loading YOLO model: {self.model_path}")
        try:
            from ultralytics import YOLO
            self._model = YOLO(self.model_path)
            self.sig_status.emit("YOLO model loaded.")
        except Exception as e:
            self.sig_error.emit(f"Failed to load YOLO model: {e}")
            return

        # Open serial
        self._open_serial()

        self._start_time = time.perf_counter()
        inference_interval = 1.0 / float(self.inference_fps)
        last_infer_time = 0.0
        prev_time = time.perf_counter()

        while not self._stop_flag:
            if not self._detecting:
                # Drain queue while idle
                if self.inference_queue is not None:
                    while not self.inference_queue.empty():
                        try:
                            self.inference_queue.get_nowait()
                        except queue.Empty:
                            break
                time.sleep(0.05)
                continue

            # Get frame from queue
            if self.inference_queue is None:
                time.sleep(0.05)
                continue

            try:
                frame = self.inference_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # Rate limiting
            now = time.perf_counter()
            elapsed_since_last = now - last_infer_time
            if elapsed_since_last < inference_interval:
                continue
            last_infer_time = now

            try:
                # Run inference
                H, W = frame.shape[:2]
                target_mask, centers, line_points, ab, infer_time_ms = process_image(
                    frame,
                    self._model,
                    conf=self.conf,
                    half=self.half,
                    target_class=self.target_class,
                    num_lines=self.num_lines,
                    y_margin=self.y_margin,
                    min_run=self.min_run,
                    fitting_mode=self.fitting_mode,
                )

                # Compute a, b and apply EMA filter
                relPosN_a = 0
                relPosE_b = 0
                detectionOK = 0
                a_val = float('nan')
                b_val = float('nan')

                if ab is not None:
                    a_raw, b_raw = ab
                    # Apply EMA filter
                    if self._filtered_a is None:
                        self._filtered_a = a_raw
                        self._filtered_b = b_raw
                    else:
                        self._filtered_a = self.ema_alpha * a_raw + (1 - self.ema_alpha) * self._filtered_a
                        self._filtered_b = self.ema_alpha * b_raw + (1 - self.ema_alpha) * self._filtered_b
                    a_val = self._filtered_a
                    b_val = self._filtered_b
                    relPosN_a = int(a_val * 100)
                    relPosE_b = int(b_val * 100)
                    detectionOK = 1

                elapsed_ms = int((time.perf_counter() - self._start_time) * 1000)
                msg = build_ubx_nav_relposned(
                    relPosN_cm=relPosN_a,
                    relPosE_cm=relPosE_b,
                    gnssFixOK=detectionOK,
                    carrSoln=0,
                    refStationId=0,
                    iTOW_ms=elapsed_ms % 0xFFFFFFFF,
                    relPosD_cm=0,
                    relPosValid=0,
                )

                # Serial send
                self._send_serial(msg)

                # FPS calculation
                curr_time = time.perf_counter()
                loop_time = curr_time - prev_time
                fps_disp = 1.0 / loop_time if loop_time > 0 else 0.0
                prev_time = curr_time

                # Visualize
                vis_frame = frame.copy()
                visualize_result(
                    vis_frame, target_mask, centers, line_points, ab,
                    infer_time_ms=infer_time_ms,
                    fps=fps_disp,
                    serial_count=self._serial_send_count,
                    mask_alpha=self.mask_alpha,
                )

                # Emit annotated preview
                self._emit_preview(vis_frame)

                # Emit result data
                serial_status_str = "disabled"
                if self._serial_conn is not None:
                    serial_status_str = "connected"
                elif self.serial_port is not None:
                    serial_status_str = "error"

                self.sig_result.emit({
                    'a': a_val,
                    'b': b_val,
                    'infer_fps': fps_disp,
                    'infer_time_ms': infer_time_ms,
                })
                self.sig_serial_status.emit(serial_status_str, self._serial_send_count)

            except Exception as e:
                self.sig_status.emit(f"Inference error: {e}")
                time.sleep(0.1)

        # Cleanup
        self._close_serial()
        self.sig_status.emit("Inference thread stopped.")
