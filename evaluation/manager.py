#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
EvaluationManager: Manages IoU evaluation as a subprocess.

Uses subprocess.Popen for GPU memory isolation (same pattern as TrainingManager).
Polls progress.json written by eval_process.py.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from state.shared_state import SharedState


class EvaluationManager:
    """Manages evaluation subprocess lifecycle."""

    def __init__(self, state: SharedState, save_dir: str = "~/ridge_data",
                 mode_manager=None):
        self._state = state
        self._mode_manager = mode_manager
        self.save_dir = os.path.expanduser(save_dir)
        self._process = None
        self._poll_thread = None
        self._stdout_thread = None
        self._stop_flag = False
        self._progress_path = ""

    def start(self, model_path: str, sessions: list,
              img_size: int = 640, conf: float = 0.25):
        """Start evaluation subprocess."""
        if self._process is not None and self._process.poll() is None:
            self._state.append_log("Evaluation already running.")
            return

        if not os.path.isfile(model_path):
            raise ValueError(f"Model not found: {model_path}")

        # Collect annotated frames
        frames = self._collect_frames(sessions)
        if not frames:
            self._state.append_log("ERROR: No annotated frames found for evaluation.")
            raise ValueError("No annotated frames found")

        model_name = os.path.basename(model_path)

        # Create a temporary work directory for this evaluation run
        run_timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(self.save_dir, "evaluation_runs", run_timestamp)
        os.makedirs(run_dir, exist_ok=True)

        # Write frames list
        frames_json_path = os.path.join(run_dir, "frames.json")
        with open(frames_json_path, 'w') as f:
            json.dump(frames, f, indent=2)

        # Progress/result files
        self._progress_path = os.path.join(run_dir, "progress.json")
        result_path = os.path.join(run_dir, "result.json")

        # Launch subprocess
        eval_script = os.path.join(os.path.dirname(__file__), "eval_process.py")
        cmd = [
            sys.executable, eval_script,
            "--model", model_path,
            "--frames-json", frames_json_path,
            "--img-size", str(img_size),
            "--conf", str(conf),
            "--progress-file", self._progress_path,
            "--result-file", result_path,
        ]

        self._state.append_log(f"Starting evaluation: {model_name}")
        self._state.append_log(f"Frames: {len(frames)} annotated frames")

        self._stop_flag = False
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        self._state.set_evaluation(
            running=True,
            current_frame=0,
            total_frames=len(frames),
            phase="starting",
            model_name=model_name,
            avg_iou=0.0,
        )

        # Start stdout reader thread
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            daemon=True,
        )
        self._stdout_thread.start()

        # Start polling thread
        self._poll_thread = threading.Thread(
            target=self._poll_progress,
            args=(result_path, model_path, sessions, run_timestamp),
            daemon=True,
        )
        self._poll_thread.start()

    def stop(self):
        """Stop evaluation subprocess."""
        self._stop_flag = True
        if self._process is not None and self._process.poll() is None:
            self._state.append_log("Stopping evaluation...")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self._state.set_evaluation(running=False, phase="stopped")

    def _read_stdout(self):
        """Read subprocess stdout/stderr and forward to log and terminal."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip('\n')
            if line:
                print(f"[eval] {line}", flush=True)
                self._state.append_log(f"[eval] {line}")

    def _poll_progress(self, result_path: str, model_path: str,
                       sessions: list, run_timestamp: str):
        """Poll progress.json and update SharedState."""
        while not self._stop_flag:
            if self._process is not None and self._process.poll() is not None:
                break
            time.sleep(1.0)

            if os.path.isfile(self._progress_path):
                try:
                    with open(self._progress_path, 'r') as f:
                        prog = json.load(f)
                    self._state.set_evaluation(
                        running=True,
                        current_frame=prog.get("current_frame", 0),
                        total_frames=prog.get("total_frames", 0),
                        phase=prog.get("phase", "inferring"),
                    )
                except (json.JSONDecodeError, IOError):
                    pass

        # Process finished - check result
        if os.path.isfile(result_path):
            try:
                with open(result_path, 'r') as f:
                    result = json.load(f)

                avg_iou = result.get("avg_iou", 0.0)
                model_name = result.get("model_name", "")

                # Save result to each session directory
                self._save_results_to_sessions(result, model_path, run_timestamp)

                self._state.set_evaluation(
                    running=False,
                    phase="completed",
                    avg_iou=avg_iou,
                    model_name=model_name,
                )
                self._state.append_log(
                    f"Evaluation completed. Average IoU: {avg_iou:.4f}"
                )
            except (json.JSONDecodeError, IOError):
                self._state.set_evaluation(running=False, phase="error")
                self._state.append_log("Evaluation finished but result.json is invalid.")
        else:
            exit_code = self._process.returncode if self._process else -1
            if self._stop_flag:
                self._state.set_evaluation(running=False, phase="stopped")
            else:
                self._state.set_evaluation(running=False, phase="error")
                self._state.append_log(
                    f"Evaluation process exited with code {exit_code}"
                )

        self._process = None

        # Return to IDLE mode
        if self._mode_manager:
            from state.shared_state import Mode
            if self._state.get_mode() == Mode.EVALUATING:
                self._mode_manager.request_mode(Mode.IDLE, source="evaluation")

    def _save_results_to_sessions(self, result: dict, model_path: str,
                                  run_timestamp: str):
        """Save evaluation results to each session directory."""
        model_stem = Path(model_path).stem
        per_frame = result.get("per_frame", [])
        sessions_in_result = set(result.get("sessions", []))

        for session_name in sessions_in_result:
            # Filter per_frame for this session
            session_frames = [
                f for f in per_frame if f.get("session") == session_name
            ]
            iou_values = [
                f["iou"] for f in session_frames
                if f.get("iou") is not None
            ]
            session_avg = sum(iou_values) / len(iou_values) if iou_values else 0.0

            session_result = {
                "model_name": result.get("model_name", ""),
                "model_path": model_path,
                "timestamp": run_timestamp,
                "total_frames": len(session_frames),
                "evaluated_frames": len(iou_values),
                "avg_iou": round(session_avg, 4),
                "per_frame": session_frames,
            }

            # Write to session directory
            session_dir = os.path.join(self.save_dir, "records", session_name)
            if os.path.isdir(session_dir):
                filename = f"evaluation_{model_stem}_{run_timestamp}.json"
                filepath = os.path.join(session_dir, filename)
                try:
                    with open(filepath, 'w') as f:
                        json.dump(session_result, f, indent=2)
                    self._state.append_log(f"Saved: {session_name}/{filename}")
                except Exception as e:
                    self._state.append_log(
                        f"WARNING: Failed to save result for {session_name}: {e}"
                    )

    def _collect_frames(self, sessions: list) -> list:
        """Collect annotated frames from selected sessions.

        Returns list of dicts: [{image_path, label_path, session, frame_name}, ...]
        """
        frames = []
        records_dir = os.path.join(self.save_dir, "records")
        if not os.path.isdir(records_dir):
            return frames

        filter_sessions = set(sessions) if sessions else None

        for session_name in sorted(os.listdir(records_dir)):
            if filter_sessions and session_name not in filter_sessions:
                continue
            session_path = os.path.join(records_dir, session_name)
            if not os.path.isdir(session_path):
                continue
            frames_dir = os.path.join(session_path, "frames")
            labels_dir = os.path.join(session_path, "labels")
            if not os.path.isdir(frames_dir) or not os.path.isdir(labels_dir):
                continue

            for fname in sorted(os.listdir(frames_dir)):
                if not fname.lower().endswith(('.jpg', '.jpeg', '.png')):
                    continue
                stem = Path(fname).stem
                label_path = os.path.join(labels_dir, f"{stem}.txt")
                if os.path.isfile(label_path):
                    frames.append({
                        "image_path": os.path.join(frames_dir, fname),
                        "label_path": label_path,
                        "session": session_name,
                        "frame_name": fname,
                    })

        return frames
