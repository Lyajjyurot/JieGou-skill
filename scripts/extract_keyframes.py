# -*- coding: utf-8 -*-
"""extract_keyframes — 镜头切分 + 自适应关键帧抽取 + 接触表生成。

用法：
    python extract_keyframes.py <video> <out_dir> [--tile 440] [--max-frames 240]

产出：
    frames/   关键帧 JPEG（原分辨率，长边上限 1600）
    sheets/   overview_XX.jpg 全局接触表（20 格）· detail_sXX.jpg 逐镜接触表（≤9 格）
    shots.json  镜头切分表 + 关键帧索引 + 运动量

策略：
  1) 以固定分析帧率采样（短视频用原帧率，长视频降到 10fps）计算 HSV 直方图 + 灰度缩略图
  2) 用「中位数 + MAD」自适应阈值检测镜头切换，并强制最小镜长
  3) 按总时长给全局关键帧预算，再按镜头时长加权分配（每镜 3~14 帧）
  4) 镜内做直方图去重，避免静止画面重复抽帧
  5) 接触表把时间码与镜号烧进画面，便于模型读图时对齐时间轴
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vds_common import (  # noqa: E402
    ensure_dirs, write_json, budget_frames, compact_tc, load_font, imwrite_unicode,
)

MAX_SIDE = 1600


# ------------------------------------------------------------------ 特征
def hs_hist(bgr: np.ndarray) -> np.ndarray:
    """色调 32 桶 + 饱和度 8 桶 的归一化联合直方图（对光照/曝光更鲁棒）。"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [32, 8], [0, 180, 0, 256])
    h = h.reshape(-1).astype(np.float32)
    s = h.sum()
    return h / s if s > 0 else h


def gray_thumb(bgr: np.ndarray, w: int = 32, h: int = 18) -> np.ndarray:
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def diff_hist(a: np.ndarray, b: np.ndarray) -> float:
    return float(0.5 * np.abs(a - b).sum())


def diff_thumb(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean())


# ------------------------------------------------------------------ 光流：区分运镜与主体动作
FLOW_W, FLOW_H = 160, 90


def flow_small(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(cv2.resize(bgr, (FLOW_W, FLOW_H),
                                   interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)


def flow_metrics(prev_gray: np.ndarray | None, gray: np.ndarray, dt: float) -> dict:
    """稠密光流分解：
    - 全局平移向量（中位数）→ 摇/移/跟的位移速度
    - 散度 → 推近/拉远速度（归一化每秒尺度变化）
    - 残差能量 → 主体/局部动作强度
    """
    if prev_gray is None or dt <= 0:
        return {"move": 0.0, "move_dx": 0.0, "move_dy": 0.0,
                "zoom": 0.0, "local": 0.0, "valid": False}
    flow = cv2.calcOpticalFlowFarneback(
        prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
    fx, fy = flow[..., 0], flow[..., 1]
    mx, my = float(np.median(fx)), float(np.median(fy))

    ys, xs = np.mgrid[0:FLOW_H, 0:FLOW_W]
    cxp, cyp = (FLOW_W - 1) / 2.0, (FLOW_H - 1) / 2.0
    dx, dy = xs - cxp, ys - cyp
    rr = (FLOW_W / 2.0) ** 2 + (FLOW_H / 2.0) ** 2
    divergence = float((dx * (fx - mx) + dy * (fy - my)).sum() / (rr * fx.size))

    resid = np.hypot(fx - mx, fy - my)
    move = float(np.hypot(mx, my)) / FLOW_W / dt
    local_mean = float(resid.mean()) / FLOW_W / dt
    # 主体动作取 P90 残差：局部运动会被大面积静止背景严重稀释，均值低估，故取高分位
    local_p90 = float(np.percentile(resid, 90)) / FLOW_W / dt
    return {
        "move": round(move, 5),
        "move_dx": round(mx / FLOW_W / dt, 5),
        "move_dy": round(my / FLOW_H / dt, 5),
        "zoom": round(divergence / dt, 5),
        "local": round(local_p90, 5),
        "local_mean": round(local_mean, 5),
        "valid": True,
    }


def move_label(move: float, zoom: float) -> str:
    """move 单位为「帧宽/秒」的位移速度。"""
    if move < 0.015 and abs(zoom) < 0.01:
        return "固定机位"
    if move < 0.05:
        return "极缓移动（呼吸感/微动）"
    if move < 0.20:
        return "缓慢移动（慢摇/慢跟）"
    if move < 0.55:
        return "中等速度移动（常规跟拍/摇移）"
    return "快速移动（甩镜/高速跟拍）"


def zoom_label(zoom: float) -> str:
    """zoom 单位为「每秒画面尺度变化率」。缓慢电影推镜约 0.01–0.04。"""
    if abs(zoom) < 0.01:
        return "无推拉"
    kind = "推近" if zoom > 0 else "拉远"
    a = abs(zoom)
    if a < 0.04:
        return f"缓慢{kind}"
    if a < 0.12:
        return f"中速{kind}"
    return f"快速{kind}（冲击变焦）"


def direction_label(dx: float, dy: float) -> str:
    if abs(dx) < 0.015 and abs(dy) < 0.015:
        return "无方向位移"
    horiz = "向右" if dx > 0 else "向左"
    vert = "向下" if dy > 0 else "向上"
    if abs(dx) < 0.015:
        return f"{vert}平移"
    if abs(dy) < 0.015:
        return f"{horiz}平移"
    return f"{horiz}{vert}斜向"


def local_label(local: float) -> str:
    """local 为 P90 残差光流速度。"""
    if local < 0.08:
        return "主体近乎静止"
    if local < 0.35:
        return "主体轻微动作"
    if local < 0.90:
        return "主体明显动作"
    return "主体剧烈动作"


# ------------------------------------------------------------------ 镜头切分
def detect_shots(samples: list[dict], min_shot: float) -> list[dict]:
    n = len(samples)
    if n == 0:
        return []
    diffs = np.zeros(n, dtype=np.float32)
    for i in range(1, n):
        d = 0.62 * diff_hist(samples[i - 1]["hist"], samples[i]["hist"]) + \
            0.38 * diff_thumb(samples[i - 1]["thumb"], samples[i]["thumb"])
        diffs[i] = d

    if n > 3:
        mid = diffs[1:]
        med = float(np.median(mid))
        mad = float(np.median(np.abs(mid - med))) * 1.4826
        thr = max(0.16, med + 3.2 * (mad if mad > 1e-4 else med + 1e-3))
    else:
        thr = 0.16
    thr = min(thr, 0.62)  # 防止阈值过高漏切

    cuts = [0]
    for i in range(1, n):
        if diffs[i] >= thr:
            if (samples[i]["t"] - samples[cuts[-1]]["t"]) >= min_shot:
                cuts.append(i)
    cuts.append(n)

    shots = []
    for k in range(len(cuts) - 1):
        a, b = cuts[k], cuts[k + 1]
        seg = samples[a:b]
        if not seg:
            continue
        inner = diffs[a + 1:b] if b - a > 1 else np.array([0.0], dtype=np.float32)
        flows = [s["flow"] for s in seg[1:] if s["flow"].get("valid")]
        def fmean(key):
            vals = [f[key] for f in flows if f.get(key) is not None]
            return round(float(np.mean(vals)), 5) if vals else 0.0
        shots.append({
            "shot_id": len(shots) + 1,
            "start": round(seg[0]["t"], 3),
            "end": round(seg[-1]["t"] + 1.0 / seg[0].get("sr", 30.0), 3),
            "motion": round(float(inner.mean()), 4),
            "motion_p95": round(float(np.percentile(inner, 95)), 4),
            "cam_move": fmean("move"),
            "cam_dx": fmean("move_dx"),
            "cam_dy": fmean("move_dy"),
            "cam_zoom": fmean("zoom"),
            "local_motion": fmean("local"),
            "n_samples": len(seg),
            "n_flow_samples": len(flows),
        })
    return shots


def motion_label(m: float) -> str:
    """仅作容器差异强度的粗参考（保留用于兼容），运镜判定请用 move_label。"""
    if m < 0.012:
        return "容器差异极小"
    if m < 0.030:
        return "容器差异小"
    if m < 0.060:
        return "容器差异中等"
    if m < 0.110:
        return "容器差异大"
    return "容器差异剧烈"


# ------------------------------------------------------------------ 帧预算分配
def pick_frame_indices(shot_dur: float, total_dur: float, budget: int,
                       n_shots: int) -> int:
    """单镜抽帧数：按镜头时长加权，夹在 3~14 之间；镜头极多时整体收敛。"""
    if n_shots <= 0:
        return 6
    share = budget / n_shots
    weight = shot_dur / max(total_dur / n_shots, 1e-6)
    n = int(round(share * (0.45 + 0.55 * min(weight, 3.0))))
    if shot_dur <= 0.6:
        n = max(3, min(n, 6))
    return int(max(3, min(14, n)))


def even_times(start: float, end: float, n: int) -> list[float]:
    """在 [start,end] 内均匀取 n 个时间点；始终包含 start(+ε) 与 end(-ε)。"""
    span = max(end - start, 0.04)
    eps = min(0.05, span * 0.06)
    lo, hi = start + eps, end - eps
    if n <= 1:
        return [round((lo + hi) / 2, 3)]
    return [round(lo + (hi - lo) * i / (n - 1), 3) for i in range(n)]


# ------------------------------------------------------------------ 接触表
def label_bar(img: Image.Image, lines: list[str], height: int = 30,
              font_size: int = 15, tint=(0, 0, 0)) -> Image.Image:
    """在图底部垫一条半透明信息条并写文字。"""
    w = img.width
    canvas = Image.new("RGB", (w, img.height + height), tint)
    canvas.paste(img, (0, 0))
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rectangle([0, img.height, w, img.height + height], fill=(12, 12, 14, 235))
    font = load_font(font_size)
    if font:
        d.text((8, img.height + (height - font_size) // 2 - 2), "  |  ".join(lines),
               font=font, fill=(240, 240, 240))
    return canvas


def make_sheet(items: list[dict], cols: int, tile_w: int, path: Path,
               title: str) -> str | None:
    """items: [{path, t, shot_id}]；生成接触表并返回路径。"""
    if not items:
        return None
    tile_h = int(round(tile_w * 9 / 16))
    bar = 30
    rows = math.ceil(len(items) / cols)
    W = cols * tile_w
    H = rows * (tile_h + bar)
    canvas = Image.new("RGB", (W, H), (16, 16, 18))
    d = ImageDraw.Draw(canvas)
    font = load_font(14)
    miss = 0
    for i, it in enumerate(items):
        try:
            im = Image.open(it["path"]).convert("RGB")
        except Exception:
            miss += 1
            continue
        im = im.resize((tile_w, tile_h), Image.LANCZOS)
        x = (i % cols) * tile_w
        y = (i // cols) * (tile_h + bar)
        canvas.paste(im, (x, y))
        d.rectangle([x, y + tile_h, x + tile_w, y + tile_h + bar], fill=(10, 10, 12))
        tag = f"S{it['shot_id']:02d} {compact_tc(it['t'])}"
        if font:
            d.text((x + 6, y + tile_h + 7), tag, font=font, fill=(235, 235, 240))
        d.rectangle([x, y, x + tile_w - 1, y + tile_h + bar - 1], outline=(70, 70, 74))
    if miss:
        print(f"[extract] 警告：接触表 {path.name} 有 {miss}/{len(items)} 格缺图（关键帧读取失败）")
    canvas.save(path, "JPEG", quality=90, subsampling=1)
    return str(path)


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out_dir")
    ap.add_argument("--tile", type=int, default=440, help="接触表单格宽（px）")
    ap.add_argument("--overview-cols", type=int, default=5)
    ap.add_argument("--detail-cols", type=int, default=3)
    ap.add_argument("--detail-tile", type=int, default=560, help="细节接触表单格宽")
    ap.add_argument("--budget", type=int, default=0, help="覆盖全局关键帧预算")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    dirs = ensure_dirs(args.out_dir)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"[extract] 无法打开视频：{video}")
        return 2
    try:
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    except Exception:
        pass

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total and fps else 0.0

    # 分析采样率：短视频用原帧率，长视频降到 10fps，控制计算量
    sr = fps if duration <= 30 else min(fps, 10.0)
    step = max(1, int(round(fps / sr)))

    samples: list[dict] = []
    idx = 0
    prev_gray = None
    dt = step / fps if fps else 0.04
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                break
            gm = flow_small(frame)
            samples.append({
                "i": idx,
                "t": idx / fps,
                "sr": sr,
                "hist": hs_hist(frame),
                "thumb": gray_thumb(frame),
                "flow": flow_metrics(prev_gray, gm, dt),
            })
            prev_gray = gm
        idx += 1
    if not samples:
        print("[extract] 未读到任何帧")
        cap.release()
        return 3
    if not duration:
        duration = samples[-1]["t"] + 1.0 / fps

    min_shot = 0.20 if duration <= 6 else (0.35 if duration <= 60 else 0.6)
    shots = detect_shots(samples, min_shot)
    print(f"[extract] 时长 {duration:.2f}s · {fps:.2f}fps · 分析采样 {len(samples)} 帧 · "
          f"镜头数 {len(shots)}")

    budget = args.budget or budget_frames(duration, len(shots))

    # ---- 为每个镜头规划要保存的时间点（去重后）
    wanted: list[dict] = []           # 待保存：{t, shot_id}
    for sh in shots:
        n = pick_frame_indices(sh["end"] - sh["start"], duration, budget, len(shots))
        sh["planned_frames"] = n
        for t in even_times(sh["start"], sh["end"], n):
            wanted.append({"t": t, "shot_id": sh["shot_id"]})

    wanted.sort(key=lambda x: x["t"])
    print(f"[extract] 全局预算 {budget} 帧，计划保存 {len(wanted)} 帧")

    # ---- 抓帧：短视频单次顺序解码，长视频按帧号 seek
    frames: list[dict] = []
    sequential = total and total <= 8000
    if sequential:
        want_by_idx = {}
        for w in wanted:
            fi = int(round(w["t"] * fps))
            want_by_idx.setdefault(fi, w)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        i = 0
        while want_by_idx:
            ok = cap.grab()
            if not ok:
                break
            if i in want_by_idx:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    frames.append({"idx": i, "t": i / fps, "shot_id": want_by_idx[i]["shot_id"],
                                   "frame": frame})
                want_by_idx.pop(i, None)
            i += 1
    else:
        for w in wanted:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(w["t"] * fps)))
            ok, frame = cap.retrieve()
            if not ok:
                ok, frame = cap.read()
            if ok and frame is not None:
                frames.append({"idx": -1, "t": w["t"], "shot_id": w["shot_id"], "frame": frame})

    cap.release()
    if not frames:
        print("[extract] 抓帧失败")
        return 4

    # ---- 去重：逐镜进行，直方图与缩略图都几乎不变才丢帧
    #      并保证每镜至少保留 min(3, planned) 帧、且首尾必留（否则会丢掉动作信息）
    by_shot_raw: dict[int, list] = {}
    for f in frames:
        by_shot_raw.setdefault(f["shot_id"], []).append(f)

    kept: list[dict] = []
    for sid in sorted(by_shot_raw):
        group = sorted(by_shot_raw[sid], key=lambda x: x["t"])
        floor_n = min(3, len(group))
        dedup: list[dict] = []
        last_h = last_t = None
        for f in group:
            h = hs_hist(f["frame"])
            t = gray_thumb(f["frame"])
            f["hist"], f["thumb"] = h, t
            if dedup and last_h is not None and \
                    diff_hist(h, last_h) < 0.012 and diff_thumb(t, last_t) < 0.008:
                continue
            dedup.append(f)
            last_h, last_t = h, t
        # 兜底 1：镜头末帧必留（用于判断收尾画面与动作终态）
        if group:
            ids = {id(x) for x in dedup}
            if id(group[-1]) not in ids:
                f = group[-1]
                f.setdefault("hist", hs_hist(f["frame"]))
                f.setdefault("thumb", gray_thumb(f["frame"]))
                dedup.append(f)
                dedup.sort(key=lambda x: x["t"])
        # 兜底 2：比下限少时，从被丢弃的帧里均匀补回
        if len(dedup) < floor_n:
            chosen = {id(x) for x in dedup}
            restore = [f for f in group if id(f) not in chosen]
            need = floor_n - len(dedup)
            if restore and need > 0:
                step = max(1, len(restore) // need)
                picked = restore[::step][:need]
                for f in picked:
                    f.setdefault("hist", hs_hist(f["frame"]))
                    f.setdefault("thumb", gray_thumb(f["frame"]))
                dedup.extend(picked)
                dedup.sort(key=lambda x: x["t"])
        kept.extend(dedup)
    kept.sort(key=lambda x: x["t"])

    # ---- 落盘
    out_frames: list[dict] = []
    for f in kept:
        fr = f["frame"]
        h, w = fr.shape[:2]
        scale = min(1.0, MAX_SIDE / max(h, w))
        if scale < 1.0:
            fr = cv2.resize(fr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        ms = int(round(f["t"] * 1000))
        name = f"s{f['shot_id']:02d}_t{ms:07d}.jpg"
        p = dirs["frames"] / name
        ok_w = imwrite_unicode(p, fr, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok_w:
            print(f"[extract] 写盘失败：{p}")
            continue
        out_frames.append({
            "file": name,
            "path": f"frames/{name}",
            "t": round(f["t"], 3),
            "shot_id": f["shot_id"],
        })

    # ---- 回填每镜关键帧 + 运动量标签
    by_shot: dict[int, list] = {}
    for of in out_frames:
        by_shot.setdefault(of["shot_id"], []).append(of)
    for sh in shots:
        fl = by_shot.get(sh["shot_id"], [])
        fl.sort(key=lambda x: x["t"])
        sh["frames"] = fl
        sh["duration"] = round(sh["end"] - sh["start"], 3)
        sh["change_label"] = motion_label(sh["motion"])
        sh["move_label"] = move_label(sh.get("cam_move", 0.0), sh.get("cam_zoom", 0.0))
        sh["zoom_label"] = zoom_label(sh.get("cam_zoom", 0.0))
        sh["direction_label"] = direction_label(sh.get("cam_dx", 0.0), sh.get("cam_dy", 0.0))
        sh["local_label"] = local_label(sh.get("local_motion", 0.0))
        sh["motion_label"] = sh["move_label"]
        sh["hero_frames"] = {
            "first": fl[0]["file"] if fl else None,
            "mid": fl[len(fl) // 2]["file"] if fl else None,
            "last": fl[-1]["file"] if fl else None,
        }

    # ---- 接触表（JSON 只存相对 out 的路径，避免写入本机绝对路径）
    sheets: list[dict] = []
    ov_items = [{"path": str(dirs["frames"] / f["file"]), "t": f["t"],
                 "shot_id": f["shot_id"]} for f in out_frames]
    per = args.overview_cols * 4
    for si in range(0, len(ov_items), per):
        chunk = ov_items[si:si + per]
        name = f"overview_{si // per + 1:02d}.jpg"
        p = dirs["sheets"] / name
        r = make_sheet_quiet(chunk, args.overview_cols, args.tile, p)
        if r:
            sheets.append({"kind": "overview", "path": f"sheets/{name}",
                           "range": [chunk[0]["t"], chunk[-1]["t"]],
                           "n": len(chunk)})

    for sh in shots:
        fl = sh.get("frames", [])
        if not fl:
            continue
        take = fl if len(fl) <= 9 else [
            fl[round(i * (len(fl) - 1) / 8)] for i in range(9)
        ]
        items = [{"path": str(dirs["frames"] / f["file"]), "t": f["t"],
                  "shot_id": f["shot_id"]} for f in take]
        name = f"detail_s{sh['shot_id']:02d}.jpg"
        p = dirs["sheets"] / name
        r = make_sheet_quiet(items, args.detail_cols, args.detail_tile, p)
        if r:
            sh["detail_sheet"] = f"sheets/{name}"

    # ---- 全片运镜统计：便于模型横向比较各镜运动量的相对大小
    def stat(key):
        vals = [sh.get(key, 0.0) or 0.0 for sh in shots]
        if not vals:
            return {}
        return {"min": round(min(vals), 5),
                "median": round(float(np.median(vals)), 5),
                "max": round(max(vals), 5),
                "max_shot": shots[int(np.argmax(vals))]["shot_id"] if vals else None}

    payload = {
        "video": video.name,
        "duration": round(duration, 3),
        "fps": round(fps, 4),
        "analyze_sr": round(sr, 2),
        "shot_count": len(shots),
        "frame_budget": budget,
        "frames_saved": len(out_frames),
        "shots": shots,
        "sheets": sheets,
        "frames": out_frames,
        "motion_stats": {
            "cam_move": stat("cam_move"),
            "cam_zoom": stat("cam_zoom"),
            "local_motion": stat("local_motion"),
        },
    }
    write_json(dirs["out"] / "shots.json", payload)

    print(f"[extract] 去重后保存 {len(out_frames)} 帧 · 接触表 {len(sheets)} 张 overview "
          f"+ {len(shots)} 张 detail")
    for sh in shots:
        print(f"  S{sh['shot_id']:02d} {compact_tc(sh['start'])}–{compact_tc(sh['end'])} "
              f"{sh['duration']:.2f}s 帧{len(sh['frames']):2d} | "
              f"运镜 {sh['move_label']}/{sh['direction_label']}/{sh['zoom_label']} | "
              f"{sh['local_label']}")
    print(f"[extract] -> {dirs['out'] / 'shots.json'}")
    return 0


def make_sheet_quiet(items, cols, tile_w, path):
    try:
        return make_sheet(items, cols, tile_w, path, "")
    except Exception as e:
        print(f"[extract] 接触表生成失败 {path.name}: {e}")
        return None


if __name__ == "__main__":
    raise SystemExit(main())
