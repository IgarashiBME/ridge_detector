# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

```bash
# Activate the virtual environment (required for ultralytics, pyzed, PySide6)
source ~/zed_yolo_venv/bin/activate

# Standard launch
python main.py

# Compact mode for 7-inch displays
python main.py --compact

# Full options example
python main.py --compact --save-dir ~/zed_records --gpio-rec-pin 31 --gpio-det-pin 33 --serial-port /dev/ttyTHS1 --serial-baud 19200 --inference-fps 30
```

Deployed as a systemd service (`ridge-detector.service`): `sudo systemctl start ridge-detector`

## Target Platform

Jetson Orin Nano, Ubuntu 22.04, Python 3.10, ZED SDK 5.x, CUDA-enabled YOLO inference.

## Architecture

### Thread Model (4 threads)

- **UIThread**: PySide6 main loop. Owns `MainWindow`, handles all display and user input.
- **CameraThread**: Runs `zed.grab()` continuously at camera FPS. In RECORDING mode, enables SVO2 + IMU CSV. In DETECTING mode, pushes resized BGR frames into a `queue.Queue(maxsize=2)`.
- **InferenceThread**: Reads frames from the queue, runs YOLO-seg, computes ridge line parameters (a, b), builds UBX-NAV-RELPOSNED messages, sends via serial, emits annotated frames to UI.
- **GpioWatcherThread**: Polls GPIO pins via `gpioget` (libgpiod CLI) at 20Hz with debounce. Emits signals on state change.

### Data Flow

```
CameraThread --[queue.Queue(maxsize=2)]--> InferenceThread
InferenceThread --[sig_frame, sig_result]--> MainWindow
CameraThread --[sig_frame]--> MainWindow (IDLE/RECORDING preview)
GpioWatcherThread --[sig_rec_trigger, sig_det_trigger]--> MainWindow
```

### Exclusive Mode State Machine

Three states: **IDLE**, **RECORDING**, **DETECTING**. Only IDLE↔RECORDING and IDLE↔DETECTING transitions allowed. Direct RECORDING↔DETECTING is blocked with a warning log. Both GPIO triggers and GUI buttons go through the same `_request_*` methods which enforce exclusivity. The `self._mode` must be set BEFORE calling worker start/stop methods (signals from workers trigger `_update_mode_ui` synchronously on the UI thread).

### GPIO Backend

Uses `gpioget --bias=pull-down` (libgpiod) instead of Jetson.GPIO (which fails on some Orin Nano configurations). BOARD pin → gpiochip0 line mapping is hardcoded in `ORIN_BOARD_TO_LINE` dict. Initial pin state is read at startup to avoid false triggers.

### Color Format

ZED SDK `get_data()` returns **BGRA**, not RGBA. Camera worker converts BGRA→RGB for QImage preview and BGRA→BGR for inference input.

### UBX Protocol

Ridge detection parameters (a, b) are encoded into UBX-NAV-RELPOSNED fields: `relPosN_cm = int(a * 100)`, `relPosE_cm = int(b * 100)`. `gnssFixOK=1` when detection is valid, `0` otherwise.

## Graceful Degradation

Each optional dependency degrades independently without crashing:
- **Jetson.GPIO unavailable** → falls back to libgpiod gpioget
- **libgpiod unavailable** → GPIO disabled, GUI buttons still work
- **pyserial missing / port unavailable** → serial disabled, inference continues
- **sklearn missing** → RANSAC falls back to numpy polyfit
- **IMU not detected** → CSV recording disabled, SVO2 continues

## Key Conventions

- Worker control methods (start_recording, stop_detecting, etc.) are called directly from the UI thread, not via signal-slot. The `_detecting` / `_recording` flags are simple booleans read by the worker thread's `run()` loop.
- Preview frames are decimated by timestamp comparison, not by frame counting.
- Inference queue overflow drops the oldest frame (not the newest).
- All log output goes through `sig_status`/`sig_error` signals to the UI log widget.
