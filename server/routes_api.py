#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
REST API endpoints for Ridge Detector v2.
"""

import math
import os
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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


class ModelSelectRequest(BaseModel):
    path: str


class EmaAlphaRequest(BaseModel):
    alpha: float


class ConfRequest(BaseModel):
    conf: float


class TestDetectRequest(BaseModel):
    session: str
    frame: str


class EvaluationStartRequest(BaseModel):
    model_path: str
    sessions: List[str]
    img_size: int = 640
    conf: float = 0.25


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _get_state(request: Request):
    return request.app.state.shared_state


def _get_mode_manager(request: Request):
    return request.app.state.mode_manager


def _get_save_dir(request: Request) -> str:
    return os.path.expanduser(request.app.state.shared_state.save_dir)


def _get_records_dir(request: Request) -> str:
    return os.path.join(_get_save_dir(request), "records")


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
    inference = request.app.state.inference_thread
    snap["ema_alpha"] = inference.ema_alpha
    snap["conf"] = inference.conf
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
    records_dir = _get_records_dir(request)
    if not os.path.isdir(records_dir):
        return []

    sessions = []
    for name in sorted(os.listdir(records_dir), reverse=True):
        session_path = os.path.join(records_dir, name)
        if not os.path.isdir(session_path):
            continue

        frames_dir = os.path.join(session_path, "frames")
        labels_dir = os.path.join(session_path, "labels")

        frame_count = 0
        if os.path.isdir(frames_dir):
            frame_count = len([
                f for f in os.listdir(frames_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))
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

        downloadable = not os.path.isfile(
            os.path.join(session_path, ".nodownload")
        )

        sessions.append({
            "name": name,
            "frame_count": frame_count,
            "annotated_count": annotated_count,
            "svo2_size_mb": round(svo2_size / (1024 * 1024), 1),
            "downloadable": downloadable,
        })

    return sessions


@router.delete("/sessions/{name}")
def delete_session(request: Request, name: str):
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    session_path = os.path.join(records_dir, name)

    if not os.path.isdir(session_path):
        raise HTTPException(404, "Session not found")

    import shutil
    shutil.rmtree(session_path)
    return {"ok": True, "message": f"Session {name} deleted"}


@router.get("/sessions/{name}/download")
def download_session(request: Request, name: str):
    """Download session frames, labels, and imu.csv as a ZIP file."""
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    session_path = os.path.join(records_dir, name)

    if not os.path.isdir(session_path):
        raise HTTPException(404, "Session not found")

    if os.path.isfile(os.path.join(session_path, ".nodownload")):
        raise HTTPException(403, "Download disabled for this session")

    # Build ZIP on disk to avoid memory exhaustion
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as zf:
            for subdir in ("frames", "labels"):
                dir_path = os.path.join(session_path, subdir)
                if not os.path.isdir(dir_path):
                    continue
                for fname in sorted(os.listdir(dir_path)):
                    fpath = os.path.join(dir_path, fname)
                    if os.path.isfile(fpath):
                        zf.write(fpath, f"{subdir}/{fname}")
            # Include imu.csv if present
            imu_path = os.path.join(session_path, "imu.csv")
            if os.path.isfile(imu_path):
                zf.write(imu_path, "imu.csv")
        tmp.close()
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{name}.zip",
        background=lambda: os.unlink(tmp.name),
    )


# ----------------------------------------------------------------
# Frames
# ----------------------------------------------------------------
@router.get("/sessions/{name}/frames")
def list_frames(request: Request, name: str):
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    frames_dir = os.path.join(records_dir, name, "frames")
    labels_dir = os.path.join(records_dir, name, "labels")

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
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    frame_path = os.path.join(records_dir, name, "frames", frame)

    if not os.path.isfile(frame_path):
        raise HTTPException(404, "Frame not found")

    media = "image/png" if frame_path.lower().endswith('.png') else "image/jpeg"
    return FileResponse(frame_path, media_type=media)


# ----------------------------------------------------------------
# Annotations
# ----------------------------------------------------------------
@router.get("/sessions/{name}/frames/{frame}/annotation")
def get_annotation(request: Request, name: str, frame: str):
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    stem = Path(frame).stem
    label_path = os.path.join(records_dir, name, "labels", f"{stem}.txt")

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
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    stem = Path(frame).stem

    # Verify frame exists
    frame_path = os.path.join(records_dir, name, "frames", frame)
    if not os.path.isfile(frame_path):
        raise HTTPException(404, "Frame not found")

    if len(body.points) < 3:
        raise HTTPException(400, "At least 3 points required")

    for p in body.points:
        if len(p) != 2:
            raise HTTPException(400, "Each point must be [x, y]")

    # Save as YOLO polygon format: class_id x1 y1 x2 y2 ... xN yN
    # Coordinates are normalized (0-1)
    labels_dir = os.path.join(records_dir, name, "labels")
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
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    frame = _sanitize_name(frame)
    stem = Path(frame).stem
    label_path = os.path.join(records_dir, name, "labels", f"{stem}.txt")

    if os.path.isfile(label_path):
        os.remove(label_path)

    return {"ok": True, "message": "Annotation deleted"}


# ----------------------------------------------------------------
# Models
# ----------------------------------------------------------------
@router.get("/models")
def list_models(request: Request):
    """List available .pt model files in {save_dir}/models/."""
    save_dir = _get_save_dir(request)
    models_dir = os.path.join(save_dir, "models")

    models = []
    if os.path.isdir(models_dir):
        for f in sorted(os.listdir(models_dir)):
            if f.endswith('.pt'):
                full_path = os.path.join(models_dir, f)
                if os.path.isfile(full_path):
                    models.append({
                        "path": full_path,
                        "name": f,
                        "size_mb": round(os.path.getsize(full_path) / (1024*1024), 1),
                    })

    # Include currently loaded model info
    inference = request.app.state.inference_thread
    current_model = getattr(inference, 'model_path', '')

    return {"models": models, "current": current_model}


@router.post("/models/select")
def select_model(request: Request, body: ModelSelectRequest):
    """Select a model for inference."""
    if not os.path.isfile(body.path):
        raise HTTPException(404, f"Model file not found: {body.path}")
    if not body.path.endswith('.pt'):
        raise HTTPException(400, "Model file must be a .pt file")

    inference = request.app.state.inference_thread
    inference.reload_model(body.path)
    state = _get_state(request)
    state.append_log(f"Model selected: {body.path}")
    return {"ok": True, "message": f"Model switching to: {os.path.basename(body.path)}"}


# ----------------------------------------------------------------
# EMA Filter
# ----------------------------------------------------------------
@router.get("/ema-alpha")
def get_ema_alpha(request: Request):
    inference = request.app.state.inference_thread
    return {"alpha": inference.ema_alpha}


@router.post("/ema-alpha")
def set_ema_alpha(request: Request, body: EmaAlphaRequest):
    if not 0.0 <= body.alpha <= 1.0:
        raise HTTPException(400, "alpha must be between 0.0 and 1.0")
    inference = request.app.state.inference_thread
    inference.ema_alpha = body.alpha
    state = _get_state(request)
    state.append_log(f"EMA alpha set to {body.alpha:.2f}")
    return {"ok": True, "alpha": body.alpha}


@router.get("/conf")
def get_conf(request: Request):
    inference = request.app.state.inference_thread
    return {"conf": inference.conf}


@router.post("/conf")
def set_conf(request: Request, body: ConfRequest):
    if not 0.01 <= body.conf <= 1.0:
        raise HTTPException(400, "conf must be between 0.01 and 1.0")
    inference = request.app.state.inference_thread
    inference.conf = body.conf
    state = _get_state(request)
    state.append_log(f"Conf threshold set to {body.conf:.2f}")
    return {"ok": True, "conf": body.conf}


# ----------------------------------------------------------------
# Test Image Detection
# ----------------------------------------------------------------
@router.post("/test-detect/start")
def start_test_detect(request: Request, body: TestDetectRequest):
    """Start detection on a static test image."""
    records_dir = _get_records_dir(request)
    session = _sanitize_name(body.session)
    frame = _sanitize_name(body.frame)
    frame_path = os.path.join(records_dir, session, "frames", frame)

    if not os.path.isfile(frame_path):
        raise HTTPException(404, "Frame not found")

    state = _get_state(request)
    mm = _get_mode_manager(request)

    state.set_test_image_path(frame_path)

    ok, msg = mm.request_mode(Mode.DETECTING, source="API")
    if not ok:
        state.set_test_image_path(None)
        raise HTTPException(409, msg)

    state.append_log(f"Test detect started: {session}/{frame}")
    return {"ok": True, "message": f"Test detection on {frame}"}


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


# ----------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------
@router.post("/evaluation/start")
def evaluation_start(request: Request, body: EvaluationStartRequest):
    em = request.app.state.evaluation_manager
    mm = _get_mode_manager(request)

    ok, msg = mm.request_mode(Mode.EVALUATING, source="API")
    if not ok:
        raise HTTPException(409, msg)

    try:
        em.start(
            model_path=body.model_path,
            sessions=body.sessions,
            img_size=body.img_size,
            conf=body.conf,
        )
    except Exception as e:
        mm.request_mode(Mode.IDLE, source="API")
        raise HTTPException(500, str(e))

    return {"ok": True, "message": "Evaluation started"}


@router.post("/evaluation/stop")
def evaluation_stop(request: Request):
    mm = _get_mode_manager(request)
    ok, msg = mm.request_mode(Mode.IDLE, source="API")
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@router.get("/evaluation/status")
def evaluation_status(request: Request):
    state = _get_state(request)
    return state.get_evaluation().__dict__


@router.get("/sessions/{name}/evaluations")
def list_session_evaluations(request: Request, name: str):
    """List evaluation result JSONs in a session directory."""
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    session_dir = os.path.join(records_dir, name)

    if not os.path.isdir(session_dir):
        raise HTTPException(404, "Session not found")

    evaluations = []
    for f in sorted(os.listdir(session_dir), reverse=True):
        if not f.startswith("evaluation_") or not f.endswith(".json"):
            continue
        filepath = os.path.join(session_dir, f)
        try:
            import json
            with open(filepath, 'r') as fh:
                data = json.load(fh)
            evaluations.append({
                "filename": f,
                "model_name": data.get("model_name", ""),
                "timestamp": data.get("timestamp", ""),
                "avg_iou": data.get("avg_iou", 0.0),
                "total_frames": data.get("total_frames", 0),
            })
        except (json.JSONDecodeError, IOError):
            continue

    return evaluations


@router.get("/sessions/{name}/evaluations/{filename}")
def get_session_evaluation(request: Request, name: str, filename: str):
    """Get a specific evaluation result JSON."""
    records_dir = _get_records_dir(request)
    name = _sanitize_name(name)
    filename = _sanitize_name(filename)

    if not filename.startswith("evaluation_") or not filename.endswith(".json"):
        raise HTTPException(400, "Invalid evaluation filename")

    filepath = os.path.join(records_dir, name, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(404, "Evaluation result not found")

    import json
    with open(filepath, 'r') as f:
        return json.load(f)
