# AI 模型目录

将训练好的 ONNX 模型放在此目录，例如：

```
models/mosaic_detector.onnx
```

## 当前状态

- 现网三台用的是 `mosaic_detector.onnx`，旁边有 `mosaic_detector.onnx.classes.json`。类别是 `normal` 和 `green_screen_mosaic`。分类告警（`ai.class_alarm`）在这三台上是开的。
- 代码里 `class_alarm` 默认 **关**：只把最高类记在节目心跳里，不因此告警。
- 没有 onnx、且 `mode` 为 `auto` 或 `heuristic` 时，会退回 OpenCV 颜色/块状统计。这不是现网的花屏检测。不要把旧的方块分数阈值当成马赛克检测器打开。
- 换模型时，`.onnx` 和 `.classes.json` 一起拷进本目录，然后重启该台 `ai-monitor`。只换 onnx 不会热加载。

## 模型约定（ONNX）

| 项目 | 约定 |
|------|------|
| 输入 | `float32`，形状 `1×3×224×224`，RGB，像素归一化到 0～1 |
| 旧二分类 | `1×2`，第二列是异常分。没有 classes 文件时按这个读。`1×1` 仍当作单一异常分 |
| 具名多类 | `1×K` softmax。列顺序写在旁边的 `模型名.onnx.classes.json`，并且和 `workers/picture_classes.py` 一致 |
| 告警 | `class_alarm` 打开，最高类不是 `normal`，且分数 ≥ `ai.threshold`，才告警 |

训练在有显卡的机器上完成。拷到监测机的是 `.onnx` 和 `.classes.json`，不要把 PyTorch 装到监测机。

AI 把「再见」这类收播画面认成高置信 `normal` 时，监测进程会抑制静帧和无伴音。这是 `workers/monitor_worker.py` 里的默认行为，不在模型文件里。

**完整流程（采样本、标注、训练、导出、导入、标定）见：[docs/TRAINING.md](../docs/TRAINING.md)**
