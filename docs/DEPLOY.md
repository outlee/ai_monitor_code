# 服务器部署说明（含 AI 模块）

本文面向 **CentOS / 类 RHEL** 生产或预生产环境，说明如何从零部署 **AI 节目异常监测系统**：规则检测（黑场 / 静帧 / 静音）、可选 AI（马赛克 / 花屏）、Manager 多进程、Web 面板。

仓库地址示例：`https://github.com/outlee/ai_monitor_code`

---

## 1. 系统架构（部署视角）

```
                    ┌─────────────────────────────────────┐
                    │  config/channels.yaml               │
                    │  （频道 / 阈值 / AI 开关，可热重载）   │
                    └──────────────┬──────────────────────┘
                                   │
           ┌───────────────────────┼───────────────────────┐
           ▼                       ▼                       ▼
   ┌───────────────┐      ┌────────────────┐      ┌────────────────┐
   │  manager.py   │      │  uvicorn Web   │      │  运维 / 磁盘    │
   │  按组拉起      │      │  面板 :8080    │      │  logs/snapshots│
   │  Worker 进程  │      │  读写配置      │      └────────────────┘
   └───────┬───────┘      └────────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
 Worker-0     Worker-1   …   （每进程多线程，每频道 1 个 FFmpeg）
     │
     ├─ 规则：blackdetect / freezedetect / silencedetect
     ├─ 旁路：snapshots/<id>/latest.jpg
     └─ 可选 AI 线程：heuristic 或 ONNX
```

| 组件 | 是否必须 | 说明 |
|------|----------|------|
| FFmpeg + Python3 + PyYAML | **必须** | 规则监测 |
| manager.py | **推荐** | 多路进程管理、崩溃拉活、配置热重载 |
| Web（FastAPI） | 可选 | 内网面板；不启也不影响监测 |
| AI 依赖 + 配置 | 可选 | 默认关闭；不装库、不改配置则零影响 |

**建议上线顺序**：先只开规则检测 1～3 路 → 稳定后再加路数 → 再开 Web → 最后试点 AI。

---

## 2. 服务器要求

### 2.1 硬件（经验值，仅规则检测）

| 配置 | 建议监测路数（规则） | 备注 |
|------|----------------------|------|
| 4 核 / 8GB | 8～12 路 | 每 Worker 2～3 路 |
| 8 核 / 16GB | 15～25 路 | 每 Worker 3～4 路 |
| 16 核+ / 32GB | 30～50 路 | 每 Worker 4～6 路 |
| 150 路 | 多机拆分 | 或加 GPU 后再开 AI |

开启 **AI** 后：每路约按 `ai.interval_sec`（默认 2s）做一次帧分析，CPU 明显上升，建议先小规模试点。

### 2.2 软件

| 软件 | 要求 |
|------|------|
| OS | CentOS 7/8、Rocky、Alma 等（本文以 yum/dnf 为例） |
| Python | 3.6+（推荐 3.8+） |
| FFmpeg | 带 `blackdetect` / `freezedetect` / `silencedetect`，支持 H.264/H.265/MPEG-2 |
| 网络 | 能加入业务 **UDP 组播**（网卡、交换机 IGMP、防火墙） |
| GPU | **不必须**；无 GPU 可用 CPU ONNX 或启发式 |

### 2.3 磁盘

- `logs/`：频道日志 + `events.jsonl`（有轮转，仍建议预留数 GB）
- `snapshots/`：异常截图 + `latest.jpg`（按频道上限清理）
- 建议与系统盘分离或定期清理策略写进运维规范

---

## 3. 获取代码

```bash
# 方式 A：git（推荐）
cd /opt
sudo git clone https://github.com/outlee/ai_monitor_code.git
# 或 SSH：
# sudo git clone git@github.com:outlee/ai_monitor_code.git
sudo chown -R "$USER":"$USER" /opt/ai_monitor_code
cd /opt/ai_monitor_code

# 方式 B：上传压缩包后解压到 /opt/ai_monitor_code
```

下文以安装目录 **`/opt/ai_monitor_code`** 为例，请按实际路径替换。

---

## 4. 安装系统依赖

### 4.1 Python

```bash
# CentOS 7
sudo yum install -y python3 python3-pip python3-devel gcc

# CentOS 8 / Rocky / Alma
# sudo dnf install -y python3 python3-pip python3-devel gcc
python3 --version
pip3 --version
```

### 4.2 FFmpeg

任选一种能跑通 `ffmpeg -version` 的方式。

**方式 1：官方/第三方静态包（省事）**

```bash
# 示例：下载官方或 johnvansickle 等静态构建，解压后放入 PATH
# 确认存在：
ffmpeg -version
ffprobe -version
```

**方式 2：RPM Fusion（需按发行版文档启用仓库）**

```bash
# 示意，具体以当前发行版文档为准
sudo yum install -y ffmpeg ffmpeg-devel
```

**自检：**

```bash
ffmpeg -filters 2>/dev/null | grep -E 'blackdetect|freezedetect|silencedetect'
# 应能看到上述三个滤镜
```

### 4.3 基础 Python 包（监测必须）

```bash
cd /opt/ai_monitor_code
pip3 install -U pip
pip3 install 'PyYAML>=6.0'
# 或
# pip3 install -r requirements.txt
# （requirements.txt 中 Web/AI 行为注释，需按需取消注释或单独安装）
```

### 4.4 Web 面板依赖（可选）

```bash
pip3 install 'fastapi>=0.110.0' 'uvicorn>=0.27.0' 'python-multipart>=0.0.9'
```

### 4.5 AI 模块依赖（可选，见第 9 节）

```bash
# 纯 CPU（推荐先装这套）
pip3 install 'onnxruntime>=1.16.0' 'opencv-python-headless>=4.8.0' 'numpy>=1.24.0' 'Pillow>=9.0.0'

# 仅当确认有 NVIDIA + CUDA 且要用 GPU 推理时：
# pip3 uninstall -y onnxruntime
# pip3 install 'onnxruntime-gpu>=1.16.0' 'opencv-python-headless>=4.8.0' 'numpy>=1.24.0' 'Pillow>=9.0.0'
```

> **说明**：`ai.enabled: false` 时不必安装 AI 包。  
> 若 `enabled: true` 但库未装，程序会**自动降级**，规则检测仍正常。

---

## 5. 目录与权限

```bash
cd /opt/ai_monitor_code
mkdir -p logs snapshots models config
chmod 755 logs snapshots models config

# 若用 systemd 以专用用户运行（推荐）：
# sudo useradd -r -s /sbin/nologin aimonitor
# sudo chown -R aimonitor:aimonitor /opt/ai_monitor_code
```

运行后主要产生：

| 路径 | 内容 |
|------|------|
| `logs/<频道ID>.log` | 频道文本日志 |
| `logs/events.jsonl` | 结构化告警事件 |
| `logs/status/<频道ID>.json` | 心跳状态 |
| `logs/worker-N.log` | 各 Worker 进程 stdout |
| `logs/status/_manager.json` | Manager 热重载状态 |
| `snapshots/<频道ID>/` | 截图与 `latest.jpg` |

---

## 6. 网络与组播检查（上线前必做）

### 6.1 网卡与路由

```bash
ip -4 addr
# 确认业务网卡 IP，例如 192.168.1.100
```

组播 URL 指定网卡示例：

```text
udp://@239.1.1.1:5000?localaddr=192.168.1.100
```

### 6.2 防火墙

```bash
# firewalld 示例：放行组播相关（按实际端口调整）
# sudo firewall-cmd --permanent --add-port=5000/udp
# sudo firewall-cmd --reload

# 或临时关闭验证（仅实验室）
# sudo systemctl stop firewalld
```

交换机 / 核心需开启 **IGMP**，服务器与源在同一组播域或路由可达。

### 6.3 FFmpeg 能否拉流

```bash
# 将地址换成真实组播；观察数秒是否有码流信息
timeout 8 ffmpeg -hide_banner -i "udp://@239.1.1.1:5000?localaddr=192.168.1.100" -t 3 -f null - 2>&1 | tail -30
```

能看到视频/音频流信息且无持续报错，再进入配置。

### 6.4 查 MPTS 节目号（program，十进制）

```bash
ffprobe -hide_banner "udp://@239.1.1.1:5000?localaddr=192.168.1.100" 2>&1 | head -80
# 或
ffmpeg -i "udp://@239.1.1.1:5000" 2>&1 | head -40
```

一地址一节目（SPTS）一般 **不填 program**；  
一地址多节目（MPTS）为每个节目建一条频道，填写十进制 `program`。

---

## 7. 配置文件 `config/channels.yaml`

### 7.1 最小可运行示例

```yaml
defaults:
  black_duration: 2.0
  freeze_duration: 3.0
  silence_duration: 3.0
  silence_threshold: -40
  save_snapshot: true
  snapshot_dir: ./snapshots
  log_dir: ./logs
  reconnect_delay: 5.0
  reconnect_max_delay: 60.0
  input_timeout_sec: 15.0
  heartbeat_interval: 5.0
  status_stale_sec: 30.0
  detect_width: 480
  frame_interval_sec: 2.0
  snapshot_prefer_latest: true
  ai_async: true

ai:
  enabled: false          # 上线初期建议 false
  mode: auto
  model_path: models/mosaic_detector.onnx
  interval_sec: 2.0
  threshold: 0.55
  green_ratio_th: 0.35
  block_score_th: 0.12

channels:
  - id: ch001
    name: 综合频道
    url: "udp://@239.1.1.1:5000?localaddr=192.168.1.100"
    enabled: true
  - id: ch002
    name: 节目B
    url: "udp://@239.1.1.1:5000?localaddr=192.168.1.100"
    program: 2            # 仅 MPTS 需要
    enabled: true
```

### 7.2 defaults 常用项

| 参数 | 含义 | 建议 |
|------|------|------|
| `black/freeze/silence_duration` | 持续多久才告警（秒） | 按台标标定 |
| `silence_threshold` | 静音 dB 阈值 | 默认 -40 |
| `input_timeout_sec` | UDP 无数据超时 | 10～20 |
| `detect_width` | 检测前缩放宽度 | 320～480 省 CPU |
| `frame_interval_sec` | 旁路 latest 刷新间隔 | 2～3；开 AI 可与 interval 对齐 |
| `snapshot_max_per_channel` | 每频道截图上限 | 50～200 |
| `events_max_bytes` | 事件文件轮转阈值 | 默认 50MB |

### 7.3 频道字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 唯一 ID，用于日志/截图目录 |
| `name` | 是 | 显示名称（Web / 告警） |
| `url` | 是 | 组播/单播/文件等 |
| `enabled` | 否 | 默认 true |
| `program` | 否 | MPEG-TS program（**十进制**），MPTS 用 |

也可用 **Web 面板** 增删改频道（写入同一 YAML，热重载约 3 秒生效）。

---

## 8. 启动监测服务

### 8.1 前台试运行（验证用）

```bash
cd /opt/ai_monitor_code

# 单路
python3 workers/monitor_worker.py -c config/channels.yaml -w . --ids ch001

# 多路（推荐 Manager）
python3 manager.py -c config/channels.yaml -w . -n 4
# -n：每个 Worker 进程负责的路数，CPU 环境建议 3～6
```

观察：

```bash
tail -f logs/worker-0.log
tail -f logs/ch001.log
cat logs/status/ch001.json
tail -f logs/events.jsonl
```

正常时 `status` 中 `state` 多为 `running`，异常有 `black` / `freeze` / `silence` 等事件。

停止：前台 `Ctrl+C`。

### 8.2 Manager 常用参数

```bash
python3 manager.py -c config/channels.yaml -w . \
  -n 4 \
  --reload-interval 3 \
  --max-restarts 50

# 关闭热重载：
# python3 manager.py ... --no-reload
```

| 参数 | 含义 |
|------|------|
| `-n` / `--per-worker` | 每进程频道数 |
| `--reload-interval` | 配置轮询间隔（秒） |
| `--max-restarts` | 单 Worker 崩溃拉活上限 |
| `--no-reload` | 禁用热重载 |

### 8.3 systemd 常驻（生产推荐）

创建监测服务单元：

```bash
sudo tee /etc/systemd/system/ai-monitor.service << 'EOF'
[Unit]
Description=AI Program Monitor (Manager)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/ai_monitor_code
ExecStart=/usr/bin/python3 /opt/ai_monitor_code/manager.py -c config/channels.yaml -w /opt/ai_monitor_code -n 4
Restart=on-failure
RestartSec=5
# User=aimonitor
# Group=aimonitor
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ai-monitor
sudo systemctl status ai-monitor
journalctl -u ai-monitor -f
```

Web 面板单独单元（可选）：

```bash
sudo tee /etc/systemd/system/ai-monitor-web.service << 'EOF'
[Unit]
Description=AI Program Monitor Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/ai_monitor_code
ExecStart=/usr/bin/python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5
# User=aimonitor
# Group=aimonitor

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ai-monitor-web
```

防火墙放行 Web（按需）：

```bash
# sudo firewall-cmd --permanent --add-port=8080/tcp
# sudo firewall-cmd --reload
```

浏览器访问：`http://<服务器IP>:8080`  
**面板无登录鉴权，请仅内网使用或前加反向代理鉴权。**

---

## 9. AI 模块部署（详细）

### 9.1 能力说明

| 模式 | 依赖 | 模型文件 | 适用 |
|------|------|----------|------|
| `enabled: false` | 无 | 无 | 默认；仅规则 |
| `heuristic` | OpenCV + numpy | 不需要 | 明显绿屏/块状马赛克，快速试点 |
| `onnx` | onnxruntime +（opencv 或 Pillow） | `models/*.onnx` | 有训练模型时 |
| `auto` | 同上 | 有模型用 ONNX，否则尝试启发式 | 推荐生产配置 |

**设计原则**：AI 失败/未装库 **绝不拖垮** 规则检测。

### 9.2 推荐部署路径

```
阶段 1：ai.enabled=false          → 规则稳定
阶段 2：enabled=true, mode=heuristic → 装 OpenCV，无模型验证流程
阶段 3：放入 .onnx，mode=auto/onnx  → 真模型推理
```

### 9.3 安装 AI 依赖（CPU）

```bash
cd /opt/ai_monitor_code
pip3 install 'onnxruntime>=1.16.0' 'opencv-python-headless>=4.8.0' 'numpy>=1.24.0' 'Pillow>=9.0.0'

# 验证导入
python3 - << 'PY'
import cv2, numpy, onnxruntime
print("opencv", cv2.__version__)
print("ort", onnxruntime.__version__)
print("providers", onnxruntime.get_available_providers())
PY
```

纯启发式可只装：

```bash
pip3 install 'opencv-python-headless>=4.8.0' 'numpy>=1.24.0'
```

### 9.4 配置 AI（channels.yaml）

```yaml
ai:
  enabled: true
  mode: heuristic          # 先 heuristic；有模型改 auto 或 onnx
  model_path: models/mosaic_detector.onnx
  interval_sec: 2.0        # 分析间隔（秒），越大越省 CPU
  threshold: 0.55          # ONNX 异常分阈值，≥ 判异常
  green_ratio_th: 0.35     # 启发式：绿色像素占比
  block_score_th: 0.12     # 启发式：块状分数
```

`defaults` 中与 AI 相关：

```yaml
defaults:
  frame_interval_sec: 2.0  # 旁路 latest 刷新，建议 ≥ 或 ≈ ai.interval_sec
  latest_max_age_sec: 5.0
  ai_async: true           # AI 独立线程，务必保持 true
```

保存后 **热重载数秒内生效**（Manager/Worker 在跑且未 `--no-reload`）。也可在 Web「功能开关」里改 AI 并保存。

### 9.5 放置 ONNX 模型（可选）

```bash
# 将模型拷到服务器
scp mosaic_detector.onnx user@server:/opt/ai_monitor_code/models/
ls -la /opt/ai_monitor_code/models/
```

**模型约定**（与 `models/README.md` 一致）：

| 项 | 约定 |
|----|------|
| 输入 | `float32`，`1×3×224×224`，RGB，像素 0～1 |
| 输出 | `1×2`（[正常, 异常]）或 `1×1`（异常分数） |

配置：

```yaml
ai:
  enabled: true
  mode: auto          # 或 onnx
  model_path: models/mosaic_detector.onnx
  threshold: 0.55
```

无模型却设 `mode: onnx`：AI 不可用，规则仍正常。  
`mode: auto` 且无模型：自动尝试 heuristic。

### 9.6 离线验证 AI（不依赖组播）

```bash
cd /opt/ai_monitor_code

# 1）关闭状态应安全跳过
python3 test_ai_offline.py

# 2）启发式 + 单图
python3 test_ai_offline.py --enable --mode heuristic --image /path/to/frame.jpg

# 3）启发式 + 视频抽帧
python3 test_ai_offline.py --enable --mode heuristic --video /path/to/sample.ts --frames 8

# 4）ONNX（需模型文件存在）
# 将 test 脚本中 model_path 与配置一致；或先改 channels 后在业务流上观察 ai_* 事件
```

期望输出类似：

```text
AI 状态: {'enabled': True, 'available': True, 'backend': 'heuristic', ...}
  正常 | frame_001.jpg | normal | score=...
⚠ 异常 | mosaic.jpg | mosaic | score=...
```

### 9.7 在线确认 AI 已挂上

```bash
# 频道日志中应有类似：
grep -i "AI 状态" logs/ch001.log | tail -5
# enabled=True, available=True, backend=heuristic|onnx

# 异常事件类型：
grep '"type": "ai_' logs/events.jsonl | tail -10
```

Web 总览卡片「AI 模块」显示开启及 mode。

### 9.8 AI 性能与调参

| 手段 | 作用 |
|------|------|
| 增大 `interval_sec` | 降推理频率 |
| 保持 `ai_async: true` | 不堵规则解析 |
| 增大 `frame_interval_sec` | 降旁路写盘频率 |
| 先 heuristic 少路试点 | 评估误报 |
| 连续异常再告警（当前未做） | 后续可增强 |

启发式阈值：

- `green_ratio_th` 调高 → 更不易报绿屏  
- `block_score_th` 调高 → 更不易报马赛克  

请用 **本台真实故障录像** 标定，勿直接照搬默认值到全量 150 路。

### 9.9 AI 常见问题

| 现象 | 处理 |
|------|------|
| `available=False` | 检查 pip 包；`mode=onnx` 时检查模型路径 |
| 无 `ai_*` 事件 | 确认 enabled、backend、interval；是否真有花屏样本 |
| 误报多 | 调高 heuristic 阈值或改用/重训 ONNX；缩小试点范围 |
| CPU 打满 | 关 AI 或加大 interval；减少每机路数 |
| 与规则重复告警 | 正常可并存；后续可做关联策略 |

---

## 10. Web 面板使用要点

```bash
cd /opt/ai_monitor_code
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
# 或 systemctl start ai-monitor-web
```

| 功能 | 说明 |
|------|------|
| 频道管理 | 新增/编辑/删除/导入导出；填名称、组播 URL、可选 program |
| 功能开关 | AI 启停、mode、阈值；规则时长阈值 |
| 状态 | ok / alarm / reconnecting / stale / offline / disabled |
| 事件 / 截图 | 读 `logs/events.jsonl` 与 `snapshots/` |
| 本地提醒 | 浏览器声音、桌面通知（需授权） |

配置写入 `channels.yaml` 后 **热重载约 3 秒** 被 Manager/Worker 应用（监测服务须在运行）。

---

## 11. 部署验收清单

按顺序打勾：

- [ ] `python3` / `ffmpeg` / `ffprobe` 可用  
- [ ] `pip3 install PyYAML` 成功  
- [ ] 组播试拉流成功（第 6.3 节）  
- [ ] `channels.yaml` 至少 1 路真实地址、`enabled: true`  
- [ ] Manager 启动后 `logs/status/<id>.json` 的 `state` 为 `running`  
- [ ] 人为或样本触发黑场/静帧/静音，有事件与截图  
- [ ] （可选）Web 可打开，能看到频道名称与状态  
- [ ] （可选）AI 离线脚本 backend 正常；在线日志 `AI 状态 available=True`  
- [ ] （可选）systemd 开机自启、`systemctl status` 正常  

---

## 12. 运维日常

### 12.1 查看状态

```bash
systemctl status ai-monitor ai-monitor-web
tail -20 logs/status/_manager.json
ls logs/status/
tail -50 logs/events.jsonl
df -h   # 磁盘
```

### 12.2 改配置

- 编辑 `config/channels.yaml` 或 Web 保存  
- 默认 **无需** 重启服务（热重载）  
- 若使用 `--no-reload`，则需：`sudo systemctl restart ai-monitor`

### 12.3 升级代码

```bash
cd /opt/ai_monitor_code
git pull
# 如有新依赖再 pip3 install
sudo systemctl restart ai-monitor
sudo systemctl restart ai-monitor-web   # 若启用了 Web
```

### 12.4 日志与截图清理

程序内已有 `events` 轮转与每频道截图上限。仍建议：

```bash
# 示例：清理 30 天前日志（按需，慎用）
# find logs -name '*.log' -mtime +30 -delete
```

---

## 13. 故障排查速查

| 现象 | 排查 |
|------|------|
| 一直 `reconnecting` / `stream_down` | 组播不通、地址错、网卡错、防火墙；`ffmpeg -i` 自测 |
| 无静音检测 / FFmpeg 退出 | 流无音轨导致 filter 失败（后续可做无音轨兼容）；暂只测有声道节目 |
| 状态 `stale` | Worker 挂了或机器卡顿；看 `worker-N.log`、systemd |
| Web 看不到新频道 | 是否写到正确 workdir 的 `config/channels.yaml`；Manager 是否在同一目录启动 |
| 截图花屏/半帧 | 已加固 latest 读取；仍差可关旁路 `frame_interval_sec: 0` 试对比 |
| AI 不工作 | `enabled`、依赖、`mode`、模型路径；看频道日志 AI 状态行 |
| 改配置不生效 | 是否 `--no-reload`；mtime 是否更新；看 Manager 日志「热重载完成」 |

---

## 14. 安全建议

1. Web **无登录**，勿对公网暴露 8080；内网 + 防火墙或 Nginx 基本鉴权。  
2. 配置文件含组播地址，注意备份与权限（如 `640`、专用用户）。  
3. 运行用户尽量非 root（systemd 中 `User=aimonitor`）。  
4. 定期 `git pull` 与依赖安全更新。

---

## 15. 快速命令汇总

```bash
# 安装目录
cd /opt/ai_monitor_code

# 必须依赖
pip3 install 'PyYAML>=6.0'

# 可选 Web
pip3 install fastapi uvicorn python-multipart

# 可选 AI（CPU）
pip3 install onnxruntime opencv-python-headless numpy Pillow

# 启动监测
python3 manager.py -c config/channels.yaml -w . -n 4

# 启动 Web
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080

# 离线测 AI
python3 test_ai_offline.py --enable --mode heuristic --image /path/to.jpg

# systemd
sudo systemctl enable --now ai-monitor
sudo systemctl enable --now ai-monitor-web
```

---

## 16. 相关文档

| 文件 | 内容 |
|------|------|
| `README.md` | 功能总览与开发向说明 |
| `models/README.md` | ONNX 输入输出约定 |
| `config/channels.yaml` | 当前运行配置 |
| `docs/DEPLOY.md` | 本文（服务器部署 + AI 推理侧） |
| `docs/TRAINING.md` | AI 训练规范与导入监测机 |
| `docs/TRAINING_AUTODL.md` | **AutoDL 从 0 训练手把手教程** |
| `training/` | 训练 / 导出 ONNX 脚本 |

---

*文档版本与仓库功能同步：多进程 Manager、热重载、program 选节目、旁路帧、可选 AI（heuristic/ONNX）。若现场发行版命令有差异，以实际包管理器文档为准。*
