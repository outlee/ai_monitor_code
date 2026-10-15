#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单帧画面分类训练（对齐监测端预处理）。

一次训练只出一个模型。按有图的文件夹决定类别，不要按类各训一个再拼。

旧目录仍然可用：
  data_root/train|val / normal|anomaly

多类目录（没有样本的类不要建；freeze、flicker 会被跳过）：
  data_root/train|val / normal|black|blue_screen|solid_green|
                         green_screen_mosaic|color_exposure|inverted

类别顺序见 workers/picture_classes.py，导出的列必须和它对上。

预处理与 workers/ai_detector.py 一致：
  RGB, resize 224x224, /255.0, NCHW, 无 ImageNet mean/std

用法：
  python train.py --data ./dataset --epochs 30 --batch-size 64 --out ./runs/exp1
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

_WORKERS = Path(__file__).resolve().parents[1] / "workers"
if str(_WORKERS) not in sys.path:
    sys.path.insert(0, str(_WORKERS))
from picture_classes import class_folders, discover_frame_classes

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
from torchvision import models
from tqdm import tqdm

try:
    import cv2
except ImportError:
    cv2 = None
from PIL import Image


IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def set_seed(seed: int = 42) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def list_images(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    files = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            files.append(p)
    return files


def load_rgb_224(path: Path) -> np.ndarray:
    """与监测端 _infer_onnx 一致的读图与缩放。"""
    if cv2 is not None:
        img = cv2.imread(str(path))
        if img is None:
            raise RuntimeError(f"无法读取: {path}")
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (224, 224), interpolation=cv2.INTER_LINEAR)
        return img
    img = Image.open(path).convert("RGB").resize((224, 224), Image.BILINEAR)
    return np.array(img)


class FolderClassDataset(Dataset):
    """按给定类别顺序贴标签。下标就是以后 ONNX 的列。"""

    def __init__(
        self,
        root: Path,
        split: str,
        classes: List[str],
        augment: bool = False,
    ):
        self.classes = list(classes)
        self.samples: List[Tuple[Path, int]] = []
        self.augment = augment
        self.missing: List[str] = []
        split_dir = root / split
        for label, name in enumerate(self.classes):
            files = []
            for folder_name in class_folders(name):
                files.extend(list_images(split_dir / folder_name))
            if not files:
                self.missing.append(name)
            for p in files:
                self.samples.append((p, label))
        if not self.samples:
            raise FileNotFoundError(
                "未找到图片: %s 下的 %s" % (split_dir, ", ".join(self.classes))
            )
        if self.missing and split == "train":
            raise FileNotFoundError(
                "训练集缺图: %s" % ", ".join(self.missing)
            )

    def __len__(self) -> int:
        return len(self.samples)

    def _augment(self, img: np.ndarray) -> np.ndarray:
        # 只做水平翻转。上下翻转会把正常画面变成倒置，标签就错了。
        if random.random() < 0.5:
            img = np.ascontiguousarray(img[:, ::-1, :])
        if random.random() < 0.3:
            factor = 1.0 + random.uniform(-0.15, 0.15)
            img = np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)
        return img

    def __getitem__(self, idx: int):
        path, label = self.samples[idx]
        img = load_rgb_224(path)
        if self.augment:
            img = self._augment(img)
        x = img.astype(np.float32) / 255.0
        x = x.transpose(2, 0, 1)  # CHW
        return torch.from_numpy(x), torch.tensor(label, dtype=torch.long)


class FolderBinaryDataset(FolderClassDataset):
    """旧的 normal=0, anomaly=1。scan_threshold.py 还在用这个名字。"""

    def __init__(self, root: Path, split: str, augment: bool = False):
        super().__init__(root, split, ["normal", "anomaly"], augment=augment)


def build_model(num_classes: int = 2, pretrained: bool = True) -> nn.Module:
    """MobileNetV3-Small：监测机 CPU 友好。"""
    try:
        weights = models.MobileNet_V3_Small_Weights.DEFAULT if pretrained else None
        model = models.mobilenet_v3_small(weights=weights)
    except Exception:
        model = models.mobilenet_v3_small(pretrained=pretrained)
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, num_classes)
    return model


def _prf(tp: int, fp: int, fn: int) -> Tuple[float, float, float]:
    prec = tp / max(tp + fp, 1)
    rec = tp / max(tp + fn, 1)
    f1 = 2 * prec * rec / max(prec + rec, 1e-8)
    return prec, rec, f1


@torch.no_grad()
def evaluate(model, loader, device, class_names: List[str]) -> Dict[str, float]:
    """整体准确率，外加每个故障类的 F1。二分类时仍给出原来的 anomaly 指标。"""
    model.eval()
    k = len(class_names)
    total = 0
    correct = 0
    tp = [0] * k
    fp = [0] * k
    fn = [0] * k
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        pred = model(x).argmax(dim=1)
        total += int(y.size(0))
        correct += int((pred == y).sum().item())
        for p, t in zip(pred.tolist(), y.tolist()):
            p = int(p)
            t = int(t)
            if p == t:
                if 0 <= t < k:
                    tp[t] += 1
            else:
                if 0 <= p < k:
                    fp[p] += 1
                if 0 <= t < k:
                    fn[t] += 1
    metrics: Dict[str, float] = {
        "acc": correct / max(total, 1),
        "n": total,
    }
    fault_f1: List[float] = []
    for i, name in enumerate(class_names):
        support = tp[i] + fn[i]
        _prec, _rec, f1 = _prf(tp[i], fp[i], fn[i])
        if support <= 0 or name == "normal":
            continue
        metrics["f1_" + name] = f1
        metrics["support_" + name] = support
        fault_f1.append(f1)
    metrics["f1_fault_macro"] = (
        sum(fault_f1) / len(fault_f1) if fault_f1 else 0.0
    )
    if class_names == ["normal", "anomaly"]:
        prec, rec, f1 = _prf(tp[1], fp[1], fn[1])
        metrics["precision_anomaly"] = prec
        metrics["recall_anomaly"] = rec
        metrics["f1_anomaly"] = f1
        metrics["tp"] = tp[1]
        metrics["fp"] = fp[1]
        metrics["tn"] = tp[0]
        metrics["fn"] = fn[1]
    return metrics


def main():
    parser = argparse.ArgumentParser(description="训练单帧画面分类")
    parser.add_argument("--data", type=str, required=True, help="数据集根目录")
    parser.add_argument("--out", type=str, default="./runs/exp", help="输出目录")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-pretrained", action="store_true")
    args = parser.parse_args()

    set_seed(args.seed)
    data_root = Path(args.data)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device}")
    if device.type == "cuda":
        print(f"GPU: {torch.cuda.get_device_name(0)}")

    classes, skipped, unknown = discover_frame_classes(data_root / "train")
    if skipped:
        print("跳过单帧看不出来的目录: %s" % ", ".join(skipped))
    if unknown:
        print("这些目录名不在类别表里，这次不学: %s" % ", ".join(unknown))
    print("classes=%s" % ",".join(classes))

    train_ds = FolderClassDataset(data_root, "train", classes, augment=True)
    val_ds = FolderClassDataset(data_root, "val", classes, augment=False)
    print(f"train={len(train_ds)} val={len(val_ds)}")
    if val_ds.missing:
        print(
            "验证集没有这些类的图，宏观 F1 不算它们: %s"
            % ", ".join(val_ds.missing)
        )

    # 样本少的类权重大一些，避免正常画面把故障淹没。
    counts = [
        sum(1 for _, y in train_ds.samples if y == i) for i in range(len(classes))
    ]
    k = len(classes)
    weight = torch.tensor(
        [len(train_ds) / max(k * n, 1) for n in counts],
        dtype=torch.float32,
        device=device,
    )
    print(
        "counts=%s weights=%s"
        % (
            {name: counts[i] for i, name in enumerate(classes)},
            [round(float(v), 4) for v in weight.tolist()],
        )
    )

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.workers,
        pin_memory=(device.type == "cuda"),
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=(device.type == "cuda"),
    )

    model = build_model(
        num_classes=len(classes), pretrained=not args.no_pretrained
    ).to(device)
    criterion = nn.CrossEntropyLoss(weight=weight)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    best_f1 = -1.0
    history = []
    best_path = out_dir / "best.pt"

    for epoch in range(1, args.epochs + 1):
        model.train()
        running = 0.0
        n_seen = 0
        pbar = tqdm(train_loader, desc=f"epoch {epoch}/{args.epochs}")
        for x, y in pbar:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(x)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            running += loss.item() * y.size(0)
            n_seen += y.size(0)
            pbar.set_postfix(loss=f"{running / max(n_seen, 1):.4f}")
        scheduler.step()

        metrics = evaluate(model, val_loader, device, classes)
        metrics["epoch"] = epoch
        metrics["train_loss"] = running / max(n_seen, 1)
        history.append(metrics)
        if classes == ["normal", "anomaly"]:
            print(
                f"val acc={metrics['acc']:.4f} "
                f"P={metrics['precision_anomaly']:.4f} "
                f"R={metrics['recall_anomaly']:.4f} "
                f"F1={metrics['f1_anomaly']:.4f}"
            )
            pick = float(metrics["f1_anomaly"])
        else:
            parts = [
                "val acc=%.4f macroF1=%.4f" % (metrics["acc"], metrics["f1_fault_macro"])
            ]
            for name in classes:
                if name == "normal" or ("f1_" + name) not in metrics:
                    continue
                parts.append("%s=%.4f" % (name, metrics["f1_" + name]))
            print(" ".join(parts))
            pick = float(metrics["f1_fault_macro"])

        # 二分类看异常类 F1，多类看各故障类 F1 的平均。
        if pick > best_f1:
            best_f1 = pick
            torch.save(
                {
                    "model": model.state_dict(),
                    "epoch": epoch,
                    "metrics": metrics,
                    "args": vars(args),
                    "classes": classes,
                    "num_classes": len(classes),
                },
                best_path,
            )
            print(f"  -> saved {best_path} (best F1={best_f1:.4f})")

        with open(out_dir / "history.json", "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    # 同时存最后一版
    torch.save(
        {
            "model": model.state_dict(),
            "epoch": args.epochs,
            "classes": classes,
            "num_classes": len(classes),
        },
        out_dir / "last.pt",
    )
    print(f"done. best_f1={best_f1:.4f} best={best_path}")
    print("下一步: python export_onnx.py --ckpt runs/exp/best.pt --out mosaic_detector.onnx")
    print("导出时会在 onnx 旁边写上 .classes.json，两个文件要一起拷。")


if __name__ == "__main__":
    main()
