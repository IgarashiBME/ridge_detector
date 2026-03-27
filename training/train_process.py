#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Standalone training script - runs as a separate subprocess.

Receives configuration via argparse, runs ultralytics model.train(),
writes progress.json during training and result.json on completion.
"""

import argparse
import json
import os
import sys


def parse_args():
    parser = argparse.ArgumentParser(description="YOLO Training Process")
    parser.add_argument("--model", type=str, required=True)
    parser.add_argument("--dataset", type=str, required=True)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--img-size", type=int, default=640)
    parser.add_argument("--run-dir", type=str, required=True)
    parser.add_argument("--progress-file", type=str, required=True)
    parser.add_argument("--result-file", type=str, required=True)
    parser.add_argument("--lr0", type=float, default=0.001)
    parser.add_argument("--lrf", type=float, default=0.1)
    parser.add_argument("--freeze", type=int, default=10)
    parser.add_argument("--flipud", type=float, default=0.5)
    parser.add_argument("--amp", type=str, default="True")
    return parser.parse_args()


def write_progress(path, epoch, total_epochs, loss, phase):
    try:
        with open(path, 'w') as f:
            json.dump({
                "epoch": epoch,
                "total_epochs": total_epochs,
                "loss": loss,
                "phase": phase,
            }, f)
    except IOError:
        pass


def main():
    args = parse_args()

    use_amp = args.amp.lower() in ("true", "1", "yes")

    print(f"Training: model={args.model}, dataset={args.dataset}")
    print(f"  epochs={args.epochs}, batch={args.batch_size}, img={args.img_size}")
    print(f"  lr0={args.lr0}, lrf={args.lrf}, freeze={args.freeze}, flipud={args.flipud}, amp={use_amp}")

    write_progress(args.progress_file, 0, args.epochs, 0.0, "loading_model")

    try:
        from ultralytics import YOLO

        model = YOLO(args.model)

        # Register callback for progress updates
        total_epochs = args.epochs

        def on_train_epoch_end(trainer):
            epoch = trainer.epoch + 1
            loss = float(trainer.loss.item()) if hasattr(trainer.loss, 'item') else 0.0
            write_progress(
                args.progress_file, epoch, total_epochs, loss, "training"
            )
            print(f"Epoch {epoch}/{total_epochs}, loss={loss:.4f}")

        model.add_callback("on_train_epoch_end", on_train_epoch_end)

        write_progress(args.progress_file, 0, args.epochs, 0.0, "training")

        # Run training
        results = model.train(
            data=args.dataset,
            epochs=args.epochs,
            batch=args.batch_size,
            imgsz=args.img_size,
            project=args.run_dir,
            name="train",
            exist_ok=True,
            device=0,
            workers=4,
            task="segment",
            lr0=args.lr0,
            lrf=args.lrf,
            freeze=args.freeze,
            flipud=args.flipud,
            amp=use_amp,
        )

        # Find best model
        best_path = os.path.join(args.run_dir, "train", "weights", "best.pt")
        if not os.path.isfile(best_path):
            # Try last.pt
            best_path = os.path.join(args.run_dir, "train", "weights", "last.pt")

        if os.path.isfile(best_path):
            with open(args.result_file, 'w') as f:
                json.dump({"model_path": best_path, "status": "success"}, f)
            print(f"Training complete. Best model: {best_path}")
        else:
            with open(args.result_file, 'w') as f:
                json.dump({"model_path": "", "status": "no_model_found"}, f)
            print("Training complete but no model weights found.")

    except Exception as e:
        print(f"Training failed: {e}", file=sys.stderr)
        write_progress(args.progress_file, 0, args.epochs, 0.0, "error")
        with open(args.result_file, 'w') as f:
            json.dump({"model_path": "", "status": "error", "error": str(e)}, f)
        sys.exit(1)


if __name__ == "__main__":
    main()
