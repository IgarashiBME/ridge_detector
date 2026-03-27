#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Visualization functions for ridge detection results.
"""

import cv2
import numpy as np


def visualize_result(frame, target_mask, centers, line_points, ab,
                     infer_time_ms=0.0, fps=0.0, serial_count=0,
                     mask_alpha=0.4):
    """Draw detection results onto frame (in-place modification).

    Args:
        frame: BGR image (H, W, 3), will be modified in-place.
        target_mask: binary mask (H, W) uint8 or None.
        centers: list of (cx, cy).
        line_points: ((x1,y1),(x2,y2)) or None.
        ab: (a, b_centered) or None.
        infer_time_ms: inference time in ms.
        fps: current processing fps.
        serial_count: serial send count.
        mask_alpha: mask overlay transparency.

    Returns:
        frame: the same frame with overlays drawn.
    """
    H, W = frame.shape[:2]

    # Draw mask overlay
    if target_mask is not None:
        color_mask = np.zeros_like(frame)
        color_mask[:, :, 2] = 255  # Red mask
        mask_indices = target_mask == 1
        frame[mask_indices] = cv2.addWeighted(
            frame[mask_indices], 1.0 - mask_alpha,
            color_mask[mask_indices], mask_alpha, 0
        ).reshape(-1, 3)

    # Draw center points
    for (cx, cy) in centers:
        cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)

    # Draw fitted line
    control_info = "No Line"
    if line_points is not None:
        p1, p2 = line_points
        cv2.line(frame, p1, p2, (255, 0, 0), 2)
        cv2.line(frame, (W // 2, H), (W // 2, H - 30), (0, 255, 255), 1)

        if ab is not None:
            a, b = ab
            control_info = f"a: {a:.4f} | b: {b:.1f}"
        else:
            control_info = "a: NaN | b: NaN"

    # Performance info
    perf_text = f"Inf:{infer_time_ms:.1f}ms FPS:{fps:.1f} TX:{serial_count}"
    cv2.putText(frame, perf_text, (10, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.putText(frame, control_info, (10, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 255, 100), 1)

    return frame
