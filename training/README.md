# 训练脚本（GPU / AutoDL）

在 **有 GPU 的机器或 AutoDL** 上使用，不要把 PyTorch 装到监测业务机（监测机只需 ONNXRuntime）。

## 文档

- **AutoDL 从 0 教程（推荐）**：[docs/TRAINING_AUTODL.md](../docs/TRAINING_AUTODL.md)
- 通用训练规范：[docs/TRAINING.md](../docs/TRAINING.md)

## 快速命令

这一次的图在 `Z:\ai_monitor_code-main\dataset_frame`。在有显卡的训练机上执行，不要在监测机上跑。步骤全文见 [docs/TRAINING.md](../docs/TRAINING.md) 的「这一次怎么训」。

```bat
cd /d Z:\ai_monitor_code-main\training
pip install -r requirements.txt
python train.py --data Z:\ai_monitor_code-main\dataset_frame --out Z:\ai_monitor_code-main\training\runs\frame1
python export_onnx.py --ckpt Z:\ai_monitor_code-main\training\runs\frame1\best.pt --out Z:\ai_monitor_code-main\training\runs\frame1\mosaic_detector.onnx
```

旧的两类数据仍可用：`dataset\train|val` 下的 `normal` 和 `anomaly`。

## 一个模型，按类放图

练习题在 `train/`，考试题在 `val/`。同一类画面放同一个文件夹，训练脚本看哪些文件夹有图，学成一个 ONNX。不要按类各训一个再拼到一起。

```text
dataset/
  train/normal/          val/normal/
  train/solid_green/           val/solid_green/
  train/green_screen_mosaic/   val/green_screen_mosaic/
```

目录名只用这些：`normal` `black` `blue_screen` `solid_green` `green_screen_mosaic` `color_exposure` `inverted`。`solid_green` 是绿屏。`green_screen_mosaic` 是花屏和马赛克放在一起。`color_exposure` 是偏色、过曝、欠曝放在一起，旧目录名 `color_cast`、`overexposed`、`underexposed` 也会收进这一类。旧目录名 `green_screen`、`mosaic` 也会被收进花屏马赛克。彩条放进 `normal`，或者放进 `color_bars` 也会按正常画面学，不告警。没有样本的类不要建目录。`snow` 这次不学。`freeze`、`flicker` 单帧看不出来，放了也会跳过。

`normal` 和 `anomaly` 两个文件夹还是原来的二分类，和现在线上的模型一样。不要把 `anomaly` 和上面这些具名故障目录混在一起。认不出的目录名这次不学，训练开始时会打印出来。

导出时旁边会写出 `mosaic_detector.onnx.classes.json`，和 onnx 一起拷。监测端默认只把最高类记在节目心跳里，不拿它告警。现有花屏、马赛克规则照旧。以后加上一类，要整次重训再导出，不能改旧文件的列。

`scan_threshold.py` 仍只扫旧的二分类验证集。

演示假数据（仅测通脚本）：

```bash
python make_demo_dataset.py --out ./dataset_demo
python train.py --data ./dataset_demo --out ./runs/demo --epochs 5 --batch-size 32
```

## 异常/正常视频 → 训练图片

### 可视化（推荐，Windows 可双击）

```bash
cd training
python video_to_frames_gui.py
# 或双击：打开抽帧工具.bat
```

界面可选视频、输出目录、每秒几张/固定张数、时间段；有「→ train/anomaly」等快捷按钮。

### 命令行

```bash
python video_to_frames.py -i /path/to/fault.ts -o ../dataset/train/anomaly --fps 1
python video_to_frames.py -i fault.mp4 -o ./out --fps 0.5 --start 30 --end 120 --max 300
python video_to_frames.py -i clip.mp4 -o ./out --count 80 --prefix clip01
```

### 视频格式

- **无特殊定制格式**：常见 **mp4 / ts / mts / mkv / avi / mov / flv / mpg / webm** 等均可试。  
- 实际能否打开取决于本机 **ffmpeg**（推荐安装并加入 PATH）或 OpenCV 解码器。  
- 台站常见 **MPEG-TS（.ts）**、**MP4** 一般没问题；若某种文件失败，先用播放器能播，再确认已装 ffmpeg。  
- 输出统一为 **jpg** 图片。

依赖：系统 **ffmpeg**（推荐）；没有则需 `opencv-python-headless`。  
抽完后请人工筛选，再放进 `train|val` / `normal|anomaly`。