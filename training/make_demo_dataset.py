#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成极小演示数据集（仅用于验证训练脚本能跑通，不能当真实模型）。

用法：
  python make_demo_dataset.py --out ./dataset_demo
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:
    raise SystemExit("需要 opencv: pip install opencv-python-headless")


def save(path: Path, img: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default="./dataset_demo")
    parser.add_argument("--n", type=int, default=40, help="每个 split/类 大约张数")
    args = parser.parse_args()
    root = Path(args.out)

    rng = np.random.default_rng(0)
    for split in ("train", "val"):
        n = args.n if split == "train" else max(args.n // 4, 8)
        for i in range(n):
            # 正常：平滑噪声图
            normal = rng.integers(40, 200, (240, 320, 3), dtype=np.uint8)
            normal = cv2.GaussianBlur(normal, (15, 15), 0)
            save(root / split / "normal" / f"n_{i:04d}.jpg", normal)

            # 异常：大色块马赛克感
            anomaly = rng.integers(0, 255, (15, 20, 3), dtype=np.uint8)
            anomaly = cv2.resize(anomaly, (320, 240), interpolation=cv2.INTER_NEAREST)
            # 加点绿屏
            if i % 3 == 0:
                anomaly[:, :, 1] = np.clip(anomaly[:, :, 1].astype(int) + 80, 0, 255).astype(
                    np.uint8
                )
            save(root / split / "anomaly" / f"a_{i:04d}.jpg", anomaly)

    print(f"demo dataset -> {root.resolve()}")
    print("仅用于 pipeline 冒烟，正式训练请换真实故障样本！")


if __name__ == "__main__":
    main()
