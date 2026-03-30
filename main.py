#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ridge Detector v2 - Entry Point

Multi-threaded Python application (no ROS2).
CameraThread + InferenceThread + FastAPI server + optional PySide6 display.
"""

import argparse
import os
import queue
import signal
import sys
import threading
import time


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ridge Detector v2: ZED2 + YOLO-seg ridge detection system"
    )

    # Camera
    parser.add_argument(
        '--save-dir', type=str, default='~/ridge_data',
        help='Data directory for recordings, models, and training (default: ~/ridge_data)')
    parser.add_argument(
        '--camera-fps', type=int, default=30,
        help='Camera FPS (default: 30)')
    parser.add_argument(
        '--camera-resolution', type=str, default='HD720',
        choices=['VGA', 'HD720', 'HD1080', 'HD2K'],
        help='Camera resolution (default: HD720)')

    # Inference
    parser.add_argument(
        '--model', type=str, default=None,
        help='YOLO model path (default: auto-detect)')
    parser.add_argument(
        '--conf', type=float, default=0.25,
        help='Confidence threshold (default: 0.25)')
    parser.add_argument(
        '--half', action='store_true', default=True,
        help='FP16 inference (default: enabled)')
    parser.add_argument(
        '--no-half', action='store_true',
        help='Disable FP16 inference')
    parser.add_argument(
        '--process-width', type=int, default=640,
        help='Inference resolution width (default: 640)')
    parser.add_argument(
        '--target-class', type=int, default=None,
        help='Target class ID (default: None = all classes)')
    parser.add_argument(
        '--fitting-mode', type=str, default='ransac',
        choices=['polyfit', 'ransac'],
        help='Line fitting mode (default: ransac)')
    parser.add_argument(
        '--num-lines', type=int, default=20,
        help='Number of scan lines (default: 20)')
    parser.add_argument(
        '--inference-fps', type=int, default=30,
        help='Inference FPS upper limit (default: 30)')

    # Serial
    parser.add_argument(
        '--serial-port', type=str, default="/dev/ttyTHS1",
        help='Serial port (default: /dev/ttyTHS1)')
    parser.add_argument(
        '--serial-baud', type=int, default=19200,
        help='Serial baud rate (default: 19200)')

    # EMA filter
    parser.add_argument(
        '--ema-alpha', type=float, default=0.3,
        help='EMA filter alpha (0.0-1.0, default: 0.3)')

    # Recording
    parser.add_argument(
        '--capture-probability', type=float, default=0.02,
        help='Random frame capture probability during recording (default: 0.02)')

    # Display
    parser.add_argument(
        '--no-display', action='store_true', default=False,
        help='Headless mode (no PySide6 window)')
    parser.add_argument(
        '--compact', action='store_true', default=False,
        help='Compact UI mode for small displays')

    # Server
    parser.add_argument(
        '--port', type=int, default=8000,
        help='FastAPI server port (default: 8000)')
    parser.add_argument(
        '--host', type=str, default='0.0.0.0',
        help='FastAPI server host (default: 0.0.0.0)')

    args = parser.parse_args()

    if args.no_half:
        args.half = False

    # Auto-detect model path from ~/ridge_data/models/
    if args.model is None:
        models_dir = os.path.expanduser(
            os.path.join(args.save_dir, 'models'))
        if os.path.isdir(models_dir):
            pts = sorted(f for f in os.listdir(models_dir)
                         if f.endswith('.pt'))
            if pts:
                args.model = os.path.join(models_dir, pts[0])
        if args.model is None:
            args.model = 'yolo11s-seg.pt'

    return args


def main():
    args = parse_args()

    # Import after arg parsing to avoid loading PySide6 in headless mode
    from state.shared_state import SharedState
    from state.mode_manager import ModeManager

    state = SharedState(save_dir=args.save_dir)
    mode_manager = ModeManager(state)

    # Inference queue (CameraThread -> InferenceThread)
    inference_queue = queue.Queue(maxsize=2)

    # Create workers
    from workers.camera_thread import CameraThread
    from workers.inference_thread import InferenceThread

    camera = CameraThread(
        state=state,
        save_dir=args.save_dir,
        camera_fps=args.camera_fps,
        camera_resolution=args.camera_resolution,
        preview_fps=15,
        process_width=args.process_width,
        inference_queue=inference_queue,
        capture_probability=args.capture_probability,
    )

    inference = InferenceThread(
        state=state,
        inference_queue=inference_queue,
        model_path=args.model,
        conf=args.conf,
        half=args.half,
        target_class=args.target_class,
        fitting_mode=args.fitting_mode,
        num_lines=args.num_lines,
        inference_fps=args.inference_fps,
        serial_port=args.serial_port,
        serial_baud=args.serial_baud,
        ema_alpha=args.ema_alpha,
    )

    # Training manager
    from training.manager import TrainingManager
    training_manager = TrainingManager(
        state=state,
        save_dir=args.save_dir,
        base_model_path=args.model,
        mode_manager=mode_manager,
    )

    # Register mode callbacks
    # Note: start_training is None here because training is started via API
    # with parameters (epochs, batch_size, img_size). The mode transition
    # happens in routes_api.py before calling training_manager.start().
    mode_manager.register_callbacks(
        start_recording=camera.start_recording,
        stop_recording=camera.stop_recording,
        start_detecting=lambda: (camera.start_detecting(), inference.start_detecting()),
        stop_detecting=lambda: (inference.stop_detecting(), camera.stop_detecting()),
        start_training=None,
        stop_training=lambda: training_manager.stop(),
    )

    # Start workers
    camera.start()
    inference.start()

    # Start FastAPI server
    from server.runner import start_server
    start_server(
        state=state,
        mode_manager=mode_manager,
        inference_thread=inference,
        training_manager=training_manager,
        host=args.host,
        port=args.port,
    )

    state.append_log(f"Server started on {args.host}:{args.port}")

    # Optional display
    display_app = None
    if not args.no_display:
        try:
            from display.display_window import run_display
            # Display runs in main thread (Qt requirement)
            # This call blocks until the window is closed
            state.append_log("Starting display window...")
            run_display(state, compact=args.compact)
        except ImportError as e:
            state.append_log(f"Display unavailable (PySide6 not installed): {e}")
            state.append_log("Running in headless mode.")
            _run_headless(state, mode_manager, camera, inference)
        except Exception as e:
            state.append_log(f"Display error: {e}")
            _run_headless(state, mode_manager, camera, inference)
    else:
        _run_headless(state, mode_manager, camera, inference)

    # Shutdown
    _shutdown(state, mode_manager, camera, inference, training_manager)


def _run_headless(state, mode_manager, camera, inference):
    """Headless main loop - wait for SIGINT/SIGTERM."""
    shutdown_event = threading.Event()

    def _signal_handler(sig, frame):
        state.append_log(f"Received signal {sig}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    state.append_log("Running in headless mode. Press Ctrl+C to stop.")

    shutdown_event.wait()


def _shutdown(state, mode_manager, camera, inference, training_manager):
    """Graceful shutdown of all components."""
    state.append_log("Shutting down...")

    # Stop current mode
    mode_manager.shutdown("shutdown")

    # Stop training if running
    training_manager.stop()

    # Stop workers
    inference.request_stop()
    camera.request_stop()

    # Wait for threads
    inference.join(timeout=3.0)
    camera.join(timeout=3.0)

    state.append_log("Shutdown complete.")


if __name__ == "__main__":
    main()
