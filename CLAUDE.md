# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ridge Detector v2: ZED2 stereo camera + YOLO11-seg ridge detection system for agricultural use on Jetson Orin Nano. Multi-threaded Python architecture (no ROS2) with FastAPI server, PWA frontend, and optional PySide6 display. Documentation is in Japanese (`doc/`).

**Platform**: Jetson Orin Nano, Ubuntu 22.04, Python 3.10, ZED SDK 5.x, CUDA

## Running the Application

```bash
source ~/zed_yolo_venv/bin/activate

# Standard (display + server)
python main.py

# Headless (server only)
python main.py --no-display

# Compact mode (7-inch display)
python main.py --compact

# Full options
python main.py --compact --save-dir ~/zed_records --serial-port /dev/ttyTHS1 --serial-baud 19200 --inference-fps 30 --capture-probability 0.02 --port 8000
```

Additional dependencies beyond the reference venv: `pip install fastapi uvicorn websockets`

YOLO model auto-detected from: `./ridge-yolo11s-seg.pt`, `~/RidgeDetector/ridge-yolo11s-seg.pt`, or `./reference/RidgeDetector/ridge-yolo11s-seg.pt`.

## Architecture

### Threading Model

| Thread | Class | Role |
|--------|-------|------|
| Main thread | PySide6 event loop or headless `Event.wait()` | Display or signal handling |
| CameraThread | `workers/camera_thread.py` | ZED grab, SVO2 recording, IMU CSV, random frame capture |
| InferenceThread | `workers/inference_thread.py` | YOLO-seg inference, EMA filter, UBX serial output |
| FastAPI server | `server/runner.py` (uvicorn daemon) | REST API + WebSocket |
| Training | `training/train_process.py` (subprocess.Popen) | Isolated CUDA context for model training |

### State Management (replaces Qt Signals)

`state/shared_state.py` — Single `threading.Lock` (deadlock-free), `threading.Event` for change notification. Workers write via `set_*()`, server/display read via `get_*()` + `Event.wait()`.

### Exclusive Mode State Machine

`state/mode_manager.py` — Four modes: **IDLE**, **RECORDING**, **DETECTING**, **TRAINING**. Only IDLE↔other transitions allowed. Direct transitions between non-IDLE modes are blocked. Callbacks registered at startup in `main.py` wire ModeManager to workers without circular imports.

**Important**: `start_training` callback is `None` because training params (epochs, batch_size, img_size) come from the API — the actual training is started in `server/routes_api.py` after the mode transition.

### Data Flow

```
CameraThread --[queue.Queue(maxsize=2)]--> InferenceThread
Workers --[SharedState.set_*()]--> FastAPI / DisplayWindow [.get_*()]
WebSocket: frame (base64 ~5fps), detection, training, status, log
```

### Recording Session Structure

```
~/zed_records/{timestamp}/
├── recording.svo2    # SVO2 raw data
├── imu.csv           # IMU log
├── frames/           # Random JPEG captures (--capture-probability)
│   └── frame_XXXXXX.jpg
└── labels/           # YOLO polygon annotations (from PWA)
    └── frame_XXXXXX.txt    # Format: "0 x1 y1 x2 y2 x3 y3 x4 y4" (normalized)
```

### Ridge Detection Pipeline (`core/`)

Copied unchanged from `reference/RidgeDetector/core/`. Pipeline: YOLO-seg mask → scan line analysis (20 lines) → RANSAC/polyfit line fitting → (a, b) parameters → EMA smoothing → UBX-NAV-RELPOSNED serial output. `a` = slope (dx/dy), `b` = horizontal offset from image center.

### FastAPI Server (`server/`)

15 REST endpoints under `/api/` — status, mode control, session CRUD, frame serving, annotation CRUD, model listing, training control, logs. WebSocket at `/ws` with subscribe/channel model. PWA static files served from `web/` at root `/`.

### Training System (`training/`)

Uses `subprocess.Popen` for GPU memory isolation (CUDA context freed on process exit). `manager.py` collects annotated frames across all sessions, creates symlinked dataset with `dataset.yaml`, launches `train_process.py`, polls `progress.json` every 2s. On completion, reads `result.json` for new model path.

### PWA (`web/`)

Vanilla HTML/JS/CSS, no build step. 4 hash-routed screens: Dashboard (#/), Sessions (#/sessions), Annotation (#/sessions/{name}), Training (#/training). Canvas-based 4-point polygon annotation.

## Key Conventions

- Color format: ZED returns **BGRA**. Camera stores **BGR** in SharedState. Inference works on **BGR**.
- Queue overflow drops the **oldest** frame (not newest).
- Preview decimation uses monotonic timestamp comparison, not frame counting.
- Worker control methods (`start_recording`, `stop_detecting`, etc.) are called directly from ModeManager callbacks, not via signals.
- `core/` files must remain unchanged from reference — all adaptations happen in `workers/` and `state/`.

## Graceful Degradation

Each dependency fails independently without crashing the system:
- **pyserial missing** → serial disabled, inference continues
- **sklearn missing** → RANSAC falls back to numpy polyfit
- **PySide6 missing** → `--no-display` headless mode automatic fallback
- **IMU not detected** → SVO2 recording continues, CSV skipped
- **Serial port unavailable** → detection continues without serial output
