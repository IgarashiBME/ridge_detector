#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
REST API endpoints for Ridge Detector v2.
"""

import math
import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from state.shared_state import Mode

router = APIRouter()


# ----------------------------------------------------------------
# Request/Response models
# ----------------------------------------------------------------
class ModeRequest(BaseModel):
    mode: str


class AnnotationRequest(BaseModel):
    points: list  # [[x, y], [x, y], [x, y], [x, y]]


class TrainingStartRequest(BaseModel):
    epochs: int = 50
    batch_size: int = 4
    img_size: int = 640
    sessions: Optional[List[str]] = None
    lr0: float = 0.001
    lrf: float = 0.1
    freeze: int = 10
    flipud: float = 0.5
    amp: bool = True


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _get_state(request: Request):
    return request.app.state.shared_state


def _get_mode_manager(request: Request):
    return request.app.state.mode_manager


def _get_save_dir(request: Request) -> str:
    return os.path.expanduser(request.app.state.shared_state.save_dir)


def _sanitize_name(name: str) -> str:
    """Prevent path traversal."""
    return Path(name).name


def _nan_to_none(v):
    """Convert NaN to None for JSON serialization."""
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


# ----------------------------------------------------------------
# System
# ----------------------------------------------------------------
@router.get("/status")
def get_status(request: Request):
    state = _get_state(request)
    snap = state.snapshot()
    # Clean NaN values for JSON
    det = snap.get("detection", {})
    for k in ("a", "b"):
        if k in det:
            det[k] = _nan_to_none(det[k])
    return snap


@router.post("/mode")
def set_mode(request: Request, body: ModeRequest):
    mm = _get_mode_manager(request)
    try:
        target = Mode(body.mode.upper())
    except ValueError:
        raise HTTPException(400, f"Invalid mode: {body.mode}")
    ok, msg = mm.request_mode(target, source="API")
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


# ----------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------
@router.get("/sessions")
def list_sessions(request: Request):
    save_dir = _get_save_dir(request)
    if not os.path.isdir(save_dir):
        return []

    sessions = []
    for name in sorted(os.listdir(save_dir), reverse=True):
        session_path = os.path.join(save_dir, name)
        if not os.path.isdir(session_path):
            continue

        frames_dir = os.path.join(session_path, "frames")
        labels_dir = os.path.join(session_path, "labels")

        frame_count = 0
        if os.path.isdir(frames_dir):
            frame_count = len([
                f for f in os.listdir(frames_dir) if f.endswith('.jpg')
            ])

        annotated_count = 0
        if os.path.isdir(labels_dir):
            annotated_count = len([
                f for f in os.listdir(labels_dir) if f.endswith('.txt')
            ])

        # SVO2 size
        svo2_size = 0
        svo2_path = os.path.join(session_path, "recording.svo2")
        if os.path.isfile(svo2_path):
            svo2_size = os.path.getsize(svo2_path)

        sessions.append({
            "name": name,
            "frame_count": frame_count,
            "annotated_count": annotated_count,
            "svo2_size_mb": round(svo2_size / (1024 * 1024), 1),
        })

    return sessions


@router.delete("/sessions/{name}")
def delete_session(request: Request, name: str):
    save_dir = _get_save_dir(request)
    name = _sanitize_name(name)
    session_path = os.path.join(save_dir, name)

    if not os.path.isdir(session_path):
        raise HTTPException(404, "Session not found")

    import shutil
    shutil.rmtree(session_path)
    return {"ok": True, "message": f"Session {name} deleted"}


# ----------------------------------------------------------------
# Frames
# ----------------------------------------------------------------
@router.get("/sessions/{name}/frames")
def list_frames(request: Request, name: str):
    save_dir = _get_save_dir(request)
    name = _sanitize_name(name)
    frames_dir = os.path.join(save_dir, name, "frames")
    labels_dir = os.path.join(save_dir, name, "labels")

    if not os.path.isdir(frames_dir):
        raise HTTPException(404, "Session not found")

    frames = []
    for f in sorted(os.listdir(frames_dir)):
        if not f.endswith('.jpg'):
            continue
        stem = Path(f).stem
        label_exists = os.path.isfile(os.path.join(labels_dir, f"{stem}.txt"))
        frames.append({
            "filename": f,
            "annotated": label_exists,
        })

    return frames


@router.get("/sessions/{name}/frames/{frame}")
def get_frame(request: Request, name: str, frame: str):
    save_dir = _get_save_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    frame_path = os.path.join(save_dir, name, "frames", frame)

    if not os.path.isfile(frame_path):
        raise HTTPException(404, "Frame not found")

    return FileResponse(frame_path, media_type="image/jpeg")


# ----------------------------------------------------------------
# Annotations
# ----------------------------------------------------------------
@router.get("/sessions/{name}/frames/{frame}/annotation")
def get_annotation(request: Request, name: str, frame: str):
    save_dir = _get_save_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    stem = Path(frame).stem
    label_path = os.path.join(save_dir, name, "labels", f"{stem}.txt")

    if not os.path.isfile(label_path):
        return {"exists": False, "points": []}

    # Parse YOLO polygon format: class_id x1 y1 x2 y2 x3 y3 x4 y4
    with open(label_path, 'r') as f:
        line = f.readline().strip()
    if not line:
        return {"exists": False, "points": []}

    parts = line.split()
    if len(parts) < 9:
        return {"exists": False, "points": []}

    points = []
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            points.append([float(parts[i]), float(parts[i + 1])])

    return {"exists": True, "points": points}


@router.put("/sessions/{name}/frames/{frame}/annotation")
def put_annotation(request: Request, name: str, frame: str, body: AnnotationRequest):
    save_dir = _get_save_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    stem = Path(frame).stem

    # Verify frame exists
    frame_path = os.path.join(save_dir, name, "frames", frame)
    if not os.path.isfile(frame_path):
        raise HTTPException(404, "Frame not found")

    if len(body.points) != 4:
        raise HTTPException(400, "Exactly 4 points required")

    for p in body.points:
        if len(p) != 2:
            raise HTTPException(400, "Each point must be [x, y]")

    # Save as YOLO polygon format: class_id x1 y1 x2 y2 x3 y3 x4 y4
    # Coordinates are normalized (0-1)
    labels_dir = os.path.join(save_dir, name, "labels")
    os.makedirs(labels_dir, exist_ok=True)
    label_path = os.path.join(labels_dir, f"{stem}.txt")

    coords = []
    for p in body.points:
        coords.extend([f"{p[0]:.6f}", f"{p[1]:.6f}"])

    with open(label_path, 'w') as f:
        f.write("0 " + " ".join(coords) + "\n")

    return {"ok": True, "message": "Annotation saved"}


@router.delete("/sessions/{name}/frames/{frame}/annotation")
def delete_annotation(request: Request, name: str, frame: str):
    save_dir = _get_save_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    stem = Path(frame).stem
    label_path = os.path.join(save_dir, name, "labels", f"{stem}.txt")

    if os.path.isfile(label_path):
        os.remove(label_path)

    return {"ok": True, "message": "Annotation deleted"}


# ----------------------------------------------------------------
# Models
# ----------------------------------------------------------------
@router.get("/models")
def list_models(request: Request):
    """List available .pt model files."""
    # Search common locations
    model_dirs = [
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'),
        os.path.expanduser('~/RidgeDetector'),
        os.path.expanduser('~'),
    ]

    models = set()
    for d in model_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith('.pt'):
                full_path = os.path.join(d, f)
                if os.path.isfile(full_path):
                    models.add(full_path)

    # Also check training output dirs
    save_dir = _get_save_dir(request)
    train_dir = os.path.join(save_dir, "training_runs")
    if os.path.isdir(train_dir):
        for root, dirs, files in os.walk(train_dir):
            for f in files:
                if f.endswith('.pt'):
                    models.add(os.path.join(root, f))

    return [{"path": p, "name": os.path.basename(p),
             "size_mb": round(os.path.getsize(p) / (1024*1024), 1)}
            for p in sorted(models)]


# ----------------------------------------------------------------
# Training
# ----------------------------------------------------------------
@router.post("/training/start")
def training_start(request: Request, body: TrainingStartRequest):
    tm = request.app.state.training_manager
    mm = _get_mode_manager(request)

    # Must be IDLE to start training
    ok, msg = mm.request_mode(Mode.TRAINING, source="API")
    if not ok:
        raise HTTPException(409, msg)

    try:
        tm.start(
            epochs=body.epochs,
            batch_size=body.batch_size,
            img_size=body.img_size,
            sessions=body.sessions,
            lr0=body.lr0,
            lrf=body.lrf,
            freeze=body.freeze,
            flipud=body.flipud,
            amp=body.amp,
        )
    except Exception as e:
        # Revert to IDLE on failure
        mm.request_mode(Mode.IDLE, source="API")
        raise HTTPException(500, str(e))

    return {"ok": True, "message": "Training started"}


@router.post("/training/stop")
def training_stop(request: Request):
    mm = _get_mode_manager(request)
    ok, msg = mm.request_mode(Mode.IDLE, source="API")
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@router.get("/training/status")
def training_status(request: Request):
    state = _get_state(request)
    return state.get_training().__dict__


@router.get("/logs")
def get_logs(request: Request, n: int = 50):
    state = _get_state(request)
    return state.get_logs(last_n=n)
