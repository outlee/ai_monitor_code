# 本地部署与本地训练说明

本文说明如何在 **本机（工作站 / 台式）** 完成两件事：

1. **部署并运行** 节目监测（规则检测 + Web + 可选 AI 推理）  
2. **本地训练** 马赛克/花屏模型并导出 ONNX，供监测使用  

云训练（AutoDL）见 [TRAINING_AUTODL.md](TRAINING_AUTODL.md)；仅监测服务器见 [DEPLOY.md](DEPLOY.md)；训练规范见 [TRAINING.md](TRAINING.md)。

---

## 1. 参考配置（本文基准）

| 项目 | 参考规格 | 说明 |
|------|----------|------|
| 机型/整机 | D16T200（示例） | 按你实际机箱/品牌即可 |
| CPU | Intel Core **i5-12400F** | 6 核 12 线程，够跑多路 FFmpeg + 训练 |
| 内存 | **16GB** DDR4-3200 | 监测够用；训练大数据时建议尽量关其它占内存程序 |
| 系统盘 | **512GB NVMe** | 系统 + 代码 + 中小规模样本；大样本建议另挂盘 |
| 显卡 | **NVIDIA RTX 3050 6GB** | 本地训练主力；监测推理可只用 CPU |

**能力预期（经验值，非承诺）：**

| 用途 | 预期 |
|------|------|
| 仅规则监测 | 约十余路～二十多路量级（视码率/编码，需实测） |
| 规则 + AI 推理 | 路数要少一些；`interval_sec` 可调大 |
| 本地训练 | 二分类 MobileNet 级模型可训；batch 建议 16～32（6GB 显存） |
| 样本规模 | 数万张以内较舒适；更大时注意磁盘与训练时间 |

若内存长期吃紧，可优先保证监测，训练时关掉浏览器多标签、其它占显存软件。

---

## 2. 总体建议怎么用这台机器

推荐 **同一台机两种角色分时做**，避免抢 GPU/磁盘：

```text
时段 A（白天/联调）：当监测机
  → 起 Manager + Web，接组播或本地测试流

时段 B（有样本时）：当训练机
  → 停掉大批量监测（或只留 1 路），用 3050 训练 → 导出 onnx
  → 再把模型拷到 models/，打开 AI 推理
```

也可以：监测常开用 **CPU 推理**，训练时独占 GPU（训练脚本默认用 CUDA）。

---

## 3. 系统环境准备

### 3.1 操作系统

任选其一即可：

| 系统 | 说明 |
|------|------|
| **Windows 10/11** | 可用 VS Code + 本机 Python；组播在 Windows 上有时比 WSL 顺利 |
| **Windows + WSL2** | 开发命令接近 CentOS；**组播收流可能受限**，收流建议本机或真服务器 |
| **Ubuntu 22.04 等** | 与文档命令最接近，驱动装好后训练体验好 |

以下命令以 **Linux / WSL** 风格为主；Windows 把路径换成项目目录（如 `D:\ai_monitor_code`）即可。

### 3.2 必装软件

1. **Python 3.8+**（推荐 3.10/3.11）  
2. **FFmpeg**（规则监测、抽帧）  
3. **Git**（拉代码）  
4. **NVIDIA 驱动**（训练必须；设备管理器 / `nvidia-smi` 能看到 3050）  
5. 训练时：按官网安装与驱动匹配的 **CUDA 版 PyTorch**（见第 6 节）

### 3.3 获取代码

从 GitHub 克隆或下载本仓库到本地，例如：

- 目录示例：`D:\ai_monitor_code` 或 `/home/你/ai_monitor_code`  
- 仓库：`https://github.com/outlee/ai_monitor_code`

---

## 4. 本地部署监测（推理侧）

### 4.1 安装依赖

在项目根目录：

| 用途 | 安装内容 |
|------|----------|
| 必须 | PyYAML |
| Web 面板 | fastapi、uvicorn、python-multipart |
| AI 推理（可选） | onnxruntime、opencv-python-headless、numpy、Pillow |

说明：

- **监测推理**用 `onnxruntime`（CPU 即可，3050 也可装 GPU 版 ort，非必须）  
- **不要**把训练用的大型 torch 环境和服务监测混成一个乱环境时，可用两个 venv：`venv-monitor` / `venv-train`

### 4.2 配置频道

编辑 `config/channels.yaml`：

- 真实组播：`udp://@239.x.x.x:port`（可加网卡 `localaddr`）  
- 本机调试：可用本地视频文件路径  
- 一地址多节目：填十进制 `program`  
- 上线初期建议 `ai.enabled: false`，规则稳定后再开  

### 4.3 启动方式

需要 **两个进程**（两个终端）：

1. **监测**  
   - 推荐：`manager.py`（多路、热重载、拉活）  
   - 或单路：`workers/monitor_worker.py` 指定频道 id  

2. **Web（可选）**  
   - `uvicorn web.app:app`，监听如 `0.0.0.0:8080` 或本机 `127.0.0.1:8080`  

浏览器打开面板后：

- **大屏**：四色交通灯  
- **管理**：频道与阈值  
- **TTS**：语音告警（保持页面打开）  
- 告警会写入 `logs/` 与 `data/monitor.db`  

详细界面说明见 [DEPLOY.md](DEPLOY.md) 中「Web 大屏、SQLite 历史与 TTS」。

### 4.4 本机无组播时怎么验

1. 用 FFmpeg 生成短黑场/测试片  
2. 频道 `url` 指到该文件  
3. 看日志是否出现黑场/静帧/静音事件、截图与心跳  

有组播时再改回真实地址压测路数。

### 4.5 本机资源建议（i5-12400F + 16G）

| 参数 | 建议起点 |
|------|----------|
| Manager `-n`（每进程路数） | 3～4 |
| `detect_width` | 480 或 320 |
| `frame_interval_sec` | 2～3 |
| 开 AI 时 `interval_sec` | 2～4（越大越省 CPU） |
| 同时开 Chrome 多标签 | 训练或高压测时少开 |

任务管理器 / `nvidia-smi` 观察 CPU、内存、GPU 占用，再加减路数。

---

## 5. 训练前：图片怎么准备

训练 **只用图片**，不用直接丢 mp4/ts 进训练脚本。

### 5.1 目录结构（必须）

```text
dataset/
  train/                 ← 用来「学」（约占 80%～90%）
    normal/              正常画面
    anomaly/             马赛克 / 花屏 / 绿屏等
  val/                   ← 用来「考」（约占 10%～20%）
    normal/
    anomaly/
```

- **train** 与 **val** 里都要有 normal 和 anomaly  
- **同一张图不要同时出现在 train 和 val**  
- 同一段故障录像抽的帧，尽量整段只进 train 或只进 val  

含义详见 README / TRAINING 中 train、val 说明。

### 5.2 参考配置下的数据量与磁盘

| 规模 | 约占用（粗算 jpg） | 本机 512G |
|------|--------------------|-----------|
| 小试 几千张 | 数百 MB～数 GB | 轻松 |
| 中等 1～3 万张 | 数 GB～十余 GB | 可行 |
| 很大 十万级+ | 数十 GB+ | 建议外置盘，系统盘留余量 |

### 5.3 从视频抽帧（概念）

对正常片源、故障录像按时间间隔抽成 jpg，再人工分到 normal / anomaly，再按比例拆到 train / val。

---

## 6. 本地训练（RTX 3050 6GB）

### 6.1 环境

1. 安装较新的 **NVIDIA Game Ready / Studio 驱动**  
2. 命令行执行 `nvidia-smi`，确认能看到 **RTX 3050** 与驱动版本  
3. 按 [PyTorch 官网](https://pytorch.org) 选择与 CUDA 匹配的安装命令，装到 **训练用** Python 环境  
4. 进入仓库 `training/` 目录，安装该目录依赖（onnx、opencv、tqdm 等；torch 以官网为准）

验证：`torch.cuda.is_available()` 应为真。

### 6.2 显存与参数（针对 3050 6GB）

| 参数 | 建议 |
|------|------|
| `--batch-size` | **16 或 32**（OOM 则降到 8） |
| `--workers` | Windows 可试 2～4；过高有时反而慢 |
| `--epochs` | 小数据 10～20；正式 20～40 |
| 模型 | 仓库默认 MobileNet 级，适合 6G 显存与 CPU 推理 |

训练输出建议放在数据盘或项目下 `runs/某次实验/`，不要塞满系统盘根目录。

### 6.3 训练 → 导出 → 扫阈值（步骤）

在 `training/` 下按文档顺序：

1. **train**：指定 `--data` 为 dataset 根目录，`--out` 为输出目录  
2. **export_onnx**：用 `best.pt` 导出 `mosaic_detector.onnx`  
3. **scan_threshold**：在 val 上扫推荐阈值  

脚本说明见 `training/README.md`；命令细节见 [TRAINING_AUTODL.md](TRAINING_AUTODL.md)（本地把路径换成你的盘符即可，不必用 AutoDL）。

### 6.4 训练时注意

- 训练时若同时跑很多路监测，CPU/磁盘会争抢，建议减少监测路数  
- 笔记本/整机注意散热，3050 满载会明显升温  
- 训完可关掉训练环境，监测只保留 onnxruntime  

---

## 7. 模型接到本地监测

1. 将 `mosaic_detector.onnx` 放到项目 `models/`  
2. `config/channels.yaml` 中：  
   - `ai.enabled: true`  
   - `mode: auto` 或 `onnx`  
   - `model_path: models/mosaic_detector.onnx`  
   - `threshold` 用扫描结果作起点，再按误报微调  
3. Manager/Worker 热重载（或重启）后看日志是否 `backend=onnx`  
4. 可用 `test_ai_offline.py` 对单图做离线验证  

无模型时仍可用 `mode: heuristic`（仅 OpenCV，不用 3050 训练）。

---

## 8. VS Code 本地联调（简要）

1. 用 VS Code 打开项目文件夹（可选 WSL 远程）  
2. 终端 1：起 Manager  
3. 终端 2：起 Web  
4. 浏览器打开本机 8080 端口看大屏 / TTS  
5. 规则逻辑可用本地测试视频验证，不必一上来就接组播  

组播若在 WSL 收不到，改用 **Windows 本机 Python** 跑监测，或到机房服务器测收流。

---

## 9. 参考配置下的检查清单

**部署**

- [ ] Python、FFmpeg 可用  
- [ ] 监测依赖与 Web 依赖已装  
- [ ] Manager + Web 能同时跑  
- [ ] 大屏能打开；有事件时日志与 `data/monitor.db` 有记录  

**训练**

- [ ] `nvidia-smi` 正常  
- [ ] dataset 已按 train/val × normal/anomaly 放好  
- [ ] 训练不 OOM（batch 已调）  
- [ ] 已导出 onnx 并用离线脚本验证  
- [ ] 监测侧 `ai.enabled` 打开且 backend 为 onnx 或 heuristic  

**磁盘**

- [ ] 系统盘剩余空间充足（建议至少留 50GB+）  
- [ ] 大样本不堆在临时目录后遗忘清理  

---

## 10. 和 AutoDL 怎么选

| 情况 | 建议 |
|------|------|
| 已有本文参考配置、样本不大 | **本地 3050 训练** 方便，数据不用上传 |
| 样本极大、要多卡/长时间 | AutoDL 更合适 |
| 只有监测、无训练 | 本机只做部署；模型可从别处拷贝 |
| 台站服务器无 GPU | 监测放服务器；训练放本机 3050 或云 |

---

## 11. 相关文档

| 文档 | 内容 |
|------|------|
| 本文 | 本地部署 + 本地训练（参考 i5-12400F / 16G / 3050 6G） |
| [DEPLOY.md](DEPLOY.md) | 服务器/通用部署、Web 大屏与 TTS |
| [TRAINING.md](TRAINING.md) | 样本规范、ONNX 约定 |
| [TRAINING_AUTODL.md](TRAINING_AUTODL.md) | 云上从 0 训练 |
| `training/README.md` | 训练脚本入口 |

---

*参考配置：D16T200 示例整机 · i5-12400F · 16G 3200 · 512G NVMe · RTX 3050 6G。实际路数与训练耗时请以本机压测为准。*
