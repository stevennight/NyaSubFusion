"""下载打包用的 ffmpeg（BtbN 共享库版：ffmpeg / ffprobe 共用一套 DLL，比静态版小一半）。

    .venv/Scripts/python packaging/fetch_ffmpeg.py

需要的能力：libaribcaption（ARIB 字幕）、NVENC / libx264（预览视频）、libass（压制）、scale_cuda（显卡解码缩放）。
"""
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ASSET = "ffmpeg-n8.1-latest-win64-gpl-shared-8.1"
URL = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{ASSET}.zip"
OUT = Path(__file__).resolve().parents[1] / "build" / "ffmpeg"


def main():
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
