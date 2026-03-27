#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ZED SVO2 recorder with PySide6 + pyzed.sl
- Record SVO2 (LOSSLESS) to ~/zed_records/YYYYmmdd_HHMMSS.svo2
- Optional IMU CSV in parallel (if sensors available) to ~/zed_records/YYYYmmdd_HHMMSS_imu.csv
- Preview with decimation and ON/OFF toggle to reduce overhead

Target environment:
- Jetson Orin Nano, Ubuntu 22.04, Python 3.10, ZED SDK 5.x
"""

import os
import sys
import time
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import pyzed.sl as sl
except Exception:
    print("Failed to import pyzed.sl. Ensure ZED SDK + Python API are installed.")
    raise

from PySide6 import QtCore, QtGui, QtWidgets


# -----------------------------
# Utilities
# -----------------------------
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


# -----------------------------
# Worker Thread
# -----------------------------
class ZedWorker(QtCore.QThread):
    # Qt signals
    sig_frame = QtCore.Signal(QtGui.QImage)
    sig_status = QtCore.Signal(str)
    sig_error = QtCore.Signal(str)
    sig_recording_state = QtCore.Signal(bool)
    sig_imu_available = QtCore.Signal(bool)

    def __init__(
        self,
        parent=None,
        save_dir: str = "~/zed_records",
        preview_fps: int = 5,
        preview_enabled: bool = True,
        preview_resize_width: int = 960,
        use_imu_csv: bool = True,
    ):
        super().__init__(parent)
        self.save_dir = expand_user(save_dir)
        self.preview_fps = max(1, int(preview_fps))
        self.preview_enabled = bool(preview_enabled)
        self.preview_resize_width = int(preview_resize_width)
        self.use_imu_csv_requested = bool(use_imu_csv)

        self._stop_flag = False

        # ZED
        self.zed = sl.Camera()
        self.runtime = sl.RuntimeParameters()
        self.image_left = sl.Mat()

        # Recording
        self._recording = False
        self._recording_start_monotonic: Optional[float] = None
        self._current_base: Optional[str] = None
        self._csv_file = None
        self._csv_writer = None
        self._imu_available = False

        # Preview timing
        self._last_preview_monotonic = 0.0

    # -------- Public controls (called from UI thread) --------
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
        if self.use_imu_csv_requested and self._imu_available:
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
        else:
            if self.use_imu_csv_requested and not self._imu_available:
                self.sig_status.emit("IMU not available: CSV disabled automatically.")

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

    # -------- Internal helpers --------
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

    def _emit_preview(self, image_np_rgba: np.ndarray):
        h, w, ch = image_np_rgba.shape
        bytes_per_line = ch * w
        qimg = QtGui.QImage(
            image_np_rgba.data,
            w,
            h,
            bytes_per_line,
            QtGui.QImage.Format.Format_RGBA8888,
        )
        self.sig_frame.emit(qimg.copy())

    def _get_left_image_rgba(self) -> Optional[np.ndarray]:
        self.zed.retrieve_image(self.image_left, sl.VIEW.LEFT)
        arr = self.image_left.get_data()  # typically RGBA uint8
        if arr is None:
            return None
        return np.ascontiguousarray(arr)

    # -------- Thread main --------
    def run(self):
        init = sl.InitParameters()
        init.camera_resolution = sl.RESOLUTION.HD720
        init.camera_fps = 30
        init.depth_mode = sl.DEPTH_MODE.NONE  # reduce load (stereo only)
        init.coordinate_units = sl.UNIT.METER

        self.sig_status.emit(sys.executable)
        self.sig_status.emit("Opening ZED camera...")
        err = self.zed.open(init)
        if err != sl.ERROR_CODE.SUCCESS:
            self.sig_error.emit(f"Camera open failed: {repr(err)}")
            return

        self._imu_available = self._detect_imu()
        self.sig_imu_available.emit(self._imu_available)
        if self._imu_available:
            self.sig_status.emit("IMU detected (likely ZED2i).")
        else:
            self.sig_status.emit("IMU not detected (ZED2 may have no IMU).")

        self.sig_status.emit("Camera opened. Ready.")

        self._last_preview_monotonic = 0.0
        preview_interval = 1.0 / float(self.preview_fps)

        while not self._stop_flag:
            err = self.zed.grab(self.runtime)
            if err != sl.ERROR_CODE.SUCCESS:
                time.sleep(0.01)
                continue

            if self._recording:
                self._write_imu_row_if_available()

            if self.preview_enabled:
                now_m = time.monotonic()
                if (now_m - self._last_preview_monotonic) >= preview_interval:
                    self._last_preview_monotonic = now_m
                    rgba = self._get_left_image_rgba()
                    if rgba is not None:
                        self._emit_preview(rgba)

        if self._recording:
            self.stop_recording()

        try:
            self.zed.close()
        except Exception:
            pass

        self.sig_status.emit("Camera closed. Bye.")


# -----------------------------
# Main Window
# -----------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("ZED SVO2 Recorder (PySide6 + pyzed.sl)")
        self.resize(1100, 700)

        self.save_dir = expand_user("~/zed_records")

        # UI
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        self.preview_label = QtWidgets.QLabel("Preview")
        self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(420)
        self.preview_label.setStyleSheet("background-color: #111; color: #aaa;")
        layout.addWidget(self.preview_label)

        controls = QtWidgets.QHBoxLayout()
        layout.addLayout(controls)

        self.btn_rec = QtWidgets.QPushButton("REC")
        font = self.btn_rec.font()
        font.setPointSize(18)
        self.btn_rec.setFont(font)
        
        self.btn_stop = QtWidgets.QPushButton("STOP")
        font = self.btn_stop.font()
        font.setPointSize(18)
        self.btn_stop.setFont(font)
        self.btn_stop.setEnabled(False)

        self.chk_preview = QtWidgets.QCheckBox("Preview ON")
        self.chk_preview.setChecked(True)

        controls.addWidget(self.btn_rec)
        controls.addWidget(self.btn_stop)
        controls.addSpacing(16)
        controls.addWidget(self.chk_preview)
        controls.addStretch(1)

        info = QtWidgets.QHBoxLayout()
        layout.addLayout(info)

        self.lbl_state = QtWidgets.QLabel("State: Idle")
        self.lbl_timer = QtWidgets.QLabel("Time: 00:00")
        self.lbl_imu = QtWidgets.QLabel("IMU: unknown")
        self.lbl_path = QtWidgets.QLabel(f"Save: {self.save_dir}")

        info.addWidget(self.lbl_state)
        info.addSpacing(16)
        info.addWidget(self.lbl_timer)
        info.addSpacing(16)
        info.addWidget(self.lbl_imu)
        info.addStretch(1)
        layout.addWidget(self.lbl_path)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        layout.addWidget(self.log)

        # state
        self._recording = False
        self._recording_start_monotonic: Optional[float] = None

        # UI timer
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.setInterval(250)
        self.ui_timer.timeout.connect(self._update_timer_label)
        self.ui_timer.start()

        # Worker
        self.worker = ZedWorker(
            save_dir=self.save_dir,
            preview_fps=20,
            preview_enabled=True,
            preview_resize_width=960,
            use_imu_csv=True,
        )
        self.worker.sig_frame.connect(self.on_frame)
        self.worker.sig_status.connect(self.on_status)
        self.worker.sig_error.connect(self.on_error)
        self.worker.sig_recording_state.connect(self.on_recording_state)
        self.worker.sig_imu_available.connect(self.on_imu_available)
        self.worker.start()

        # Connect UI
        self.btn_rec.clicked.connect(self.worker.start_recording)
        self.btn_stop.clicked.connect(self.worker.stop_recording)
        self.chk_preview.toggled.connect(self.worker.set_preview_enabled)

    def append_log(self, s: str):
        self.log.appendPlainText(s)

    @QtCore.Slot(QtGui.QImage)
    def on_frame(self, qimg: QtGui.QImage):
        if not self.chk_preview.isChecked():
            return

        pix = QtGui.QPixmap.fromImage(qimg)

        # Optional downscale for GUI (saves GPU/CPU a bit)
        pix = pix.scaled(
            self.preview_label.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(pix)

    @QtCore.Slot(str)
    def on_status(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append_log(f"[{ts}] {msg}")

    @QtCore.Slot(str)
    def on_error(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append_log(f"[{ts}] ERROR: {msg}")
        QtWidgets.QMessageBox.critical(self, "Error", msg)

    @QtCore.Slot(bool)
    def on_recording_state(self, is_rec: bool):
        self._recording = bool(is_rec)
        if self._recording:
            self._recording_start_monotonic = time.monotonic()
            self.lbl_state.setText("State: Recording")
            self.btn_rec.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.btn_rec.setStyleSheet("background-color: #b00020; color: white; font-weight: bold;")
        else:
            self._recording_start_monotonic = None
            self.lbl_state.setText("State: Idle")
            self.btn_rec.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.btn_rec.setStyleSheet("")

    @QtCore.Slot(bool)
    def on_imu_available(self, available: bool):
        self.lbl_imu.setText(f"IMU: {'available' if available else 'not available'}")

    def _update_timer_label(self):
        if self._recording and self._recording_start_monotonic is not None:
            elapsed = time.monotonic() - self._recording_start_monotonic
            mm = int(elapsed // 60)
            ss = int(elapsed % 60)
            self.lbl_timer.setText(f"Time: {mm:02d}:{ss:02d}")
        else:
            self.lbl_timer.setText("Time: 00:00")

    def closeEvent(self, event: QtGui.QCloseEvent):
        try:
            self.worker.request_stop()
            self.worker.wait(2000)
        except Exception:
            pass
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

