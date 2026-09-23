# -*- coding: utf-8 -*-
"""vds_common — video-to-director-script 公共工具。

约定：所有脚本共享的输出目录结构

    <out>/
      frames/            关键帧 JPEG
      sheets/            接触表 JPEG
      audio/             提取的 wav 与音频分析
      meta.json          视频元数据
      shots.json         镜头切分 + 关键帧索引
      shots_metrics.json 逐镜客观指标
      audio.json         音频时间轴事件
      context_pack.md    汇总包（供模型读取）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 控制台编码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def load_font(size: int):
    """返回一个可用的 TrueType 字体；找不到就返回 None（PIL 默认位图字体）。"""
    from PIL import ImageFont

    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return None


# ---------------------------------------------------------------- ffmpeg 定位
def ffmpeg_exe() -> str:
    """按优先级定位 ffmpeg：环境变量 > imageio-ffmpeg > PATH。"""
    env = os.environ.get("VDS_FFMPEG")
    if env and os.path.exists(env):
        return env
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "未找到 ffmpeg。请安装：pip install imageio-ffmpeg，"
        "或设置环境变量 VDS_FFMPEG 指向 ffmpeg 可执行文件。"
    )


def run_ffmpeg(args, timeout: int = 900):
    """执行 ffmpeg，返回 (returncode, stderr文本)。ffmpeg 把信息都写在 stderr。"""
    exe = ffmpeg_exe()
    cmd = [exe, "-hide_banner", "-nostdin"] + [str(a) for a in args]
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return p.returncode, p.stderr.decode("utf-8", "replace")


# ---------------------------------------------------------------- 目录与 IO
def ensure_dirs(out: str | Path) -> dict:
    out = Path(out)
    d = {
        "out": out,
        "frames": out / "frames",
        "sheets": out / "sheets",
        "audio": out / "audio",
    }
    for v in d.values():
        v.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 中文路径安全 IO
# cv2.imread / cv2.imwrite 在 Windows 非 ASCII 路径下会静默失败，
# 必须走 imencode/imdecode + Python 文件句柄。
def imwrite_unicode(path, img, params=None) -> bool:
    import cv2

    path = Path(path)
    ext = path.suffix or ".jpg"
    ok, buf = cv2.imencode(ext, img, params or [])
    if not ok:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(buf.tobytes())
    return True


def imread_unicode(path, flags=None):
    import cv2
    import numpy as np

    if flags is None:
        flags = cv2.IMREAD_COLOR
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


# ---------------------------------------------------------------- 时间码
def tc(seconds: float) -> str:
    """秒 → mm:ss.ff（两位小数）。"""
    if seconds is None:
        return "00:00.00"
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


def compact_tc(seconds: float) -> str:
    """秒 → 3.20s / 1:03.20 这类紧凑标签（用于烧进画面）。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.2f}s"
    return tc(seconds)


# ---------------------------------------------------------------- 调色小工具
def hex_of(rgb) -> str:
    r, g, b = (int(max(0, min(255, round(float(c))))) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def kelvin_of_rgb(rgb) -> int:
    """用 Tanner Helland 近似法把 RGB 均值反推成色温 K（仅作倾向性参考）。"""
    r, g, b = [max(1.0, float(c)) / 255.0 for c in rgb]
    if r >= b:
        t = (r - b) / max(r, 1e-6)
        kelvin = 6600.0 - 3200.0 * min(t, 0.5) / 0.5 if t < 0.5 else 4700.0 - 1400.0 * (t - 0.5) / 0.5
    else:
        t = (b - r) / max(b, 1e-6)
        kelvin = 6600.0 + 3400.0 * min(t, 1.0)
    return int(round(kelvin / 100.0) * 100)


def temp_label(kelvin: int) -> str:
    if kelvin < 3200:
        return "暖黄（钨丝/烛光倾向）"
    if kelvin < 4500:
        return "暖白（日出日落/暖调布光）"
    if kelvin < 5800:
        return "中性白（日间自然光）"
    if kelvin < 7000:
        return "偏冷白（阴天/日光灯）"
    return "冷蓝（蓝调时刻/冷调布光）"


TILE_LAYOUT = {  # 每个镜头实际抽多少帧，按镜头时长自适应
    "dense": 14,
    "mid": 10,
    "sparse": 8,
}


def budget_frames(duration: float, n_shots: int) -> int:
    """按总时长决定全局关键帧预算（短视频优先高密度）。"""
    if duration <= 1.5:
        base = 60
    elif duration <= 3:
        base = 80
    elif duration <= 10:
        base = int(duration * 12)
    elif duration <= 30:
        base = int(duration * 7)
    elif duration <= 60:
        base = int(duration * 5)
    elif duration <= 180:
        base = int(duration * 2.2)
    else:
        base = int(duration * 1.1)
    base = max(36, base)
    return int(min(base, 520))
