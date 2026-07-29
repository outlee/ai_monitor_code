#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
马赛克/花屏 二分类训练脚本（对齐监测端预处理）

数据目录：
  data_root/
    train/normal/*.jpg
    train/anomaly/*.jpg
    val/normal/*.jpg
    val/anomaly/*.jpg

预处理与 workers/ai_detector.py 一致：
  RGB, resize 224x224, /255.0, NCHW, 无 ImageNet mean/std

用法：
  python train.py --data ./dataset --epochs 30 --batch-size 64 --out ./runs/exp1
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Dict, List, Tuple

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


class FolderBinaryDataset(Dataset):
    """train|val 下 normal=0, anomaly=1。"""

    def __init__(self, root: Path, split: str, augment: bool = False):
        self.samples: List[Tuple[Path, int]] = []
        self.augment = augment
        split_dir = root / split
        for name, label in (("normal", 0), ("anomaly", 1)):
            for p in list_images(split_dir / name):
                self.samples.append((p, label))
        if not self.samples:
            raise FileNotFoundError(
                f"未找到图片: {split_dir}/normal 或 {split_dir}/anomaly"
            )

    def __len__(self) -> int:
        return len(self.samples)

    def _augment(self, img: np.ndarray) -> np.ndarray:
        # 轻量增强，避免破坏故障特征
        if random.random() < 0.5:
            img = np.ascontiguousarray(img[:, ::-1, :])  # 水平翻转
        if random.random() < 0.3:
            # 轻微亮度
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


@torch.no_grad()
def evaluate(model, loader, device) -> Dict[str, float]:
    model.eval()
    total = 0
    correct = 0
    tp = fp = tn = fn = 0
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        logits = model(x)
        pred = logits.argmax(dim=1)
        total += y.size(0)
        correct += (pred == y).sum().item()
        for p, t in zip(pred.tolist(), y.tolist()):
            if p == 1 and t == 1:
                tp += 1
            elif p == 1 and t == 0:
                fp += 1
            elif p == 0 and t == 0:
                tn += 1
            else:
                fn += 1
    acc = correct / max(total, 1)
    prec = tp / max(tp + fp, 1)
    rec = tp / max(tp + fn, 1)
    f1 = 2 * prec * rec / max(prec + rec, 1e-8)
    return {
        "acc": acc,
        "precision_anomaly": prec,
        "recall_anomaly": rec,
        "f1_anomaly": f1,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "n": total,
    }


def main():
    parser = argparse.ArgumentParser(description="训练马赛克/花屏二分类")
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

    train_ds = FolderBinaryDataset(data_root, "train", augment=True)
    val_ds = FolderBinaryDataset(data_root, "val", augment=False)
    print(f"train={len(train_ds)} val={len(val_ds)}")

    # 类别权重（缓解正常远多于异常）
    n0 = sum(1 for _, y in train_ds.samples if y == 0)
    n1 = sum(1 for _, y in train_ds.samples if y == 1)
    w0 = len(train_ds) / max(2 * n0, 1)
    w1 = len(train_ds) / max(2 * n1, 1)
    weight = torch.tensor([w0, w1], dtype=torch.float32, device=device)
    print(f"class counts normal={n0} anomaly={n1} weights={weight.tolist()}")

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

    model = build_model(pretrained=not args.no_pretrained).to(device)
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

        metrics = evaluate(model, val_loader, device)
        metrics["epoch"] = epoch
        metrics["train_loss"] = running / max(n_seen, 1)
        history.append(metrics)
        print(
            f"val acc={metrics['acc']:.4f} "
            f"P={metrics['precision_anomaly']:.4f} "
            f"R={metrics['recall_anomaly']:.4f} "
            f"F1={metrics['f1_anomaly']:.4f}"
        )

        # 以异常类 F1 选最优
        if metrics["f1_anomaly"] > best_f1:
            best_f1 = metrics["f1_anomaly"]
            torch.save(
                {
                    "model": model.state_dict(),
                    "epoch": epoch,
                    "metrics": metrics,
                    "args": vars(args),
                },
                best_path,
            )
            print(f"  -> saved {best_path} (best F1={best_f1:.4f})")

        with open(out_dir / "history.json", "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    # 同时存最后一版
    torch.save({"model": model.state_dict(), "epoch": args.epochs}, out_dir / "last.pt")
    print(f"done. best_f1={best_f1:.4f} best={best_path}")
    print("下一步: python export_onnx.py --ckpt runs/exp/best.pt --out mosaic_detector.onnx")


if __name__ == "__main__":
    main()
