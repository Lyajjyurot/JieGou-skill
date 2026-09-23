# -*- coding: utf-8 -*-
"""build_context — 把 meta/shots/metrics/audio 汇总成 context_pack.md（供模型读取）。

用法：
    python build_context.py <out_dir> [--video-name NAME]

context_pack.md 是模型生成导演脚本时的主要输入：
    素材概况 → 全片客观画像 → 镜头总表 → 逐镜明细 → 接触表清单 → 读图指引 → 音频时间轴
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vds_common import ensure_dirs, read_json, tc  # noqa: E402


def safe(d, *keys, default="—"):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def audio_segments_for(segs: list[dict], a: float, b: float) -> list[dict]:
    out = []
    for s in segs:
        if s["end"] > a and s["start"] < b:
            out.append(s)
    return out


def bar(value: float | None, lo: float, hi: float, width: int = 18) -> str:
    if value is None:
        return "—"
    t = 0.0 if hi <= lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
    n = int(round(t * width))
    return "█" * n + "·" * (width - n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--video-name", default=None)
    args = ap.parse_args()

    dirs = ensure_dirs(args.out_dir)
    out = dirs["out"]
    meta = read_json(out / "meta.json")
    shots_doc = read_json(out / "shots.json")
    metrics = read_json(out / "shots_metrics.json")
    try:
        audio = read_json(out / "audio.json")
    except Exception:
        audio = {"has_audio": False}

    name = args.video_name or meta.get("file_name", "video")
    m_by_id = {m["shot_id"]: m for m in metrics.get("shots", [])}
    overall = metrics.get("overall") or {}
    ol = overall.get("labels") or {}
    pal = overall.get("palette") or {}
    segs = audio.get("segments") or []
    shots = shots_doc["shots"]

    L: list[str] = []
    A = L.append

    A(f"# 视频解构上下文包 · {name}")
    A("")
    A("> 本文件由 video-to-director-script 流水线自动生成，是撰写导演脚本的**主要事实来源**。")
    A("> 所有 hex、色温、光比、颗粒、响度、BPM、卡点率均为程序实测值，可直接引用；")
    A("> 画面语义、人物动作、情绪、叙事由模型读取关键帧后判断。")
    A("")

    # ---------------------------------------------------------------- 0
    A("## 0. 素材概况")
    A("")
    A(f"- 文件：`{meta.get('file_name') or meta.get('file')}`")
    A(f"- 时长：**{tc(meta.get('duration', 0))}**（{meta.get('duration')}s）")
    A(f"- 画面：{meta.get('width')}×{meta.get('height')} · {meta.get('fps')}fps · "
      f"档位「{meta.get('tier')}」")
    vs = meta.get("video_stream") or {}
    if vs:
        A(f"- 视频流：{vs.get('codec','?')} · 码率 {vs.get('bitrate_kbps','?')} kbps"
          + (f" · {vs.get('pix_fmt','')}" if vs.get('pix_fmt') else ""))
    if meta.get("rotation"):
        A(f"- 旋转元数据：{meta['rotation']}°（已按显示方向解码）")
    ast = meta.get("audio_stream") or {}
    if ast:
        A(f"- 音轨：{ast.get('codec','?')} · {ast.get('sample_rate','?')}Hz · "
          f"{ast.get('layout','?')} · {ast.get('bitrate_kbps','?')} kbps")
    else:
        A("- 音轨：**无**")
    if meta.get("subtitle_tracks"):
        A(f"- 内嵌字幕轨：{len(meta['subtitle_tracks'])} 条（可用于补齐对白原文）")
    A(f"- 文件体积：{meta.get('size_mb')} MB")
    A(f"- 切分结果：**{shots_doc['shot_count']} 个镜头** · 抽样 {shots_doc['frames_saved']} 帧"
      f"（预算 {shots_doc['frame_budget']}，分析采样 {shots_doc['analyze_sr']}fps）")
    A("")

    # ---------------------------------------------------------------- 1
    A("## 1. 全片客观画像（程序实测）")
    A("")
    A("### 1.1 影像")
    A("")
    A("| 维度 | 数值 | 判定 |")
    A("|---|---|---|")
    A(f"| 调色板 dominant | `{pal.get('dominant','—')}` | 画面占比最大的基调色 |")
    A(f"| 调色板 accent | `{pal.get('accent','—')}` | 高饱和强调色 |")
    A(f"| 调色板 shadow | `{pal.get('shadow','—')}` | 最暗簇，暗部基调 |")
    A(f"| 调色板 unique | `{pal.get('unique','—')}` | 与主色相对立的异质色（视觉钩子） |")
    A(f"| 平均亮度 | {safe(overall,'lum_mean')} | p5 {safe(overall,'lum_p5')} / p50 "
      f"{safe(overall,'lum_p50')} / p95 {safe(overall,'lum_p95')} |")
    A(f"| 对比度 | {safe(overall,'contrast')} | {ol.get('contrast','—')} |")
    A(f"| 光比 | {safe(overall,'light_ratio_stops')} 级 | {ol.get('light_ratio','—')} |")
    A(f"| 暗部占比 | {safe(overall,'dark_ratio')} | {bar(overall.get('dark_ratio'),0,1)} |")
    A(f"| 高光占比 | {safe(overall,'highlight_ratio')} | {bar(overall.get('highlight_ratio'),0,1)} |")
    A(f"| 平均饱和度 | {safe(overall,'sat_mean')} | {ol.get('saturation','—')} |")
    A(f"| 色温 | {safe(overall,'kelvin')} K | {ol.get('color_temp','—')} |")
    A(f"| 颗粒 | σ={safe(overall,'grain_sigma')} | {ol.get('grain','—')} |")
    A(f"| 锐度（归一拉普拉斯方差） | {safe(overall,'lap_var_norm')} | {ol.get('sharpness','—')} |")
    A(f"| 前景/背景锐度比 | {safe(overall,'fg_bg_sharpness_ratio')} | 中心/边缘拉普拉斯方差比，>3 中心显著锐（景深浅的弱线索） |")
    if overall.get("split_tone"):
        A(f"| 分离调色 | — | {overall['split_tone']} |")
    if overall.get("monochrome"):
        A("| 单色判定 | — | **近单色/黑白**，色彩语言以明度层次为主 |")
    if overall.get("letterbox"):
        A("| 画幅 | — | 检测到上下黑边（宽银幕感/信箱化） |")
    A("")

    A("### 1.2 声音")
    A("")
    if audio.get("has_audio"):
        lo = audio.get("loudness") or {}
        s = audio.get("summary") or {}
        b = audio.get("beats") or {}
        ca = audio.get("cut_alignment") or {}
        A("| 维度 | 数值 | 说明 |")
        A("|---|---|---|")
        A(f"| 积分响度 | {lo.get('integrated_lufs','—')} LUFS | 广播标准 −14 LUFS（流媒体）/ −23（EBU） |")
        A(f"| 响度范围 LRA | {lo.get('lra_lu','—')} LU | 数字越大动态起伏越强 |")
        A(f"| 真峰值 | {lo.get('true_peak_dbfs','—')} dBFS | 接近 0 表示母带压得满 |")
        A(f"| 平均/峰值音量 | {lo.get('mean_volume_db','—')} / {lo.get('max_volume_db','—')} dB | volumedetect |")
        A(f"| 疑似人声占比 | {s.get('speech_like_ratio','—')} | 基于 300–3400Hz 占比+过零率+谱质心 |")
        A(f"| 疑似音乐占比 | {s.get('music_like_ratio','—')} | 基于低频能量+谱平坦度+onset |")
        A(f"| 静音占比 | {s.get('silence_ratio','—')} | 低于 −45dBFS 的时长比例 |")
        A(f"| BPM | {b.get('bpm','—')} | 自相关测速，置信 {b.get('confidence','—')} |")
        A(f"| 卡点率 | {ca.get('rate','—')} | {ca.get('on_beat','—')}/{ca.get('cut_count','—')} "
          f"个切换点落在节拍 ±{ca.get('tolerance_s','—')}s 内 |")
        A("")
        if audio.get("sparkline"):
            A(f"**响度曲线**（每格 {audio.get('sparkline_step_s')}s，低位→高位）：")
            A("")
            A("```")
            A(audio["sparkline"])
            A("```")
            A("")
        bp = audio.get("band_profile") or {}
        if bp:
            A("**频段能量画像**（占比）：")
            A("")
            A("| " + " | ".join(bp.keys()) + " |")
            A("|" + "---|" * len(bp))
            A("| " + " | ".join(f"{v:.3f}" for v in bp.values()) + " |")
            A("")
    else:
        A("本素材无音轨。声音维度全部留空，脚本中的 `[声音]`/`[对白]` 字段应标注「无音轨」。")
        A("")

    # ---------------------------------------------------------------- 2
    A("## 2. 镜头总表")
    A("")
    A("| # | 时间码 | 时长s | 帧数 | 运镜 | 位移方向 | 推拉 | 主体动作 | dominant | accent | 对比 | 光比 | 色温K | 饱和 | 颗粒σ | 锐度 | 肤色% | 音频段 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for sh in shots:
        m = m_by_id.get(sh["shot_id"], {})
        p = m.get("palette") or {}
        seg = audio_segments_for(segs, sh["start"], sh["end"])
        seg_txt = " → ".join(x["kind"] for x in seg) if seg else "—"
        A(f"| {sh['shot_id']} | {tc(sh['start'])}–{tc(sh['end'])} | {sh['duration']:.2f} | "
          f"{len(sh.get('frames',[]))} | {sh.get('move_label','—')} | "
          f"{sh.get('direction_label','—')} | {sh.get('zoom_label','—')} | "
          f"{sh.get('local_label','—')} | "
          f"`{p.get('dominant','—')}` | `{p.get('accent','—')}` | "
          f"{m.get('contrast','—')} | {m.get('light_ratio_stops','—')} | "
          f"{m.get('kelvin','—')} | {m.get('sat_mean','—')} | {m.get('grain_sigma','—')} | "
          f"{m.get('lap_var_norm','—')} | {m.get('skin_ratio','—')} | {seg_txt} |")
    A("")
    ms = shots_doc.get("motion_stats") or {}
    if ms:
        A("**全片运动量统计**（用于横向比较各镜相对大小；`cam_move` 单位=帧宽/秒，"
          "`cam_zoom` 单位=每秒尺度变化率，`local_motion` 为 P90 残差流速）：")
        A("")
        A("| 指标 | min | 中位数 | max | 最大者 |")
        A("|---|---|---|---|---|")
        label = {"cam_move": "运镜位移速度", "cam_zoom": "推拉速度",
                 "local_motion": "主体动作强度"}
        for k, v in ms.items():
            A(f"| {label.get(k,k)} | {v.get('min'):.5f} | {v.get('median'):.5f} | "
              f"{v.get('max'):.5f} | 镜头 {v.get('max_shot')} |")
        A("")

    # ---------------------------------------------------------------- 3
    A("## 3. 逐镜明细")
    A("")
    for sh in shots:
        m = m_by_id.get(sh["shot_id"], {})
        p = m.get("palette") or {}
        lab = m.get("labels") or {}
        A(f"### 镜头 {sh['shot_id']} · {tc(sh['start'])}–{tc(sh['end'])} "
          f"（{sh['duration']:.2f}s）· {sh['motion_label']}")
        A("")
        A(f"- **detail 接触表**：`{sh.get('detail_sheet','—')}`")
        fl = sh.get("frames", [])
        A(f"- **关键帧**（{len(fl)} 帧）：" + " · ".join(
            f"`{f['file']}`({tc(f['t'])})" for f in fl))
        A(f"- **调色板**：dominant `{p.get('dominant','—')}` · accent `{p.get('accent','—')}`"
          f" · shadow `{p.get('shadow','—')}` · unique `{p.get('unique','—')}`")
        A(f"- **光影**：对比 {m.get('contrast','—')}（{lab.get('contrast','—')}）· "
          f"光比 {m.get('light_ratio_stops','—')}级（{lab.get('light_ratio','—')}）· "
          f"暗部 {m.get('dark_ratio','—')} · 高光 {m.get('highlight_ratio','—')} · "
          f"色温 {m.get('kelvin','—')}K（{lab.get('color_temp','—')}）")
        if m.get("split_tone"):
            A(f"- **分离调色**：{m['split_tone']}")
        A(f"- **材质**：颗粒 σ{m.get('grain_sigma','—')}（{lab.get('grain','—')}）· "
          f"锐度 {m.get('lap_var_norm','—')}（{lab.get('sharpness','—')}）· "
          f"高频能量 {m.get('hf_energy','—')}")
        A(f"- **饱和**：{m.get('sat_mean','—')}（{lab.get('saturation','—')}）· "
          f"高饱和像素占比 {m.get('vivid_ratio','—')}")
        A(f"- **人物线索**：肤色像素占比 {m.get('skin_ratio','—')}"
          + ("（画面中大概率存在人物近景/特写）" if (m.get("skin_ratio") or 0) > 0.06 else ""))
        A(f"- **运镜（光流实测）**：{sh.get('move_label','—')} · {sh.get('direction_label','—')} · "
          f"{sh.get('zoom_label','—')} | 位移速度 {sh.get('cam_move',0):.4f} 帧宽/秒（水平分量 "
          f"{sh.get('cam_dx',0):+.4f}，垂直 {sh.get('cam_dy',0):+.4f}）· 推拉速度 "
          f"{sh.get('cam_zoom',0):+.4f} /秒 · 光流有效样本 {sh.get('n_flow_samples','—')}")
        A(f"- **主体动作（P90 残差光流）**：{sh.get('local_label','—')} "
          f"（{sh.get('local_motion',0):.4f} 帧宽/秒）")
        A(f"- **容器差异**：{sh['motion']:.4f} · p95 {sh['motion_p95']:.4f} · {sh.get('change_label','—')}"
          "（直方图+缩略图综合差异，仅用于辅助判断切点强度）")
        if m.get("letterbox"):
            A("- **画幅**：存在上下黑边（宽银幕化）")
        seg = audio_segments_for(segs, sh["start"], sh["end"])
        if seg:
            A("- **音频**：" + " | ".join(
                f"{tc(x['start'])}–{tc(x['end'])} {x['kind']}({x['conf']})" for x in seg))
        A("")

    # ---------------------------------------------------------------- 4
    A("## 4. 接触表清单（按时间顺序）")
    A("")
    for s in shots_doc.get("sheets", []):
        A(f"- `{s['path']}` — overview #{s['n']} 帧 · {tc(s['range'][0])}–{tc(s['range'][1])}")
    for sh in shots:
        if sh.get("detail_sheet"):
            A(f"- `{sh['detail_sheet']}` — 镜头 {sh['shot_id']} 细节表 · "
              f"{tc(sh['start'])}–{tc(sh['end'])}")
    A("")

    # ---------------------------------------------------------------- 5
    A("## 5. 读图指引")
    A("")
    A("1. 先按顺序读全部 `overview_*.jpg`：获得整体内容、场景变化、画面情绪走向。")
    A("2. 再逐镜读 `detail_sXX.jpg`：判定景别、机位、运镜、人物动作、表演细节。")
    A("3. 需要确认某一瞬间的细节时，直接读该镜 `frames/` 下的单帧 JPEG。")
    A("4. 镜头数量多时可分批读，但**每个镜头至少读过它自己的 detail 接触表**再落笔。")
    A("5. 图表中烧入的时间码格式为 `S镜号 秒数`，用于把画面与时间轴对齐。")
    A("")

    # ---------------------------------------------------------------- 6
    A("## 6. 音频时间轴")
    A("")
    if audio.get("has_audio"):
        A("### 6.1 分段")
        A("")
        A("| 起 | 止 | 时长 | 判定 | 置信 |")
        A("|---|---|---|---|---|")
        for s in segs:
            A(f"| {tc(s['start'])} | {tc(s['end'])} | {s['duration']:.2f} | {s['kind']} | {s['conf']} |")
        A("")
        A("### 6.2 静默留白区间")
        A("")
        sil = audio.get("silences") or []
        if sil:
            for s in sil:
                A(f"- {tc(s['start'])} – {tc(s['end'])}（{s['duration']:.2f}s）")
        else:
            A("- 未检测到 ≥0.15s 的静默段（音频连续铺满）")
        A("")
        b = audio.get("beats") or {}
        if b.get("times"):
            A("### 6.3 节拍网格")
            A("")
            A(f"- BPM **{b.get('bpm')}** · 周期 {b.get('period')}s · "
              f"共 {len(b['times'])} 个拍点 · 置信 {b.get('confidence')}")
            A(f"- 拍点（前 60 个）：" + ", ".join(f"{t:.2f}" for t in b["times"][:60]))
            A("")
        ca = audio.get("cut_alignment") or {}
        if ca.get("per_cut"):
            A("### 6.4 卡点分析")
            A("")
            A(f"- 共 {ca['cut_count']} 个切换点，{ca['on_beat']} 个踩在节拍上，"
              f"**卡点率 {ca['rate']}**（容差 ±{ca['tolerance_s']}s）")
            A("")
            A("| 切换时间 | 最近拍点 | 偏差s | 卡点 |")
            A("|---|---|---|---|")
            for c in ca["per_cut"]:
                A(f"| {tc(c['t'])} | {c['nearest_beat']:.2f} | {c['offset']:+.3f} | "
                  f"{'✓' if c['on_beat'] else '✗'} |")
            A("")
    else:
        A("无音轨，本节留空。")
        A("")

    A("---")
    A("")
    A("**输出模板与字段规范见** `references/script_template.md`；")
    A("**指标→术语的映射规则见** `references/analysis_rules.md`；")
    A("**风格参考库见** `references/style_lock_library.md`。")

    text = "\n".join(L)
    path = out / "context_pack.md"
    path.write_text(text, encoding="utf-8")
    print(f"[context] 已写出 {path}（{len(text)} 字符，约 {len(text)//3} token 量级）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
