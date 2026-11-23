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


def _fetch_macos():
    bin_dir = OUT / "bin"
    shutil.rmtree(bin_dir, ignore_errors=True)
    bin_dir.mkdir(parents=True)
    for tool in ("ffmpeg", "ffprobe"):
        src = shutil.which(tool) or next((str(p) for p in (Path("/opt/homebrew/bin") / tool, Path("/usr/local/bin") / tool) if p.exists()), None)
        if not src:
            raise SystemExit(f"找不到 {tool}，请先安装（例如 brew install ffmpeg）")
        dst = bin_dir / tool
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | 0o755)
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
    print(f"macOS ffmpeg 已就绪：{bin_dir}")


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
