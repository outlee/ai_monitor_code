# 训练脚本（GPU / AutoDL）

在 **有 GPU 的机器或 AutoDL** 上使用，不要把 PyTorch 装到监测业务机（监测机只需 ONNXRuntime）。

## 文档

- **AutoDL 从 0 教程（推荐）**：[docs/TRAINING_AUTODL.md](../docs/TRAINING_AUTODL.md)
- 通用训练规范：[docs/TRAINING.md](../docs/TRAINING.md)

## 快速命令

```bash
pip install -r requirements.txt

# 目录: data/train|val / normal|anomaly
python train.py --data ./dataset --out ./runs/exp1 --epochs 30 --batch-size 64
python export_onnx.py --ckpt ./runs/exp1/best.pt --out ./mosaic_detector.onnx
python scan_threshold.py --onnx ./mosaic_detector.onnx --data ./dataset --split val
```

演示假数据（仅测通脚本）：

```bash
python make_demo_dataset.py --out ./dataset_demo
python train.py --data ./dataset_demo --out ./runs/demo --epochs 5 --batch-size 32
```
