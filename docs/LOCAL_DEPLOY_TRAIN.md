# 本地训练说明（仅训练机）

本文面向 **只做 AI 训练、不做节目监测** 的本机（工作站）。

目标：在本机准备图片数据 → 训练二分类模型 → 导出 ONNX → 把模型交给 **监测服务器** 使用。

监测部署、Web 大屏等请看 [DEPLOY.md](DEPLOY.md)，与本机无关。  
云训练（AutoDL）见 [TRAINING_AUTODL.md](TRAINING_AUTODL.md)。  
样本与 ONNX 约定见 [TRAINING.md](TRAINING.md)。

---

## 1. 参考配置（本文基准）

本机 **只承担训练**，不跑组播监测、不起 Manager/Web。

| 项目 | 参考规格 | 对训练的意义 |
|------|----------|--------------|
| 机型/整机 | D16T200（示例） | 按实际品牌即可 |
| CPU | Intel Core **i5-12400F** | 数据加载、预处理够用 |
| 内存 | **16GB** DDR4-3200 | 中小规模样本够用；训大集时少开其它占内存程序 |
| 系统盘 | **512GB NVMe** | 系统 + 环境 + 样本 + 实验输出；大样本建议外置盘 |
| 显卡 | **NVIDIA RTX 3050 6GB** | 训练主力，注意 batch 别撑爆显存 |

**能力预期（经验值）：**

| 项 | 说明 |
|----|------|
| 模型 | 仓库 `training/` 默认 MobileNet 级二分类，适合 6G 显存 |
| batch-size | 建议 **16 或 32**；OOM 则降到 8 |
| 样本规模 | 数万张以内较舒适；更大时注意磁盘与时间 |
| 产出 | `best.pt` + `mosaic_detector.onnx` + 推荐 threshold |

训完后：**只把 `.onnx`（及阈值说明）拷到监测设备**，本机不必装监测服务。

---

## 2. 你需要提前准备什么

### 2.1 图片数据（必须）

训练 **只用图片**（jpg/png 等），不要直接把整段 mp4/ts 丢进训练脚本。  
视频请先抽帧，再人工分成「正常 / 异常」。

**目录必须长这样：**

```text
dataset/
  train/              ← 用来学（约占 80%～90% 的图）
    normal/           正常可播画面
    anomaly/          马赛克、花屏、绿屏等损坏画面
  val/                ← 用来考、选最好模型（约占 10%～20%）
    normal/
    anomaly/
```

| 目录 | 放什么 |
|------|--------|
| **train** | 大部分正常图 + 大部分异常图，给模型学习、改参数 |
| **val** | 少量正常图 + 少量异常图，训练过程中打分，**不参与改参数** |

注意：

- train、val **两边都要有** normal 和 anomaly，不是「train 只放正常、val 只放异常」。  
- **同一张图不要既在 train 又在 val**。  
- 同一段故障录像抽的帧，尽量整段只进 train 或只进 val（避免泄题、验证虚高）。  
- 黑场/静帧主要靠监测机规则检测，训练集重点放 **花屏/马赛克/绿屏**；正常集尽量多样（多场景，含易误报画面）。

更细的采集规范见 [TRAINING.md](TRAINING.md)。

### 2.2 软件环境

| 软件 | 用途 |
|------|------|
| Python 3.8+（推荐 3.10/3.11） | 跑训练脚本 |
| NVIDIA 显卡驱动 | `nvidia-smi` 能看到 RTX 3050 |
| 与驱动匹配的 **CUDA 版 PyTorch** | 用 GPU 训练 |
| Git（可选） | 拉取本仓库 `training/` |
| FFmpeg（可选） | 仅当你要从视频抽帧时需要 |

**本机不必安装：** 监测用的 Manager、FFmpeg 常驻收流、FastAPI Web 面板（那是监测服务器的事）。

### 2.3 代码

从 GitHub 获取本仓库即可，训练只用其中的：

```text
training/
  train.py
  export_onnx.py
  scan_threshold.py
  make_demo_dataset.py   # 假数据冒烟，不能当正式模型
  requirements.txt
  README.md
```

仓库地址示例：`https://github.com/outlee/ai_monitor_code`

---

## 3. 环境安装（训练机）

1. 安装较新的 NVIDIA 驱动，命令行执行 **`nvidia-smi`**，确认有 **RTX 3050**。  
2. 建议单独建虚拟环境（只服务训练，干净好维护）。  
3. 按 [PyTorch 官网](https://pytorch.org) 选择与本机 CUDA 匹配的安装方式，装 **GPU 版 torch**。  
4. 进入 `training/`，安装其余依赖（onnx、opencv、numpy、pillow、tqdm 等；以 `requirements.txt` 为准，torch 以官网为准避免装成 CPU 版）。  
5. 确认：`torch.cuda.is_available()` 为真。

磁盘建议：

- 样本与 `runs/` 输出放在空间大的盘  
- 系统盘留足余量（建议至少几十 GB 空闲）

---

## 4. 训练流程（本机）

### 4.1 数据放好

把整理好的 `dataset/` 放到本机任意路径，例如：

- `D:\datasets\mosaic_dataset`  
- 或 `E:\ai_data\dataset`  

保证其下已有 `train/normal`、`train/anomaly`、`val/normal`、`val/anomaly`。

### 4.2 开始训练

在 `training/` 目录运行训练脚本，指定：

- 数据根目录 → 你的 `dataset`  
- 输出目录 → 如 `runs/exp1`  
- **batch-size：16 或 32**（3050 6G；爆显存就降到 8）  
- epochs：小试 10～20，正式 20～40  
- workers：Windows 上常用 2～4  

训练过程会：

- 用 **train** 更新模型  
- 用 **val** 算准确率 / 异常类 F1  
- 按验证集表现保存 **`best.pt`**

具体参数名见 `training/README.md`（本地路径换成你的盘符即可）。

### 4.3 导出 ONNX

用 `best.pt` 运行导出脚本，得到例如 **`mosaic_detector.onnx`**。

导出约定与监测端一致（输入 224×RGB÷255，输出两类概率），详见 [TRAINING.md](TRAINING.md) 第 2 节。  
可用一张异常图做导出后的快速检查。

### 4.4 扫描推荐阈值

在 **val** 集上跑阈值扫描脚本，得到建议的 `threshold`（如 0.55、0.60）。  
把该数值和模型一起交给监测侧配置 `ai.threshold`。

### 4.5 没有真实图时

可用 `make_demo_dataset.py` 生成假数据 **只验证流程能跑通**，**不能**用于上线。

---

## 5. 训完之后做什么（交给监测设备）

本机训练机 **到此结束**，不负责收流监测。

交给监测服务器的人/流程：

| 交付物 | 说明 |
|--------|------|
| `mosaic_detector.onnx`（建议带版本日期命名） | 拷到监测机 `models/` |
| 推荐 `threshold` | 写入监测机配置 |
| 简要说明 | 数据日期、大致张数、val 指标（可选） |

监测机侧：打开 AI、指向模型路径、热重载或重启 Worker——见 [DEPLOY.md](DEPLOY.md)，**不在本机操作**。

---

## 6. 针对本参考配置的注意点

| 点 | 建议 |
|----|------|
| 显存 6GB | batch 从 16/32 试起；OOM 就减半 |
| 内存 16GB | 训练时少开浏览器多标签、其它占内存软件 |
| 512G 盘 | 大样本及时清理旧 `runs/`；重要结果拷走备份 |
| 散热 | 满载训练注意机箱通风，避免降频 |
| 与监测分离 | 本机不装组播、不起 7×24 监测，专心训完拷模型 |

---

## 7. 检查清单（训练机）

- [ ] `nvidia-smi` 正常，能看到 3050  
- [ ] PyTorch CUDA 可用  
- [ ] `dataset` 已按 train/val × normal/anomaly 放好，无交叉混图  
- [ ] 训练完成且生成 `best.pt`  
- [ ] 已导出 onnx，checker / 抽检图正常  
- [ ] 已记录推荐 threshold  
- [ ] 模型已备份并交付监测侧（本机可不保留监测环境）  

---

## 8. 相关文档

| 文档 | 何时看 |
|------|--------|
| **本文** | 本机只训练 |
| [TRAINING.md](TRAINING.md) | 样本规范、ONNX 契约、导入监测的约定 |
| [TRAINING_AUTODL.md](TRAINING_AUTODL.md) | 改用云 GPU 时 |
| [DEPLOY.md](DEPLOY.md) | **监测服务器**部署（非本机） |
| `training/README.md` | 脚本入口说明 |

---

*参考训练机配置：D16T200 示例 · i5-12400F · 16G 3200 · 512G NVMe · RTX 3050 6G。本机不做监测。*
