#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DisplayWindow: PySide6 view-only display.
No buttons - mode control is from the PWA only.

Polls SharedState via QTimer at ~15Hz.
"""

import math
import signal
import sys
import time

import cv2
import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

from state.shared_state import SharedState, Mode


class DisplayWindow(QtWidgets.QMainWindow):
    def __init__(self, state: SharedState, compact: bool = False):
        super().__init__()
        self._state = state
        self._compact = compact
        self.setWindowTitle("Ridge Detector v2")

        self._build_ui()
        self._apply_layout()

        # Poll timer
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(66)  # ~15 Hz
        self._timer.timeout.connect(self._poll_state)
        self._timer.start()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        # Preview
        self.preview_label = QtWidgets.QLabel("Waiting for camera...")
        self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background-color: #111; color: #aaa;")
        layout.addWidget(self.preview_label, stretch=1)

        # Info bar
        info = QtWidgets.QWidget()
        info_layout = QtWidgets.QHBoxLayout(info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(8)

        # Mode badge
        self.lbl_mode = QtWidgets.QLabel("IDLE")
        self.lbl_mode.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        info_layout.addWidget(self.lbl_mode)

        # Detection params
        info_layout.addWidget(QtWidgets.QLabel("a:"))
        self.lbl_a = QtWidgets.QLabel("--")
        self.lbl_a.setStyleSheet("font-weight: bold; font-family: monospace;")
        info_layout.addWidget(self.lbl_a)

        info_layout.addWidget(QtWidgets.QLabel("b:"))
        self.lbl_b = QtWidgets.QLabel("--")
        self.lbl_b.setStyleSheet("font-weight: bold; font-family: monospace;")
        info_layout.addWidget(self.lbl_b)

        info_layout.addWidget(QtWidgets.QLabel("FPS:"))
        self.lbl_fps = QtWidgets.QLabel("--")
        info_layout.addWidget(self.lbl_fps)

        info_layout.addWidget(QtWidgets.QLabel("Serial:"))
        self.lbl_serial = QtWidgets.QLabel("--")
        info_layout.addWidget(self.lbl_serial)

        # Timer / progress
        self.lbl_timer = QtWidgets.QLabel("")
        info_layout.addWidget(self.lbl_timer)

        info_layout.addStretch(1)
        layout.addWidget(info)

    def _apply_layout(self):
        if self._compact:
            self.resize(800, 480)
            font = self.lbl_mode.font()
            font.setPointSize(12)
            font.setBold(True)
            self.lbl_mode.setFont(font)
            self.lbl_mode.setMinimumWidth(100)
        else:
            self.resize(1200, 800)
            self.preview_label.setMinimumHeight(400)
            font = self.lbl_mode.font()
            font.setPointSize(16)
            font.setBold(True)
            self.lbl_mode.setFont(font)
            self.lbl_mode.setMinimumWidth(140)

    def _poll_state(self):
        """Read SharedState and update UI."""
        mode = self._state.get_mode()

        # Mode badge
        if mode == Mode.RECORDING:
            self.lbl_mode.setText("RECORDING")
            self.lbl_mode.setStyleSheet(
                "background-color: #cc0000; color: white; "
                "padding: 4px; border-radius: 4px;"
            )
        elif mode == Mode.DETECTING:
            self.lbl_mode.setText("DETECTING")
            self.lbl_mode.setStyleSheet(
                "background-color: #00aa00; color: white; "
                "padding: 4px; border-radius: 4px;"
            )
        elif mode == Mode.TRAINING:
            self.lbl_mode.setText("TRAINING")
            self.lbl_mode.setStyleSheet(
                "background-color: #cc8800; color: white; "
                "padding: 4px; border-radius: 4px;"
            )
        else:
            self.lbl_mode.setText("IDLE")
            self.lbl_mode.setStyleSheet(
                "background-color: #666666; color: white; "
                "padding: 4px; border-radius: 4px;"
            )

        # Detection params
        if mode == Mode.DETECTING:
            det = self._state.get_detection()
            a_str = f"{det.a:.4f}" if not math.isnan(det.a) else "--"
            b_str = f"{det.b:.1f}" if not math.isnan(det.b) else "--"
            self.lbl_a.setText(a_str)
            self.lbl_b.setText(b_str)
            self.lbl_fps.setText(f"{det.fps:.1f}")
            self.lbl_serial.setText(f"{det.serial_status} (TX:{det.serial_count})")
        else:
            self.lbl_a.setText("--")
            self.lbl_b.setText("--")
            self.lbl_fps.setText("--")
            self.lbl_serial.setText("--")

        # Timer / progress
        if mode == Mode.RECORDING:
            rec_start, _ = self._state.get_recording_info()
            if rec_start is not None:
                elapsed = time.monotonic() - rec_start
                mm = int(elapsed // 60)
                ss = int(elapsed % 60)
                self.lbl_timer.setText(f"REC {mm:02d}:{ss:02d}")
            else:
                self.lbl_timer.setText("")
        elif mode == Mode.TRAINING:
            t = self._state.get_training()
            if t.running:
                self.lbl_timer.setText(
                    f"Epoch {t.epoch}/{t.total_epochs} | {t.phase}"
                )
            else:
                self.lbl_timer.setText("")
        else:
            self.lbl_timer.setText("")

        # Display frame
        frame = self._state.get_display_frame()
        if frame is not None:
            self._show_frame(frame)

    def _show_frame(self, bgr: np.ndarray):
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QtGui.QImage(
            rgb.data, w, h, bytes_per_line,
            QtGui.QImage.Format.Format_RGB888,
        )
        pix = QtGui.QPixmap.fromImage(qimg)
        pix = pix.scaled(
            self.preview_label.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(pix)


def run_display(state: SharedState, compact: bool = False):
    """Run PySide6 display (blocks until window is closed)."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    window = DisplayWindow(state, compact=compact)
    window.showMaximized()

    app.exec()
