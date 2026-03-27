#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ridge detection logic extracted from serial_ridge_detector_zed.py.
Provides get_runs, line fitting (polyfit/ransac), line_points_to_ab,
and process_image for YOLO-seg mask-based ridge center detection.
"""

import cv2
import numpy as np

try:
    from sklearn.linear_model import RANSACRegressor
    sklearn_available = True
except ImportError:
    sklearn_available = False


def get_runs(row_data):
    """Find contiguous runs of 1s in a binary row.
    Returns list of (start, end) tuples.
    """
    row_data = row_data.astype(np.int32)
    padded = np.pad(row_data, (1, 1), mode='constant')
    diff = np.diff(padded)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    return list(zip(starts, ends))


def calculate_line_polyfit(centers, height):
    """Fit a line through center points using numpy polyfit.
    Returns ((top_x, 0), (bottom_x, height)) or None.
    """
    if len(centers) < 2:
        return None
    pts = np.array(centers)
    Y, X = pts[:, 1], pts[:, 0]
    slope, intercept = np.polyfit(Y, X, 1)
    top_x = int(slope * 0 + intercept)
    bottom_x = int(slope * height + intercept)
    return (top_x, 0), (bottom_x, height)


def calculate_line_ransac(centers, height):
    """Fit a line through center points using RANSAC.
    Falls back to polyfit if sklearn is unavailable or too few points.
    Returns ((top_x, 0), (bottom_x, height)) or None.
    """
    if not sklearn_available or len(centers) < 3:
        return calculate_line_polyfit(centers, height)
    pts = np.array(centers)
    Y, X = pts[:, 1].reshape(-1, 1), pts[:, 0]
    ransac = RANSACRegressor(min_samples=2, residual_threshold=10.0)
    try:
        ransac.fit(Y, X)
        line_x = ransac.predict(np.array([[0], [height]]))
        return (int(line_x[0]), 0), (int(line_x[1]), height)
    except Exception:
        return None


def line_points_to_ab(p1, p2, frame_width):
    """Convert two points on the line into (a, b_centered) for x_centered = a*y + b_centered.
    - a: dx/dy
    - b_centered: x at y=0, where x is centered so that image center is 0 (right positive).
    """
    if p1 is None or p2 is None:
        return None
    x1, y1 = p1
    x2, y2 = p2
    dy = (y2 - y1)
    if dy == 0:
        return None
    a = (x2 - x1) / dy  # dx/dy
    b = x1 - a * y1     # x at y=0 in pixel coords (0..W)
    b_centered = b - (frame_width / 2.0)
    return float(a), float(b_centered)


def process_image(frame, model, conf=0.25, half=True,
                  target_class=None, num_lines=20,
                  y_margin=0.1, min_run=5, fitting_mode='ransac'):
    """Run YOLO-seg inference and ridge detection on a single frame.

    Returns:
        target_mask: binary mask (H, W) uint8 or None
        centers: list of (cx, cy) center points
        line_points: ((x1,y1),(x2,y2)) or None
        ab: (a, b_centered) or None
        infer_time_ms: inference time in milliseconds
    """
    H, W = frame.shape[:2]

    results = model(frame, verbose=False, conf=conf, half=half)
    result = results[0]
    infer_time_ms = result.speed['inference']

    target_mask = None
    centers = []
    line_points = None
    ab = None

    if result.masks is not None:
        if target_class is not None:
            class_ids = result.boxes.cls.cpu().numpy().astype(int)
            target_indices = [i for i, c in enumerate(class_ids) if c == target_class]
            masks = result.masks.data[target_indices] if target_indices else []
        else:
            masks = result.masks.data

        if len(masks) > 0:
            areas = masks.sum(dim=(1, 2))
            max_idx = areas.argmax().item()

            raw_mask = masks[max_idx].cpu().numpy()
            target_mask = cv2.resize(raw_mask, (W, H), interpolation=cv2.INTER_NEAREST)
            target_mask = (target_mask > 0.5).astype(np.uint8)

    if target_mask is not None and num_lines > 0:
        y_start = int(H * y_margin)
        y_end = int(H * (1.0 - y_margin))
        y_coords = np.linspace(y_start, y_end, num_lines, dtype=int)

        for y in y_coords:
            row = target_mask[y, :]
            runs = get_runs(row)
            valid_runs = [r for r in runs if (r[1] - r[0]) >= min_run]
            if valid_runs:
                best_run = max(valid_runs, key=lambda x: x[1] - x[0])
                cx = int((best_run[0] + best_run[1]) / 2)
                centers.append((cx, y))

        if fitting_mode == 'ransac':
            line_points = calculate_line_ransac(centers, H)
        else:
            line_points = calculate_line_polyfit(centers, H)

        if line_points is not None:
            ab = line_points_to_ab(line_points[0], line_points[1], W)

    return target_mask, centers, line_points, ab, infer_time_ms
