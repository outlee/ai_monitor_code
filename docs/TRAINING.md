# AI 模型训练与导入指南（详细版）

本文说明：**如何在有 GPU 的机器 / 云主机上训练马赛克·花屏检测模型**，导出为 **ONNX**，并导入到 **本监测系统（多为 CentOS 纯 CPU）** 使用。

> **分工原则（与系统设计一致）**  
> - **训练**：在外部 GPU / 云电脑（如 **AutoDL**）完成。  
> - **推理**：监测机只做加载 ONNX + 抽帧推理（`workers/ai_detector.py`）。  
> - **无模型也能先用**：`ai.mode: heuristic` 启发式，无需训练。

相关文档：

| 文档 | 内容 |
|------|------|
| **[TRAINING_AUTODL.md](TRAINING_AUTODL.md)** | **AutoDL 从 0 开机到导出 ONNX（手把手）** |
| **[LOCAL_DEPLOY_TRAIN.md](LOCAL_DEPLOY_TRAIN.md)** | **本机仅训练**（参考 i5-12400F / 16G / RTX 3050 6G，不做监测） |
| [DEPLOY.md](DEPLOY.md) | 监测服务器部署（含 AI 依赖安装） |
| [models/README.md](../models/README.md) | 模型目录与输入输出约定摘要 |
| [../training/](../training/) | 可运行训练 / 导出脚本 |
| [README.md](../README.md) | 系统总览 |

---

## 1. 总体流程

```
┌──────────────────────────────────────────────────────────────────┐
│  A. 样本采集（监测现场 / 录像带）                                  │
│     正常画面 + 马赛克 + 绿屏/花屏 + 边界难例                        │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  B. 数据整理与划分（训练机）                                       │
│     dataset/train|val  ·  标注  ·  清洗  ·  增强策略                 │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  C. 训练（GPU / 云）                                               │
│     二分类或轻量 backbone  ·  验证集调参  ·  记录最佳 checkpoint     │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  D. 导出 ONNX（严格对齐监测端预处理）                               │
│     1×3×224×224 float32 RGB /255  ·  输出异常分或二类概率           │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  E. 离线对齐测试（训练机或跳板机）                                   │
│     onnxruntime 本地跑  ·  与训练精度对比  ·  扫 threshold          │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  F. 导入监测设备                                                   │
│     scp → models/*.onnx  ·  改 channels.yaml  ·  热重载/重启        │
│     test_ai_offline.py  ·  小流量试点  ·  全量推广                   │
└──────────────────────────────────────────────────────────────────┘
```

**时间与角色建议**

| 阶段 | 建议环境 | 负责人 |
|------|----------|--------|
| 采样本 | 机房 / 收录系统 | 播出运维 |
| 标注与训练 | 有 GPU 的工作站或云 | 算法 / 开发 |
| 导入与标定 | 监测 CentOS 机 | 运维 + 开发 |
| 上线 | 监测机 | 运维 |

---

## 2. 监测端推理约定（训练必须对齐）

监测代码路径：`workers/ai_detector.py` → `_infer_onnx`。  
**训练时的预处理、导出图结构必须与此一致**，否则线上分数不可用。

### 2.1 输入

| 项 | 约定 |
|----|------|
| 类型 | `float32` |
| 形状 | **`1 × 3 × 224 × 224`**（NCHW） |
| 颜色 | **RGB**（注意：OpenCV 读图默认 BGR，训练与导出前须转 RGB） |
| 缩放 | 短边/直接 resize 到 **224×224**（与线上一致：直接 `resize(224,224)`，无 letterbox） |
| 归一化 | **像素 / 255.0**，得到约 \[0, 1\] |
| **不做** | ImageNet mean/std 归一化（除非你同时改监测端代码） |

线上等价伪代码：

```python
# 与 ai_detector._infer_onnx 一致
img = cv2.imread(path)                 # BGR
img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
img = cv2.resize(img, (224, 224))
x = img.astype("float32") / 255.0
x = x.transpose(2, 0, 1)[None, ...]    # 1x3x224x224
session.run(None, {input_name: x})
```

### 2.2 输出（二选一）

监测端兼容两种：

| 形式 | 形状示例 | 监测端取分方式 |
|------|----------|----------------|
| **A. 二类概率** | `1×2` | `score = out[0][1]`（第 1 维为**异常**概率） |
| **B. 单一异常分** | `1×1` 或任意可 ravel | `score = float(out.ravel()[0])` |

判定：

```text
is_anomaly = (score >= ai.threshold)   # 默认 threshold=0.55
label      = "mosaic" if is_anomaly else "normal"
```

> 当前线上标签较粗，统一记为 `ai_mosaic` 一类异常（事件类型 `ai_` + label）。  
> 若你训练了多类（马赛克 / 绿屏），导出时仍可输出异常总概率，或后续扩展监测端多类解析。

### 2.3 推荐：二分类 softmax

- 类别 0：`normal`（正常）  
- 类别 1：`anomaly`（马赛克 / 花屏 / 绿屏等**画面损坏**统称）  

导出 ONNX 时保留 **softmax 后的 1×2**，便于用阈值扫 PR 曲线。

### 2.4 文件名与路径

| 项 | 建议 |
|----|------|
| 默认文件名 | `models/mosaic_detector.onnx` |
| 配置项 | `ai.model_path: models/mosaic_detector.onnx`（相对工作目录） |
| Opset | 建议 **11～17**，便于旧版 `onnxruntime` |
| 执行 | 监测端默认 **CPUExecutionProvider** |

---

## 3. 样本采集（最关键）

### 3.1 目标现象（与业务一致）

| 类别 | 画面特征 | 来源建议 |
|------|----------|----------|
| **正常** | 正常播出、台标、字幕、运动镜头 | 多频道、多时段 |
| **马赛克** | 块状、宏块、严重量化损坏 | 故障录像、模拟降码率 |
| **绿屏/花屏** | 解码错误绿、花屏、严重条带 | 故障录像、坏 TS |
| **难例（强烈建议）** | 绿草地正常镜头、几何纹理墙、大色块包装、静帧非故障 | 降低误报 |

规则检测已覆盖 **黑场 / 静帧 / 静音**，AI 训练样本应**聚焦画面内容损坏**，不要把大量「纯黑场」当唯一正样本（黑场规则已会抓）。

### 3.2 从监测系统顺带收集

监测跑起来后，旁路与截图可用：

| 路径 | 用途 |
|------|------|
| `snapshots/<频道ID>/latest.jpg` | 近似当前画面（正常时段可定时拷） |
| `snapshots/<频道ID>/black_*.jpg` 等 | 规则告警瞬间，需人工甄别是否适合当 AI 样本 |
| 收录系统 / 探针录像 | 完整故障时段，质量最高 |

**采集建议**

1. 每个目标频道至少覆盖：**白天 / 晚间 / 广告 / 片头片尾**。  
2. 正常样本总量建议 **≥ 异常样本的 3～10 倍**（类别不平衡时用加权损失或重采样）。  
3. 分辨率：原始组播分辨率即可，训练时再统一 resize 到 224。  
4. 同一故障连续帧：可抽 **每秒 1 帧**，避免 25 帧几乎重复导致过拟合。

### 3.3 主动制造异常样本（实验室）

在无足够真实故障时，可用工具近似（**真实故障仍优先**）：

| 手段 | 近似目标 |
|------|----------|
| FFmpeg 极低码率 / 强 quant | 块状马赛克感 |
| 截断 TS、破坏 PES | 花屏、绿块 |
| 后期加马赛克滤镜 | 仅作补充，勿占异常集主导 |

```bash
# 示例：从录像抽帧（训练机）
ffmpeg -i fault.ts -vf fps=1 -q:v 2 frames/fault_%05d.jpg

# 示例：正常节目抽帧
ffmpeg -i normal.ts -vf fps=1/2 -q:v 2 frames/normal_%05d.jpg
```

### 3.4 数据量经验（起步）

| 阶段 | 正常 | 异常 | 说明 |
|------|------|------|------|
| 最小可训 | 2000+ | 500+ | 仅验证 pipeline |
| 可用试点 | 1 万+ | 2000+ | 单台/少频道 |
| 较稳 | 数万 | 异常覆盖多形态 | 多频道推广前 |

质量远比「只堆正常帧」重要：异常类要 **多种故障形态**，正常类要 ** greenery / 运动 / 字幕** 等易误报场景。

---

## 4. 数据目录与标注

### 4.1 推荐目录结构（训练机）

```text
dataset/
├── train/
│   ├── normal/          # 标签 0
│   │   ├── xxx.jpg
│   │   └── ...
│   └── anomaly/         # 标签 1（马赛克+花屏+绿屏可先合并）
│       ├── yyy.jpg
│       └── ...
├── val/
│   ├── normal/
│   └── anomaly/
└── test/                # 可选，最终报告用；勿参与调参
    ├── normal/
    └── anomaly/
```

划分比例建议：`train : val : test ≈ 8 : 1 : 1`，**按时间或按节目切分**，避免同一镜头同时出现在 train 和 val。

### 4.2 多类（可选扩展）

若希望区分类型，可先在标注层保留细类，训练时映射：

```text
normal      → 0
mosaic      → 1
green_screen→ 1   # 或单独类 2，导出时再合成 anomaly 概率
```

监测端当前把异常统一当 `mosaic` 标签展示；多类展示需改 `ai_detector.py`（后续需求）。

### 4.3 标注规范（给标注同事）

1. **正常**：人眼认为可播、无解码花屏/马赛克。  
2. **异常**：人眼明显损坏，影响收看。  
3. **存疑**：单独文件夹 `review/`，不进 train，直到复核。  
4. 截图模糊、黑场、纯彩条：单独策略（黑场靠规则；彩条可标 normal 或排除）。  
5. 文件名不含中文空格亦可，但路径尽量 ASCII，减少工具链问题。

### 4.4 清单文件（可选）

```text
# labels_train.csv
path,label
train/normal/a.jpg,0
train/anomaly/b.jpg,1
```

便于复现实验与排查坏图。

---

## 5. 训练环境（GPU / 云）

### 5.1 硬件建议

| 资源 | 最低 | 推荐 |
|------|------|------|
| GPU | 6GB 显存 | 8GB+（RTX 3060 / T4 / A10 等） |
| 内存 | 16GB | 32GB |
| 系统 | Ubuntu 20.04/22.04 常见 | 任意能装 CUDA 的 Linux |
| 磁盘 | 50GB+ | 视样本量 |

云主机：阿里云 / 腾讯云 / AutoDL / AWS 等选 **带 GPU 的镜像**，装好驱动与 CUDA 后按框架文档安装 PyTorch/TensorFlow。

### 5.2 软件栈示例（PyTorch）

```bash
# 示例：按官网选择对应 CUDA 的 torch 版本
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install onnx onnxruntime opencv-python-headless numpy pillow tqdm
```

监测机 **不需要** 安装 torch，只需 `onnxruntime`（见 DEPLOY.md）。

### 5.3 模型选择建议

| 模型 | 特点 | 适用 |
|------|------|------|
| MobileNetV2/V3 | 小、CPU 推理快 | **优先推荐** 监测机 CPU |
| EfficientNet-B0 | 精度/速度均衡 | 样本多时 |
| ResNet18 | 简单好训 | 教学与基线 |
| 更大模型 | 精度高、CPU 慢 | 路数少或有 GPU 监测机 |

输入统一改成 **224×224**，分类头 **2 类**。

### 5.4 训练要点（实现由你方脚本完成）

本仓库不附带训练脚本，实现时请保证：

1. **预处理与第 2 节完全一致**（RGB、/255、无 ImageNet normalize，除非改监测代码）。  
2. 使用 **ImageFolder** 或 CSV 读 `normal/anomaly`。  
3. `CrossEntropyLoss`；类别不平衡时用 `weight` 或 focal loss。  
4. 数据增强（仅 train）：轻量随机翻转、轻微亮度对比度、小角度旋转；**不要**增强到看不出故障。  
5. 验证集每个 epoch 看 **Accuracy / F1 / 异常类 Recall**。  
6. 保存 `best.pt`（验证集最优），不要只留最后一个 epoch。  
7. 记录超参：`lr`、`batch`、`epoch`、seed、数据版本日期。

**参考超参起点（需按数据改）**

| 项 | 起点 |
|----|------|
| batch size | 32～128（视显存） |
| lr | 1e-3～3e-4（AdamW） |
| epochs | 20～50 + early stopping |
| 输入 | 224×224 |

---

## 6. 导出 ONNX（关键步骤）

以下为 **PyTorch 思路示例**（需在你自己的训练工程里执行，路径按实际改）。

### 6.1 导出

```python
import torch
import torch.nn as nn

# model = ...  # 已 load best.pt，eval 模式
model.eval()

dummy = torch.randn(1, 3, 224, 224, dtype=torch.float32)

torch.onnx.export(
    model,
    dummy,
    "mosaic_detector.onnx",
    input_names=["input"],
    output_names=["output"],
    opset_version=13,
    dynamo=False,  # 视 torch 版本；以能被 onnxruntime 加载为准
    # 动态 batch 可选：
    # dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
)

print("exported mosaic_detector.onnx")
```

导出后建议：

```bash
python -c "import onnx; onnx.checker.check_model(onnx.load('mosaic_detector.onnx'))"
```

### 6.2 输出必须是「异常分」语义

- 若网络最后是 `Linear → 2 logits`：导出前加 `Softmax(dim=1)`，保证 `output[0,1]` 为异常概率。  
- 或导出 logits，但监测端当前 **不做 softmax**，会直接把第二维当 score——**请在导出图内完成 softmax**，或改为单输出 sigmoid 概率。

**推荐导出计算图末端：**

```text
... → logits(1,2) → Softmax → output(1,2)
```

### 6.3 在训练机用 onnxruntime 对齐

```python
import numpy as np
import cv2
import onnxruntime as ort

def preprocess(path):
    img = cv2.imread(path)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = cv2.resize(img, (224, 224))
    x = img.astype(np.float32) / 255.0
    x = x.transpose(2, 0, 1)[None, ...]
    return x

sess = ort.InferenceSession("mosaic_detector.onnx", providers=["CPUExecutionProvider"])
inp = sess.get_inputs()[0].name
x = preprocess("some.jpg")
out = sess.run(None, {inp: x})[0]
print(out.shape, out)
# 期望 (1,2)，且 out[0,1] 为异常概率
```

用 **同一张图** 对比：

1. PyTorch `model` 输出  
2. ONNX Runtime 输出  

误差应在 1e-4～1e-5 量级；差很多则预处理或导出图不一致。

### 6.4 阈值初选（在验证集 / 测试集上）

对每张图算 `score = out[0,1]`，扫描 threshold：

| threshold | 效果倾向 |
|-----------|----------|
| 偏低（如 0.3） | 召回高、误报多 |
| **默认 0.55** | 监测配置起点 |
| 偏高（如 0.7） | 误报少、漏报多 |

选出验证集上 F1 或「业务可接受误报率」对应的阈值，写入监测配置 `ai.threshold`。

---

## 7. 导入监测设备

### 7.1 拷贝模型

```bash
# 在训练机或运维机
scp mosaic_detector.onnx user@监测机IP:/opt/ai_monitor_code/models/

# 监测机上确认
ls -lh /opt/ai_monitor_code/models/mosaic_detector.onnx
```

### 7.2 安装推理依赖（若尚未安装）

详见 [DEPLOY.md 第 9 节](DEPLOY.md)。摘要：

```bash
cd /opt/ai_monitor_code
pip3 install 'onnxruntime>=1.16.0' 'opencv-python-headless>=4.8.0' 'numpy>=1.24.0' 'Pillow>=9.0.0'
```

### 7.3 修改配置

编辑 `config/channels.yaml`（或 Web「功能开关」）：

```yaml
ai:
  enabled: true
  mode: auto              # 有模型优先 ONNX；无模型可回落 heuristic
  # mode: onnx            # 强制仅 ONNX，失败则 AI 不可用
  model_path: models/mosaic_detector.onnx
  interval_sec: 2.0       # 每路分析间隔，越大越省 CPU
  threshold: 0.55         # 与训练机扫出的阈值对齐
  green_ratio_th: 0.35    # 仅 heuristic 用
  block_score_th: 0.12
```

`defaults` 建议：

```yaml
defaults:
  frame_interval_sec: 2.0   # 旁路 latest.jpg，建议与 interval_sec 同量级
  latest_max_age_sec: 5.0
  ai_async: true            # AI 独立线程
```

保存后 **热重载约 3 秒**生效（Manager 未加 `--no-reload` 时）。

### 7.4 监测机离线验证

```bash
cd /opt/ai_monitor_code

# 将一张已知异常图拷到服务器后：
python3 test_ai_offline.py --enable --mode onnx --image /path/to/fault.jpg

# 或视频抽帧
python3 test_ai_offline.py --enable --mode auto --video /path/to/clip.ts --frames 8
```

期望：`backend` 为 `onnx`，异常图 `is_anomaly` 多为真，正常图多为假。

也可直接：

```python
from workers.ai_detector import AIDetector
d = AIDetector({
    "enabled": True,
    "mode": "onnx",
    "model_path": "models/mosaic_detector.onnx",
    "threshold": 0.55,
}, work_dir=".")
print(d.status())
print(d.analyze_image("snapshots/xxx/latest.jpg"))
```

### 7.5 在线确认

```bash
grep -i "AI 状态" logs/某频道.log | tail -5
# 期望：enabled=True, available=True, backend=onnx

grep 'ai_' logs/events.jsonl | tail -20
```

Web 总览中 AI 模块显示开启；出现异常时事件类型形如 `ai_mosaic`。

### 7.6 上线策略（强烈建议）

1. **1～3 路** 打开 AI，观察 1～3 天误报/漏报。  
2. 根据真实误报调 `threshold` 或补充难例再训一版。  
3. 再扩到更多频道。  
4. 保留 `mode: auto`：模型文件损坏时有机会回落 heuristic（视依赖是否安装）。  
5. 模型版本管理：文件名带版本，如 `mosaic_detector_v20260401.onnx`，配置改 `model_path`，旧文件保留可回滚。

---

## 8. 与启发式（heuristic）的关系

| 项目 | heuristic | ONNX |
|------|-----------|------|
| 训练 | 不需要 | 需要 |
| 依赖 | OpenCV | onnxruntime + 解码库 |
| 能力 | 明显绿屏、粗块状 | 取决于数据与模型 |
| 适用 | 流程验证、无模型应急 | 正式提升检出 |

建议路径：

```text
规则稳定 → heuristic 试点流程 → 采集真实样本 → GPU 训练 → ONNX 导入 → 阈值标定 → 扩容
```

---

## 9. 版本与变更管理

建议维护一张表（可放在运维 wiki）：

| 字段 | 示例 |
|------|------|
| 模型文件 | mosaic_detector_v3.onnx |
| 训练数据日期 | 2026-04-01 |
| 正常/异常张数 | 20k / 4k |
| 验证集 Recall@0.55 | 0.91 |
| 验证集 误报率 | 0.03 |
| 导出 opset | 13 |
| 上线 threshold | 0.58 |
| 负责人 | 张三 |
| 回滚文件 | mosaic_detector_v2.onnx |

监测机升级模型：

```bash
cp models/mosaic_detector_v3.onnx models/mosaic_detector.onnx
# 或改 yaml 中 model_path 指向 v3
# 热重载后检查 backend=onnx
```

---

## 10. 常见问题（训练 ↔ 监测）

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 训练准、线上全乱 | 预处理不一致（BGR/RGB、normalize） | 严格按第 2 节；ORT 与 PyTorch 对拍 |
| 线上 score 恒为 0/1 | 导出了 logits 未 softmax；或类别维反了 | 导出 softmax；确认 `out[0][1]` 是异常类 |
| `available=False` | 未装 ort、路径错、模型坏 | pip、路径、`onnx.checker` |
| mode=onnx 不工作 | 无模型文件 | 改 auto 或放对文件 |
| CPU 打满 | interval 太小、路数太多、模型太大 | 加大 `interval_sec`；换 MobileNet；减路数 |
| 误报草地/运动 | 正常集缺少该类 | 补难例重训 |
| 漏报轻微马赛克 | 异常集过「重」、阈值过高 | 补轻症样本；降 threshold |
| 热重载后仍旧模型 | 进程未吃到新文件、路径缓存 | 看日志是否重启线程；确认 mtime；必要时 restart 服务 |

---

## 11. 检查清单（交付给监测机前）

训练机侧：

- [ ] 数据按 normal/anomaly 划分，val 未泄漏  
- [ ] 预处理 = RGB + resize 224 + /255，**无** ImageNet mean/std（除非已改监测代码）  
- [ ] best 模型验证指标达标并记录  
- [ ] 已导出 ONNX，`onnx.checker` 通过  
- [ ] ORT 与 PyTorch 同图输出一致  
- [ ] 已扫描 threshold，并写出推荐值  
- [ ] 模型文件命名含版本，说明文档齐  

监测机侧：

- [ ] `models/` 下文件权限可读  
- [ ] onnxruntime / opencv 等已装  
- [ ] `ai.enabled: true`，`mode: auto|onnx`，`threshold` 已填  
- [ ] `test_ai_offline.py` 结果符合预期  
- [ ] 在线日志 `backend=onnx`  
- [ ] 小流量试点无异常资源占用  

---

## 12. 本仓库边界说明

| 包含 | 不包含 |
|------|--------|
| ONNX / 启发式 **推理** | AutoDL 账号与充值 |
| `training/` 二分类训练与导出脚本 | 全自动标注平台 |
| 抽帧、告警、Web、部署 | 云厂商计费纠纷处理 |
| 输入输出 **契约** + [AutoDL 教程](TRAINING_AUTODL.md) | |

在 AutoDL 上的逐步操作请直接打开 **[TRAINING_AUTODL.md](TRAINING_AUTODL.md)**。

---

## 13. 快速对照：从训练到上线的最少命令

```bash
# ========== 训练机（示意）==========
# 1. 整理 dataset/train/{normal,anomaly} 与 val
# 2. 运行你们的 train.py → best.pt
# 3. export_onnx.py → mosaic_detector.onnx
# 4. 本地 ORT 验证 + 选 threshold

scp mosaic_detector.onnx user@monitor:/opt/ai_monitor_code/models/

# ========== 监测机 ==========
cd /opt/ai_monitor_code
pip3 install onnxruntime opencv-python-headless numpy Pillow   # 若未装

# 编辑 config/channels.yaml：
#   ai.enabled: true
#   ai.mode: auto
#   ai.model_path: models/mosaic_detector.onnx
#   ai.threshold: <训练机选定值>

python3 test_ai_offline.py --enable --mode onnx --image /path/to/test.jpg
# 确认 Manager 热重载或：systemctl restart ai-monitor
grep "AI 状态" logs/*.log | tail
```

---

*文档与当前推理实现同步：`workers/ai_detector.py`（1×3×224×224，RGB，/255，输出 1×2 取下标 1 或标量异常分，threshold 默认 0.55）。若修改监测端预处理，请同步修订本文第 2 节。*
