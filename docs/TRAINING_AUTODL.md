# 基于 AutoDL 从 0 训练马赛克/花屏模型（详细教程）

本文面向 **从未用过 AutoDL** 的同事，从注册开机到导出 ONNX、拷回监测机，一步步做完。

配套代码在本仓库：

```text
training/
  requirements.txt      # 训练机依赖
  train.py              # 训练
  export_onnx.py        # 导出 ONNX
  scan_threshold.py     # 扫阈值
  make_demo_dataset.py  # 假数据冒烟（勿当正式模型）
```

通用训练规范见 [TRAINING.md](TRAINING.md)；监测机部署见 [DEPLOY.md](DEPLOY.md)。

---

## 0. 你将得到什么

| 产物 | 说明 |
|------|------|
| `best.pt` | 验证集最优 PyTorch 权重 |
| `mosaic_detector.onnx` | 给监测系统用的模型 |
| 推荐 `threshold` | 写入 `config/channels.yaml` 的 `ai.threshold` |

监测端约定（脚本已对齐）：

- 输入：`1×3×224×224`，RGB，像素 `/255`
- 输出：`1×2` softmax，`[正常概率, 异常概率]`，取 `out[0][1]`

---

## 1. 注册与充值 AutoDL

1. 打开官网： [https://www.autodl.com](https://www.autodl.com)  
2. 手机号 / 微信注册并登录。  
3. 实名认证（按网站要求，否则可能无法开机）。  
4. **费用中心** 充值少量金额（训练几小时通常几元～几十元，视 GPU 型号）。  
5. 阅读计费说明：**关机才几乎不扣算力费**；数据盘可另计费。  

> 提示：不用时务必 **关机**，不要只关网页。

---

## 2. 租用 GPU 实例（从 0 创建）

### 2.1 进入容器实例

1. 登录后点 **「容器实例」** → **「租用新实例」**（文案可能随改版微调）。  
2. **地区**：选延迟低、有空闲卡的区域（如西二、北京等，以页面为准）。  
3. **GPU**：入门推荐  
   - **RTX 3080 / 3090 / 4090** 或  
   - 云上常见 **RTX 3060 12G / T4**  
   显存 **≥ 8GB** 更从容；演示可用 6GB+。  
4. **镜像**（重要）：选带 PyTorch 的官方镜像，例如：  
   - `PyTorch 2.x` + `Python 3.10` + `CUDA 11.8/12.x`  
   名称类似：`PyTorch → 2.1.0 → 3.10(ubuntu22.04) → CUDA 12.1`  
   **不要**选纯净系统再自己装 CUDA（新手易失败）。  
5. **磁盘**：  
   - 系统盘默认即可  
   - **数据盘** 建议 ≥ 50GB（样本多就加大），路径一般是 **`/root/autodl-tmp`**  
6. 确认计费方式（按量），创建并 **开机**。

### 2.2 看到「运行中」

实例列表状态为 **运行中** 后，可以使用：

| 入口 | 用途 |
|------|------|
| **JupyterLab** | 浏览器里终端 + 上传文件，新手友好 |
| **SSH** | 本地终端登录，适合 git / scp |

本教程以 **JupyterLab 终端** 为主。

点击 **「JupyterLab」**，打开后菜单 **File → New → Terminal**，得到 Linux 命令行。

---

## 3. 第一次进入：看环境和磁盘

在 Jupyter 终端执行：

```bash
# 谁、在哪
whoami
pwd
hostname

# GPU 是否可见
nvidia-smi

# Python / 预装框架（镜像不同略有差异）
python -V
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"

# 数据盘（务必把大数据放这里，关机后通常还在）
ls -la /root/autodl-tmp
df -h
```

期望：

- `nvidia-smi` 能看到 GPU  
- `torch.cuda.is_available()` 为 `True`  

若 CUDA 为 False：多半镜像选错或驱动异常，关机换 **PyTorch+CUDA** 镜像重建实例。

---

## 4. 准备代码（本仓库 training 目录）

### 方式 A：git 克隆整个监测仓库（推荐）

```bash
cd /root/autodl-tmp
git clone https://github.com/outlee/ai_monitor_code.git
cd ai_monitor_code/training
ls
```

应看到：`train.py` `export_onnx.py` `scan_threshold.py` 等。

### 方式 B：只上传 training 文件夹

1. 本机把仓库里的 `training/` 打成 zip。  
2. JupyterLab 左侧 **上传** 到 `/root/autodl-tmp/`。  
3. 解压：

```bash
cd /root/autodl-tmp
unzip training.zip -d .
cd training   # 以实际目录为准
```

### 安装训练依赖

镜像已有 torch 时，可只补缺的包：

```bash
cd /root/autodl-tmp/ai_monitor_code/training

# 若镜像 torch 已可用，可跳过重装 torch，只装其它：
pip install onnx onnxruntime opencv-python-headless numpy pillow tqdm -i https://pypi.tuna.tsinghua.edu.cn/simple

# 或完整按文件装（可能重装 torch，耗时更长）：
# pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

验证：

```bash
python - << 'PY'
import torch, cv2, onnx
print("cuda", torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
print("cv2", cv2.__version__)
PY
```

---

## 5. 准备数据集

### 5.1 目录结构（必须一致）

```text
/root/autodl-tmp/dataset/
  train/
    normal/     # 正常画面 jpg/png
    anomaly/    # 马赛克、花屏、绿屏等
  val/
    normal/
    anomaly/
```

说明：

- 标签由**文件夹名**决定：`normal=0`，`anomaly=1`  
- 扩展名支持：`.jpg` `.jpeg` `.png` `.bmp` `.webp`  
- **真实故障样本** 请按 [TRAINING.md](TRAINING.md) 采集；不要用纯黑场代替马赛克  

### 5.2 上传真实样本

**本机整理好再上传**（示例）：

```text
# 本机
dataset/
  train/normal/...
  train/anomaly/...
  val/normal/...
  val/anomaly/...
```

打包上传：

```bash
# 本机
tar czvf dataset.tar.gz dataset
# 用 Jupyter 上传 dataset.tar.gz 到 /root/autodl-tmp/
```

AutoDL 上解压：

```bash
cd /root/autodl-tmp
tar xzvf dataset.tar.gz
find dataset -type f | head
echo "train normal:" $(find dataset/train/normal -type f | wc -l)
echo "train anomaly:" $(find dataset/train/anomaly -type f | wc -l)
echo "val normal:" $(find dataset/val/normal -type f | wc -l)
echo "val anomaly:" $(find dataset/val/anomaly -type f | wc -l)
```

也可用 **AutoDL 文件存储 / 网盘同步**（以官网当前功能为准），最终保证路径可读即可。

### 5.3 没有真实数据时：先跑通流程（演示用）

```bash
cd /root/autodl-tmp/ai_monitor_code/training
python make_demo_dataset.py --out /root/autodl-tmp/dataset_demo --n 80
```

> **警告**：`dataset_demo` 是程序生成的假图，只能验证「训练脚本能跑完」，**不能**用于上线监测。

---

## 6. 开始训练（从 0 敲命令）

### 6.1 正式训练命令

```bash
cd /root/autodl-tmp/ai_monitor_code/training

# 真实数据（改 --data 为你的路径）
python train.py \
  --data /root/autodl-tmp/dataset \
  --out /root/autodl-tmp/runs/exp1 \
  --epochs 30 \
  --batch-size 64 \
  --lr 0.0003 \
  --workers 4
```

演示数据：

```bash
python train.py \
  --data /root/autodl-tmp/dataset_demo \
  --out /root/autodl-tmp/runs/demo \
  --epochs 5 \
  --batch-size 32 \
  --workers 2
```

### 6.2 参数说明

| 参数 | 含义 | 建议 |
|------|------|------|
| `--data` | 数据集根目录 | 含 train/val |
| `--out` | 输出目录 | 放数据盘 `autodl-tmp/runs/...` |
| `--epochs` | 训练轮数 | 真实数据 20～50 |
| `--batch-size` | 批大小 | 显存不够就降到 32/16 |
| `--lr` | 学习率 | 默认 3e-4 |
| `--workers` | DataLoader 进程 | 2～8 |
| `--no-pretrained` | 不用 ImageNet 预训练 | 一般不用加 |

### 6.3 训练时屏幕上正常样子

```text
device=cuda
GPU: NVIDIA GeForce RTX ...
train=xxxx val=yyy
class counts normal=... anomaly=... weights=[...]
epoch 1/30: 100%|...| loss=0.5...
val acc=0.9... P=... R=... F1=...
  -> saved .../best.pt (best F1=...)
```

- **F1** 针对异常类；脚本按 **验证集 F1 最高** 存 `best.pt`  
- 同时写 `history.json`、`last.pt`  

### 6.4 显存不足 OOM

```bash
# 减小 batch
python train.py --data ... --out ... --batch-size 16 --workers 2
```

或换更大显存 GPU 实例。

### 6.5 中途断开

- 浏览器断了，**进程可能还在跑**（视实例与终端类型）。  
- 稳妥做法：用 `tmux` / `screen`：

```bash
tmux new -s train
# 在 tmux 里跑 python train.py ...
# 断线后：tmux attach -t train
```

---

## 7. 导出 ONNX

训练结束后：

```bash
cd /root/autodl-tmp/ai_monitor_code/training

python export_onnx.py \
  --ckpt /root/autodl-tmp/runs/exp1/best.pt \
  --out /root/autodl-tmp/runs/exp1/mosaic_detector.onnx \
  --opset 13
```

用一张异常图做快速检查（路径改成你的）：

```bash
python export_onnx.py \
  --ckpt /root/autodl-tmp/runs/exp1/best.pt \
  --out /root/autodl-tmp/runs/exp1/mosaic_detector.onnx \
  --verify /root/autodl-tmp/dataset/val/anomaly/某张图.jpg \
  --threshold 0.55
```

期望输出类似：

```text
exported: .../mosaic_detector.onnx
onnx.checker: OK
output shape=(1, 2) values=[[0.12 0.88]]
anomaly_score=0.8800 threshold=0.55 -> ANOMALY
```

---

## 8. 扫描推荐阈值

```bash
cd /root/autodl-tmp/ai_monitor_code/training

python scan_threshold.py \
  --onnx /root/autodl-tmp/runs/exp1/mosaic_detector.onnx \
  --data /root/autodl-tmp/dataset \
  --split val
```

看表格里 **F1 最高** 或 **业务可接受的 FP（误报）** 对应的 `th`，例如 `0.55` / `0.60`。  
该值写入监测机：

```yaml
ai:
  threshold: 0.55   # 换成扫描结果
```

---

## 9. 下载模型到本地 / 监测机

### 9.1 从 AutoDL 下载到自己电脑

1. JupyterLab 左侧文件树进入 `/root/autodl-tmp/runs/exp1/`  
2. 右键 `mosaic_detector.onnx` → **Download**  
3. 建议同时下载 `best.pt`、`history.json` 做备份  

或用 SSH/scp（实例详情里有 SSH 命令与端口）：

```bash
# 在你自己的电脑上（端口、地址以 AutoDL 控制台为准）
scp -P <端口> root@<区域主机>:/root/autodl-tmp/runs/exp1/mosaic_detector.onnx ./
```

### 9.2 上传到监测服务器

```bash
scp mosaic_detector.onnx user@监测机IP:/opt/ai_monitor_code/models/
```

### 9.3 监测机配置与验证

```bash
# 监测机
cd /opt/ai_monitor_code
pip3 install onnxruntime opencv-python-headless numpy Pillow   # 若未装

# 编辑 config/channels.yaml
```

```yaml
ai:
  enabled: true
  mode: auto
  model_path: models/mosaic_detector.onnx
  interval_sec: 2.0
  threshold: 0.55    # 与 scan_threshold 一致
```

```bash
python3 test_ai_offline.py --enable --mode onnx --image /path/to/test.jpg
# 日志中 backend=onnx 后，热重载或 restart Manager
```

细节见 [DEPLOY.md 第 9 节](DEPLOY.md) 与 [TRAINING.md 第 7 节](TRAINING.md)。

---

## 10. 关机与费用（务必做）

1. 确认 `mosaic_detector.onnx` / `best.pt` **已下载**。  
2. AutoDL 控制台对该实例点 **「关机」**。  
3. 长期不用可 **释放** 实例（注意：仅系统盘数据可能清空；**数据盘** 规则以官网为准，重要数据先下载）。  

未关机会持续计费。

---

## 11. 全流程命令清单（复制用）

```bash
# ===== AutoDL Jupyter 终端 =====
cd /root/autodl-tmp
git clone https://github.com/outlee/ai_monitor_code.git
cd ai_monitor_code/training
pip install onnx onnxruntime opencv-python-headless numpy pillow tqdm -i https://pypi.tuna.tsinghua.edu.cn/simple

# 上传并解压真实 dataset 到 /root/autodl-tmp/dataset 后：
python train.py \
  --data /root/autodl-tmp/dataset \
  --out /root/autodl-tmp/runs/exp1 \
  --epochs 30 --batch-size 64 --lr 0.0003

python export_onnx.py \
  --ckpt /root/autodl-tmp/runs/exp1/best.pt \
  --out /root/autodl-tmp/runs/exp1/mosaic_detector.onnx

python scan_threshold.py \
  --onnx /root/autodl-tmp/runs/exp1/mosaic_detector.onnx \
  --data /root/autodl-tmp/dataset --split val

# 下载 onnx → 监测机 models/ → 改 channels.yaml → 验证
```

---

## 12. 常见问题（AutoDL 场景）

| 问题 | 处理 |
|------|------|
| 找不到 GPU | 换 PyTorch+CUDA 镜像；`nvidia-smi` |
| pip 很慢 | 用清华源 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 数据放 `/root` 关机丢了 | 大数据放 **`/root/autodl-tmp`** |
| 训练断了 | `tmux`；或从上次重新训（当前脚本未做断点续训，可降低 epoch 重跑） |
| Jupyter 打不开 | 看实例是否运行中、浏览器插件；改用 SSH |
| 克隆 GitHub 失败 | 本机 zip 上传；或 AutoDL 网络/代理设置 |
| 导出 onnx 报 dynamo | 脚本已 `dynamo=False`；升级/固定 torch 版本 |
| 监测机 score 不对 | 确认用的是本仓库 `export_onnx.py`（带 Softmax）；预处理一致 |
| 演示数据 F1 很高 | 假数据过拟合正常，**换真实样本** |
| 费用突然变多 | 实例未关机；控制台查账单 |

---

## 13. 真实业务训练建议（AutoDL 上）

1. 先用 **少量真实数据**（每类数百张）跑 5～10 epoch，确认 pipeline。  
2. 再上 **全量数据** 30 epoch，看 val 的 Recall / 误报。  
3. 把验证集里 **误报图** 加进 `train/normal` 难例，再训一版（迭代 2～3 轮）。  
4. 模型命名带日期：`mosaic_detector_20260408.onnx`，监测机可回滚。  
5. **不要**在 AutoDL 上跑监测收流；训练完关机，推理在台内 CentOS。

---

## 14. 目录对照（AutoDL 上推荐布局）

```text
/root/autodl-tmp/
  ai_monitor_code/          # git clone
    training/
      train.py
      export_onnx.py
      ...
  dataset/                  # 真实样本
    train/normal|anomaly
    val/normal|anomaly
  runs/
    exp1/
      best.pt
      last.pt
      history.json
      mosaic_detector.onnx
```

---

## 15. 与监测系统衔接检查表

- [ ] AutoDL 上 `export_onnx.py` 成功，`onnx.checker OK`  
- [ ] `scan_threshold` 得到推荐 threshold  
- [ ] `mosaic_detector.onnx` 已离开云主机（本机或监测机有备份）  
- [ ] 监测机 `models/` 可读  
- [ ] `ai.enabled: true`，`mode: auto` 或 `onnx`  
- [ ] `test_ai_offline.py` 异常图能报异常  
- [ ] 在线日志 `backend=onnx`  
- [ ] AutoDL 实例已 **关机**  

---

*AutoDL 控制台文案、镜像名、价格可能变更，以官网为准；训练脚本与 `workers/ai_detector.py` 预处理约定绑定，改监测端预处理时请同步改 `training/train.py` 中的 `load_rgb_224`。*
