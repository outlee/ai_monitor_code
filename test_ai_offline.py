#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线测试 AI 检测模块（不依赖组播、不影响线上）

用法：
  # 默认关闭状态（应安全跳过）
  python3 test_ai_offline.py

  # 强制用启发式测马赛克样本（需已安装 opencv）
  python3 test_ai_offline.py --enable --mode heuristic --image mosaic_sample.mp4

  # 从视频抽若干帧再测
  python3 test_ai_offline.py --enable --mode heuristic --video mosaic_sample.mp4 --frames 8
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "workers"))

from ai_detector import AIDetector  # noqa: E402


def extract_frames(video: Path, out_dir: Path, n: int = 8) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    # 均匀抽 n 帧
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"fps=1/{max(1, 28 // n)}",
        "-frames:v", str(n),
        str(out_dir / "frame_%03d.jpg"),
    ]
    subprocess.run(cmd, check=False)
    return sorted(out_dir.glob("frame_*.jpg"))


def main():
    parser = argparse.ArgumentParser(description="离线测试 AI 检测")
    parser.add_argument("--enable", action="store_true", help="临时启用 AI")
    parser.add_argument("--mode", default="auto", choices=["auto", "onnx", "heuristic"])
    parser.add_argument("--image", help="单张图片路径")
    parser.add_argument("--video", help="视频路径，将抽帧测试")
    parser.add_argument("--frames", type=int, default=8)
    args = parser.parse_args()

    ai_cfg = {
        "enabled": args.enable,
        "mode": args.mode,
        "model_path": "models/mosaic_detector.onnx",
        "green_ratio_th": 0.30,
        "block_score_th": 0.10,
        "threshold": 0.55,
    }

    detector = AIDetector(ai_cfg, work_dir=str(ROOT))
    print("AI 状态:", detector.status())
    print("-" * 50)

    images = []
    if args.image:
        images = [Path(args.image)]
    elif args.video:
        tmp = ROOT / "snapshots" / "_ai_test_frames"
        images = extract_frames(Path(args.video), tmp, args.frames)
        print(f"从视频抽取 {len(images)} 帧")
    else:
        # 默认试一下 mosaic 抽帧（若存在）
        sample = ROOT / "mosaic_sample.mp4"
        if sample.is_file() and args.enable:
            tmp = ROOT / "snapshots" / "_ai_test_frames"
            images = extract_frames(sample, tmp, args.frames)
            print(f"默认使用 mosaic_sample.mp4，抽取 {len(images)} 帧")
        else:
            print("未指定 --image/--video，且 AI 未启用。")
            print("示例: python3 test_ai_offline.py --enable --mode heuristic --video mosaic_sample.mp4")
            return

    for img in images:
        r = detector.analyze_image(str(img))
        flag = "⚠ 异常" if r.get("is_anomaly") else "  正常"
        print(f"{flag} | {img.name} | {r.get('label')} | score={r.get('score')} | {r.get('message')}")


if __name__ == "__main__":
    main()
