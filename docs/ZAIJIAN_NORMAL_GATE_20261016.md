# 「再见」样本 + AI normal 抑制静帧（2026-10-16）

## 样本

- 生产 173、176、177 的 SSH 是 **端口 22**。2026-10-16 用 `ssh -p 22` / `scp` 只读拉取了 `freeze_*.jpg`。
- 拉取临时目录：`D:\ai_monitor_code\_zaijian_pull_20261016\from_{173,176,177}\`
  - 173：拉到 **58** 张静帧，筛出蓝底「再见」**3** 张（夜间 0–2 点，目视确认）
  - 176：拉到 **82** 张，筛出 **6** 张
  - 177：拉到 **138** 张，筛出 **34** 张
  - 新确认 **43** 张。正午蓝底、普通静帧已排除。与原有 9 张按 MD5 去重后写入样本目录。
- 本机：`D:\ai_monitor_code\dataset_zaijian\` → **train/normal 41** 张，**val/normal 11** 张（共 **52**）。
- GPU 盘：`Z:\ai_monitor_code-main\dataset_frame\{train,val}\normal\zaijian_*` 已是同样的 41 + 11。
- 画面是蓝底抽象背景加竖排「再见」，算 **normal**。不要放进 `blue_screen`。
- 频道：cctv_hd_115（证券资讯）、cctv10 高清/标清、cctv11 标清、cctv12 高清/标清、cctv14 高清、cctv15 标清、cctv17、cctv7、风云剧场、风云音乐、怀旧剧场。

## 监测端门控（已在三台运行）

2026-10-19 核对过：173、176、177 上的 `workers/monitor_worker.py` 和仓库里这份一致，GitHub `cb222bd` 已包含这段逻辑。三台 `channels.yaml` 没有写 `ai_suppress_*`，用的是代码默认：

| 项 | 默认 |
|----|------|
| `ai_suppress_freeze` | 开 |
| `ai_suppress_silence` | 开 |
| `ai_suppress_min_score` | 0.7 |
| `ai_suppress_max_age_sec` | 30 秒 |

规则要落静帧或无伴音之前，若最近一次 AI 最高类是 `normal`、分数不低于 0.7、而且结果没超过 30 秒，就抑制，并打 info 日志「因 AI normal 抑制…」。模型没有结果、分数不够或判成故障时，照常报。

注释在 `config/channels.yaml.example`。本机备份 `workers/monitor_worker.py.bak_20261016_090018` 等不要提交。

## 还没做：用这些图重新训练

现有模型没有用这 52 张再训过，新的 onnx 也没有换上三台。现在的抑制靠当前模型把画面认成 `normal`。

在有显卡的训练机上做，不要在监测机上跑。`runs\frame1` 不要覆盖。

```bat
cd /d Z:\ai_monitor_code-main\training
python train.py --data Z:\ai_monitor_code-main\dataset_frame --out Z:\ai_monitor_code-main\training\runs\frame_zaijian1
python export_onnx.py --ckpt Z:\ai_monitor_code-main\training\runs\frame_zaijian1\best.pt --out Z:\ai_monitor_code-main\training\runs\frame_zaijian1\mosaic_detector.onnx
```

换上之前：`mosaic_detector.onnx` 和 `mosaic_detector.onnx.classes.json` 一起拷到该台 `models/`，再 `systemctl restart ai-monitor`。先看收播时段：「再见」应少报或不报静帧；真静帧、以及模型判成故障的画面，仍应上报。
