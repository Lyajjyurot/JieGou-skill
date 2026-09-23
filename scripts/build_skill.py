# -*- coding: utf-8 -*-
"""build_skill — 把仓库根目录（扁平布局）打包/安装成标准 Skill 目录。

本仓库在开发时是扁平布局（SKILL.md / scripts/ / references/ 直接放在仓库根），
而 WorkBuddy 需要 skill 位于 `<skills_dir>/<skill-name>/` 下。本脚本负责这个转换，
跨平台、不需要符号链接。

用法：
    python build_skill.py --check                 # 只列出会被打包的文件
    python build_skill.py --install               # 安装到用户级技能目录
    python build_skill.py --install --dest <dir>  # 安装到指定技能目录（项目级）
    python build_skill.py --zip <输出目录>         # 产出可分发的 zip

「安装」与「分发」包含的文件集不同：
  - 安装：包含 references/output_template.md（若存在）—— 那是使用者自己的模板
  - 分发：排除 references/output_template.md —— 模板版权归属使用者，不应随包分发
两者都排除：.workbuddy/、outputs/、__pycache__、任何非白名单文件。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 白名单：这些路径模式才属于 skill 本体
INCLUDE_DIRS = ("scripts", "references")
INCLUDE_FILES = ("SKILL.md",)
# 额外资源目录（若存在则一并收录）
INCLUDE_EXTRA = ("assets",)
# 仅安装时包含、分发时排除
LOCAL_ONLY = ("references/output_template.md",)
# 一律排除
EXCLUDE_NAMES = {".workbuddy", "outputs", "__pycache__", ".git", ".venv", "venv", "dist"}


def skill_name() -> str:
    md = ROOT / "SKILL.md"
    if md.exists():
        m = re.search(r"^name:\s*(\S+)", md.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip().strip("\"'")
    return ROOT.name


def collect(dist: bool) -> list[Path]:
    """返回相对 ROOT 的文件列表。dist=True 时排除 LOCAL_ONLY。"""
    local_only = {p.replace("\\", "/") for p in LOCAL_ONLY}
    out: list[Path] = []
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.is_file():
            out.append(Path(f))
    for d in INCLUDE_DIRS + INCLUDE_EXTRA:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_NAMES for part in p.parts):
                continue
            if p.name.endswith(".pyc"):
                continue
            rel = p.relative_to(ROOT)
            if dist and rel.as_posix() in local_only:
                continue
            out.append(rel)
    return out


def cmd_check() -> int:
    ins = collect(dist=False)
    dis = collect(dist=True)
    name = skill_name()
    print(f"skill name : {name}")
    print(f"repo root  : {ROOT}")
    print("")
    print(f"[安装用] {len(ins)} 个文件")
    for p in ins:
        print(f"  {p.as_posix()}")
    skipped = [p for p in ins if p.as_posix() not in {x.as_posix() for x in dis}]
    print("")
    print(f"[分发用] {len(dis)} 个文件（比安装用少 {len(skipped)} 个）")
    if skipped:
        print("  分发时排除：")
        for p in skipped:
            print(f"    {p.as_posix()}  ← 使用者自有资源，不随包分发")
    return 0


def do_install(dest: Path | None) -> int:
    """就地覆盖安装。

    刻意**不做「先删后写」**：那样一旦删除环节失败（权限、回收站不可用、
    文件被占用等），会留下半残的技能目录。这里只覆盖白名单内的文件，
    再尽力清理多余文件，清理失败也只告警不中断。
    """
    name = skill_name()
    base = dest if dest else (Path.home() / ".workbuddy" / "skills")
    target = base / name
    files = collect(dist=False)

    target.mkdir(parents=True, exist_ok=True)

    copied, failed = 0, []
    for rel in files:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(ROOT / rel, dst)
            copied += 1
        except Exception as e:
            failed.append((rel.as_posix(), str(e)))
    print(f"[build] 已写入 {copied}/{len(files)} 个文件 → {target}")

    if failed:
        print("[build] 以下文件写入失败：")
        for f, e in failed:
            print(f"  - {f}: {e}")
        print("[build] 提示：若目标目录被其他程序占用，请关闭后重试；"
              "或改用 `--zip` 手动解压。")
        return 1

    # 尽力清理本技能目录中不属于白名单的残留文件（失败只告警）
    keep = {p.as_posix() for p in files}
    stale, undeletable = [], []
    for p in sorted(target.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(target).as_posix()
        if rel in keep or p.name.endswith(".pyc"):
            continue
        stale.append(rel)
        try:
            p.unlink()
        except Exception:
            undeletable.append(rel)
    if stale and not undeletable:
        print(f"[build] 已清理 {len(stale)} 个多余文件（{', '.join(stale)}）")
    elif undeletable:
        print("[build] 注意：以下残留文件无法自动清理，请手动删除：")
        for s in undeletable:
            print(f"  - {target / s}")

    print("[build] 若技能列表未刷新，重启一次 WorkBuddy 会话即可。")
    return 0


def do_zip(out_dir: Path) -> int:
    name = skill_name()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{name}.zip"
    files = collect(dist=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(ROOT / rel, f"{name}/{rel.as_posix()}")
    print(f"[build] 已打包 {len(files)} 个文件 → {zip_path}")
    print(f"[build] 体积 {zip_path.stat().st_size / 1024:.1f} KB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把扁平仓库打包/安装成标准 Skill 目录")
    ap.add_argument("--check", action="store_true", help="列出会被打包的文件")
    ap.add_argument("--install", action="store_true",
                    help="安装到技能目录（默认 ~/.workbuddy/skills/）")
    ap.add_argument("--dest", default=None, help="技能目录（配合 --install）")
    ap.add_argument("--zip", default=None, help="产出分发 zip 到指定目录")
    args = ap.parse_args()

    if not (ROOT / "SKILL.md").exists():
        print(f"[build] 在 {ROOT} 找不到 SKILL.md，请从仓库根目录运行本脚本")
        return 1

    did = False
    if args.check:
        did = True
        rc = cmd_check()
        if rc:
            return rc
    if args.install:
        did = True
        rc = do_install(Path(args.dest).resolve() if args.dest else None)
        if rc:
            return rc
    if args.zip:
        did = True
        rc = do_zip(Path(args.zip).resolve())
        if rc:
            return rc
    if not did:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
