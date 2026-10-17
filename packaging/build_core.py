"""构建内核：界面 → PyInstaller → 附带 ffmpeg → 放到 desktop/src-tauri/core/ 给 Tauri 打包。

    .venv/Scripts/python packaging/build_core.py [--skip-web]
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build" / "core"
TAURI_CORE = ROOT / "desktop" / "src-tauri" / "core"


def run(cmd, **kw):
    print("$", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def du_mb(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file()) / 2**20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-web", action="store_true", help="界面已经构建过")
    args = ap.parse_args()

    if not args.skip_web:
        run(["pnpm", "build"], cwd=ROOT / "web", shell=os.name == "nt")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
         "--workpath", str(ROOT / "build" / "pyinstaller"), str(ROOT / "packaging" / "nyasub-core.spec")])
    out = DIST / "nyasub-core"
    internal = out / "_internal"

    # 附带 ffmpeg / ffprobe：优先用 fetch_ffmpeg.py 下载的共享库版，没有就用 PATH 里的
    ff = internal / "ffmpeg"
    ff.mkdir(exist_ok=True)
    shared = ROOT / "build" / "ffmpeg" / "bin"
    if (shared / "ffmpeg.exe").exists():
        for f in shared.iterdir():
            shutil.copy2(f, ff / f.name)
    else:
        print("提示：没有找到 build/ffmpeg（先运行 packaging/fetch_ffmpeg.py 可以小一半），改用 PATH 里的 ffmpeg")
        for tool in ("ffmpeg", "ffprobe"):
            src = shutil.which(tool)
            if not src:
                raise SystemExit(f"找不到 {tool}，无法打包")
            shutil.copy2(src, ff / Path(src).name)
    print(f"内核：{out}（{du_mb(out):.0f} MB）")

    if TAURI_CORE.exists():
        shutil.rmtree(TAURI_CORE)
    shutil.copytree(out, TAURI_CORE)
    print(f"已复制到 {TAURI_CORE}")


if __name__ == "__main__":
    main()
