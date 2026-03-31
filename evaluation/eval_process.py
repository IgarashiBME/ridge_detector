#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Evaluation subprocess: loads a YOLO model, runs inference on annotated frames,
computes IoU against ground truth labels, and writes results to JSON.

Runs as a standalone process for GPU memory isolation (same pattern as train_process.py).
"""

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np


def parse_label_file(label_path, img_h, img_w):
    """Parse YOLO polygon label file and create a binary mask.

    Each line: class_id x1 y1 x2 y2 ... xN yN (normalized coordinates).
    Multiple lines are merged with logical OR.
    """
    mask = np.zeros((img_h, img_w), dtype=np.uint8)

    with open(label_path, 'r') as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 7:  # class_id + at least 3 points
            continue

        # Parse normalized coordinates
        coords = []
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                x = float(parts[i]) * img_w
                y = float(parts[i + 1]) * img_h
                coords.append([int(round(x)), int(round(y))])

        if len(coords) >= 3:
            pts = np.array(coords, dtype=np.int32)
            cv2.fillPoly(mask, [pts], 1)

    return mask


def compute_iou(gt_mask, pred_mask):
    """Compute IoU between two binary masks."""
    intersection = np.logical_and(gt_mask, pred_mask).sum()
    union = np.logical_or(gt_mask, pred_mask).sum()

    if union == 0:
        # Both empty = perfect agreement
        return 1.0
    return float(intersection) / float(union)


def write_progress(progress_path, current, total, phase="inferring"):
    """Write progress.json for polling by the manager."""
    try:
        tmp = progress_path + ".tmp"
        with open(tmp, 'w') as f:
            json.dump({
                "current_frame": current,
                "total_frames": total,
                "phase": phase,
            }, f)
        os.replace(tmp, progress_path)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="IoU evaluation subprocess")
    parser.add_argument("--model", required=True, help="Path to .pt model file")
    parser.add_argument("--frames-json", required=True, help="Path to JSON file with frame list")
    parser.add_argument("--img-size", type=int, default=640, help="Inference image size")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    parser.add_argument("--progress-file", required=True, help="Path to write progress.json")
    parser.add_argument("--result-file", required=True, help="Path to write result.json")
    args = parser.parse_args()

    # Write initial progress
    write_progress(args.progress_file, 0, 0, "starting")

    # Load frames list
    with open(args.frames_json, 'r') as f:
        frames = json.load(f)

    total = len(frames)
    if total == 0:
        print("ERROR: No frames to evaluate", flush=True)
        with open(args.result_file, 'w') as f:
            json.dump({"error": "No frames to evaluate"}, f)
        sys.exit(1)

    print(f"Loading model: {args.model}", flush=True)
    write_progress(args.progress_file, 0, total, "loading_model")

    # Load YOLO model
    from ultralytics import YOLO
    model = YOLO(args.model)

    print(f"Evaluating {total} frames...", flush=True)
    write_progress(args.progress_file, 0, total, "inferring")

    model_name = os.path.basename(args.model)
    per_frame_results = []
    iou_values = []

    for i, frame_info in enumerate(frames):
        image_path = frame_info["image_path"]
        label_path = frame_info["label_path"]
        session = frame_info["session"]
        frame_name = frame_info["frame_name"]

        try:
            img = cv2.imread(image_path)
            if img is None:
                print(f"WARNING: Cannot read {image_path}", flush=True)
                per_frame_results.append({
                    "session": session,
                    "frame": frame_name,
                    "iou": None,
                    "error": "Cannot read image",
                })
                continue

            img_h, img_w = img.shape[:2]

            # Ground truth mask
            gt_mask = parse_label_file(label_path, img_h, img_w)

            # YOLO inference
            results = model(img, imgsz=args.img_size, conf=args.conf, verbose=False)

            # Build prediction mask
            pred_mask = np.zeros((img_h, img_w), dtype=np.uint8)
            if results and results[0].masks is not None:
                masks_data = results[0].masks.data.cpu().numpy()
                for m in masks_data:
                    # Resize mask to original image size
                    resized = cv2.resize(m, (img_w, img_h), interpolation=cv2.INTER_LINEAR)
                    pred_mask = np.logical_or(pred_mask, resized > 0.5).astype(np.uint8)

            iou = compute_iou(gt_mask, pred_mask)
            iou_values.append(iou)

            per_frame_results.append({
                "session": session,
                "frame": frame_name,
                "iou": round(iou, 4),
            })

            print(f"  [{i+1}/{total}] {session}/{frame_name} IoU={iou:.4f}", flush=True)

        except Exception as e:
            print(f"ERROR: {session}/{frame_name}: {e}", flush=True)
            per_frame_results.append({
                "session": session,
                "frame": frame_name,
                "iou": None,
                "error": str(e),
            })

        write_progress(args.progress_file, i + 1, total, "inferring")

    # Compute average IoU (excluding errors)
    avg_iou = sum(iou_values) / len(iou_values) if iou_values else 0.0

    print(f"Average IoU: {avg_iou:.4f} ({len(iou_values)}/{total} frames)", flush=True)

    # Write result
    result = {
        "model_name": model_name,
        "model_path": args.model,
        "timestamp": time.strftime("%Y%m%d_%H%M%S"),
        "sessions": sorted(set(f["session"] for f in frames)),
        "total_frames": total,
        "evaluated_frames": len(iou_values),
        "avg_iou": round(avg_iou, 4),
        "per_frame": per_frame_results,
    }

    with open(args.result_file, 'w') as f:
        json.dump(result, f, indent=2)

    write_progress(args.progress_file, total, total, "completed")
    print("Evaluation complete.", flush=True)


if __name__ == "__main__":
    main()
