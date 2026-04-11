#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TrainingManager: Manages YOLO training as a subprocess.

Uses subprocess.Popen for GPU memory isolation.
Polls progress.json written by train_process.py.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from state.shared_state import SharedState


def _now_stamp(state: SharedState) -> str:
    """Generate a timestamp string using client-synced time when available.

    Jetson has no NTP/RTC, so prefer the client-provided offset (set via
    /api/time/sync). Falls back to the Jetson local clock when unsynced.
    """
    epoch = state.corrected_now()
    if epoch is None:
        epoch = time.time()
    return datetime.fromtimestamp(epoch).strftime("%Y%m%d_%H%M%S")


class TrainingManager:
    """Manages training subprocess lifecycle."""

    def __init__(self, state: SharedState, save_dir: str = "~/zed_records",
                 base_model_path: str = "yolo11s-seg.pt",
                 mode_manager=None):
        self._state = state
        self._mode_manager = mode_manager
        self.save_dir = os.path.expanduser(save_dir)
        self.base_model_path = base_model_path
        self._process = None
        self._poll_thread = None
        self._stop_flag = False
        self._progress_path = ""

    def start(self, epochs: int = 50, batch_size: int = 4, img_size: int = 640,
              sessions: list = None, lr0: float = 0.001, lrf: float = 0.1,
              freeze: int = 10, flipud: float = 0.5, amp: bool = True):
        """Start training subprocess.

        Args:
            sessions: Optional list of session names to include.
                      If None or empty, all sessions are used.
            lr0: Initial learning rate.
            lrf: Final learning rate ratio.
            freeze: Number of backbone layers to freeze.
            flipud: Vertical flip augmentation probability.
            amp: Enable automatic mixed precision.
        """
        if self._process is not None and self._process.poll() is None:
            self._state.append_log("Training already running.")
            return

        # Collect annotated data
        dataset_info = self._collect_dataset(sessions=sessions)
        if dataset_info["count"] == 0:
            self._state.append_log("ERROR: No annotated frames found. Cannot start training.")
            raise ValueError("No annotated frames found")

        # Create training run directory
        run_dir = os.path.join(self.save_dir, "training_runs",
                               _now_stamp(self._state))
        os.makedirs(run_dir, exist_ok=True)

        # Save dataset_info.json (which sessions / how many frames)
        per_session = {}
        for img_path in dataset_info["images"]:
            session_name = Path(img_path).parent.parent.name
            per_session[session_name] = per_session.get(session_name, 0) + 1
        dataset_info_path = os.path.join(run_dir, "dataset_info.json")
        with open(dataset_info_path, 'w') as f:
            json.dump({
                "sessions": sorted(per_session.keys()),
                "total_frames": dataset_info["count"],
                "per_session": per_session,
            }, f, indent=2)

        # Write dataset.yaml
        dataset_yaml = self._create_dataset_yaml(dataset_info, run_dir)

        # Progress/result files
        self._progress_path = os.path.join(run_dir, "progress.json")
        result_path = os.path.join(run_dir, "result.json")

        # Launch subprocess
        train_script = os.path.join(os.path.dirname(__file__), "train_process.py")
        cmd = [
            sys.executable, train_script,
            "--model", self.base_model_path,
            "--dataset", dataset_yaml,
            "--epochs", str(epochs),
            "--batch-size", str(batch_size),
            "--img-size", str(img_size),
            "--run-dir", run_dir,
            "--progress-file", self._progress_path,
            "--result-file", result_path,
            "--lr0", str(lr0),
            "--lrf", str(lrf),
            "--freeze", str(freeze),
            "--flipud", str(flipud),
            "--amp", str(amp),
        ]

        self._state.append_log(f"Starting training: {epochs} epochs, batch={batch_size}")
        self._state.append_log(f"Dataset: {dataset_info['count']} annotated frames")

        self._stop_flag = False
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        self._state.set_training(
            running=True, epoch=0, total_epochs=epochs,
            loss=0.0, phase="starting",
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
            args=(result_path,),
            daemon=True,
        )
        self._poll_thread.start()

    def stop(self):
        """Stop training subprocess."""
        self._stop_flag = True
        if self._process is not None and self._process.poll() is None:
            self._state.append_log("Stopping training...")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self._state.set_training(running=False, phase="stopped")

    def _read_stdout(self):
        """Read subprocess stdout/stderr and forward to log and terminal."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip('\n')
            if line:
                print(f"[train] {line}", flush=True)
                self._state.append_log(f"[train] {line}")

    def _poll_progress(self, result_path: str):
        """Poll progress.json and update SharedState."""
        while not self._stop_flag:
            # Check if process is still running
            if self._process is not None and self._process.poll() is not None:
                break
            time.sleep(2.0)

            if os.path.isfile(self._progress_path):
                try:
                    with open(self._progress_path, 'r') as f:
                        prog = json.load(f)
                    self._state.set_training(
                        running=True,
                        epoch=prog.get("epoch", 0),
                        total_epochs=prog.get("total_epochs", 0),
                        loss=prog.get("loss", 0.0),
                        phase=prog.get("phase", "training"),
                    )
                except (json.JSONDecodeError, IOError):
                    pass

        # Process finished - check result
        if os.path.isfile(result_path):
            try:
                with open(result_path, 'r') as f:
                    result = json.load(f)
                new_model = result.get("model_path", "")
                if new_model and os.path.isfile(new_model):
                    # Copy to models/ with timestamp
                    copied = self._copy_model_to_models_dir(new_model)
                    self._state.set_training(
                        running=False, phase="completed",
                        new_model_path=copied or new_model,
                    )
                    self._state.append_log(
                        f"Training completed. New model: {copied or new_model}"
                    )
                else:
                    self._state.set_training(running=False, phase="completed")
                    self._state.append_log("Training completed (no model output found).")
            except (json.JSONDecodeError, IOError):
                self._state.set_training(running=False, phase="error")
                self._state.append_log("Training finished but result.json is invalid.")
        else:
            exit_code = self._process.returncode if self._process else -1
            if self._stop_flag:
                self._state.set_training(running=False, phase="stopped")
            else:
                self._state.set_training(running=False, phase="error")
                self._state.append_log(
                    f"Training process exited with code {exit_code}"
                )

        self._process = None

        # Return to IDLE mode
        if self._mode_manager:
            from state.mode_manager import Mode
            if self._state.get_mode() == Mode.TRAINING:
                self._mode_manager.request_mode(Mode.IDLE, source="training")

    def _collect_dataset(self, sessions: list = None) -> dict:
        """Scan sessions for annotated frames.

        Args:
            sessions: Optional list of session names to include.
                      If None or empty, all sessions are used.
        """
        images = []
        labels = []

        records_dir = os.path.join(self.save_dir, "records")
        if not os.path.isdir(records_dir):
            return {"count": 0, "images": [], "labels": []}

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
                    images.append(os.path.join(frames_dir, fname))
                    labels.append(label_path)

        return {"count": len(images), "images": images, "labels": labels}

    def _create_dataset_yaml(self, dataset_info: dict, run_dir: str) -> str:
        """Create YOLO dataset.yaml with symlinks to collected data."""
        dataset_dir = os.path.join(run_dir, "dataset")
        img_dir = os.path.join(dataset_dir, "images", "train")
        lbl_dir = os.path.join(dataset_dir, "labels", "train")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(lbl_dir, exist_ok=True)

        # Create symlinks. Frame filenames are globally unique by convention
        # (session stamp embedded at capture time), so we can link them as-is.
        # Legacy frames using the old "frame_NNNNNN" name still work because
        # only one such session can ever appear under a single training run
        # without colliding — if a collision does occur, fall back to a
        # session-prefixed name.
        for img_path, lbl_path in zip(dataset_info["images"], dataset_info["labels"]):
            img_name = os.path.basename(img_path)
            lbl_name = os.path.basename(lbl_path)

            img_link = os.path.join(img_dir, img_name)
            lbl_link = os.path.join(lbl_dir, lbl_name)

            if os.path.exists(img_link) or os.path.exists(lbl_link):
                session_name = Path(img_path).parent.parent.name
                img_link = os.path.join(img_dir, f"{session_name}_{img_name}")
                lbl_link = os.path.join(lbl_dir, f"{session_name}_{lbl_name}")

            if not os.path.exists(img_link):
                os.symlink(img_path, img_link)
            if not os.path.exists(lbl_link):
                os.symlink(lbl_path, lbl_link)

        # Write dataset.yaml
        yaml_path = os.path.join(dataset_dir, "dataset.yaml")
        with open(yaml_path, 'w') as f:
            f.write(f"path: {dataset_dir}\n")
            f.write("train: images/train\n")
            f.write("val: images/train\n")  # Use same for val (small dataset)
            f.write("\n")
            f.write("names:\n")
            f.write("  0: ridge\n")

        self._state.append_log(f"Dataset YAML: {yaml_path}")
        return yaml_path

    def _copy_model_to_models_dir(self, model_path: str) -> str:
        """Copy trained model to {save_dir}/models/ with timestamp name.

        Returns the destination path, or empty string on failure.
        """
        try:
            models_dir = os.path.join(self.save_dir, "models")
            os.makedirs(models_dir, exist_ok=True)

            # Build name: {base_model_stem}_{run_timestamp}.pt
            base_stem = Path(self.base_model_path).stem
            # run_dir is like .../training_runs/YYYYMMDD_HHMMSS/train/weights/best.pt
            # Extract timestamp from training_runs/{timestamp}
            parts = Path(model_path).parts
            run_timestamp = ""
            for i, p in enumerate(parts):
                if p == "training_runs" and i + 1 < len(parts):
                    run_timestamp = parts[i + 1]
                    break
            if not run_timestamp:
                run_timestamp = _now_stamp(self._state)

            dest_name = f"{base_stem}_{run_timestamp}.pt"
            dest_path = os.path.join(models_dir, dest_name)
            shutil.copy2(model_path, dest_path)
            self._state.append_log(f"Model copied to: {dest_path}")
            return dest_path
        except Exception as e:
            self._state.append_log(f"WARNING: Failed to copy model: {e}")
            return ""
