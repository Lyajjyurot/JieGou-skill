# -*- coding: utf-8 -*-
"""analyze_audio — 音频波形分析（无需语音识别），写出 audio.json。

用法：
    python analyze_audio.py <video> <out_dir> [--blocks 0.5]

测量内容：
    1. 响度：ffmpeg ebur128 积分响度 LUFS / LRA / 真峰值；volumedetect 均值与峰值
    2. 包络：25ms/10ms 帧的 RMS 分贝曲线（含 ASCII 迷你曲线便于直接读）
    3. 频段画像：sub/bass/low-mid/mid/high-mid/high 六段能量占比
    4. 分块启发式分类：静音留白 / 疑似人声主导 / 疑似音乐主导 / 环境氛围 / 混合
       （依据语音带占比 300-3400Hz、谱平坦度、过零率、谱质心、低频节奏能量）
    5. 节拍：谱通量 onset 包络 + 自相关测 BPM，输出节拍时间点
    6. 卡点率：镜头切换时间与节拍网格的对齐度 —— 量化「剪辑是否踩点」
    7. 静默区间（silencedetect）与留白统计

注意：本脚本不做语音转写，对白文本需外部字幕或人工补录；分类为启发式判定。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vds_common import ensure_dirs, read_json, write_json, run_ffmpeg, ffmpeg_exe  # noqa: E402

BANDS = [
    ("sub", 20, 80), ("bass", 80, 250), ("low_mid", 250, 500),
    ("mid", 500, 2000), ("high_mid", 2000, 4000), ("high", 4000, 8000),
]
SPEECH_BAND = (300, 3400)
MIN_DB = -90.0


def db(x: float) -> float:
    return float(20 * math.log10(max(x, 1e-7)))


# ------------------------------------------------------------------ 抽取
def extract_wav(video: Path, out_wav: Path, sr: int = 16000, mono: bool = True) -> bool:
    args = ["-y", "-i", str(video), "-vn", "-ac", "1" if mono else "2",
            "-ar", str(sr), "-c:a", "pcm_s16le", str(out_wav)]
    rc, err = run_ffmpeg(args, timeout=1200)
    if rc != 0 or not out_wav.exists():
        print("[audio] 音频提取失败：", err.strip().splitlines()[-1] if err else rc)
        return False
    return True


def read_wav(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as w:
        n_ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_ch > 1:
        a = a.reshape(-1, n_ch).mean(axis=1)
    return a, sr, n_ch


# ------------------------------------------------------------------ 响度
def ffmpeg_loudness(video: Path) -> dict:
    out = {}
    try:
        rc, err = run_ffmpeg(["-i", str(video), "-af", "ebur128=peak=true", "-f", "null", "-"],
                             timeout=1800)
        # ebur128 会逐帧打印进度行（I 值尚未收敛），必须只取 Summary 段
        tail = err.split("Summary:")[-1] if "Summary:" in err else err
        m = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", tail)
        if m:
            out["integrated_lufs"] = float(m.group(1))
        m = re.search(r"LRA:\s*(-?[\d.]+)\s*LU", tail)
        if m:
            out["lra_lu"] = float(m.group(1))
        m = re.search(r"Peak:\s*(-?[\d.]+)\s*dBFS", tail)
        if m:
            out["true_peak_dbfs"] = float(m.group(1))
        if "integrated_lufs" not in out:
            vals = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", err)
            if vals:
                out["integrated_lufs"] = float(vals[-1])
    except Exception as e:
        out["ebur128_error"] = str(e)
    try:
        rc, err = run_ffmpeg(["-i", str(video), "-af", "volumedetect", "-f", "null", "-"],
                             timeout=1800)
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", err)
        if m:
            out["mean_volume_db"] = float(m.group(1))
        m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", err)
        if m:
            out["max_volume_db"] = float(m.group(1))
    except Exception as e:
        out["volumedetect_error"] = str(e)
    return out


def ffmpeg_silences(video: Path, noise_db: float = -38.0, min_d: float = 0.15) -> list[dict]:
    try:
        rc, err = run_ffmpeg(["-i", str(video), "-af",
                              f"silencedetect=noise={noise_db}dB:d={min_d}",
                              "-f", "null", "-"], timeout=1800)
    except Exception:
        return []
    out, starts = [], []
    for m in re.finditer(r"silence_start:\s*(-?[\d.]+)", err):
        starts.append(float(m.group(1)))
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*(-?[\d.]+)", err)]
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is not None and e > s:
            out.append({"start": round(s, 3), "end": round(e, 3),
                        "duration": round(e - s, 3)})
    return out


# ------------------------------------------------------------------ 谱分析
def stft(sig: np.ndarray, sr: int, win: int = 1024, hop: int = 512):
    if sig.size < win:
        sig = np.pad(sig, (0, win - sig.size))
    n_frames = 1 + (sig.size - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = sig[idx] * np.hanning(win)[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / sr).astype(np.float32)
    return spec, freqs, hop / sr


def block_features(sig: np.ndarray, sr: int, blocks: float) -> list[dict]:
    bs = max(1, int(round(blocks * sr)))
    n = int(math.ceil(sig.size / bs))
    win, hop = 1024, 512
    rows = []
    for b in range(n):
        seg = sig[b * bs:(b + 1) * bs]
        if seg.size < 256:
            seg = np.pad(seg, (0, 256 - seg.size))
        rms = float(np.sqrt(np.mean(seg ** 2)))
        spec, freqs, _ = stft(seg, sr, win, hop)
        power = (spec ** 2).sum(axis=0) + 1e-12
        total = float(power.sum())
        band = {}
        for name, lo, hi in BANDS:
            m = (freqs >= lo) & (freqs < hi)
            band[name] = round(float(power[m].sum() / total), 4) if m.any() else 0.0
        speech_m = (freqs >= SPEECH_BAND[0]) & (freqs <= SPEECH_BAND[1])
        speech_ratio = float(power[speech_m].sum() / total) if speech_m.any() else 0.0
        p = power / total
        flatness = float(np.exp(np.mean(np.log(p + 1e-12))) / (np.mean(p) + 1e-12))
        centroid = float((freqs * p).sum())
        zcr = float(np.mean(np.abs(np.diff(np.sign(seg))) > 0))
        spec_t = spec.mean(axis=0)
        flux = float(np.maximum(np.diff(spec_t), 0).sum())
        rows.append({
            "t": round(b * blocks, 3),
            "db": round(db(rms), 1),
            "speech_ratio": round(speech_ratio, 4),
            "bands": band,
            "flatness": round(flatness, 5),
            "centroid": round(centroid, 1),
            "zcr": round(zcr, 4),
            "flux": round(flux, 3),
        })
    # onset 相对强度
    fl = np.array([r["flux"] for r in rows], dtype=np.float32)
    if fl.size > 2:
        base = np.convolve(fl, np.ones(5) / 5, mode="same")
        onset = np.maximum(fl - base, 0)
        mx = float(onset.max()) or 1.0
        onset = onset / mx
    else:
        onset = np.zeros_like(fl)
    for r, o in zip(rows, onset):
        r["onset"] = round(float(o), 3)
    return rows


def classify(r: dict) -> tuple[str, float]:
    """启发式分级：返回 (标签, 置信度)。无 ASR，故一律标注为「疑似」。"""
    if r["db"] < -45:
        return "静音留白", 0.95
    b = r["bands"]
    speechish = r["speech_ratio"] >= 0.40 and 300 <= r["centroid"] <= 3200 and 0.01 <= r["zcr"] <= 0.30
    musicish = (b["bass"] + b["sub"]) >= 0.28 and r["flatness"] < 0.06 and r["db"] > -38
    rhythmic = r["onset"] > 0.35
    if speechish and not musicish:
        return "疑似人声主导", 0.6 if r["speech_ratio"] < 0.55 else 0.75
    if musicish and not speechish:
        return "疑似音乐主导", 0.7 if rhythmic else 0.55
    if musicish and speechish:
        return "人声+音乐叠混", 0.5
    if r["db"] < -32 and r["flatness"] > 0.12:
        return "环境氛围音", 0.5
    if rhythmic:
        return "节奏/音效驱动", 0.5
    return "混合/待判", 0.3


def segmentize(rows: list[dict], blocks: float) -> list[dict]:
    segs = []
    for r in rows:
        lab, conf = classify(r)
        r["kind"] = lab
        r["conf"] = conf
        if segs and segs[-1]["kind"] == lab and \
                r["t"] - segs[-1]["end"] <= blocks * 1.5:
            segs[-1]["end"] = round(r["t"] + blocks, 3)
            segs[-1]["n"] += 1
            segs[-1]["conf"] = round(max(segs[-1]["conf"], conf), 2)
        else:
            segs.append({"kind": lab, "start": r["t"], "end": round(r["t"] + blocks, 3),
                         "conf": conf, "n": 1})
    for s in segs:
        s["duration"] = round(s["end"] - s["start"], 3)
    # 合并过短的碎段
    merged = []
    for s in segs:
        if merged and s["duration"] < blocks * 1.5 and len(merged) > 0:
            merged[-1]["end"] = s["end"]
            merged[-1]["duration"] = round(merged[-1]["end"] - merged[-1]["start"], 3)
            merged[-1]["n"] += s["n"]
            continue
        merged.append(s)
    return merged


def detect_beats(rows: list[dict], blocks: float) -> dict:
    """基于分块 onset 包络的 BPM 估计 + 节拍网格。"""
    env = np.array([r["onset"] for r in rows], dtype=np.float32)
    env = env - env.mean()
    if env.size < 8 or float(np.abs(env).sum()) < 1e-3:
        return {"bpm": None, "times": [], "confidence": 0.0}
    ac = np.correlate(env, env, mode="full")[env.size - 1:]
    ac = ac / (ac[0] + 1e-9)
    lag_min = max(1, int(round((60 / 200) / blocks)))
    lag_max = min(ac.size - 1, int(round((60 / 50) / blocks)))
    if lag_max <= lag_min:
        return {"bpm": None, "times": [], "confidence": 0.0}
    seg = ac[lag_min:lag_max + 1]
    lag = lag_min + int(np.argmax(seg))
    score = float(seg.max())
    bpm = 60.0 / (lag * blocks)
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    period = 60.0 / bpm
    # 以最强 onset 为锚点铺节拍网格
    anchor_i = int(np.argmax(env))
    anchor_t = rows[anchor_i]["t"]
    times = []
    t = anchor_t
    while t > 0:
        t -= period
    t += period
    while t < rows[-1]["t"] + blocks:
        times.append(round(t, 3))
        t += period
    return {"bpm": round(bpm, 1), "period": round(period, 3),
            "times": times, "confidence": round(min(1.0, max(0.0, score)), 3)}


def cut_alignment(shots: list[dict], beats: dict, tol: float = 0.14) -> dict:
    times = beats.get("times") or []
    cuts = [s["start"] for s in shots[1:]]
    if not times or not cuts:
        return {"cut_count": len(cuts), "on_beat": 0, "rate": None, "per_cut": []}
    arr = np.array(times, dtype=np.float32)
    per, on = [], 0
    for c in cuts:
        k = int(np.argmin(np.abs(arr - c)))
        off = float(c - arr[k])
        hit = abs(off) <= tol
        on += int(hit)
        per.append({"t": round(c, 3), "nearest_beat": round(float(arr[k]), 3),
                    "offset": round(off, 3), "on_beat": hit})
    return {"cut_count": len(cuts), "on_beat": on,
            "rate": round(on / len(cuts), 3), "tolerance_s": tol, "per_cut": per}


def sparkline(rows: list[dict], every: int = 1) -> str:
    bars = "▁▂▃▄▅▆▇█"
    vals = [r["db"] for r in rows][::every]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 1e-6)
    return "".join(bars[min(7, max(0, int((v - lo) / span * 7.999)))] for v in vals)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out_dir")
    ap.add_argument("--blocks", type=float, default=0.5, help="分块时长（秒）")
    ap.add_argument("--sr", type=int, default=16000)
    args = ap.parse_args()

    video = Path(args.video).resolve()
    dirs = ensure_dirs(args.out_dir)
    result: dict = {"has_audio": False, "blocks": args.blocks}

    wav_path = dirs["audio"] / "mono16k.wav"
    if not extract_wav(video, wav_path, args.sr):
        result["note"] = "视频无音轨或提取失败，音频维度留空"
        write_json(dirs["out"] / "audio.json", result)
        return 0

    sig, sr, n_ch = read_wav(wav_path)
    dur = sig.size / sr
    result.update({"has_audio": True, "sample_rate": sr, "duration": round(dur, 3),
                   "wav": str(wav_path).replace("\\", "/")})

    result["loudness"] = ffmpeg_loudness(video)
    result["silences"] = ffmpeg_silences(video)

    rows = block_features(sig, sr, args.blocks)
    segs = segmentize(rows, args.blocks)
    beats = detect_beats(rows, args.blocks)

    shots_doc = None
    try:
        shots_doc = read_json(dirs["out"] / "shots.json")
    except Exception:
        pass
    if shots_doc:
        result["cut_alignment"] = cut_alignment(shots_doc["shots"], beats)

    # 整体频段画像
    band_mean = {}
    for name, _, _ in BANDS:
        band_mean[name] = round(float(np.mean([r["bands"][name] for r in rows])), 4)

    # 精简：包络只保留到 0.25s 粒度存盘，避免 JSON 过大
    keep_every = max(1, int(round(0.25 / args.blocks)))
    result["envelope"] = [{"t": r["t"], "db": r["db"]} for r in rows[::keep_every]]
    result["blocks_detail"] = rows
    result["segments"] = segs
    result["beats"] = beats
    result["band_profile"] = band_mean
    result["sparkline"] = sparkline(rows, keep_every)
    result["sparkline_step_s"] = round(args.blocks * keep_every, 3)

    speech_blocks = sum(1 for r in rows if r["kind"] == "疑似人声主导")
    music_blocks = sum(1 for r in rows if r["kind"] == "疑似音乐主导")
    result["summary"] = {
        "duration": round(dur, 3),
        "speech_like_ratio": round(speech_blocks / max(len(rows), 1), 3),
        "music_like_ratio": round(music_blocks / max(len(rows), 1), 3),
        "silence_ratio": round(sum(r["db"] < -45 for r in rows) / max(len(rows), 1), 3),
        "sql": None if result["loudness"].get("integrated_lufs") is None else
               round(10 ** ((result["loudness"]["integrated_lufs"] + 0.691) / 10), 4),
    }

    write_json(dirs["out"] / "audio.json", result)
    print(json.dumps({
        "响度": result["loudness"],
        "分块分类统计": {"人声疑似": speech_blocks, "音乐疑似": music_blocks,
                         "总块数": len(rows)},
        "BPM": beats.get("bpm"),
        "卡点率": (result.get("cut_alignment") or {}).get("rate"),
        "静默段数": len(result["silences"]),
        "频段画像": band_mean,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
