# -*- coding: utf-8 -*-
"""单帧多类模型的类别顺序。下标就是 ONNX 输出的列，训练和监测必须用同一份。

没有样本的类不要建目录。以后加上一类，要重新训练并导出，不能把两个模型拼在一起。
静帧和闪烁单帧看不见，不要放进这个模型。
"""

from pathlib import Path

FRAME_CLASSES = (
    "normal",
    "black",
    "blue_screen",
    "solid_green",
    "green_screen_mosaic",
    "color_exposure",
    "inverted",
)

TEMPORAL_CLASSES = ("freeze", "flicker")

FRAME_LABEL_ZH = {
    "black": "黑屏",
    "blue_screen": "蓝屏",
    "solid_green": "绿屏",
    "green_screen": "花屏",
    "mosaic": "马赛克",
    "green_screen_mosaic": "花屏马赛克",
    "color_cast": "偏色",
    "overexposed": "过曝",
    "underexposed": "欠曝",
    "color_exposure": "偏色曝光",
    "inverted": "画面倒置",
}


# 花屏和马赛克训成一类。偏色、过曝、欠曝训成一类。
# 彩条是节目结束时的测试图，跟正常画面一类，不告警。
CLASS_ALIASES = {
    "green_screen": "green_screen_mosaic",
    "mosaic": "green_screen_mosaic",
    "color_cast": "color_exposure",
    "overexposed": "color_exposure",
    "underexposed": "color_exposure",
    "color_bars": "normal",
}


def canonical_class(name):
    return CLASS_ALIASES.get(name, name)


def class_folders(class_name):
    """这一类要读哪些目录。别名目录里的图和正式目录算同一类。"""
    names = [class_name]
    for alias, target in CLASS_ALIASES.items():
        if target == class_name:
            names.append(alias)
    return names


def select_frame_classes(found):
    """found 是 train 下有图的目录名。返回有序类别，以及被跳过的时序类。"""
    raw = set(found or [])
    found_set = set(canonical_class(name) for name in raw)
    skipped = [name for name in TEMPORAL_CLASSES if name in raw]
    faults = [name for name in FRAME_CLASSES if name != "normal" and name in found_set]
    has_anomaly = "anomaly" in found_set
    has_normal = "normal" in found_set
    if not has_normal:
        raise ValueError("缺少 normal")
    if not faults and has_anomaly:
        return ["normal", "anomaly"], skipped
    if not faults:
        raise ValueError("至少还要有一类故障")
    if has_anomaly:
        raise ValueError("多类目录和 anomaly 目录不要同时使用")
    return ["normal"] + faults, skipped


_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _dir_has_image(folder):
    for path in Path(folder).rglob("*"):
        if path.is_file() and path.suffix.lower() in _IMG_EXTS:
            return True
    return False


def discover_frame_classes(train_dir):
    """看 train 这一层哪些子目录里有图。返回类别、跳过的时序目录、认不出的目录名。"""
    root = Path(train_dir)
    found = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and _dir_has_image(child):
                found.append(child.name)
    known = set(FRAME_CLASSES) | set(TEMPORAL_CLASSES) | set(CLASS_ALIASES) | set(["anomaly"])
    unknown = [name for name in found if name not in known]
    classes, skipped = select_frame_classes(found)
    return classes, skipped, unknown
