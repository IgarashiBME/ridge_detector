#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ModeManager: Exclusive mode transitions.

Extracted from MainWindow._request_* methods.
Transitions: IDLE <-> RECORDING, IDLE <-> DETECTING, IDLE <-> TRAINING.
Direct transitions between non-IDLE modes are blocked.
"""

import threading
from typing import Callable, Optional, Tuple

from state.shared_state import SharedState, Mode


class ModeManager:
    """Manages exclusive mode transitions with callback-based worker control.

    Usage:
        mm = ModeManager(state)
        mm.register_callbacks(
            start_recording=cam.start_recording,
            stop_recording=cam.stop_recording,
            start_detecting=lambda: (cam.start_detecting(), inf.start_detecting()),
            stop_detecting=lambda: (inf.stop_detecting(), cam.stop_detecting()),
            start_training=training_mgr.start,
            stop_training=training_mgr.stop,
        )
        ok, msg = mm.request_mode(Mode.RECORDING, source="API")
    """

    def __init__(self, state: SharedState):
        self._state = state
        self._lock = threading.Lock()

        # Callbacks (set via register_callbacks)
        self._start_recording: Optional[Callable] = None
        self._stop_recording: Optional[Callable] = None
        self._start_detecting: Optional[Callable] = None
        self._stop_detecting: Optional[Callable] = None
        self._start_training: Optional[Callable] = None
        self._stop_training: Optional[Callable] = None
        self._start_evaluating: Optional[Callable] = None
        self._stop_evaluating: Optional[Callable] = None

    def register_callbacks(
        self,
        start_recording: Optional[Callable] = None,
        stop_recording: Optional[Callable] = None,
        start_detecting: Optional[Callable] = None,
        stop_detecting: Optional[Callable] = None,
        start_training: Optional[Callable] = None,
        stop_training: Optional[Callable] = None,
        start_evaluating: Optional[Callable] = None,
        stop_evaluating: Optional[Callable] = None,
    ):
        self._start_recording = start_recording
        self._stop_recording = stop_recording
        self._start_detecting = start_detecting
        self._stop_detecting = stop_detecting
        self._start_training = start_training
        self._stop_training = stop_training
        self._start_evaluating = start_evaluating
        self._stop_evaluating = stop_evaluating

    def request_mode(self, target: Mode, source: str = "API") -> Tuple[bool, str]:
        """Request a mode transition.

        Returns (success, message).
        """
        with self._lock:
            current = self._state.get_mode()

            # Already in target mode
            if current == target:
                return True, f"Already in {target.value}"

            # Transition to IDLE (stop current mode)
            if target == Mode.IDLE:
                return self._stop_current(current, source)

            # Transition from IDLE to target
            if current == Mode.IDLE:
                return self._start_mode(target, source)

            # Non-IDLE to non-IDLE: block
            return False, (
                f"Cannot switch from {current.value} to {target.value}. "
                f"Stop {current.value} first."
            )

    def _stop_current(self, current: Mode, source: str) -> Tuple[bool, str]:
        """Stop current mode and return to IDLE."""
        if current == Mode.IDLE:
            return True, "Already IDLE"

        if current == Mode.RECORDING:
            self._state.set_mode(Mode.IDLE)
            if self._stop_recording:
                self._stop_recording()
            self._state.append_log(f"Recording stopped ({source}).")
            return True, "Recording stopped"

        if current == Mode.DETECTING:
            self._state.set_mode(Mode.IDLE)
            if self._stop_detecting:
                self._stop_detecting()
            self._state.append_log(f"Detection stopped ({source}).")
            return True, "Detection stopped"

        if current == Mode.TRAINING:
            self._state.set_mode(Mode.IDLE)
            if self._stop_training:
                self._stop_training()
            self._state.append_log(f"Training stopped ({source}).")
            return True, "Training stopped"

        if current == Mode.EVALUATING:
            self._state.set_mode(Mode.IDLE)
            if self._stop_evaluating:
                self._stop_evaluating()
            self._state.append_log(f"Evaluation stopped ({source}).")
            return True, "Evaluation stopped"

        return False, f"Unknown mode: {current}"

    def _start_mode(self, target: Mode, source: str) -> Tuple[bool, str]:
        """Start target mode from IDLE."""
        if target == Mode.RECORDING:
            self._state.set_mode(Mode.RECORDING)
            if self._start_recording:
                self._start_recording()
            self._state.append_log(f"Recording started ({source}).")
            return True, "Recording started"

        if target == Mode.DETECTING:
            self._state.set_mode(Mode.DETECTING)
            if self._start_detecting:
                self._start_detecting()
            self._state.append_log(f"Detection started ({source}).")
            return True, "Detection started"

        if target == Mode.TRAINING:
            self._state.set_mode(Mode.TRAINING)
            if self._start_training:
                self._start_training()
            self._state.append_log(f"Training started ({source}).")
            return True, "Training started"

        if target == Mode.EVALUATING:
            self._state.set_mode(Mode.EVALUATING)
            if self._start_evaluating:
                self._start_evaluating()
            self._state.append_log(f"Evaluation started ({source}).")
            return True, "Evaluation started"

        return False, f"Unknown target mode: {target}"

    def shutdown(self, source: str = "shutdown"):
        """Stop whatever mode is active and return to IDLE."""
        current = self._state.get_mode()
        if current != Mode.IDLE:
            self._stop_current(current, source)
