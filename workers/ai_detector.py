#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 画面异常检测模块（可选）

设计原则：
- 默认关闭，不影响现有规则检测
- 未安装 onnxruntime / opencv 或未放置模型时，自动降级，不抛异常
- 支持两种模式：
  1. ONNX 模型推理（真正 AI，推荐）
  2. 轻量启发式（颜色/块状统计，无需模型，可先验证流程）

用法：
    detector = AIDetector(ai_config, work_dir)
    result = detector.analyze_image("path/to/frame.jpg")
    if result.get("is_anomaly"):
        ...
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("ai_detector")


class AIDetector:
    """可选 AI 检测器。enabled=False 或依赖缺失时安全空转。"""

    def __init__(self, ai_config: Optional[Dict[str, Any]] = None, work_dir: str = "."):
        ai_config = ai_config or {}
        self.enabled = bool(ai_config.get("enabled", False))
        self.work_dir = Path(work_dir)
        self.model_path = self.work_dir / ai_config.get("model_path", "models/mosaic_detector.onnx")
        self.mode = ai_config.get("mode", "auto")  # auto | onnx | heuristic | off
        self.interval_sec = float(ai_config.get("interval_sec", 2.0))
        self.threshold = float(ai_config.get("threshold", 0.55))
        self.green_ratio_th = float(ai_config.get("green_ratio_th", 0.35))
        self.block_score_th = float(ai_config.get("block_score_th", 0.12))

        self.available = False
        self.backend = "none"  # none | onnx | heuristic
        self._session = None
        self._input_name = None
        self._cv2 = None
        self._np = None

        if not self.enabled:
            logger.info("AI 模块已关闭（config.ai.enabled=false），仅使用规则检测")
            return

        self._init_backend()

    def _init_backend(self) -> None:
        """按优先级尝试加载后端，失败则安全降级。"""
        prefer = self.mode

        if prefer in ("auto", "onnx"):
            if self._try_load_onnx():
                return
            if prefer == "onnx":
                logger.warning("指定 mode=onnx 但加载失败，AI 将不可用")
                return

        if prefer in ("auto", "heuristic"):
            if self._try_load_heuristic():
                return

        logger.warning(
            "AI 模块已启用，但未找到可用后端（onnxruntime/模型 或 opencv）。"
            "将跳过 AI 检测，规则检测不受影响。"
        )

    def _try_load_onnx(self) -> bool:
        try:
            import onnxruntime as ort  # type: ignore
            import numpy as np  # type: ignore

            if not self.model_path.is_file():
                logger.info(f"ONNX 模型不存在: {self.model_path}，跳过 ONNX 后端")
                return False

            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = 2
            sess_options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(self.model_path),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name
            self._np = np
            self.available = True
            self.backend = "onnx"
            logger.info(f"AI 后端已加载: ONNX ({self.model_path})")
            return True
        except ImportError:
            logger.info("未安装 onnxruntime，跳过 ONNX 后端")
            return False
        except Exception as e:
            logger.warning(f"加载 ONNX 模型失败: {e}")
            return False

    def _try_load_heuristic(self) -> bool:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore

            self._cv2 = cv2
            self._np = np
            self.available = True
            self.backend = "heuristic"
            logger.info("AI 后端已加载: 启发式（颜色/块状统计，无需模型）")
            return True
        except ImportError:
            logger.info("未安装 opencv-python，跳过启发式后端")
            return False
        except Exception as e:
            logger.warning(f"启发式后端初始化失败: {e}")
            return False

    @property
    def is_ready(self) -> bool:
        return self.enabled and self.available

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "backend": self.backend,
            "model_path": str(self.model_path),
            "interval_sec": self.interval_sec,
        }

    def analyze_image(self, image_path: str) -> Dict[str, Any]:
        """
        分析单张图片。

        返回统一结构：
        {
            "ai_enabled": bool,
            "backend": str,
            "is_anomaly": bool,
            "score": float,          # 0~1，越高越像异常
            "label": str,            # normal / mosaic / green_screen / unknown
            "detail": dict,
            "message": str
        }
        """
        base = {
            "ai_enabled": self.enabled,
            "backend": self.backend,
            "is_anomaly": False,
            "score": 0.0,
            "label": "skipped",
            "detail": {},
            "message": "",
        }

        if not self.enabled:
            base["message"] = "AI 未启用"
            return base

        if not self.available:
            base["message"] = "AI 已启用但后端不可用"
            return base

        path = Path(image_path)
        if not path.is_file():
            base["message"] = f"图片不存在: {image_path}"
            return base

        try:
            if self.backend == "onnx":
                return self._infer_onnx(path, base)
            if self.backend == "heuristic":
                return self._infer_heuristic(path, base)
        except Exception as e:
            logger.error(f"AI 分析异常: {e}")
            base["message"] = f"分析失败: {e}"
            return base

        base["message"] = "未知后端"
        return base

    def _infer_onnx(self, path: Path, base: Dict) -> Dict[str, Any]:
        """ONNX 推理（模型输入约定：1x3x224x224，RGB，归一化到 0~1）。"""
        assert self._session is not None and self._np is not None
        np = self._np

        # 延迟导入 cv2 做 resize；若没有 cv2 用最简方式
        try:
            import cv2
            img = cv2.imread(str(path))
            if img is None:
                base["message"] = "无法读取图片"
                return base
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (224, 224))
        except ImportError:
            from PIL import Image
            img = Image.open(path).convert("RGB").resize((224, 224))
            img = np.array(img)

        x = img.astype("float32") / 255.0
        x = x.transpose(2, 0, 1)[None, ...]  # NCHW

        outputs = self._session.run(None, {self._input_name: x})
        # 约定输出: [normal_prob, anomaly_prob] 或 单一 anomaly score
        out = outputs[0]
        if out.ndim == 2 and out.shape[1] >= 2:
            score = float(out[0][1])
        else:
            score = float(np.ravel(out)[0])

        is_anomaly = score >= self.threshold
        label = "mosaic" if is_anomaly else "normal"
        base.update(
            {
                "is_anomaly": is_anomaly,
                "score": round(score, 4),
                "label": label,
                "detail": {"raw": float(np.ravel(out)[0]) if out.size else score},
                "message": f"ONNX 判定: {label} (score={score:.3f})",
            }
        )
        return base

    def _infer_heuristic(self, path: Path, base: Dict) -> Dict[str, Any]:
        """
        启发式检测（针对严重马赛克/绿屏花屏）：
        - 绿色主色占比过高 → green_screen
        - 局部块方差异常 → mosaic
        不依赖训练模型，装了 opencv 即可用。
        """
        cv2 = self._cv2
        np = self._np
        assert cv2 is not None and np is not None

        img = cv2.imread(str(path))
        if img is None:
            base["message"] = "无法读取图片"
            return base

        h, w = img.shape[:2]
        # 缩小加速
        scale = 320 / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)))

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 绿色范围（含偏青的解码错误绿）
        lower = np.array([35, 40, 40])
        upper = np.array([95, 255, 255])
        green_mask = cv2.inRange(hsv, lower, upper)
        green_ratio = float(np.count_nonzero(green_mask)) / green_mask.size

        # 简单块状分数：把图分成 16x16 网格，算相邻块均值差
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gh, gw = gray.shape
        bs = 16
        block_diffs = []
        for y in range(0, gh - bs, bs):
            for x in range(0, gw - bs, bs):
                block = gray[y : y + bs, x : x + bs]
                m = float(block.mean())
                if x + bs < gw:
                    right = gray[y : y + bs, x + bs : x + 2 * bs]
                    if right.size == block.size:
                        block_diffs.append(abs(m - float(right.mean())))
                if y + bs < gh:
                    below = gray[y + bs : y + 2 * bs, x : x + bs]
                    if below.size == block.size:
                        block_diffs.append(abs(m - float(below.mean())))

        block_score = float(np.mean(block_diffs) / 255.0) if block_diffs else 0.0

        # 综合判定
        is_green = green_ratio >= self.green_ratio_th
        is_blocky = block_score >= self.block_score_th
        is_anomaly = is_green or is_blocky

        if is_green and is_blocky:
            label = "green_screen+mosaic"
            score = min(1.0, 0.5 * green_ratio + 0.5 * min(block_score * 3, 1.0) + 0.2)
        elif is_green:
            label = "green_screen"
            score = min(1.0, green_ratio + 0.2)
        elif is_blocky:
            label = "mosaic"
            score = min(1.0, block_score * 4)
        else:
            label = "normal"
            score = max(green_ratio, block_score)

        base.update(
            {
                "is_anomaly": is_anomaly,
                "score": round(float(score), 4),
                "label": label,
                "detail": {
                    "green_ratio": round(green_ratio, 4),
                    "block_score": round(block_score, 4),
                },
                "message": (
                    f"启发式判定: {label} "
                    f"(green={green_ratio:.2%}, block={block_score:.3f})"
                ),
            }
        )
        return base


def create_detector(config: Dict[str, Any], work_dir: str = ".") -> AIDetector:
    """从完整 channels.yaml 配置创建检测器。"""
    ai_cfg = config.get("ai", {}) if config else {}
    return AIDetector(ai_cfg, work_dir)
