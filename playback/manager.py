#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PlaybackManager: Plays back a recorded SVO2 file through the live inference
pipeline (YOLO-seg → scan line → RANSAC → EMA → UBX serial output).

Runs an in-process thread (not a subprocess) because it needs to push frames
into the same inference_queue the CameraThread normally feeds. The existing
InferenceThread consumes the queue unchanged — it does not need to know the
frame source is an SVO file.
"""

import os
import queue
import threading
import time
from typing import Optional

import cv2
import numpy as np

try:
    import pyzed.sl as sl
except Exception:
    sl = None

from state.shared_state import Mode, SharedState


class PlaybackManager:
    def __init__(
        self,
        state: SharedState,
        save_dir: str,
        inference_queue: queue.Queue,
        mode_manager,
        inference_thread,
        process_width: int = 640,
    ):
        self._state = state
        self.save_dir = os.path.expanduser(save_dir)
        self._queue = inference_queue
        self._mode_manager = mode_manager
        self._inference = inference_thread
        self.process_width = process_width

        self._thread: Optional[threading.Thread] = None
        self._stop_flag = False
        self._paused = False
        self._lock = threading.Lock()

    # ----------------------------------------------------------------
    # Public control
    # ----------------------------------------------------------------
    def start(self, session_name: Optional[str] = None,
              video_filename: Optional[str] = None):
        """Start playback from either an SVO session or a video file.

        Caller must have already transitioned the mode to PLAYBACK.
        Exactly one of session_name / video_filename must be provided.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Playback already running")

        if bool(session_name) == bool(video_filename):
            raise ValueError("Specify exactly one of session_name or video_filename")

        if session_name:
            if sl is None:
                raise RuntimeError("pyzed.sl not available")
            svo_path = os.path.join(
                self.save_dir, "records", session_name, "recording.svo2"
            )
            if not os.path.isfile(svo_path):
                raise FileNotFoundError(f"SVO file not found: {svo_path}")
            source = "svo"
            source_name = session_name
            path = svo_path
            target = self._run_svo
        else:
            video_path = os.path.join(self.save_dir, "videos", video_filename)
            if not os.path.isfile(video_path):
                raise FileNotFoundError(f"Video file not found: {video_path}")
            source = "video"
            source_name = video_filename
            path = video_path
            target = self._run_video

        self._stop_flag = False
        self._paused = False

        self._state.set_playback(
            running=True,
            paused=False,
            source=source,
            source_name=source_name,
            session_name=source_name if source == "svo" else "",
            svo_path=path if source == "svo" else "",
            current_frame=0,
            total_frames=0,
            skipped_frames=0,
            phase="starting",
        )
        self._state.append_log(f"Playback start ({source}): {source_name}")

        # Ensure inference thread consumes the queue.
        self._inference.start_detecting()

        self._thread = threading.Thread(
            target=target, args=(path,), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop_flag = True
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)
        self._thread = None
        self._inference.stop_detecting()
        self._state.set_playback(running=False, paused=False, phase="stopped")

    def set_paused(self, paused: bool):
        with self._lock:
            self._paused = paused
        self._state.set_playback(paused=paused)

    # ----------------------------------------------------------------
    # Thread body — shared helpers
    # ----------------------------------------------------------------
    def _push_frame(self, bgr: np.ndarray, skipped: int) -> int:
        """Push a BGR frame to the inference queue (non-blocking).

        Returns the updated skipped-frames count.
        """
        if self._queue.full():
            try:
                self._queue.get_nowait()
                skipped += 1
            except queue.Empty:
                pass
        try:
            self._queue.put_nowait(bgr)
        except queue.Full:
            skipped += 1
        return skipped

    # ----------------------------------------------------------------
    # Thread body — SVO
    # ----------------------------------------------------------------
    def _run_svo(self, svo_path: str):
        zed = sl.Camera()
        init = sl.InitParameters()
        init.set_from_svo_file(svo_path)
        init.svo_real_time_mode = False  # we control pacing ourselves
        init.depth_mode = sl.DEPTH_MODE.NONE
        init.coordinate_units = sl.UNIT.METER

        err = zed.open(init)
        if err != sl.ERROR_CODE.SUCCESS:
            self._state.append_log(
                f"ERROR: Failed to open SVO: {repr(err)}"
            )
            self._state.set_playback(running=False, phase="error")
            self._return_to_idle()
            return

        try:
            cam_info = zed.get_camera_information()
            orig_w = cam_info.camera_configuration.resolution.width
            orig_h = cam_info.camera_configuration.resolution.height
            native_fps = cam_info.camera_configuration.fps or 30
            total = zed.get_svo_number_of_frames()
            if total <= 0:
                total = 0

            if self.process_width > 0 and orig_w > 0:
                scale = self.process_width / orig_w
                proc_w = self.process_width
                proc_h = int(orig_h * scale)
            else:
                proc_w = orig_w
                proc_h = orig_h

            self._state.set_playback(
                total_frames=total, phase="playing"
            )
            self._state.append_log(
                f"Playback: {orig_w}x{orig_h} @ {native_fps}fps, "
                f"{total} frames"
            )

            frame_interval = 1.0 / float(native_fps)
            image_left = sl.Mat()
            runtime = sl.RuntimeParameters()
            next_deadline = time.monotonic()
            skipped = 0
            current = 0

            while not self._stop_flag:
                with self._lock:
                    paused = self._paused
                if paused:
                    time.sleep(0.05)
                    next_deadline = time.monotonic()
                    continue

                err = zed.grab(runtime)
                if err == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                    self._state.set_playback(phase="completed")
                    self._state.append_log("Playback: end of file.")
                    break
                if err != sl.ERROR_CODE.SUCCESS:
                    self._state.append_log(
                        f"Playback grab error: {repr(err)}"
                    )
                    break

                zed.retrieve_image(image_left, sl.VIEW.LEFT)
                arr = image_left.get_data()
                if arr is None:
                    continue
                bgra = np.ascontiguousarray(arr)
                bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
                if proc_w > 0 and (bgr.shape[1] != proc_w or bgr.shape[0] != proc_h):
                    bgr = cv2.resize(bgr, (proc_w, proc_h))

                skipped = self._push_frame(bgr, skipped)
                current += 1
                if current % 5 == 0 or current == total:
                    self._state.set_playback(
                        current_frame=current,
                        skipped_frames=skipped,
                    )

                # Pace at native FPS.
                next_deadline += frame_interval
                sleep_for = next_deadline - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    # Running behind — don't try to catch up.
                    next_deadline = time.monotonic()

        finally:
            try:
                zed.close()
            except Exception:
                pass

        # Final state update and return to IDLE.
        self._state.set_playback(running=False)
        self._return_to_idle()

    # ----------------------------------------------------------------
    # Thread body — generic video (cv2.VideoCapture)
    # ----------------------------------------------------------------
    def _run_video(self, video_path: str):
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            self._state.append_log(f"ERROR: Failed to open video: {video_path}")
            self._state.set_playback(running=False, phase="error")
            self._return_to_idle()
            return

        try:
            native_fps = cap.get(cv2.CAP_PROP_FPS) or 0
            if native_fps <= 0 or native_fps != native_fps:  # 0 or NaN
                native_fps = 30.0
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

            if self.process_width > 0 and orig_w > 0:
                scale = self.process_width / orig_w
                proc_w = self.process_width
                proc_h = int(orig_h * scale)
            else:
                proc_w = orig_w
                proc_h = orig_h

            self._state.set_playback(total_frames=total, phase="playing")
            self._state.append_log(
                f"Playback: {orig_w}x{orig_h} @ {native_fps:.2f}fps, "
                f"{total} frames (video)"
            )

            frame_interval = 1.0 / float(native_fps)
            next_deadline = time.monotonic()
            skipped = 0
            current = 0

            while not self._stop_flag:
                with self._lock:
                    paused = self._paused
                if paused:
                    time.sleep(0.05)
                    next_deadline = time.monotonic()
                    continue

                ok, bgr = cap.read()
                if not ok or bgr is None:
                    self._state.set_playback(phase="completed")
                    self._state.append_log("Playback: end of file.")
                    break

                if proc_w > 0 and (bgr.shape[1] != proc_w or bgr.shape[0] != proc_h):
                    bgr = cv2.resize(bgr, (proc_w, proc_h))

                skipped = self._push_frame(bgr, skipped)
                current += 1
                if current % 5 == 0 or current == total:
                    self._state.set_playback(
                        current_frame=current, skipped_frames=skipped
                    )

                next_deadline += frame_interval
                sleep_for = next_deadline - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_deadline = time.monotonic()

        finally:
            try:
                cap.release()
            except Exception:
                pass

        self._state.set_playback(running=False)
        self._return_to_idle()

    def _return_to_idle(self):
        """Transition back to IDLE mode if we are still in PLAYBACK."""
        if self._mode_manager is None:
            return
        if self._state.get_mode() == Mode.PLAYBACK:
            self._mode_manager.request_mode(Mode.IDLE, source="playback")
