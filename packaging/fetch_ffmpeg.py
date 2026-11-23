"""准备打包用的 ffmpeg：
- Windows：下载 BtbN 共享库版（ffmpeg / ffprobe 共用一套 DLL，比静态版小一半）。
- macOS：检出本机 ffmpeg / ffprobe（优先 PATH / Homebrew）并校验 VideoToolbox / libass / libaribcaption 能力。

    .venv/Scripts/python packaging/fetch_ffmpeg.py   # Windows
    .venv/bin/python packaging/fetch_ffmpeg.py       # macOS
"""
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ASSET = "ffmpeg-n8.1-latest-win64-gpl-shared-8.1"
URL = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{ASSET}.zip"
OUT = Path(__file__).resolve().parents[1] / "build" / "ffmpeg"


def _macos_rpaths(binary: Path) -> list[Path]:
    out = subprocess.run(["otool", "-l", str(binary)], capture_output=True, text=True).stdout
    rpaths, lines = [], out.splitlines()
    for i, line in enumerate(lines):
        if "cmd LC_RPATH" in line and i + 2 < len(lines):
            m = lines[i + 2].strip()
            if m.startswith("path "):
                p = m[5:].split(" (offset", 1)[0].strip()
                if p and not p.startswith("@"):
                    rpaths.append(Path(p))
    return rpaths


def _bundle_macos_dylibs(bin_dir: Path, roots: list[Path]):
    """把 ffmpeg / ffprobe 依赖的非系统 .dylib 递归复制到 bin_dir（配合 @executable_path rpath 可独立运行）。"""
    rpaths: list[Path] = []
    for b in roots:
        for p in _macos_rpaths(b):
            if p not in rpaths:
                rpaths.append(p)
    for extra in (Path("/opt/homebrew/lib"), Path("/usr/local/lib")):
        if extra.is_dir() and extra not in rpaths:
            rpaths.append(extra)

    seen: set[str] = set()
    queue = list(roots)
    while queue:
        cur = queue.pop(0)
        out = subprocess.run(["otool", "-L", str(cur)], capture_output=True, text=True).stdout
        for line in out.splitlines()[1:]:
            dep = line.strip().split(" (", 1)[0].strip()
            if not dep or dep.startswith(("/System/", "/usr/lib/")):
                continue
            name = Path(dep).name
            if name in seen:
                continue
            src = None
            if dep.startswith("@rpath/"):
                rel = dep[len("@rpath/"):]
                for rp in rpaths:
                    cand = rp / rel
                    if cand.exists():
                        src = cand
                        break
            elif Path(dep).exists():
                src = Path(dep)
            if not src:
                continue
            seen.add(name)
            dst = bin_dir / name
            shutil.copy2(src, dst)
            dst.chmod(dst.stat().st_mode | 0o755)
            queue.append(dst)


def _fetch_macos():
    bin_dir = OUT / "bin"
    shutil.rmtree(bin_dir, ignore_errors=True)
    bin_dir.mkdir(parents=True)
    copied = []
    for tool in ("ffmpeg", "ffprobe"):
        src = shutil.which(tool) or next((str(p) for p in (Path("/opt/homebrew/bin") / tool, Path("/usr/local/bin") / tool) if p.exists()), None)
        if not src:
            raise SystemExit(f"找不到 {tool}，请先安装（例如 brew install ffmpeg）")
        dst = bin_dir / tool
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | 0o755)
        copied.append(dst)
    _bundle_macos_dylibs(bin_dir, copied)
    ff = str(bin_dir / "ffmpeg")
    dec = subprocess.run([ff, "-hide_banner", "-decoders"], capture_output=True, text=True).stdout
    enc = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    flt = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    missing = []
    if "h264_videotoolbox" not in enc or "hevc_videotoolbox" not in enc:
        missing.append("VideoToolbox 硬件编码器")
    if "subtitles" not in flt:
        missing.append("libass (subtitles 滤镜)")
    if missing:
        raise SystemExit("当前 ffmpeg 缺少必需能力：" + "、".join(missing))
    if "libaribcaption" not in dec:
        print("警告：当前 ffmpeg 未编译 libaribcaption（日本广播 ARIB 字幕将不可用，普通字幕/压制不受影响）")
    dylibs = sum(1 for p in bin_dir.iterdir() if p.suffix == ".dylib")
    print(f"macOS ffmpeg 已就绪：{bin_dir}（含 {dylibs} 个附属 dylib）")


def main():
    if sys.platform == "darwin":
        return _fetch_macos()
    OUT.mkdir(parents=True, exist_ok=True)
    zpath = OUT / f"{ASSET}.zip"
    if not zpath.exists():
        print(f"下载 {URL}")
        urllib.request.urlretrieve(URL, zpath)
    bin_dir = OUT / "bin"
    shutil.rmtree(bin_dir, ignore_errors=True)
    bin_dir.mkdir()
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if "/bin/" in info.filename and name and name != "ffplay.exe":
                with z.open(info) as src, open(bin_dir / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    out = subprocess.run([str(bin_dir / "ffmpeg.exe"), "-hide_banner", "-decoders"], capture_output=True, text=True).stdout
    if "libaribcaption" not in out:
        raise SystemExit("这个 ffmpeg 不支持 ARIB 字幕（缺 libaribcaption）")
    print(f"ffmpeg 已就绪：{bin_dir}")


if __name__ == "__main__":
    main()
