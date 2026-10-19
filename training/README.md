# 训练脚本（GPU / AutoDL）

在 **有 GPU 的机器或 AutoDL** 上使用，不要把 PyTorch 装到监测业务机（监测机只需 ONNXRuntime）。

## 文档

- **AutoDL 从 0 教程（推荐）**：[docs/TRAINING_AUTODL.md](../docs/TRAINING_AUTODL.md)
- 通用训练规范：[docs/TRAINING.md](../docs/TRAINING.md)

## 快速命令

图在 `Z:\ai_monitor_code-main\dataset_frame`。在有显卡的训练机上执行，不要在 173、176、177 或这台没有显卡的开发机上跑。

`runs\frame1` 是已经训过的目录，不要覆盖。下一次把「再见」算进 `normal` 之后，输出到新目录：

```bat
cd /d Z:\ai_monitor_code-main\training
pip install -r requirements.txt
python train.py --data Z:\ai_monitor_code-main\dataset_frame --out Z:\ai_monitor_code-main\training\runs\frame_zaijian1
python export_onnx.py --ckpt Z:\ai_monitor_code-main\training\runs\frame_zaijian1\best.pt --out Z:\ai_monitor_code-main\training\runs\frame_zaijian1\mosaic_detector.onnx
```

「再见」样本在本仓库 `dataset_zaijian/`（`train/normal` 41 张，`val/normal` 11 张），并已同步到 `dataset_frame` 的 `normal\zaijian_*`。那是收播正常画面，不要放进 `blue_screen`。

旧的两类数据仍可用：只有 `normal` 和 `anomaly` 两个文件夹时，脚本仍训练成原来的二分类。不要把 `anomaly` 和下面的具名故障目录放在同一份数据里。

## 一个模型，按类放图

练习题在 `train/`，考试题在 `val/`。同一类画面放同一个文件夹，训练脚本看哪些文件夹有图，学成一个 ONNX。不要按类各训一个再拼到一起。

每个类在 `train/` 和 `val/` 下各放一个同名文件夹。没有图的类不要建目录。脚本认的名字以 `workers/picture_classes.py` 为准。

会学成模型里一列的正式目录：

| 文件夹 | 中文 | 告警 |
|--------|------|------|
| `normal` | 正常画面，包括彩条和「再见」 | 不告警 |
| `black` | 黑屏 | 告警 |
| `blue_screen` | 蓝屏 | 告警 |
| `solid_green` | 绿屏 | 告警 |
| `green_screen_mosaic` | 花屏和马赛克 | 告警 |
| `color_exposure` | 偏色、过曝、欠曝 | 告警 |
| `inverted` | 画面倒置 | 告警 |

旧文件夹名还能读，图会并进上面的类，不会单独成一列：

| 旧文件夹 | 并进 |
|----------|------|
| `green_screen` | `green_screen_mosaic`（花屏） |
| `mosaic` | `green_screen_mosaic`（马赛克） |
| `color_cast` | `color_exposure`（偏色） |
| `overexposed` | `color_exposure`（过曝） |
| `underexposed` | `color_exposure`（欠曝） |
| `color_bars` | `normal`（彩条，不告警） |

放了也不会学：

| 文件夹 | 原因 |
|--------|------|
| `freeze` | 静帧，单帧看不出来，训练时跳过 |
| `flicker` | 闪烁，单帧看不出来，训练时跳过 |
| `snow` | 雪花不在类别表里，开始时会打印「这次不学」 |
| `anomaly` | 只有它和 `normal` 两个文件夹时，才按旧的二分类学。不要和上面的故障目录放在一起 |

`normal` 和 `anomaly` 两个文件夹是旧的二分类。现网模型不是这一档，而是 `normal` 和 `green_screen_mosaic`，旁边有 classes 文件。认不出的目录名这次不学，训练开始时会打印出来。

导出时旁边会写出 `mosaic_detector.onnx.classes.json`，和 onnx 一起拷到监测机 `models/`，然后重启该台 `ai-monitor`。代码里 `ai.class_alarm` 默认关，只把最高类记在心跳里。三台现网这个开关是开的：最高类不是 `normal`、分数达到 `ai.threshold` 才告警。绿屏占比和纯绿小块在现网设成大于 1，那条规则不告警。以后加上一类，要整次重训再导出，不能改旧文件的列。

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