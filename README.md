# AI 节目异常监测系统（CentOS 版）

基于 FFmpeg 的 UDP 组播节目监测，支持黑场、静帧、静音检测。  
可选 **AI 马赛克/花屏检测**（默认关闭，不部署也不影响使用）。  
适配 **CentOS + 纯 CPU** 环境，可扩展到多路。

> **服务器部署（含 AI 模块）请见：[docs/DEPLOY.md](docs/DEPLOY.md)**  
> **AI 训练与导入请见：[docs/TRAINING.md](docs/TRAINING.md)** · **AutoDL 从 0 教程：[docs/TRAINING_AUTODL.md](docs/TRAINING_AUTODL.md)**  
> 训练脚本目录：`training/`（GPU/云上使用，监测机只跑 ONNX 推理）

## 功能

- 支持 UDP 组播（`udp://@239.x.x.x:port`）
- 兼容 H.264 / H.265 / MPEG-2
- **规则检测**：黑场、静帧、静音（含 start / end）
- **可选 AI 检测**：马赛克 / 绿屏花屏（ONNX 或启发式）
- 异常时自动记录日志 + 保存截图
- 配置文件管理多路节目
- **多进程 Manager + Worker 内多线程并行**
- **流断自动重连**、Worker 异常拉活
- **心跳状态**（`logs/status/<id>.json`）供 Web 显示 online/离线/重连中
- 事件日志与截图自动轮转/清理
- **P2 性能**：检测前降采样、`latest.jpg` 旁路帧（截图/AI 共用）、AI 独立线程
- **配置热重载**：改 `channels.yaml` 后 Manager/Worker 自动应用（可 `--no-reload`）
- **Web 监控面板**（可选）：频道状态、事件、截图
- **大屏交通灯** + **TTS 语音告警**（5 分钟抑制 / 多路聚合）
- **SQLite 告警历史**（与 jsonl 双写，`data/monitor.db`）

## 目录结构

```
ai_monitor_code/
├── config/
│   └── channels.yaml         # 频道 + AI 开关 + 阈值
├── workers/
│   ├── monitor_worker.py     # 监测 Worker（规则 + 抽帧）
│   └── ai_detector.py        # AI 推理（ONNX / 启发式）
├── web/
│   ├── app.py                # FastAPI 面板
│   └── static/
├── models/                   # 监测机放置 .onnx（可选）
│   └── README.md
├── training/                 # 【仅 GPU/云训练用，监测机不必跑】
│   ├── train.py              # 二分类训练
│   ├── export_onnx.py        # 导出 ONNX
│   ├── scan_threshold.py     # 扫 ai.threshold
│   ├── make_demo_dataset.py  # 假数据冒烟
│   ├── requirements.txt
│   └── README.md
├── docs/
│   ├── DEPLOY.md             # 服务器部署（含 AI 推理侧）
│   ├── TRAINING.md           # 训练规范与导入
│   └── TRAINING_AUTODL.md    # AutoDL 从 0 手把手
├── event_db.py               # SQLite 告警/状态历史
├── manager.py
├── test_ai_offline.py
├── requirements.txt          # 监测机基础依赖
├── data/                     # monitor.db（运行生成，gitignore）
├── logs/                     # 运行生成
├── snapshots/                # 运行生成
└── README.md
```

## CentOS 安装依赖

```bash
# 1. Python3 + pip
sudo yum install -y python3 python3-pip

# 2. FFmpeg（静态包或 RPM Fusion）
# ...

# 3. 基础依赖（必须）
cd /path/to/ai_monitor
pip3 install -r requirements.txt
```

### 启用 AI 时再装（可选）

```bash
# 纯 CPU
pip3 install onnxruntime opencv-python-headless numpy Pillow

# 有 NVIDIA 显卡可改用
# pip3 install onnxruntime-gpu opencv-python-headless numpy Pillow
```

装好后编辑 `config/channels.yaml`：

```yaml
ai:
  enabled: true
  mode: auto          # auto | onnx | heuristic
  model_path: "models/mosaic_detector.onnx"
```

- **没有模型**：`mode: heuristic` 即可用颜色/块状统计检测明显花屏马赛克  
- **有 ONNX 模型**：放到 `models/`，`mode: auto` 或 `onnx`

**不装这些库、不改 enabled 时，系统行为与原来完全一致。**

### 启用 Web 面板时再装（可选）

```bash
pip3 install fastapi uvicorn
```

启动面板（与监测进程独立，只读展示）：

```bash
cd /path/to/ai_monitor
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
```

浏览器访问：`http://服务器IP:8080`

面板功能：
- 节目数量 / 异常频道统计
- 各频道状态与最近异常类型
- 实时事件列表（读 `logs/events.jsonl`）
- 异常截图浏览
- AI 开关状态显示

> 不启动 Web 服务不影响监测本身。

## 配置频道

```yaml
channels:
  - id: "ch001"
    name: "综合频道"
    url: "udp://@239.1.1.1:5000"
    enabled: true
    # program: 不填即可（一地址一节目 SPTS）

  # 少数 MPTS：同一组播多个节目，用 program 区分，各建一条
  - id: "ch002"
    name: "节目B"
    url: "udp://@239.1.1.1:5000"
    program: 2          # MPEG-TS program / service id
    enabled: true
```

组播格式：`udp://@239.1.1.1:5000`  
指定网卡：`udp://@239.1.1.1:5000?localaddr=192.168.1.100`  

**program**：可选。Web「新增/编辑」可填；FFmpeg 使用 `0:p:<program>:v/a` 选轨。查节目号可用：

```bash
ffprobe -hide_banner "udp://@239.1.1.1:5000"
# 或
ffmpeg -i "udp://@239.1.1.1:5000" 2>&1 | head -40
```

## 运行

### 1. 单路 / 多路 Worker（线程并行）

```bash
cd /path/to/ai_monitor_code
# 单路
python3 workers/monitor_worker.py -c config/channels.yaml -w . --ids test01
# 同一进程多路（线程，每路一个 FFmpeg）
python3 workers/monitor_worker.py -c config/channels.yaml -w . --ids ch001 ch002 ch003
```

### 2. 多路管理启动（推荐）

```bash
python3 manager.py -c config/channels.yaml -w . -n 4
# Worker 日志: logs/worker-0.log ...
# 异常退出会自动拉活（--max-restarts 控制上限，默认 50）
# 配置热重载默认开启（--reload-interval 3，--no-reload 关闭）
# 热重载状态: logs/status/_manager.json
```

### 配置热重载说明

| 角色 | 行为 |
|------|------|
| **Manager** | 轮询配置 mtime；按「分组指纹」只重启内容有变的 Worker；增删分组会启停进程 |
| **Worker** | 进程内按频道指纹增删改监测线程（URL/阈值/AI 等变更会重启该路） |

Web 面板保存配置后，一般 **3 秒内**生效，无需手工重启。  
禁用：`python3 manager.py ... --no-reload` 或 Worker `--no-reload`。

### 3. 离线验证 AI（不依赖组播）

```bash
# AI 关闭时（应安全跳过）
python3 test_ai_offline.py

# 启用启发式，测马赛克样本
python3 test_ai_offline.py --enable --mode heuristic --video mosaic_sample.mp4
```

## 日志与告警

| 路径 | 说明 |
|------|------|
| `logs/<频道ID>.log` | 频道文本日志 |
| `logs/events.jsonl` | 结构化事件（超阈值轮转为 `.1`..`.N`） |
| `logs/status/<频道ID>.json` | 心跳：state / active_alarms / ffmpeg_pid |
| `logs/worker-N.log` | Manager 下各 Worker 进程输出 |
| `snapshots/<频道ID>/` | 异常截图（每频道保留上限可配） |

事件类型示例：

- 开始：`black` / `freeze` / `silence` / `stream_down` / `ai_*`
- 结束：`black_end` / `freeze_end` / `silence_end`（可含 `duration`）

频道 Web 状态：`ok` / `alarm` / `reconnecting` / `stale` / `offline` / `disabled`

## defaults 运维参数（channels.yaml）

```yaml
defaults:
  reconnect_delay: 5.0
  reconnect_max_delay: 60.0
  input_timeout_sec: 15.0       # UDP/RTP 无数据超时，超时后重连
  heartbeat_interval: 5.0
  status_stale_sec: 30.0
  events_max_bytes: 52428800    # 50MB
  events_keep_files: 5
  snapshot_max_per_channel: 100
  # P2 性能
  detect_width: 480             # 规则检测缩放宽度，0=不缩放
  frame_interval_sec: 2.0       # 旁路帧间隔；0=关闭
  latest_max_age_sec: 5.0
  snapshot_prefer_latest: true
  ai_async: true
```

### P2 工作原理（单路一个 FFmpeg）

```
输入流
  → scale(detect_width) 降采样
  → split
       ├─ blackdetect + freezedetect → null（规则）
       └─ fps(1/frame_interval) → snapshots/<id>/latest.jpg（旁路）
  + silencedetect → null
```

- 告警截图：优先 `copy latest.jpg`；过期才后台独立拉流  
- AI：独立线程读 `latest.jpg`，不占用 stderr 解析循环

## 性能建议（纯 CPU）

| 机器配置 | 建议每 Worker 路数 | 总建议路数（仅规则） |
|----------|--------------------|----------------------|
| 4 核     | 2~3                | 8~12                 |
| 8 核     | 3~4                | 15~25                |
| 16 核+   | 4~6                | 30~50                |

开启 AI 后：每路约每 2 秒抽 1 帧推理，CPU 占用会增加。  
150 路建议多机部署，或后续加 GPU。

## AI 模块说明

| 配置 | 行为 |
|------|------|
| `ai.enabled: false`（默认） | 不加载任何 AI 代码路径，零影响 |
| `enabled: true` + 未装库 | 自动降级，只打日志，规则检测正常 |
| `enabled: true` + heuristic | OpenCV 统计检测绿屏/块状（**无需训练**） |
| `enabled: true` + onnx 模型 | 深度学习推理（模型在 **GPU/云训练** 后导入） |

### 训练 vs 监测（分工）

| 环境 | 做什么 | 目录/文档 |
|------|--------|-----------|
| **AutoDL / 有 GPU 的机器** | 用**图片**训练 → 导出 `.onnx` | `training/` · [TRAINING_AUTODL.md](docs/TRAINING_AUTODL.md) |
| **监测机（CentOS）** | 只装 onnxruntime，加载模型推理 | `models/` · [DEPLOY.md](docs/DEPLOY.md) |

训练**不直接读视频**；录像需先抽帧成 jpg/png。详见 [TRAINING.md](docs/TRAINING.md)。

### 训练数据目录里 `train` / `val` 是什么

训练脚本要求的数据布局（在 AutoDL 上自建，**不是**仓库自带目录）：

```text
dataset/                 # 你自己准备，常放 /root/autodl-tmp/dataset
├── train/               # 训练集：拿来更新模型权重（可做数据增强）
│   ├── normal/          # 正常画面 → 标签 0
│   └── anomaly/         # 马赛克/花屏/绿屏 → 标签 1
└── val/                 # 验证集：训练过程中评估，不参与反传
    ├── normal/
    └── anomaly/
```

| 目录 | 英文 | 作用 |
|------|------|------|
| **`train/`** | training set | **训练用**。模型看这些图算损失、改参数。占数据大头（常见约 80%）。 |
| **`val/`** | validation set | **验证用**。每个 epoch 结束后在这套图上算准确率/F1，用来选 **best.pt**、调阈值；**不拿来反传**，避免「背答案」。 |

要点：

1. **`val` 不是测试上线**：只是训练阶段的「模拟考试」，防止只在训练集上过拟合。  
2. **`train` 与 `val` 不要混同一镜头**：同一段故障录像抽的帧应只进一边，否则验证虚高。  
3. 比例经验：`train : val ≈ 8 : 1` 或 `9 : 1`；有条件可再留 `test/` 做最终报告（当前脚本默认只用 train+val）。  
4. 文件夹名必须是 **`normal`** / **`anomaly`**（英文），由路径决定标签。

```bash
# 在 GPU/AutoDL 上示例
cd training
python train.py --data /root/autodl-tmp/dataset --out /root/autodl-tmp/runs/exp1 --epochs 30
python export_onnx.py --ckpt .../best.pt --out .../mosaic_detector.onnx
# 再把 onnx 拷到监测机 models/ ，配置 ai.enabled=true
```

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/DEPLOY.md](docs/DEPLOY.md) | 监测服务器部署（含 AI 依赖、systemd） |
| [docs/TRAINING.md](docs/TRAINING.md) | 训练规范、样本、ONNX 约定、导入 |
| [docs/TRAINING_AUTODL.md](docs/TRAINING_AUTODL.md) | AutoDL 从 0 租机到导出 |
| [training/README.md](training/README.md) | 训练脚本命令速查 |
| [models/README.md](models/README.md) | 模型文件约定 |

## 注意事项

- 确保服务器能加入组播（网卡、防火墙、IGMP）
- 先规则检测试点，再开 AI；**训练在 GPU/云，不要在监测机上装 PyTorch 硬训**
- 正式阈值请用原始组播故障样本标定（`scan_threshold.py` + 现场微调）
