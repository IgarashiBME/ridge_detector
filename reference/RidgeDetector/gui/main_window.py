#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MainWindow: PySide6 GUI for ZED2 ridge detection & recording.
Extends the layout from zed_recoder_gui.py with detection controls,
GPIO status, and detection parameter display.
Supports compact mode for small displays (7-inch etc.).
"""

import math
import time
import queue
from datetime import datetime
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from workers.camera_worker import CameraThread, expand_user
from workers.inference_worker import InferenceThread
from workers.gpio_worker import GpioWatcherThread


class ModeState:
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    DETECTING = "DETECTING"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.setWindowTitle("ZED2 Ridge Detector & Recorder")

        self.save_dir = expand_user(args.save_dir)
        self._mode = ModeState.IDLE
        self._recording_start_monotonic: Optional[float] = None
        self._compact = bool(getattr(args, 'compact', False))

        # Shared inference queue (CameraThread -> InferenceThread)
        self.inference_queue = queue.Queue(maxsize=2)

        self._last_inference_frame_time = 0.0

        self._build_ui()
        self._apply_compact(self._compact)
        self.showMaximized()
        self._create_workers()
        self._connect_signals()
        self._start_workers()

        # UI update timer
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.setInterval(250)
        self.ui_timer.timeout.connect(self._update_timer_label)
        self.ui_timer.start()

    # ================================================================
    # UI Construction
    # ================================================================
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        self.main_layout = QtWidgets.QVBoxLayout(central)
        self.main_layout.setContentsMargins(4, 4, 4, 4)
        self.main_layout.setSpacing(2)

        # --- Preview area ---
        self.preview_label = QtWidgets.QLabel("Preview")
        self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background-color: #111; color: #aaa;")
        self.main_layout.addWidget(self.preview_label, stretch=1)

        # --- Mode control row ---
        self.mode_row = QtWidgets.QWidget()
        mode_layout = QtWidgets.QHBoxLayout(self.mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(4)

        self.btn_rec_start = QtWidgets.QPushButton("REC Start")
        self.btn_rec_stop = QtWidgets.QPushButton("REC Stop")
        self.btn_det_start = QtWidgets.QPushButton("DET Start")
        self.btn_det_stop = QtWidgets.QPushButton("DET Stop")

        self.btn_rec_stop.setEnabled(False)
        self.btn_det_stop.setEnabled(False)

        self.lbl_mode = QtWidgets.QLabel("IDLE")
        self.lbl_mode.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._update_mode_label()

        self.chk_preview = QtWidgets.QCheckBox("Preview")
        self.chk_preview.setChecked(True)

        self.chk_compact = QtWidgets.QCheckBox("Compact")
        self.chk_compact.setChecked(self._compact)
        self.chk_compact.toggled.connect(self._on_compact_toggled)

        mode_layout.addWidget(self.btn_rec_start)
        mode_layout.addWidget(self.btn_rec_stop)
        mode_layout.addWidget(self.btn_det_start)
        mode_layout.addWidget(self.btn_det_stop)
        mode_layout.addWidget(self.lbl_mode)
        mode_layout.addWidget(self.chk_preview)
        mode_layout.addWidget(self.chk_compact)
        mode_layout.addStretch(1)

        self.main_layout.addWidget(self.mode_row)

        # --- Info row (GPIO + detection params in one line) ---
        self.info_row = QtWidgets.QWidget()
        info_layout = QtWidgets.QHBoxLayout(self.info_row)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(8)

        # GPIO indicators
        info_layout.addWidget(QtWidgets.QLabel("GPIO-A:"))
        self.lbl_gpio_rec = QtWidgets.QLabel("--")
        self.lbl_gpio_rec.setStyleSheet("font-weight: bold;")
        info_layout.addWidget(self.lbl_gpio_rec)

        info_layout.addWidget(QtWidgets.QLabel("GPIO-B:"))
        self.lbl_gpio_det = QtWidgets.QLabel("--")
        self.lbl_gpio_det.setStyleSheet("font-weight: bold;")
        info_layout.addWidget(self.lbl_gpio_det)

        self.chk_gpio_enabled = QtWidgets.QCheckBox("GPIO")
        self.chk_gpio_enabled.setChecked(True)
        info_layout.addWidget(self.chk_gpio_enabled)

        # Separator
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        info_layout.addWidget(sep)

        # Detection parameters
        info_layout.addWidget(QtWidgets.QLabel("a:"))
        self.lbl_a = QtWidgets.QLabel("--")
        self.lbl_a.setStyleSheet("font-weight: bold; font-family: monospace;")
        info_layout.addWidget(self.lbl_a)

        info_layout.addWidget(QtWidgets.QLabel("b:"))
        self.lbl_b = QtWidgets.QLabel("--")
        self.lbl_b.setStyleSheet("font-weight: bold; font-family: monospace;")
        info_layout.addWidget(self.lbl_b)

        info_layout.addWidget(QtWidgets.QLabel("FPS:"))
        self.lbl_infer_fps = QtWidgets.QLabel("--")
        info_layout.addWidget(self.lbl_infer_fps)

        info_layout.addWidget(QtWidgets.QLabel("Serial:"))
        self.lbl_serial = QtWidgets.QLabel("--")
        info_layout.addWidget(self.lbl_serial)

        info_layout.addStretch(1)

        self.main_layout.addWidget(self.info_row)

        # --- Expanded info panels (normal mode only) ---
        self.expanded_panels = QtWidgets.QWidget()
        exp_layout = QtWidgets.QHBoxLayout(self.expanded_panels)
        exp_layout.setContentsMargins(0, 0, 0, 0)

        # GPIO status panel
        gpio_group = QtWidgets.QGroupBox("GPIO Status")
        gpio_layout = QtWidgets.QGridLayout(gpio_group)

        gpio_layout.addWidget(QtWidgets.QLabel("GPIO-A (Rec):"), 0, 0)
        self.lbl_gpio_rec_exp = QtWidgets.QLabel("--")
        self.lbl_gpio_rec_exp.setStyleSheet("font-weight: bold;")
        gpio_layout.addWidget(self.lbl_gpio_rec_exp, 0, 1)

        rec_pin_str = str(self.args.gpio_rec_pin) if self.args.gpio_rec_pin else "N/A"
        gpio_layout.addWidget(QtWidgets.QLabel(f"Pin: {rec_pin_str}"), 0, 2)

        gpio_layout.addWidget(QtWidgets.QLabel("GPIO-B (Det):"), 1, 0)
        self.lbl_gpio_det_exp = QtWidgets.QLabel("--")
        self.lbl_gpio_det_exp.setStyleSheet("font-weight: bold;")
        gpio_layout.addWidget(self.lbl_gpio_det_exp, 1, 1)

        det_pin_str = str(self.args.gpio_det_pin) if self.args.gpio_det_pin else "N/A"
        gpio_layout.addWidget(QtWidgets.QLabel(f"Pin: {det_pin_str}"), 1, 2)

        exp_layout.addWidget(gpio_group)

        # Detection parameter panel
        det_group = QtWidgets.QGroupBox("Detection Parameters")
        det_layout = QtWidgets.QGridLayout(det_group)

        det_layout.addWidget(QtWidgets.QLabel("a:"), 0, 0)
        self.lbl_a_exp = QtWidgets.QLabel("--")
        self.lbl_a_exp.setStyleSheet("font-weight: bold; font-family: monospace;")
        det_layout.addWidget(self.lbl_a_exp, 0, 1)

        det_layout.addWidget(QtWidgets.QLabel("b:"), 1, 0)
        self.lbl_b_exp = QtWidgets.QLabel("--")
        self.lbl_b_exp.setStyleSheet("font-weight: bold; font-family: monospace;")
        det_layout.addWidget(self.lbl_b_exp, 1, 1)

        det_layout.addWidget(QtWidgets.QLabel("Infer FPS:"), 2, 0)
        self.lbl_infer_fps_exp = QtWidgets.QLabel("--")
        det_layout.addWidget(self.lbl_infer_fps_exp, 2, 1)

        det_layout.addWidget(QtWidgets.QLabel("Serial:"), 3, 0)
        self.lbl_serial_exp = QtWidgets.QLabel("--")
        det_layout.addWidget(self.lbl_serial_exp, 3, 1)

        exp_layout.addWidget(det_group)

        self.main_layout.addWidget(self.expanded_panels)

        # --- Status bar ---
        self.status_row = QtWidgets.QWidget()
        status_layout = QtWidgets.QHBoxLayout(self.status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)

        self.lbl_state = QtWidgets.QLabel("State: Idle")
        self.lbl_timer = QtWidgets.QLabel("Time: 00:00")
        self.lbl_imu = QtWidgets.QLabel("IMU: unknown")
        self.lbl_path = QtWidgets.QLabel(f"Save: {self.save_dir}")

        status_layout.addWidget(self.lbl_state)
        status_layout.addWidget(self.lbl_timer)
        status_layout.addWidget(self.lbl_imu)
        status_layout.addStretch(1)
        status_layout.addWidget(self.lbl_path)

        self.main_layout.addWidget(self.status_row)

        # --- Log area ---
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.main_layout.addWidget(self.log)

    # ================================================================
    # Compact mode
    # ================================================================
    def _apply_compact(self, compact: bool):
        """Switch between compact and normal layout."""
        self._compact = compact

        if compact:
            self.resize(800, 480)
            self.preview_label.setMinimumHeight(0)
            self.log.setMaximumHeight(60)

            # Small fonts
            for btn in (self.btn_rec_start, self.btn_rec_stop,
                        self.btn_det_start, self.btn_det_stop):
                font = btn.font()
                font.setPointSize(10)
                btn.setFont(font)
                btn.setMinimumHeight(0)

            font = self.lbl_mode.font()
            font.setPointSize(12)
            font.setBold(True)
            self.lbl_mode.setFont(font)
            self.lbl_mode.setMinimumWidth(100)

            # Show compact info row, hide expanded panels
            self.info_row.setVisible(True)
            self.expanded_panels.setVisible(False)
        else:
            self.resize(1200, 800)
            self.preview_label.setMinimumHeight(400)
            self.log.setMaximumHeight(150)

            # Normal fonts
            for btn in (self.btn_rec_start, self.btn_rec_stop,
                        self.btn_det_start, self.btn_det_stop):
                font = btn.font()
                font.setPointSize(14)
                btn.setFont(font)
                btn.setMinimumHeight(40)

            font = self.lbl_mode.font()
            font.setPointSize(18)
            font.setBold(True)
            self.lbl_mode.setFont(font)
            self.lbl_mode.setMinimumWidth(160)

            # Hide compact info row, show expanded panels
            self.info_row.setVisible(False)
            self.expanded_panels.setVisible(True)

    @QtCore.Slot(bool)
    def _on_compact_toggled(self, checked: bool):
        self._apply_compact(checked)

    # ================================================================
    # Worker creation & wiring
    # ================================================================
    def _create_workers(self):
        args = self.args

        self.camera_worker = CameraThread(
            save_dir=args.save_dir,
            camera_fps=args.camera_fps,
            camera_resolution=args.camera_resolution,
            preview_fps=15,
            preview_enabled=True,
            process_width=args.process_width,
            inference_queue=self.inference_queue,
        )

        self.inference_worker = InferenceThread(
            inference_queue=self.inference_queue,
            model_path=args.model,
            conf=args.conf,
            half=args.half,
            target_class=args.target_class,
            fitting_mode=args.fitting_mode,
            num_lines=args.num_lines,
            inference_fps=args.inference_fps,
            serial_port=args.serial_port,
            serial_baud=args.serial_baud,
            ema_alpha=getattr(args, 'ema_alpha', 0.3),
        )

        self.gpio_worker = GpioWatcherThread(
            rec_pin=args.gpio_rec_pin,
            det_pin=args.gpio_det_pin,
            debounce_ms=args.debounce_ms,
        )

    def _connect_signals(self):
        # Camera worker signals
        self.camera_worker.sig_frame.connect(self.on_camera_frame)
        self.camera_worker.sig_status.connect(self.on_status)
        self.camera_worker.sig_error.connect(self.on_error)
        self.camera_worker.sig_recording_state.connect(self.on_recording_state)
        self.camera_worker.sig_detecting_state.connect(self.on_detecting_state)
        self.camera_worker.sig_imu_available.connect(self.on_imu_available)

        # Inference worker signals
        self.inference_worker.sig_frame.connect(self.on_inference_frame)
        self.inference_worker.sig_result.connect(self.on_inference_result)
        self.inference_worker.sig_status.connect(self.on_status)
        self.inference_worker.sig_error.connect(self.on_error)
        self.inference_worker.sig_serial_status.connect(self.on_serial_status)

        # GPIO worker signals
        self.gpio_worker.sig_rec_trigger.connect(self.on_gpio_rec_trigger)
        self.gpio_worker.sig_det_trigger.connect(self.on_gpio_det_trigger)
        self.gpio_worker.sig_gpio_state.connect(self.on_gpio_state)
        self.gpio_worker.sig_status.connect(self.on_status)
        self.gpio_worker.sig_error.connect(self.on_error)

        # UI buttons -> mode transitions
        self.btn_rec_start.clicked.connect(self._on_rec_start_clicked)
        self.btn_rec_stop.clicked.connect(self._on_rec_stop_clicked)
        self.btn_det_start.clicked.connect(self._on_det_start_clicked)
        self.btn_det_stop.clicked.connect(self._on_det_stop_clicked)

        # Preview toggle
        self.chk_preview.toggled.connect(self.camera_worker.set_preview_enabled)

        # GPIO enable toggle
        self.chk_gpio_enabled.toggled.connect(self.gpio_worker.set_enabled)

    def _start_workers(self):
        self.camera_worker.start()
        self.inference_worker.start()
        self.gpio_worker.start()

    # ================================================================
    # Mode transitions (exclusive logic)
    # ================================================================
    def _request_recording_start(self, source: str = "GUI"):
        if self._mode == ModeState.RECORDING:
            self.append_log(f"[WARN] Already recording. Ignoring {source} request.")
            return
        if self._mode == ModeState.DETECTING:
            self.append_log(
                f"[WARN] Currently DETECTING. Cannot start recording from {source}. "
                "Stop detection first."
            )
            return

        self._mode = ModeState.RECORDING
        self.camera_worker.start_recording()
        self.append_log(f"Recording started ({source}).")

    def _request_recording_stop(self, source: str = "GUI"):
        if self._mode != ModeState.RECORDING:
            self.append_log(f"[WARN] Not recording. Ignoring stop from {source}.")
            return

        self._mode = ModeState.IDLE
        self.camera_worker.stop_recording()
        self.append_log(f"Recording stopped ({source}).")

    def _request_detecting_start(self, source: str = "GUI"):
        if self._mode == ModeState.DETECTING:
            self.append_log(f"[WARN] Already detecting. Ignoring {source} request.")
            return
        if self._mode == ModeState.RECORDING:
            self.append_log(
                f"[WARN] Currently RECORDING. Cannot start detection from {source}. "
                "Stop recording first."
            )
            return

        self._mode = ModeState.DETECTING
        self.camera_worker.start_detecting()
        self.inference_worker.start_detecting()
        self.append_log(f"Detection started ({source}).")

    def _request_detecting_stop(self, source: str = "GUI"):
        if self._mode != ModeState.DETECTING:
            self.append_log(f"[WARN] Not detecting. Ignoring stop from {source}.")
            return

        self._mode = ModeState.IDLE
        self.inference_worker.stop_detecting()
        self.camera_worker.stop_detecting()
        self.append_log(f"Detection stopped ({source}).")

    # ================================================================
    # Button handlers
    # ================================================================
    def _on_rec_start_clicked(self):
        self._request_recording_start("GUI button")

    def _on_rec_stop_clicked(self):
        self._request_recording_stop("GUI button")

    def _on_det_start_clicked(self):
        self._request_detecting_start("GUI button")

    def _on_det_stop_clicked(self):
        self._request_detecting_stop("GUI button")

    # ================================================================
    # GPIO handlers
    # ================================================================
    @QtCore.Slot(bool)
    def on_gpio_rec_trigger(self, high: bool):
        if high:
            self._request_recording_start("GPIO-A")
        else:
            self._request_recording_stop("GPIO-A")

    @QtCore.Slot(bool)
    def on_gpio_det_trigger(self, high: bool):
        if high:
            self._request_detecting_start("GPIO-B")
        else:
            self._request_detecting_stop("GPIO-B")

    @QtCore.Slot(str, bool)
    def on_gpio_state(self, pin_name: str, high: bool):
        state_str = "HIGH" if high else "LOW"
        style_hi = "color: #00cc00; font-weight: bold;"
        style_lo = "color: #888888; font-weight: bold;"
        style = style_hi if high else style_lo
        if pin_name == "rec":
            self.lbl_gpio_rec.setText(state_str)
            self.lbl_gpio_rec.setStyleSheet(style)
            self.lbl_gpio_rec_exp.setText(state_str)
            self.lbl_gpio_rec_exp.setStyleSheet(style)
        elif pin_name == "det":
            self.lbl_gpio_det.setText(state_str)
            self.lbl_gpio_det.setStyleSheet(style)
            self.lbl_gpio_det_exp.setText(state_str)
            self.lbl_gpio_det_exp.setStyleSheet(style)

    # ================================================================
    # Camera / Inference frame handlers
    # ================================================================
    @QtCore.Slot(QtGui.QImage)
    def on_camera_frame(self, qimg: QtGui.QImage):
        """Preview from camera. Always displayed unless inference frames
        are actively arriving (in which case those take priority)."""
        if not self.chk_preview.isChecked():
            return
        if self._mode == ModeState.DETECTING:
            # If inference frames are flowing, skip camera frames
            elapsed = time.monotonic() - self._last_inference_frame_time
            if elapsed < 1.0:
                return
        self._display_preview(qimg)

    @QtCore.Slot(QtGui.QImage)
    def on_inference_frame(self, qimg: QtGui.QImage):
        """Annotated preview from inference (used in DETECTING mode)."""
        if not self.chk_preview.isChecked():
            return
        if self._mode != ModeState.DETECTING:
            return
        self._last_inference_frame_time = time.monotonic()
        self._display_preview(qimg)

    def _display_preview(self, qimg: QtGui.QImage):
        pix = QtGui.QPixmap.fromImage(qimg)
        pix = pix.scaled(
            self.preview_label.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(pix)

    # ================================================================
    # State change handlers
    # ================================================================
    @QtCore.Slot(bool)
    def on_recording_state(self, is_rec: bool):
        if is_rec:
            self._recording_start_monotonic = time.monotonic()
        else:
            self._recording_start_monotonic = None
        self._update_mode_ui()

    @QtCore.Slot(bool)
    def on_detecting_state(self, is_det: bool):
        self._update_mode_ui()

    @QtCore.Slot(bool)
    def on_imu_available(self, available: bool):
        self.lbl_imu.setText(f"IMU: {'available' if available else 'not available'}")

    @QtCore.Slot(object)
    def on_inference_result(self, data: dict):
        a = data.get('a', float('nan'))
        b = data.get('b', float('nan'))
        fps = data.get('infer_fps', 0.0)

        if math.isnan(a):
            a_str, b_str = "--", "--"
        else:
            a_str = f"{a:.4f}"
            b_str = f"{b:.1f}"

        # Update both compact and expanded labels
        self.lbl_a.setText(a_str)
        self.lbl_b.setText(b_str)
        self.lbl_infer_fps.setText(f"{fps:.1f}")
        self.lbl_a_exp.setText(a_str)
        self.lbl_b_exp.setText(b_str)
        self.lbl_infer_fps_exp.setText(f"{fps:.1f}")

    @QtCore.Slot(str, int)
    def on_serial_status(self, status: str, count: int):
        txt = f"{status} (TX: {count})"
        self.lbl_serial.setText(txt)
        self.lbl_serial_exp.setText(txt)

    # ================================================================
    # Log / status
    # ================================================================
    def append_log(self, s: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{ts}] {s}")

    @QtCore.Slot(str)
    def on_status(self, msg: str):
        self.append_log(msg)

    @QtCore.Slot(str)
    def on_error(self, msg: str):
        self.append_log(f"ERROR: {msg}")

    # ================================================================
    # UI update helpers
    # ================================================================
    def _update_mode_ui(self):
        """Update button states and mode label based on current mode."""
        if self._mode == ModeState.IDLE:
            self.btn_rec_start.setEnabled(True)
            self.btn_rec_stop.setEnabled(False)
            self.btn_det_start.setEnabled(True)
            self.btn_det_stop.setEnabled(False)
            self.lbl_state.setText("State: Idle")
        elif self._mode == ModeState.RECORDING:
            self.btn_rec_start.setEnabled(False)
            self.btn_rec_stop.setEnabled(True)
            self.btn_det_start.setEnabled(False)
            self.btn_det_stop.setEnabled(False)
            self.lbl_state.setText("State: Recording")
        elif self._mode == ModeState.DETECTING:
            self.btn_rec_start.setEnabled(False)
            self.btn_rec_stop.setEnabled(False)
            self.btn_det_start.setEnabled(False)
            self.btn_det_stop.setEnabled(True)
            self.lbl_state.setText("State: Detecting")

        self._update_mode_label()

        # Reset detection params when leaving detection mode
        if self._mode != ModeState.DETECTING:
            for lbl in (self.lbl_a, self.lbl_b, self.lbl_infer_fps,
                        self.lbl_a_exp, self.lbl_b_exp, self.lbl_infer_fps_exp):
                lbl.setText("--")

    def _update_mode_label(self):
        """Update the mode indicator with color coding."""
        if self._mode == ModeState.RECORDING:
            self.lbl_mode.setText("RECORDING")
            self.lbl_mode.setStyleSheet(
                "background-color: #cc0000; color: white; padding: 4px; border-radius: 4px;"
            )
        elif self._mode == ModeState.DETECTING:
            self.lbl_mode.setText("DETECTING")
            self.lbl_mode.setStyleSheet(
                "background-color: #00aa00; color: white; padding: 4px; border-radius: 4px;"
            )
        else:
            self.lbl_mode.setText("IDLE")
            self.lbl_mode.setStyleSheet(
                "background-color: #666666; color: white; padding: 4px; border-radius: 4px;"
            )

    def _update_timer_label(self):
        if self._mode == ModeState.RECORDING and self._recording_start_monotonic is not None:
            elapsed = time.monotonic() - self._recording_start_monotonic
            mm = int(elapsed // 60)
            ss = int(elapsed % 60)
            self.lbl_timer.setText(f"Time: {mm:02d}:{ss:02d}")
        else:
            self.lbl_timer.setText("Time: 00:00")

    # ================================================================
    # Cleanup
    # ================================================================
    def closeEvent(self, event: QtGui.QCloseEvent):
        self.append_log("Shutting down...")

        # Stop current mode
        if self._mode == ModeState.RECORDING:
            self._request_recording_stop("shutdown")
        elif self._mode == ModeState.DETECTING:
            self._request_detecting_stop("shutdown")

        # Stop all workers
        self.inference_worker.request_stop()
        self.gpio_worker.request_stop()
        self.camera_worker.request_stop()

        self.inference_worker.wait(3000)
        self.gpio_worker.wait(2000)
        self.camera_worker.wait(3000)

        event.accept()
