# -*- coding: utf-8 -*-
"""run_pipeline — 一条命令跑完解构流水线（probe → 抽帧 → 影像指标 → 音频 → 汇总）。

用法：
    python run_pipeline.py <video> [--out <dir>] [--name NAME] [--budget N]
    python run_pipeline.py --selftest            # 合成测试片跑通全链路，验证环境

默认输出目录：<video 所在目录>/vds_out/<视频名>/
所有子脚本都用当前解释器（sys.executable）启动，因此只需保证「跑本脚本的解释器」装好了依赖。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REQUIRED = ["cv2", "numpy", "PIL"]
REQUIREMENTS = "requirements.txt"


def require(mods: list[str]) -> list[str]:
    import importlib
    missing = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception:
            missing.append(m)
    return missing


def check_env() -> int:
    """返回 0 表示环境可用；否则打印修复指引并返回非零。"""
    missing = require(REQUIRED)
    if missing:
        print("缺少依赖：" + ", ".join(missing))
        print("")
        print("安装（在当前解释器下）：")
        print(f"  {sys.executable} -m pip install -r {HERE.parent / REQUIREMENTS}")
        print("")
        print("若某些包在镜像源上找不到（如报 No matching distribution found），换源重试，例如：")
        print(f"  {sys.executable} -m pip install -r {HERE.parent / REQUIREMENTS} "
              "-i https://mirrors.aliyun.com/pypi/simple/")
        return 1
    try:
        import imageio_ffmpeg  # noqa: F401
    except Exception:
        print("[warn] 未安装 imageio-ffmpeg：找不到内置 ffmpeg，"
              "音频提取与响度分析会失败（会尝试退回系统 ffmpeg）")
    return 0


def run(script: str, *args, timeout: int = 3600) -> int:
    cmd = [sys.executable, str(HERE / script)] + [str(a) for a in args]
    print(f"\n$ {' '.join(cmd)}")
    p = subprocess.run(cmd, timeout=timeout)
    return p.returncode


def run_steps(video: Path, out: Path, *, tile: int, budget: int,
              blocks: float, skip_audio: bool) -> int:
    out.mkdir(parents=True, exist_ok=True)
    steps: list[tuple[str, list]] = [
        ("probe_video.py", [video, out]),
        ("extract_keyframes.py", [video, out, "--tile", tile]),
        ("analyze_shots.py", [out]),
    ]
    if budget:
        steps[1] = ("extract_keyframes.py",
                    [video, out, "--tile", tile, "--budget", budget])
    if not skip_audio:
        steps.append(("analyze_audio.py", [video, out, "--blocks", blocks]))
    steps.append(("build_context.py", [out, "--video-name", video.stem]))

    for script, a in steps:
        rc = run(script, *a)
        if rc != 0:
            print(f"\n[pipeline] 步骤 {script} 失败（exit {rc}），中止。")
            return rc
    return 0


def selftest() -> int:
    """现场合成一段测试视频，跑完整流程，并校验产物是否齐备。"""
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    tmp = Path(tempfile.mkdtemp(prefix="vds_selftest_"))
    video = tmp / "selftest.mp4"
    out = tmp / "out"
    print(f"[selftest] ffmpeg: {ff}")
    print(f"[selftest] 临时目录: {tmp}")

    # 三段不同画面 + 含静默段的音轨，用于覆盖切镜/色彩/音频分段三类逻辑
    gen = [
        ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3",
         "-vf", "colorbalance=rs=0.3:bs=-0.25",
         "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(tmp / "c1.mp4")],
        ["-f", "lavfi", "-i", "smptebars=size=640x360:rate=25:duration=3",
         "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(tmp / "c2.mp4")],
        ["-f", "lavfi", "-i", "gradients=size=640x360:rate=25:duration=3:n=4",
         "-vf", "eq=saturation=0.35:contrast=1.3",
         "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(tmp / "c3.mp4")],
    ]
    for g in gen:
        if subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", *g]).returncode != 0:
            print("[selftest] 合成素材失败")
            return 1

    concat = tmp / "list.txt"
    concat.write_text("".join(f"file 'c{i}.mp4'\n" for i in (1, 2, 3)), encoding="utf-8")
    silent = tmp / "silent.m4a"
    if subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                       "-f", "concat", "-safe", "0", "-i", str(concat),
                       "-c", "copy", str(tmp / "v.mp4")]).returncode != 0:
        print("[selftest] 拼接失败")
        return 1
    if subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                       "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                       "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
                       "-f", "lavfi", "-i", "sine=frequency=180:duration=6",
                       "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1,volume=0.5",
                       "-c:a", "aac", str(silent)]).returncode != 0:
        print("[selftest] 合成音轨失败")
        return 1
    if subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                       "-i", str(tmp / "v.mp4"), "-i", str(silent),
                       "-c:v", "copy", "-c:a", "aac", "-shortest",
                       str(video)]).returncode != 0:
        print("[selftest] 合并音视频失败")
        return 1

    rc = run_steps(video, out, tile=440, budget=0, blocks=0.5, skip_audio=False)
    if rc != 0:
        print("[selftest] 流水线失败")
        return rc

    checks = {
        "meta.json": True, "shots.json": True, "shots_metrics.json": True,
        "audio.json": True, "context_pack.md": True,
    }
    bad = [k for k in checks if not (out / k).exists()]
    n_frames = len(list((out / "frames").glob("*.jpg"))) if (out / "frames").is_dir() else 0
    n_sheets = len(list((out / "sheets").glob("*.jpg"))) if (out / "sheets").is_dir() else 0
    if n_frames == 0:
        bad.append("frames/ 为空")
    if n_sheets == 0:
        bad.append("sheets/ 为空")

    print("\n" + "=" * 62)
    if bad:
        print("[selftest] 失败，以下项不正常：")
        for b in bad:
            print("  -", b)
        print(f"[selftest] 产物保留在 {tmp} 供排查")
        print("=" * 62)
        return 1
    print(f"[selftest] 通过：关键帧 {n_frames} 张 · 接触表 {n_sheets} 张 · 五份数据齐备")
    print(f"[selftest] 产物：{out}")
    print("=" * 62)
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        print("[selftest] 临时目录已清理")
    except Exception:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", help="视频路径（--selftest 时可省略）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--budget", type=int, default=0)
    ap.add_argument("--tile", type=int, default=440)
    ap.add_argument("--blocks", type=float, default=0.5)
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="合成测试视频跑通全链路，验证环境是否可用")
    args = ap.parse_args()

    rc = check_env()
    if rc:
        return rc

    if args.selftest:
        return selftest()
    if not args.video:
        ap.error("需要给出视频路径，或使用 --selftest")

    video = Path(args.video).resolve()
    if not video.exists():
        print(f"找不到视频：{video}")
        return 2

    name = args.name or video.stem
    out = Path(args.out).resolve() if args.out else (video.parent / "vds_out" / name)
    print(f"[pipeline] 视频：{video}")
    print(f"[pipeline] 输出：{out}")

    rc = run_steps(video, out, tile=args.tile, budget=args.budget,
                   blocks=args.blocks, skip_audio=args.skip_audio)
    if rc:
        return rc

    print("\n" + "=" * 62)
    print("[pipeline] 完成。下一步：")
    print(f"  1. 读 {out / 'context_pack.md'}")
    print(f"  2. 按 context_pack 的读图指引读 {out / 'sheets'}")
    print(f"  3. 套用模板写导演脚本，落到 {out}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
