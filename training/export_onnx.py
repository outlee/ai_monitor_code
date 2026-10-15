#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 best.pt 导出为监测系统可用的 ONNX。

输出是 softmax 概率。旧的二分类是 1x2：[normal, anomaly]。
多类时列顺序写在 onnx 旁边的 .classes.json，监测端按这个文件认列。

用法：
  python export_onnx.py --ckpt ./runs/exp1/best.pt --out ./mosaic_detector.onnx
  python export_onnx.py --ckpt ./runs/exp1/best.pt --out ./mosaic_detector.onnx --verify ./dataset/val/anomaly/xxx.jpg
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional, Tuple

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


def _class_list(ckpt, state) -> List[str]:
    """优先用训练时记下的类别。对不上最后一层宽度时，退回二分类或 class_0。"""
    classes: Optional[List[str]] = None
    if isinstance(ckpt, dict):
        raw = ckpt.get("classes")
        if isinstance(raw, list) and raw:
            classes = [str(item) for item in raw]
    rows = None
    if isinstance(state, dict) and "classifier.3.weight" in state:
        rows = int(state["classifier.3.weight"].shape[0])
    if classes and rows and len(classes) != rows:
        print(
            "警告: 权重输出 %d 类，checkpoint 记录 %d 类，按权重导出"
            % (rows, len(classes))
        )
        classes = None
    if classes:
        return classes
    if rows == 2 or rows is None:
        return ["normal", "anomaly"]
    return ["class_%d" % i for i in range(int(rows))]


def load_checkpoint(ckpt_path: Path, device: torch.device) -> Tuple[nn.Module, List[str]]:
    try:
        ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
    except TypeError:
        ckpt = torch.load(ckpt_path, map_location=device)
    state = ckpt["model"] if isinstance(ckpt, dict) and "model" in ckpt else ckpt
    classes = _class_list(ckpt, state)
    model = build_model(num_classes=len(classes), pretrained=False)
    model.load_state_dict(state)
    model.eval()
    return model, classes


def write_class_sidecar(out_path: Path, classes: List[str]) -> None:
    side = Path(str(out_path) + ".classes.json")
    side.write_text(
        json.dumps({"classes": list(classes)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("classes: %s -> %s" % (side, ",".join(classes)))


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


def verify_onnx(
    onnx_path: Path,
    image_path: Path,
    threshold: float = 0.55,
    classes: Optional[List[str]] = None,
) -> None:
    import onnxruntime as ort

    img = load_rgb_224(image_path)
    x = img.astype(np.float32) / 255.0
    x = x.transpose(2, 0, 1)[None, ...]

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0].name
    out = sess.run(None, {inp: x})[0]
    print(f"output shape={out.shape} values={out}")
    names = list(classes or [])
    if out.ndim == 2 and names and int(out.shape[1]) == len(names):
        top = int(np.argmax(out[0]))
        print(
            "top=%s score=%.4f threshold=%.2f"
            % (names[top], float(out[0][top]), threshold)
        )
        return
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
    model, classes = load_checkpoint(Path(args.ckpt), device)
    out_path = Path(args.out)
    export_onnx(model, out_path, opset=args.opset)
    write_class_sidecar(out_path, classes)

    try:
        import onnx

        onnx.checker.check_model(onnx.load(args.out))
        print("onnx.checker: OK")
    except Exception as e:
        print(f"onnx.checker 跳过/失败: {e}")

    if args.verify:
        verify_onnx(
            out_path, Path(args.verify), threshold=args.threshold, classes=classes
        )

    print("拷贝到监测机: %s 和 %s.classes.json" % (out_path.name, out_path.name))
    print("配置: ai.enabled=true, mode=auto|onnx")
    print("多类模型默认只记最高类，不告警。要告警再加 ai.class_alarm: true")


if __name__ == "__main__":
    main()
