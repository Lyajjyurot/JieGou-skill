# -*- coding: utf-8 -*-
"""analyze_shots — 逐镜客观视觉指标测算，写出 shots_metrics.json。

用法：
    python analyze_shots.py <out_dir>

输出每镜：
    调色板 dominant/accent/shadow/unique（真实 hex）
    亮度五数概括 / 暗部占比 / 高光占比 / 对比度 / 光比（级数）
    平均饱和度 / 色温 K / 单色判定
    颗粒噪声强度 / 锐度（拉普拉斯方差归一） / 前景背景锐度比（景深倾向）
    肤色占比（人物线索） / 画面细节密度 / 宽银幕黑边检测
    分离调色判定（暗部色相 vs 亮部色相 -> 青橙调等）
所有数值同时给出可直接引用的中文描述串，供写脚本时取值。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vds_common import (  # noqa: E402
    ensure_dirs, read_json, write_json, hex_of, kelvin_of_rgb, temp_label, imread_unicode,
)


# ------------------------------------------------------------------ 基础量
def luminance_stats(gray: np.ndarray) -> dict:
    f = gray.reshape(-1).astype(np.float32) / 255.0
    p5, p25, p50, p75, p95 = np.percentile(f, [5, 25, 50, 75, 95])
    dark = float((f < 0.18).mean())
    high = float((f > 0.85).mean())
    mid = float(((f >= 0.18) & (f <= 0.85)).mean())

    # 明暗比：亮部 15% 均值 / 暗部 15% 均值。
    # 分母设 5/255 下限，避免死黑区域导致比值爆炸（曾出现 3327:1 这类无意义值）；
    # 上限封顶 6 级（64:1）。这是「画面亮度比」，不等于布光光比。
    hi_px = f[f >= np.percentile(f, 85)]
    lo_px = f[f <= np.percentile(f, 15)]
    hi = float(hi_px.mean()) if hi_px.size else 0.0
    lo = max(float(lo_px.mean()) if lo_px.size else 0.0, 0.02)
    ratio = (hi + 1e-4) / lo
    stops = min(math.log2(max(ratio, 1.0)), 6.0)

    return {
        "clipped_shadow_ratio": round(float((f < 0.03).mean()), 4),
        "clipped_highlight_ratio": round(float((f > 0.97).mean()), 4),
        "mean": round(float(f.mean()), 4),
        "p5": round(float(p5), 4), "p25": round(float(p25), 4),
        "p50": round(float(p50), 4), "p75": round(float(p75), 4),
        "p95": round(float(p95), 4),
        "dark_ratio": round(dark, 4),
        "highlight_ratio": round(high, 4),
        "mid_ratio": round(mid, 4),
        "contrast": round(float(p95 - p5), 4),
        "light_ratio_stops": round(stops, 2),
    }


def contrast_label(c: float) -> str:
    if c < 0.30:
        return "低对比/柔和灰调"
    if c < 0.48:
        return "中低对比"
    if c < 0.62:
        return "中等对比"
    if c < 0.75:
        return "较高对比"
    return "高对比/硬调"


def light_ratio_label(stops: float) -> str:
    """stops 为画面亮度比的对数（已封顶 6 级）。标注为明暗比而非布光光比。"""
    r = 2 ** stops
    if stops < 1.0:
        return f"约 {r:.1f}:1（画面明暗接近，近纯环境光）"
    if stops < 2.0:
        return f"约 {r:.0f}:1（常规明暗层次）"
    if stops < 3.0:
        return f"约 {r:.0f}:1（暗部明显，主光造型感）"
    if stops < 4.5:
        return f"约 {r:.0f}:1（强明暗交错，暗部占比大）"
    return f"约 {r:.0f}:1 以上（极端明暗对比：死黑/纯白或舞台式布光）"


def saturation_stats(img: np.ndarray) -> dict:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1].astype(np.float32)
    v = hsv[:, :, 2].astype(np.float32)
    sat = s[s > 0]
    return {
        "sat_mean": round(float(s.mean() / 255.0), 4),
        "sat_p90": round(float((np.percentile(s, 90) if s.size else 0) / 255.0), 4),
        "vivid_ratio": round(float((s > 140).mean()), 4),
        "desat_ratio": round(float((s < 40).mean()), 4),
        "monochrome": bool(s.mean() < 20),
        "_v_mean": float(v.mean()),
    }


def sat_label(sat: float, mono: bool) -> str:
    if mono:
        return "近单色（黑白或极低饱和）"
    if sat < 0.12:
        return "极低饱和（去色/冷峻纪实）"
    if sat < 0.22:
        return "低饱和（电影感克制）"
    if sat < 0.34:
        return "中饱和（自然写实）"
    if sat < 0.48:
        return "偏高饱和（明亮商业感）"
    return "高饱和（浓烈/糖果色/特效感）"


def noise_grain(gray: np.ndarray) -> dict:
    g = gray
    h, w = g.shape[:2]
    y0, y1 = int(h * 0.15), int(h * 0.85)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    crop = g[y0:y1, x0:x1]
    if crop.size < 64:
        crop = g
    base = cv2.medianBlur(crop, 3).astype(np.float32)
    resid = crop.astype(np.float32) - base
    sigma = float(resid.std())
    # 高频能量：Sobel 幅值均值，衡量细节与颗粒总和
    gx = cv2.Sobel(crop, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(crop, cv2.CV_32F, 0, 1, ksize=3)
    hf = float(np.sqrt(gx ** 2 + gy ** 2).mean())
    return {"grain_sigma": round(sigma, 3),
            "hf_energy": round(hf, 3),
            "grain_score": round(min(1.0, sigma / 6.0), 4)}


def grain_label(g: dict) -> str:
    s = g["grain_score"]
    if s < 0.10:
        return "干净无颗粒（数码锐利/高码率）"
    if s < 0.22:
        return "细微颗粒（轻胶片感）"
    if s < 0.38:
        return "明显颗粒（胶片/复古质感）"
    return "强颗粒（粗粝/高感光/风格化噪点）"


def sharpness(gray: np.ndarray, img: np.ndarray) -> dict:
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    lapvar = float(lap.var())
    h, w = gray.shape[:2]
    lap_norm = lapvar / (max(h * w, 1) / (1280 * 720))
    # 前景/背景锐度比（浅景深倾向）：中心区 vs 四条边缘带。
    # 必须对「空间连续」的图像块算拉普拉斯方差，不能拿打散的边界像素做差分。
    ch, cw = int(h * 0.5), int(w * 0.5)
    cy, cx = h // 4, w // 4
    center = gray[cy:cy + ch, cx:cx + cw]
    band = max(4, int(min(h, w) * 0.12))
    strips = [gray[0:band, :], gray[h - band:h, :],
              gray[:, 0:band], gray[:, w - band:w]]
    c_lap = float(cv2.Laplacian(center, cv2.CV_64F).var()) if center.size > 64 else 0.0
    b_vals = [float(cv2.Laplacian(s, cv2.CV_64F).var()) for s in strips if s.size > 64]
    b_lap = float(np.mean(b_vals)) if b_vals else 0.0
    ratio = c_lap / max(b_lap, 1e-6)
    return {
        "lap_var": round(lapvar, 1),
        "lap_var_norm": round(lap_norm, 1),
        "center_lap_var": round(c_lap, 1),
        "border_lap_var": round(b_lap, 1),
        "fg_bg_sharpness_ratio": round(ratio, 2),
    }


def sharp_label(s: dict) -> str:
    v = s.get("lap_var_norm", 0.0)
    r = s.get("fg_bg_sharpness_ratio", 1.0)
    if v < 60:
        base = "整体偏软/失焦或强虚化"
    elif v < 250:
        base = "中等锐度"
    elif v < 900:
        base = "清晰锐利"
    else:
        base = "极高锐度/细节密集"
    # 中心/边缘清晰度比只是景深的弱线索：背景为平滑面（天空/墙壁）时也会偏高，
    # 必须结合读图确认，不可仅凭此判定光圈。
    if r > 3.0:
        base += "，中心明显锐于边缘（浅景深倾向，或主体居中而背景为平滑面）"
    elif r > 1.5:
        base += "，中心略锐于边缘"
    elif r < 0.6:
        base += "，边缘锐于中心（环境/背景细节更丰富，倾向深景深或主体偏侧）"
    else:
        base += "，画面清晰度分布均匀（深景深倾向）"
    return base


def skin_ratio(img: np.ndarray) -> float:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    m = (((h <= 25) | (h >= 160)) & (s >= 30) & (s <= 190) & (v >= 55))
    return round(float(m.mean()), 4)


def letterbox(gray: np.ndarray) -> dict:
    h, w = gray.shape[:2]
    def bar_rows(rows):
        return float((rows.mean(axis=1) < 22).mean())
    top = bar_rows(gray[:max(1, int(h * 0.12)), :])
    bot = bar_rows(gray[-max(1, int(h * 0.12)):, :])
    return {"top_black": round(top, 3), "bottom_black": round(bot, 3),
            "has_letterbox": bool(top > 0.7 and bot > 0.7)}


def split_tone(img: np.ndarray, gray: np.ndarray) -> dict:
    """分离调色：暗部与亮部各自的色相倾向。"""
    f = gray.astype(np.float32) / 255.0
    dark_m = f < 0.25
    light_m = f > 0.75
    b, g, r = [img[:, :, i].astype(np.float32) for i in range(3)]

    def hue_of(mask):
        if mask.sum() < 50:
            return None
        px = np.stack([b[mask].mean(), g[mask].mean(), r[mask].mean()])
        hsv = cv2.cvtColor(px.reshape(1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV)[0, 0]
        return int(hsv[0]) * 2, int(hsv[1])          # 转成 0-360 度

    dres = hue_of(dark_m)
    lres = hue_of(light_m)
    dh, ds = dres if dres else (None, None)
    lh, ls = lres if lres else (None, None)
    out = {"shadow_hue_deg": dh, "shadow_sat": ds,
           "highlight_hue_deg": lh, "highlight_sat": ls}
    if dh is not None and lh is not None:
        diff = abs(dh - lh)
        diff = min(diff, 360 - diff)
        if diff >= 90 and ds and ls and min(ds, ls) > 45:
            if 150 <= dh <= 230 and (lh <= 60 or lh >= 330):
                out["split_tone"] = "青橙分离调色（暗部青蓝 / 亮部暖橙，典型商业片与港片调性）"
            elif lh >= 180 and (dh <= 90 or dh >= 330):
                out["split_tone"] = "冷亮暖暗逆向分离调色（亮部偏冷 / 暗部偏暖）"
            else:
                out["split_tone"] = f"分离调色（暗部色相 {dh}° / 亮部色相 {lh}°）"
        elif (ds or 0) < 30 and (ls or 0) < 30:
            out["split_tone"] = "无分离调色（暗部亮部均近中性）"
    return out


# ------------------------------------------------------------------ 调色板
def palette(img: np.ndarray, k: int = 5) -> dict:
    small = cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA)
    data = small.reshape(-1, 3).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    try:
        _, labels, centers = cv2.kmeans(data, k, None, crit, 4, cv2.KMEANS_PP_CENTERS)
    except Exception:
        centers = np.array([[64, 64, 64]] * k, np.float32)
        labels = np.zeros((data.shape[0], 1), np.int32)
    labels = labels.reshape(-1)
    entries = []
    for ci in range(len(centers)):
        m = labels == ci
        w = float(m.mean())
        if w <= 0.005:
            continue
        bgr = centers[ci]
        rgb = (float(bgr[2]), float(bgr[1]), float(bgr[0]))
        hsv = cv2.cvtColor(np.uint8([[[bgr[0], bgr[1], bgr[2]]]]), cv2.COLOR_BGR2HSV)[0, 0]
        lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        entries.append({
            "rgb": [round(c) for c in rgb],
            "hex": hex_of(rgb),
            "weight": round(w, 3),
            "hue": int(hsv[0]) * 2,
            "sat": round(float(hsv[1]) / 255.0, 3),
            "lum": round(lum, 1),
        })
    if not entries:
        return {"dominant": "#808080", "accent": "#808080", "shadow": "#202020",
                "unique": "#808080", "entries": []}

    entries.sort(key=lambda e: -e["weight"])
    dominant = entries[0]
    shadow = min(entries, key=lambda e: e["lum"])
    cand = [e for e in entries if e["sat"] >= 0.25 and e is not dominant] or entries
    accent = max(cand, key=lambda e: e["sat"] * (0.4 + e["weight"]))

    def hue_gap(a, b):
        d = abs(a - b)
        return min(d, 360 - d)

    uniq_pool = [e for e in entries if e["sat"] >= 0.18] or entries
    unique = max(uniq_pool,
                 key=lambda e: hue_gap(e["hue"], dominant["hue"]) * 0.6
                 + e["sat"] * 0.9 + (0.4 - min(e["weight"], 0.4)))
    return {
        "dominant": dominant["hex"], "dominant_rgb": dominant["rgb"],
        "accent": accent["hex"], "accent_rgb": accent["rgb"],
        "shadow": shadow["hex"], "shadow_rgb": shadow["rgb"],
        "unique": unique["hex"], "unique_rgb": unique["rgb"],
        "entries": entries,
    }


# ------------------------------------------------------------------ 聚合
def analyze_image(path: str) -> dict:
    img = imread_unicode(path, cv2.IMREAD_COLOR)
    if img is None:
        return {}
    img = cv2.resize(img, (640, 360), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    b, g, r = [float(img[:, :, i].mean()) for i in range(3)]
    lum = luminance_stats(gray)
    sat = saturation_stats(img)
    gr = noise_grain(gray)
    sh = sharpness(gray, img)
    kelvin = kelvin_of_rgb((r, g, b))
    return {
        "lum": lum,
        "sat": sat,
        "grain": gr,
        "sharp": sh,
        "palette": palette(img),
        "skin_ratio": skin_ratio(img),
        "letterbox": letterbox(gray),
        "split_tone": split_tone(img, gray),
        "rgb_mean": [round(r), round(g), round(b)],
        "kelvin": kelvin,
        "kelvin_label": temp_label(kelvin),
        "labels": {
            "contrast": contrast_label(lum["contrast"]),
            "light_ratio": light_ratio_label(lum["light_ratio_stops"]),
            "saturation": sat_label(sat["sat_mean"], sat["monochrome"]),
            "grain": grain_label(gr),
            "sharpness": sharp_label(sh),
        },
    }


def mean_of(per_frame: list[dict], path: tuple) -> float | None:
    vals = []
    for d in per_frame:
        cur = d
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, (int, float)):
            vals.append(float(cur))
    return round(float(np.mean(vals)), 4) if vals else None


def mode_hex(per_frame: list[dict], key: str) -> str | None:
    """调色板角色取众数（按各帧直方图加权的近似：直接投票）。"""
    from collections import Counter
    c = Counter(d["palette"].get(key) for d in per_frame if d.get("palette"))
    c.pop(None, None)
    return c.most_common(1)[0][0] if c else None


def merge_shot(per_frame: list[dict]) -> dict:
    valid = [d for d in per_frame if d]
    if not valid:
        return {}
    nums = [
        ("lum_mean", ("lum", "mean")),
        ("lum_p5", ("lum", "p5")),
        ("lum_p50", ("lum", "p50")),
        ("lum_p95", ("lum", "p95")),
        ("dark_ratio", ("lum", "dark_ratio")),
        ("highlight_ratio", ("lum", "highlight_ratio")),
        ("contrast", ("lum", "contrast")),
        ("light_ratio_stops", ("lum", "light_ratio_stops")),
        ("sat_mean", ("sat", "sat_mean")),
        ("vivid_ratio", ("sat", "vivid_ratio")),
        ("grain_sigma", ("grain", "grain_sigma")),
        ("hf_energy", ("grain", "hf_energy")),
        ("lap_var_norm", ("sharp", "lap_var_norm")),
        ("fg_bg_sharpness_ratio", ("sharp", "fg_bg_sharpness_ratio")),
        ("center_lap_var", ("sharp", "center_lap_var")),
        ("border_lap_var", ("sharp", "border_lap_var")),
        ("skin_ratio", ("skin_ratio",)),
        ("kelvin", ("kelvin",)),
    ]
    out = {}
    for name, p in nums:
        v = mean_of(valid, p)
        if v is not None:
            out[name] = v
    out["palette"] = {
        "dominant": mode_hex(valid, "dominant"),
        "accent": mode_hex(valid, "accent"),
        "shadow": mode_hex(valid, "shadow"),
        "unique": mode_hex(valid, "unique"),
    }
    out["monochrome"] = bool(np.mean([d["sat"]["monochrome"] for d in valid]) > 0.5)
    out["letterbox"] = bool(np.mean([d["letterbox"]["has_letterbox"] for d in valid]) > 0.5)
    st = [d["split_tone"].get("split_tone") for d in valid if d["split_tone"].get("split_tone")]
    from collections import Counter
    out["split_tone"] = Counter(st).most_common(1)[0][0] if st else None
    out["labels"] = {
        "contrast": contrast_label(out.get("contrast", 0.45)),
        "light_ratio": light_ratio_label(out.get("light_ratio_stops", 1.0)),
        "saturation": sat_label(out.get("sat_mean", 0.25), out["monochrome"]),
        "grain": grain_label({"grain_score": min(1.0, out.get("grain_sigma", 1.5) / 6.0)}),
        "sharpness": sharp_label({"lap_var_norm": out.get("lap_var_norm", 200),
                                  "fg_bg_sharpness_ratio": out.get("fg_bg_sharpness_ratio", 1.0)}),
        "color_temp": temp_label(int(out.get("kelvin", 5600))),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    args = ap.parse_args()
    dirs = ensure_dirs(args.out_dir)
    shots_doc = read_json(dirs["out"] / "shots.json")

    result = {"shots": [], "per_frame_cache": {}}
    for sh in shots_doc["shots"]:
        per_frame = []
        for f in sh.get("frames", []):
            p = f["path"]
            d = analyze_image(p)
            if d:
                per_frame.append(d)
                result["per_frame_cache"][f["file"]] = d
        merged = merge_shot(per_frame)
        merged["shot_id"] = sh["shot_id"]
        merged["start"] = sh["start"]
        merged["end"] = sh["end"]
        merged["duration"] = sh["duration"]
        merged["motion"] = sh["motion"]
        merged["motion_label"] = sh["motion_label"]
        merged["n_frames_analyzed"] = len(per_frame)
        result["shots"].append(merged)

    # 全片聚合
    all_pf = list(result["per_frame_cache"].values())
    result["overall"] = merge_shot(all_pf)
    write_json(dirs["out"] / "shots_metrics.json", result)

    print(f"[metrics] 完成 {len(result['shots'])} 镜指标测算")
    o = result["overall"]
    if o:
        print(json.dumps({
            "整体对比度": o.get("contrast"), "暗部占比": o.get("dark_ratio"),
            "平均饱和": o.get("sat_mean"), "色温K": o.get("kelvin"),
            "颗粒": o.get("grain_sigma"), "调色板": o.get("palette"),
            "标签": o.get("labels"),
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
