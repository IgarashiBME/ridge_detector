#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GpioWatcherThread: Monitors two GPIO pins for recording and detection triggers.
Uses libgpiod (gpioget command) as backend since Jetson.GPIO may fail on some
Jetson Orin Nano configurations.
"""

import subprocess
import time
from typing import Optional

from PySide6 import QtCore

# BOARD pin -> (gpiochip, line) mapping for Jetson Orin Nano / NX
# Source: NVIDIA/jetson-gpio gpio_pin_data.py JETSON_ORIN_NX_PIN_DEFS
ORIN_BOARD_TO_LINE = {
    7: ("gpiochip0", 144),
    11: ("gpiochip0", 112),
    12: ("gpiochip0", 50),
    13: ("gpiochip0", 122),
    15: ("gpiochip0", 85),
    16: ("gpiochip0", 126),
    18: ("gpiochip0", 125),
    19: ("gpiochip0", 135),
    21: ("gpiochip0", 134),
    22: ("gpiochip0", 123),
    23: ("gpiochip0", 133),
    24: ("gpiochip0", 136),
    26: ("gpiochip0", 137),
    29: ("gpiochip0", 105),
    31: ("gpiochip0", 106),
    32: ("gpiochip0", 41),
    33: ("gpiochip0", 43),
    35: ("gpiochip0", 53),
    36: ("gpiochip0", 113),
    37: ("gpiochip0", 124),
    38: ("gpiochip0", 52),
    40: ("gpiochip0", 51),
}


def _gpioget(chip: str, line: int) -> Optional[bool]:
    """Read a GPIO line using gpioget with pull-down bias.
    Returns True for HIGH, False for LOW, None on error."""
    try:
        result = subprocess.run(
            ["gpioget", "--bias=pull-down", chip, str(line)],
            capture_output=True, text=True, timeout=1.0,
        )
        if result.returncode == 0:
            return result.stdout.strip() == "1"
    except Exception:
        pass
    return None


class GpioWatcherThread(QtCore.QThread):
    """Watches two GPIO pins and emits signals on state change.

    GPIO-A: recording trigger (HIGH = start, LOW = stop)
    GPIO-B: detection trigger (HIGH = start, LOW = stop)

    Uses libgpiod (gpioget) with BOARD-pin-to-line mapping.
    """

    sig_rec_trigger = QtCore.Signal(bool)      # True=HIGH, False=LOW
    sig_det_trigger = QtCore.Signal(bool)      # True=HIGH, False=LOW
    sig_gpio_state = QtCore.Signal(str, bool)  # ("rec"/"det", HIGH/LOW)
    sig_status = QtCore.Signal(str)
    sig_error = QtCore.Signal(str)

    def __init__(
        self,
        parent=None,
        rec_pin: Optional[int] = None,
        det_pin: Optional[int] = None,
        debounce_ms: int = 500,
    ):
        super().__init__(parent)
        self.rec_pin = rec_pin
        self.det_pin = det_pin
        self.debounce_ms = debounce_ms

        self._stop_flag = False
        self._enabled = True

        self._rec_chip_line = None   # (chip, line) or None
        self._det_chip_line = None
        self._last_rec_state = False
        self._last_det_state = False

    @QtCore.Slot()
    def request_stop(self):
        self._stop_flag = True

    @QtCore.Slot(bool)
    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def _resolve_pin(self, board_pin: int, label: str):
        """Resolve BOARD pin to (chip, line). Returns tuple or None."""
        if board_pin not in ORIN_BOARD_TO_LINE:
            self.sig_error.emit(
                f"{label} BOARD pin {board_pin} not in mapping table. "
                f"Valid pins: {sorted(ORIN_BOARD_TO_LINE.keys())}"
            )
            return None

        chip, line = ORIN_BOARD_TO_LINE[board_pin]

        # Verify gpioget works
        val = _gpioget(chip, line)
        if val is None:
            self.sig_error.emit(
                f"{label} gpioget {chip} {line} failed. "
                "Check libgpiod installation and permissions."
            )
            return None

        state_str = "HIGH" if val else "LOW"
        self.sig_status.emit(
            f"{label} BOARD pin {board_pin} -> {chip} line {line} configured (current: {state_str})."
        )
        return (chip, line)

    def run(self):
        # Check gpioget is available
        try:
            subprocess.run(["gpioget", "--version"], capture_output=True, timeout=2.0)
        except FileNotFoundError:
            self.sig_error.emit("gpioget not found. Install libgpiod: sudo apt install gpiod")
            while not self._stop_flag:
                time.sleep(0.5)
            return
        except Exception as e:
            self.sig_error.emit(f"gpioget check failed: {e}")
            while not self._stop_flag:
                time.sleep(0.5)
            return

        # Resolve pins
        if self.rec_pin is not None:
            self._rec_chip_line = self._resolve_pin(self.rec_pin, "GPIO-A (rec)")

        if self.det_pin is not None:
            self._det_chip_line = self._resolve_pin(self.det_pin, "GPIO-B (det)")

        if self._rec_chip_line is None and self._det_chip_line is None:
            self.sig_status.emit("No GPIO pins available. GPIO watcher idle.")
            while not self._stop_flag:
                time.sleep(0.5)
            return

        # Read initial state so we only fire on *changes* from startup
        if self._rec_chip_line is not None:
            chip, line = self._rec_chip_line
            val = _gpioget(chip, line)
            if val is not None:
                self._last_rec_state = bool(val)
                self.sig_gpio_state.emit("rec", self._last_rec_state)
                self.sig_status.emit(
                    f"GPIO-A (rec) initial state: {'HIGH' if self._last_rec_state else 'LOW'}"
                )

        if self._det_chip_line is not None:
            chip, line = self._det_chip_line
            val = _gpioget(chip, line)
            if val is not None:
                self._last_det_state = bool(val)
                self.sig_gpio_state.emit("det", self._last_det_state)
                self.sig_status.emit(
                    f"GPIO-B (det) initial state: {'HIGH' if self._last_det_state else 'LOW'}"
                )

        debounce_sec = self.debounce_ms / 1000.0
        last_rec_change = 0.0
        last_det_change = 0.0

        self.sig_status.emit("GPIO watcher started (libgpiod backend). Waiting for state changes...")

        while not self._stop_flag:
            now = time.monotonic()

            if self._enabled:
                # Read recording pin (active-high: HIGH=start, LOW=stop)
                if self._rec_chip_line is not None:
                    chip, line = self._rec_chip_line
                    val = _gpioget(chip, line)
                    if val is not None:
                        state = bool(val)
                        if state != self._last_rec_state and (now - last_rec_change) >= debounce_sec:
                            self._last_rec_state = state
                            last_rec_change = now
                            self.sig_rec_trigger.emit(state)
                            self.sig_gpio_state.emit("rec", state)

                # Read detection pin (active-high: HIGH=start, LOW=stop)
                if self._det_chip_line is not None:
                    chip, line = self._det_chip_line
                    val = _gpioget(chip, line)
                    if val is not None:
                        state = bool(val)
                        if state != self._last_det_state and (now - last_det_change) >= debounce_sec:
                            self._last_det_state = state
                            last_det_change = now
                            self.sig_det_trigger.emit(state)
                            self.sig_gpio_state.emit("det", state)

            time.sleep(0.05)  # 20Hz polling

        self.sig_status.emit("GPIO watcher stopped.")
