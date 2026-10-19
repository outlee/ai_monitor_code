# AI 节目异常监测系统（CentOS 版）

基于 FFmpeg 的 UDP 组播节目监测。规则检测黑场、静帧、无伴音、无信号。  
可选 **AI 画面分类**（一个 ONNX 认多类；仓库模板里默认关闭）。  
三台监测机只收流、检测。值班电脑上的桌面程序汇总三台，自己不加入组播。  
适配 **CentOS + 纯 CPU**。

> **服务器部署（含 AI 模块）请见：[docs/DEPLOY.md](docs/DEPLOY.md)**  
> **桌面汇总见下文「桌面程序」**。源码在 `desktop/`。已打包的 exe 在本机 `dist_desktop/`，不进仓库。  
> **本地训练机说明（仅训练、参考 i5-12400F / 16G / 3050）请见：[docs/LOCAL_DEPLOY_TRAIN.md](docs/LOCAL_DEPLOY_TRAIN.md)**  
> **AI 训练与导入请见：[docs/TRAINING.md](docs/TRAINING.md)** · **AutoDL 从 0 教程：[docs/TRAINING_AUTODL.md](docs/TRAINING_AUTODL.md)**  
> 训练脚本目录：`training/`（有显卡的机器或 AutoDL；监测机只跑 ONNX）

## 功能

- 支持 UDP 组播（`udp://@239.x.x.x:port`）
- 兼容 H.264 / H.265 / MPEG-2
- **规则检测**：黑场、静帧、无伴音、无信号（网口无载波）。静帧按墙钟计时。黑场、静帧、无信号的恢复事件带持续秒数
- **可选 AI**：一个 ONNX 按类别认画面。现网模型是 `normal` 和 `green_screen_mosaic`，打开分类告警后才按最高类告警。AI 高置信认为是正常画面时，抑制静帧和无伴音（例如央视「再见」收播画面）
- 异常时自动记录日志 + 保存截图
- 配置文件管理多路节目
- **多进程 Manager + Worker 内多线程并行**
- **流断自动重连**、Worker 异常拉活
- **心跳状态**（`logs/status/<id>.json`）供 Web 显示 online/离线/重连中
- 事件日志与截图自动轮转/清理
- **收流**：业务网卡抓包进共享缓冲，监测线程不再为每一路另加组播。缩略图单独抽帧，AI 在独立线程里看画面
- **配置热重载**：改 `channels.yaml` 后 Manager/Worker 自动应用（可 `--no-reload`）
- **Web 监控面板**（可选）：频道状态、事件、截图、分类筛选、节目排列
- **大屏交通灯** + **TTS 语音告警**（新告警 5 分钟抑制；未恢复约每 20 秒再提醒 / 多路聚合）
- **桌面汇总**：值班电脑 `127.0.0.1:8765`，三台节目排在一起；点节目或未恢复告警看本机实时预览，最近告警看当时的截图
- **SQLite 告警历史**（与 jsonl 双写，`data/monitor.db`）

## 目录结构

```
ai_monitor_code/
├── config/
│   ├── channels.yaml.example # 字段中文说明（模板，不是现网表）
│   └── channels.yaml         # 仓库里的一份参考；三台现网表不要覆盖进来，也不要提交现网改动
├── workers/
│   ├── monitor_worker.py     # 规则、截图、AI 线程、normal 抑制
│   ├── ai_detector.py        # ONNX / 启发式
│   ├── picture_classes.py    # 画面类别和列顺序
│   ├── frame_quality.py      # 绿屏、花屏、遮挡条
│   ├── freeze_rules.py       # 静帧墙钟
│   └── iface_mcast.py        # 业务网卡抓包，写入共享缓冲
├── web/
│   ├── app.py                # FastAPI 面板
│   ├── hub.py                # 一台汇总多台时的只读聚合
│   └── static/
├── desktop/                  # 值班电脑汇总程序（监测机不部署）
│   ├── launch.py
│   ├── server.py
│   ├── 启动.bat
│   └── nodes.empty.json      # 空节点模板；活的地址在 nodes.json
├── models/                   # 监测机放置 .onnx 和 .classes.json
│   └── README.md
├── training/                 # 只在有显卡的机器上跑
│   ├── train.py              # 按文件夹学一个模型
│   ├── export_onnx.py
│   ├── scan_threshold.py     # 只扫旧的二分类验证集
│   └── README.md
├── docs/
│   ├── DEPLOY.md
│   ├── TRAINING.md
│   └── TRAINING_AUTODL.md
├── dataset_zaijian/          # 「再见」正常画面，train 41 + val 11
├── event_db.py
├── manager.py
├── requirements.txt
├── data/                     # monitor.db，运行生成
├── logs/
├── snapshots/
└── README.md
```

`dist_desktop/` 是本机已打好的桌面程序，`.gitignore` 排除，不要提交。

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
  class_alarm: true   # 具名类别才按最高类告警；代码默认 false
  threshold: 0.7      # 建议值。各台当前数值以自己的 channels.yaml 和页面为准
```

- 具名模型要同时放 `models/mosaic_detector.onnx.classes.json`。列顺序和 `workers/picture_classes.py` 一致，不能改旧文件的列。
- 仓库模板 `ai.enabled` 是 `false`。10.6.9.173、176、177 现网已开 ONNX，分类告警是开的。
- 没有模型、且 `mode` 为 `auto` 或 `heuristic` 时，会退回 OpenCV 颜色/块状统计。现网花屏马赛克走分类模型，不要把旧的方块分数阈值当成检测器打开。
- 绿屏占比、纯绿小块的代码默认会告警。现网这两项设成大于 1（页面上限 2），这条规则不触发。
- 不装这些库、不把 `enabled` 打开时，规则检测照常跑。

### 启用 Web 面板时再装（可选）

```bash
pip3 install fastapi uvicorn
```

启动面板（与监测进程独立；配置可写，大屏/TTS 需浏览器打开）：

```bash
cd /path/to/ai_monitor_code
# 1）先起监测（告警会写入 data/monitor.db）
python3 manager.py -c config/channels.yaml -w . -n 6

# 2）再起 Web
pip3 install fastapi uvicorn pyyaml   # 首次
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
```

浏览器访问：`http://服务器IP:8080`

面板功能：
- **大屏**：四色交通灯网格（绿正常 / 红异常 / 黄离线重连 / 灰禁用）
- **管理**：频道增删改、AI/规则开关、导入导出
- 事件列表、异常截图、24h 告警 TOP（SQLite）
- **TTS 语音告警**（勾选 TTS；新告警 5 分钟同类型抑制；未恢复约每 20 秒再播；多路聚合播报）
- 声音蜂鸣、桌面通知（未恢复的告警同样约每 20 秒再提醒，直到恢复）

> 不启动 Web 不影响监测本身。大屏/TTS 详细说明见 [docs/DEPLOY.md](docs/DEPLOY.md) 中「Web 大屏、SQLite 历史与 TTS」。

## 桌面程序

值班电脑用它看三台，监测机不要装这一份。

源码启动：双击 `desktop/启动.bat`（必须是 UTF-8 BOM 和 CRLF），或在仓库根目录执行 `py -3 desktop/launch.py`。双击 `launch.py` 没有反应，这个系统没有把 `.py` 关联到 Python。服务只听 `127.0.0.1:8765`，窗口标题是「AI 节目监测」。就绪看 `GET /api/desktop/info`。

本机现在开着的是已经打好的程序：

`D:\ai_monitor_code\dist_desktop\AI_Monitor_Desktop\AI_Monitor_Desktop.exe`

它是 2026-10-15 17:04 的打包结果。页面文件和当前源码文字一致。`server.py`、`player.py`、`launch.py` 冻在 exe 里。`dist_desktop/`、`desktop/nodes.json`、`desktop/data/channel_order.json` 都留在这台电脑。

节点写在本机 `desktop/nodes.json`。仓库里只有空模板 `desktop/nodes.empty.json`。节目排列写在 `desktop/data/channel_order.json`。汇总时向每台要 `GET /api/dashboard?local=1`，各台只回自己的节目。不要把一台的 `channels.yaml` 覆盖到另外两台。

点监测中的节目或未恢复告警，本机用 ffplay 打开该台已有的 `GET /api/preview/{id}`，一次只开一路。最近告警和异常截图看当时的画面。预览不从值班电脑直接收组播。

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
现网选网卡写 `iface: enp1s0f1`（defaults 或单条频道）。抓包进程按这个网卡收流。URL 上的 `?localaddr=` 是旧写法。

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
python3 manager.py -c config/channels.yaml -w . -n 6
# -n 是每个 Worker 进程负责的节目数，不是 Worker 个数
# 现网 systemd 使用 -n 6。Worker 日志: logs/worker-0.log ...
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

- 开始：`black` / `freeze` / `silence` / `no_signal` / `stream_down` / `ai_*`
- 结束：`black_end` / `freeze_end` / `silence_end` / `no_signal_end`（可含 `duration`）
- 花屏马赛克没有恢复事件

频道 Web 状态：`ok` / `alarm` / `reconnecting` / `stale` / `offline` / `disabled`

## defaults 运维参数（channels.yaml）

字段说明以 [config/channels.yaml.example](config/channels.yaml.example) 为准。和现网容易看错的几项：

| 项 | 行为 |
|----|------|
| `snapshot_max_per_channel` | 代码没写这项时按 **500** 张。模板 `channels.yaml.example` 里写的是 100。各台以自己的 `channels.yaml` 为准 |
| `ai_suppress_freeze` / `ai_suppress_silence` | 不写也默认 **开**。AI 最高类是 `normal`、分数 ≥ `ai_suppress_min_score`（默认 0.7）、结果不超过 `ai_suppress_max_age_sec`（默认 30 秒）时，静帧和无伴音先不报 |
| `iface` | 业务网卡。现网是 `enp1s0f1`。频道可以单独覆盖 |
| `-n` | 每个 Worker 进程里的节目数，不是进程个数 |

### 收流和画面

业务网卡由抓包进程写入 `/dev/shm` 环形缓冲。监测线程读这份缓冲，不为每一路再加入一次组播。缩略图是另一次单帧抓取，写成 `latest_ok.jpg`。规则检测和 AI 不要再开第二路 UDP。

AI 在独立线程里看画面。上面的 normal 抑制打在落静帧、无伴音告警之前，日志是「因 AI normal 抑制…」。模型没有结果、分数不够或判成故障时，规则照常报。

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
| `ai.enabled: false`（模板默认） | 不跑画面模型 |
| `enabled: true` + 未装库 | 自动降级，只打日志，规则检测正常 |
| `enabled: true` + 无模型 | `auto` / `heuristic` 退回 OpenCV 颜色和块状统计 |
| `enabled: true` + onnx + `class_alarm` | 按 `*.classes.json` 的最高类告警。现网是这一档 |
| `class_alarm: false`（代码默认） | 只把最高类记在节目心跳里，不因此告警 |

### 训练 vs 监测（分工）

| 环境 | 做什么 | 目录/文档 |
|------|--------|-----------|
| **AutoDL / 有 GPU 的机器** | 用**图片**训练 → 导出 `.onnx` | `training/` · [TRAINING_AUTODL.md](docs/TRAINING_AUTODL.md) |
| **监测机（CentOS）** | 只装 onnxruntime，加载模型推理 | `models/` · [DEPLOY.md](docs/DEPLOY.md) |

训练**不直接读视频**；录像需先抽帧成 jpg。类别文件夹、列顺序和导出见 [training/README.md](training/README.md)。一次训练只出一个模型，不要按类各训一个再拼。

### 训练数据目录里 `train` / `val` 是什么

图放在有显卡的那台机器上，常用 `Z:\ai_monitor_code-main\dataset_frame`。仓库不带这份大图。

| 目录 | 作用 |
|------|------|
| **`train/`** | 练习。模型看这些图改参数，占数据大头 |
| **`val/`** | 考试。每轮结束后在这里算成绩、选 `best.pt`，不拿来改参数 |

同一段录像抽出来的帧只放一边。`train` 和 `val` 下面按类别建文件夹，例如 `normal`、`green_screen_mosaic`。旧的 `normal` + `anomaly` 两个文件夹仍是二分类，不要和具名故障目录混在同一份数据里。彩条放进 `normal`。静帧、闪烁单帧看不出来，放了也会跳过。

「再见」收播画面是正常画面，在 `dataset_zaijian/`（练习 41 张、考试 11 张），并已同步到 `dataset_frame` 的 `normal`。不要放进 `blue_screen`。

```bat
cd /d Z:\ai_monitor_code-main\training
python train.py --data Z:\ai_monitor_code-main\dataset_frame --out Z:\ai_monitor_code-main\training\runs\frame_zaijian1
python export_onnx.py --ckpt Z:\ai_monitor_code-main\training\runs\frame_zaijian1\best.pt --out Z:\ai_monitor_code-main\training\runs\frame_zaijian1\mosaic_detector.onnx
```

`frame1` 是已经训过的目录，下次不要覆盖它。导出后把 `.onnx` 和旁边的 `.classes.json` 一起拷到监测机 `models/`，再重启该台的 `ai-monitor`。不要在 173、176、177 上装 PyTorch。

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/DEPLOY.md](docs/DEPLOY.md) | 监测服务器部署（含 AI 依赖、systemd） |
| [docs/LOCAL_DEPLOY_TRAIN.md](docs/LOCAL_DEPLOY_TRAIN.md) | **本地训练机**（仅训练，参考 12400F/16G/3050） |
| [config/channels.yaml.example](config/channels.yaml.example) | **defaults / ai / 频道字段中文说明**（模板） |
| [config/channels_vlan20_all_disabled.yaml](config/channels_vlan20_all_disabled.yaml) | VLAN20 全量节目（默认全不启用） |
| [docs/TRAINING.md](docs/TRAINING.md) | 训练规范、样本、ONNX 约定、导入 |
| [docs/TRAINING_AUTODL.md](docs/TRAINING_AUTODL.md) | AutoDL 从 0 租机到导出 |
| [training/README.md](training/README.md) | 训练脚本命令、类别文件夹 |
| [models/README.md](models/README.md) | 模型文件约定 |
| [dataset_zaijian/README.txt](dataset_zaijian/README.txt) | 「再见」正常画面样本 |

## 注意事项

- 确保服务器能加入组播（网卡、防火墙、IGMP）
- 训练在有显卡的机器上做，不要在监测机上装 PyTorch
- `scan_threshold.py` 只扫旧的二分类验证集。具名模型换上之前先看导出日志里的类别
- 不要把三台现网的 `channels.yaml` 提交上来，也不要提交 `desktop/nodes.json`、节目排列、`*.onnx`、`dist_desktop/`
