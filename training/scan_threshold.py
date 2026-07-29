#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在验证集上扫描 threshold，辅助选择 ai.threshold。

用法：
  python scan_threshold.py --onnx mosaic_detector.onnx --data ./dataset --split val
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from train import FolderBinaryDataset, load_rgb_224


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--onnx", type=str, required=True)
    parser.add_argument("--data", type=str, required=True)
    parser.add_argument("--split", type=str, default="val")
    args = parser.parse_args()

    import onnxruntime as ort

    ds = FolderBinaryDataset(Path(args.data), args.split, augment=False)
    sess = ort.InferenceSession(args.onnx, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0].name

    scores = []
    labels = []
    for path, label in ds.samples:
        img = load_rgb_224(path)
        x = img.astype(np.float32) / 255.0
        x = x.transpose(2, 0, 1)[None, ...]
        out = sess.run(None, {inp: x})[0]
        score = float(out[0][1]) if out.ndim == 2 and out.shape[1] >= 2 else float(np.ravel(out)[0])
        scores.append(score)
        labels.append(label)

    scores = np.array(scores)
    labels = np.array(labels)
    print(f"samples={len(labels)} anomaly={labels.sum()} normal={(labels == 0).sum()}")
    print(f"{'th':>6} {'P':>8} {'R':>8} {'F1':>8} {'FP':>6} {'FN':>6}")
    best = (0.0, 0.55)
    for th in np.arange(0.20, 0.91, 0.05):
        pred = (scores >= th).astype(int)
        tp = int(((pred == 1) & (labels == 1)).sum())
        fp = int(((pred == 1) & (labels == 0)).sum())
        fn = int(((pred == 0) & (labels == 1)).sum())
        prec = tp / max(tp + fp, 1)
        rec = tp / max(tp + fn, 1)
        f1 = 2 * prec * rec / max(prec + rec, 1e-8)
        print(f"{th:6.2f} {prec:8.4f} {rec:8.4f} {f1:8.4f} {fp:6d} {fn:6d}")
        if f1 > best[0]:
            best = (f1, float(th))
    print(f"\n建议起点 threshold={best[1]:.2f} (val F1={best[0]:.4f})")
    print("写入监测机 config: ai.threshold: <上值>，再按现场误报微调")


if __name__ == "__main__":
    main()
