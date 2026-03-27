#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ZED2 Ridge Detector & Recorder - Integrated GUI Application

Combines SVO2 recording and YOLO-seg ridge detection with
GPIO triggers and serial output in a single PySide6 application.

Usage:
    python main.py [options]

See --help for full argument list.
"""

import sys
import signal
import argparse

from PySide6 import QtWidgets

from gui.main_window import MainWindow


def parse_args():
    parser = argparse.ArgumentParser(
        description="ZED2 Ridge Detector & Recorder GUI"
    )

    # Camera
    parser.add_argument(
        '--save-dir', type=str, default='~/zed_records',
        help='SVO2 save directory (default: ~/zed_records)')
    parser.add_argument(
        '--camera-fps', type=int, default=30,
        help='Camera FPS (default: 30)')
    parser.add_argument(
        '--camera-resolution', type=str, default='HD720',
        choices=['VGA', 'HD720', 'HD1080', 'HD2K'],
        help='Camera resolution (default: HD720)')

    # Inference (detection mode)
    parser.add_argument(
        '--model', type=str, default='/home/ubuntu/RidgeDetector/ridge-yolo11s-seg.pt',
        help='YOLO model path (default: yolo11s-seg.pt)')
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
        help='Inference FPS upper limit (default: 10)')

    # Serial (detection mode)
    parser.add_argument(
        '--serial-port', type=str, default="/dev/ttyTHS1",
        help='Serial port (default: None = serial disabled)')
    parser.add_argument(
        '--serial-baud', type=int, default=19200,
        help='Serial baud rate (default: 19200)')

    # GPIO
    parser.add_argument(
        '--gpio-rec-pin', type=int, default=31,
        help='GPIO pin for recording trigger (default: None = GPIO rec disabled)')
    parser.add_argument(
        '--gpio-det-pin', type=int, default=33,
        help='GPIO pin for detection trigger (default: None = GPIO det disabled)')
    parser.add_argument(
        '--debounce-ms', type=int, default=500,
        help='GPIO debounce time in ms (default: 500)')

    # EMA filter
    parser.add_argument(
        '--ema-alpha', type=float, default=0.3,
        help='EMA filter alpha for a,b smoothing (0.0-1.0, default: 0.3). '
             'Higher = more responsive, lower = more smooth. 1.0 = no filter.')

    # Display
    parser.add_argument(
        '--compact', action='store_true', default=False,
        help='Compact UI mode for small displays (7-inch etc.)')

    args = parser.parse_args()

    # Handle --no-half overriding --half
    if args.no_half:
        args.half = False

    return args


def main():
    args = parse_args()

    app = QtWidgets.QApplication(sys.argv)

    # Allow Ctrl+C to close the application
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    window = MainWindow(args)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
