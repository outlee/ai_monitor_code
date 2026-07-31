#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从视频中抽帧，便于整理训练图片（异常/正常均可）。

依赖：系统已安装 ffmpeg（推荐）；未装时尝试用 OpenCV。

用法示例：
  # 每秒 1 帧，输出到 anomaly 目录
  python video_to_frames.py -i fault.ts -o ../dataset/train/anomaly --fps 1

  # 每 2 秒 1 帧，文件名前缀 fault01
  python video_to_frames.py -i fault.mp4 -o ./out --fps 0.5 --prefix fault01

  # 只抽 10s～40s 片段，最多 200 张
  python video_to_frames.py -i clip.mp4 -o ./out --start 10 --end 40 --fps 2 --max 200

  # 均匀抽固定张数（不按时长 fps）
  python video_to_frames.py -i clip.mp4 -o ./out --count 50
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def which_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def extract_ffmpeg(
    video: Path,
    out_dir: Path,
    *,
    fps: float | None,
    count: int | None,
    start: float | None,
    end: float | None,
    prefix: str,
    quality: int,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / f"{prefix}_%05d.jpg")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd.extend(["-ss", str(start)])
    cmd.extend(["-i", str(video)])
    if end is not None:
        if start is not None:
            cmd.extend(["-t", str(max(0.0, end - start))])
        else:
            cmd.extend(["-t", str(end)])

    vf_parts = []
    if count is not None and count > 0:
        # 先探测时长再算 fps；失败则退回 1fps
        duration = _probe_duration(video)
        if duration and duration > 0:
            # 均匀：fps = count / duration
            vf_parts.append(f"fps={count / duration}")
        else:
            vf_parts.append("fps=1")
    elif fps is not None and fps > 0:
        vf_parts.append(f"fps={fps}")
    else:
        vf_parts.append("fps=1")

    cmd.extend(["-vf", ",".join(vf_parts)])
    if count is not None and count > 0:
        cmd.extend(["-frames:v", str(count)])
    cmd.extend(["-q:v", str(quality), pattern])

    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败，返回码 {r.returncode}")
    return len(list(out_dir.glob(f"{prefix}_*.jpg")))


def _probe_duration(video: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            text=True,
        ).strip()
        return float(out)
    except Exception:
        return None


def extract_opencv(
    video: Path,
    out_dir: Path,
    *,
    fps: float | None,
    count: int | None,
    start: float | None,
    end: float | None,
    prefix: str,
    max_frames: int | None,
) -> int:
    try:
        import cv2
    except ImportError as e:
        raise RuntimeError("未安装 ffmpeg，且未安装 opencv-python-headless") from e

    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video}")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / src_fps if src_fps > 0 and total > 0 else 0.0

    start_f = int((start or 0) * src_fps)
    end_f = int((end * src_fps) if end is not None else (total or 10**12))

    if count is not None and count > 0 and duration > 0:
        # 均匀取 count 帧
        span = max(end_f - start_f, 1)
        indices = {start_f + int(i * (span - 1) / max(count - 1, 1)) for i in range(count)}
        use_set = True
        step = 1
    else:
        use_set = False
        target_fps = fps if fps and fps > 0 else 1.0
        step = max(int(round(src_fps / target_fps)), 1)
        indices = set()

    if start_f > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)

    n = 0
    idx = start_f
    while idx < end_f:
        if use_set:
            if idx not in indices:
                if not cap.grab():
                    break
                idx += 1
                continue
        elif (idx - start_f) % step != 0:
            if not cap.grab():
                break
            idx += 1
            continue

        ok, frame = cap.read()
        if not ok:
            break
        out_path = out_dir / f"{prefix}_{n:05d}.jpg"
        cv2.imwrite(str(out_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        n += 1
        if max_frames and n >= max_frames:
            break
        if use_set and n >= count:
            break
        idx += 1

    cap.release()
    return n


def main():
    parser = argparse.ArgumentParser(
        description="视频抽帧 → 训练用图片（可直接输出到 anomaly/normal 目录）"
    )
    parser.add_argument("-i", "--input", required=True, help="输入视频路径（mp4/ts/mkv…）")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="输出目录，例如 dataset/train/anomaly",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=1.0,
        help="每秒抽取帧数，默认 1（即每秒 1 张）。0.5=每 2 秒 1 张",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="均匀抽取固定张数（指定后优先于 --fps）",
    )
    parser.add_argument("--start", type=float, default=None, help="开始时间（秒）")
    parser.add_argument("--end", type=float, default=None, help="结束时间（秒）")
    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="输出文件名前缀，默认用视频文件名",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="最多输出张数（防止抽爆磁盘）",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=3,
        help="ffmpeg JPEG 质量 2–31，越小越清晰，默认 3",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "ffmpeg", "opencv"],
        default="auto",
        help="抽帧后端，默认 auto（优先 ffmpeg）",
    )
    args = parser.parse_args()

    video = Path(args.input).expanduser().resolve()
    if not video.is_file():
        print(f"找不到视频: {video}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output).expanduser().resolve()
    prefix = args.prefix or video.stem.replace(" ", "_")
    # 清理前缀非法字符
    prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix)

    backend = args.backend
    if backend == "auto":
        backend = "ffmpeg" if which_ffmpeg() else "opencv"

    print(f"视频: {video}")
    print(f"输出: {out_dir}")
    print(f"后端: {backend}  前缀: {prefix}")

    try:
        if backend == "ffmpeg":
            if not which_ffmpeg():
                raise RuntimeError("系统未找到 ffmpeg")
            # max：ffmpeg 用 -frames 限制时与 count 合并逻辑简单处理
            count = args.count
            if args.max and count:
                count = min(count, args.max)
            elif args.max and not count:
                # 按时长估算后截断：先抽再删多余较麻烦，用 fps 抽完后裁剪
                pass
            n = extract_ffmpeg(
                video,
                out_dir,
                fps=None if count else args.fps,
                count=count,
                start=args.start,
                end=args.end,
                prefix=prefix,
                quality=max(2, min(args.quality, 31)),
            )
            if args.max and n > args.max:
                # 删除多余
                files = sorted(out_dir.glob(f"{prefix}_*.jpg"))
                for f in files[args.max :]:
                    f.unlink(missing_ok=True)
                n = args.max
        else:
            n = extract_opencv(
                video,
                out_dir,
                fps=args.fps,
                count=args.count,
                start=args.start,
                end=args.end,
                prefix=prefix,
                max_frames=args.max,
            )
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"完成: 共 {n} 张 → {out_dir}")
    print("请人工核对后，确认异常图在 anomaly、正常图在 normal。")


if __name__ == "__main__":
    main()
