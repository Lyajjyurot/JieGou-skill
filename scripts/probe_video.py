# -*- coding: utf-8 -*-
"""probe_video — 读取视频元数据，写出 meta.json。

用法：
    python probe_video.py <video> <out_dir>

产出 <out_dir>/meta.json：
    duration / fps / 总帧数 / 分辨率 / 旋转 / 码率 / 音轨参数 / 字幕轨 / 是否含BGM线索
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vds_common import ffmpeg_exe, run_ffmpeg, write_json, ensure_dirs  # noqa: E402


def parse_ffmpeg_stderr(text: str) -> dict:
    """从 ffmpeg -i 的 stderr 里抠出流信息。"""
    info: dict = {"video": {}, "audio": {}, "subtitles": [], "raw_streams": []}

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info["duration"] = h * 3600 + mi * 60 + s

    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        info["bitrate_kbps"] = int(m.group(1))

    rot = re.search(r"rotate\s*:\s*(\d+)", text)
    if rot:
        info["rotation"] = int(rot.group(1))
    else:
        sd = re.search(r"displaymatrix:\s*rotation of\s*(-?\d+(?:\.\d+)?)", text)
        if sd:
            info["rotation"] = abs(int(round(float(sd.group(1))))) % 360

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Stream #"):
            continue
        info["raw_streams"].append(line)
        if " Video:" in line:
            v = info["video"]
            codec = re.search(r"Video:\s*([A-Za-z0-9_\-]+)", line)
            if codec:
                v["codec"] = codec.group(1)
            res = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if res:
                v["width"], v["height"] = int(res.group(1)), int(res.group(2))
            mfps = re.search(r"([\d.]+)\s*fps", line)
            if mfps:
                v["fps"] = float(mfps.group(1))
            pix = re.search(r"Video:\s*\S+[^,]*,\s*([a-z0-9]+(?:\([^)]*\))?)[,\s]", line)
            if pix:
                v["pix_fmt"] = pix.group(1)
            prof = re.search(r"\(([^()]*\b(?:Main|High|Baseline|Simple|Constrained|profile)[^()]*)\)", line)
            if prof:
                v["profile"] = prof.group(1)
            mbr = re.search(r"(\d+)\s*kb/s", line)
            if mbr:
                v["bitrate_kbps"] = int(mbr.group(1))
        elif " Audio:" in line:
            a = info["audio"]
            codec = re.search(r"Audio:\s*([A-Za-z0-9_\-]+)", line)
            if codec:
                a["codec"] = codec.group(1)
            sr = re.search(r"(\d+)\s*Hz", line)
            if sr:
                a["sample_rate"] = int(sr.group(1))
            ch = re.search(r"Hz,\s*([a-z0-9().\s]+?),", line)
            if ch:
                a["layout"] = ch.group(1).strip()
            abr = re.search(r"(\d+)\s*kb/s", line)
            if abr:
                a["bitrate_kbps"] = int(abr.group(1))
        elif " Subtitle" in line:
            info["subtitles"].append(line)
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out_dir")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.exists():
        print(f"[probe] 找不到视频：{video}")
        return 2
    dirs = ensure_dirs(args.out_dir)

    try:
        rc, err = run_ffmpeg(["-i", str(video)], timeout=180)
        stream = parse_ffmpeg_stderr(err)
    except Exception as e:  # ffmpeg 缺失时降级
        print(f"[probe] ffmpeg 不可用（{e}），仅使用 OpenCV 元数据")
        stream = {"video": {}, "audio": {}, "subtitles": [], "raw_streams": []}

    meta: dict = {
        "file": video.name,
        "file_name": video.name,
        "size_mb": round(video.stat().st_size / 1048576, 2),
        "has_ffmpeg": True,
    }

    # OpenCV 兜底/校正
    try:
        import cv2

        cap = cv2.VideoCapture(str(video))
        meta["fps"] = round(float(cap.get(cv2.CAP_PROP_FPS) or 0), 4)
        meta["frame_count"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        meta["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        meta["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        cap.release()
    except Exception as e:
        meta["cv2_error"] = str(e)
        meta.setdefault("fps", 0)
        meta.setdefault("frame_count", 0)

    if not meta.get("fps"):
        meta["fps"] = stream.get("video", {}).get("fps") or 25.0

    duration = stream.get("duration")
    if not duration and meta.get("fps"):
        duration = meta["frame_count"] / meta["fps"]
    meta["duration"] = round(float(duration or 0), 3)

    for k in ("bitrate_kbps", "rotation"):
        if k in stream:
            meta[k] = stream[k]

    meta["video_stream"] = stream.get("video", {})
    meta["audio_stream"] = stream.get("audio", {})
    meta["has_audio"] = bool(stream.get("audio"))
    meta["subtitle_tracks"] = stream.get("subtitles", [])
    meta["raw_stream_lines"] = stream.get("raw_streams", [])

    # 时长档位，供后续脚本选抽帧策略
    d = meta["duration"]
    if d <= 1.5:
        meta["tier"] = "超短(<=1.5s) 逐帧级"
    elif d <= 3:
        meta["tier"] = "极短(<=3s) 高密度"
    elif d <= 15:
        meta["tier"] = "短(<=15s) 中高密度"
    elif d <= 60:
        meta["tier"] = "短视频(<=60s) 中密度"
    elif d <= 180:
        meta["tier"] = "中长(<=3min) 抽样"
    else:
        meta["tier"] = "长视频(>3min) 分段抽样"

    write_json(dirs["out"] / "meta.json", meta)
    print(json.dumps({k: meta[k] for k in (
        "file_name", "duration", "fps", "width", "height",
        "has_audio", "tier")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
