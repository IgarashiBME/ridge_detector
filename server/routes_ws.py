#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
WebSocket endpoint for Ridge Detector v2.

Server -> Client messages:
  {type: "status", data: {...}}
  {type: "detection", data: {...}}
  {type: "training", data: {...}}
  {type: "log", data: [...]}
  {type: "frame", data: "<base64 jpeg>"}

Client -> Server messages:
  {type: "subscribe", channels: ["status", "detection", "frame", ...]}
  {type: "ping"}
"""

import asyncio
import base64
import math
import time

import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


def _nan_to_none(v):
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _frame_to_base64(frame: np.ndarray, quality: int = 50) -> str:
    """Encode BGR frame as base64 JPEG."""
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode('ascii')


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state = ws.app.state.shared_state

    # Default subscriptions
    channels = {"status", "detection", "training", "evaluation", "log", "frame"}

    # Track what was last sent to avoid redundant updates
    last_mode = None
    last_detection_time = 0.0
    last_training_time = 0.0
    last_frame_time = 0.0
    last_log_count = 0

    frame_interval = 1.0 / 5.0  # ~5 FPS for WebSocket frames

    # Send existing logs on connect
    existing_logs = state.get_logs(last_n=50)
    if existing_logs:
        await ws.send_json({"type": "log", "data": existing_logs})
        last_log_count = len(existing_logs)

    try:
        while True:
            # Check for incoming messages (non-blocking)
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=0.05)
                if msg.get("type") == "subscribe":
                    channels = set(msg.get("channels", []))
                elif msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()

            # Status updates (on mode change)
            if "status" in channels:
                if state.mode_changed.is_set():
                    state.mode_changed.clear()
                    snap = state.snapshot()
                    det = snap.get("detection", {})
                    for k in ("a", "b"):
                        if k in det:
                            det[k] = _nan_to_none(det[k])
                    await ws.send_json({"type": "status", "data": snap})
                    last_mode = snap.get("mode")

            # Detection updates
            if "detection" in channels:
                if state.detection_updated.is_set():
                    state.detection_updated.clear()
                    det = state.get_detection()
                    await ws.send_json({"type": "detection", "data": {
                        "a": _nan_to_none(det.a),
                        "b": _nan_to_none(det.b),
                        "fps": det.fps,
                        "infer_time_ms": det.infer_time_ms,
                        "serial_status": det.serial_status,
                        "serial_count": det.serial_count,
                    }})

            # Training updates
            if "training" in channels:
                if state.training_updated.is_set():
                    state.training_updated.clear()
                    t = state.get_training()
                    await ws.send_json({"type": "training", "data": {
                        "running": t.running,
                        "epoch": t.epoch,
                        "total_epochs": t.total_epochs,
                        "loss": t.loss,
                        "phase": t.phase,
                    }})

            # Evaluation updates
            if "evaluation" in channels:
                if state.evaluation_updated.is_set():
                    state.evaluation_updated.clear()
                    ev = state.get_evaluation()
                    await ws.send_json({"type": "evaluation", "data": {
                        "running": ev.running,
                        "current_frame": ev.current_frame,
                        "total_frames": ev.total_frames,
                        "phase": ev.phase,
                        "model_name": ev.model_name,
                        "avg_iou": ev.avg_iou,
                    }})

            # Log updates
            if "log" in channels:
                if state.log_updated.is_set():
                    state.log_updated.clear()
                    logs = state.get_logs(last_n=10)
                    if len(logs) > last_log_count:
                        new_logs = logs[last_log_count:] if last_log_count > 0 else logs
                        last_log_count = len(logs)
                        await ws.send_json({"type": "log", "data": new_logs})

            # Frame updates (~5 FPS)
            if "frame" in channels:
                if (now - last_frame_time) >= frame_interval:
                    if state.frame_updated.is_set():
                        state.frame_updated.clear()
                        frame = state.get_display_frame()
                        if frame is not None:
                            b64 = _frame_to_base64(frame)
                            await ws.send_json({"type": "frame", "data": b64})
                            last_frame_time = now

            await asyncio.sleep(0.02)  # ~50Hz polling

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
