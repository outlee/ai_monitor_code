#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 best.pt 导出为监测系统可用的 ONNX。

输出：1x2 softmax 概率，[normal, anomaly]，监测端取 out[0][1] 作异常分。

用法：
  python export_onnx.py --ckpt ./runs/exp1/best.pt --out ./mosaic_detector.onnx
  python export_onnx.py --ckpt ./runs/exp1/best.pt --out ./mosaic_detector.onnx --verify ./dataset/val/anomaly/xxx.jpg
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

from train import build_model, load_rgb_224


class SoftmaxWrapper(nn.Module):
    """导出时带 Softmax，保证监测端拿到概率而非 logits。"""

    def __init__(self, backbone: nn.Module):
        super().__init__()
        self.backbone = backbone
        self.softmax = nn.Softmax(dim=1)

    def forward(self, x):
        return self.softmax(self.backbone(x))


def load_checkpoint(ckpt_path: Path, device: torch.device) -> nn.Module:
    try:
        ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
    except TypeError:
        ckpt = torch.load(ckpt_path, map_location=device)
    model = build_model(pretrained=False)
    state = ckpt["model"] if isinstance(ckpt, dict) and "model" in ckpt else ckpt
    model.load_state_dict(state)
    model.eval()
    return model


def export_onnx(model: nn.Module, out_path: Path, opset: int = 13) -> None:
    wrapped = SoftmaxWrapper(model).eval()
    dummy = torch.randn(1, 3, 224, 224, dtype=torch.float32)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    torch.onnx.export(
        wrapped,
        dummy,
        str(out_path),
        input_names=["input"],
        output_names=["output"],
        opset_version=opset,
        dynamo=False,
    )
    print(f"exported: {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")


def verify_onnx(onnx_path: Path, image_path: Path, threshold: float = 0.55) -> None:
    import onnxruntime as ort

    img = load_rgb_224(image_path)
    x = img.astype(np.float32) / 255.0
    x = x.transpose(2, 0, 1)[None, ...]

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0].name
    out = sess.run(None, {inp: x})[0]
    print(f"output shape={out.shape} values={out}")
    if out.ndim == 2 and out.shape[1] >= 2:
        score = float(out[0][1])
    else:
        score = float(np.ravel(out)[0])
    print(f"anomaly_score={score:.4f} threshold={threshold} -> "
          f"{'ANOMALY' if score >= threshold else 'normal'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", type=str, required=True)
    parser.add_argument("--out", type=str, default="mosaic_detector.onnx")
    parser.add_argument("--opset", type=int, default=13)
    parser.add_argument("--verify", type=str, default="", help="导出后用一张图 ORT 验证")
    parser.add_argument("--threshold", type=float, default=0.55)
    args = parser.parse_args()

    device = torch.device("cpu")  # 导出在 CPU 即可
    model = load_checkpoint(Path(args.ckpt), device)
    export_onnx(model, Path(args.out), opset=args.opset)

    try:
        import onnx

        onnx.checker.check_model(onnx.load(args.out))
        print("onnx.checker: OK")
    except Exception as e:
        print(f"onnx.checker 跳过/失败: {e}")

    if args.verify:
        verify_onnx(Path(args.out), Path(args.verify), threshold=args.threshold)

    print("拷贝到监测机: models/mosaic_detector.onnx")
    print("配置: ai.enabled=true, mode=auto|onnx, threshold=...")


if __name__ == "__main__":
    main()
