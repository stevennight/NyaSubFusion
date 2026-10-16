"""ffmpeg / ffprobe 相关：探测、抽音频、抽内封字幕、逐帧读取。视频和纯音频文件都支持。"""
import json
import shutil
import subprocess
from fractions import Fraction

import numpy as np

TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "arib_caption"}


def _need(tool):
    if not shutil.which(tool):
        raise RuntimeError(f"找不到 {tool}，请先安装 ffmpeg 并加入 PATH")


def probe(path):
    _need("ffprobe")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, check=True,
    ).stdout
    info = json.loads(out.decode("utf-8", "replace"))
    fmt = info.get("format", {})
    res = {
        "duration": float(fmt.get("duration") or 0),
        "start_time": float(fmt.get("start_time") or 0),
        "video": None,
        "has_audio": False,
        "subtitles": [],
    }
    sub_idx = 0
    for s in info.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and res["video"] is None and s.get("disposition", {}).get("attached_pic", 0) == 0:
            w, h = int(s["width"]), int(s["height"])
            dar = s.get("display_aspect_ratio")
            try:
                num, den = map(int, dar.split(":"))
                aspect = num / den if num and den else w / h
            except Exception:
                sar = s.get("sample_aspect_ratio", "1:1")
                try:
                    aspect = w / h * float(Fraction(sar.replace(":", "/")))
                except Exception:
                    aspect = w / h
            field = s.get("field_order", "progressive")
            res["video"] = {
                "index": s["index"], "width": w, "height": h, "aspect": aspect, "codec": s.get("codec_name", ""),
                "interlaced": field not in ("progressive", "unknown", None),
                "start_time": float(s.get("start_time") or res["start_time"]),
            }
        elif kind == "audio" and "audio_offset" not in res:
            res["has_audio"] = True
            # wav 没有时间戳，记录音频相对文件起点的偏移，ASR 时间要加回去
            res["audio_offset"] = max(0.0, float(s.get("start_time") or res["start_time"]) - res["start_time"])
        elif kind == "data":
            # 4K/8K 广播（ISDB-S3）的字幕是 ARIB-TTML，在 m2ts 里表现为 bin_data 数据流
            res.setdefault("data_streams", []).append(s["index"])
        elif kind == "subtitle":
            tags = s.get("tags", {}) or {}
            res["subtitles"].append({
                "sub_index": sub_idx, "codec": s.get("codec_name", ""),
                "lang": tags.get("language", ""), "title": tags.get("title", ""),
            })
            sub_idx += 1
    if res["video"] is None and not res["has_audio"]:
        raise RuntimeError(f"{path} 里既没有视频也没有音频")
    return res


def extract_audio(path, out_wav):
    _need("ffmpeg")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", out_wav],
        check=True,
    )


def extract_sub_stream(path, sub, out_path):
    """把内封文本字幕抽成 .ass。ARIB 字幕用 libaribcaption 解码。"""
    _need("ffmpeg")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if sub["codec"] == "arib_caption":
        cmd += ["-fix_sub_duration", "-c:s", "libaribcaption", "-sub_type", "ass", "-ignore_ruby", "1"]
    cmd += ["-i", path, "-map", f"0:s:{sub['sub_index']}", "-c:s", "ass", out_path]
    subprocess.run(cmd, check=True)


def dump_data_stream(path, index, out_path, probe_bytes=0):
    """原样导出数据流内容。probe_bytes>0 时只读开头一小段（用来判断是不是字幕）。"""
    _need("ffmpeg")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", path, "-map", f"0:{index}", "-c", "copy", "-f", "data"]
    if probe_bytes:
        cmd += ["-t", "900"]  # 只看前 15 分钟，空数据流不必扫完整个文件
    cmd.append("-")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    got = 0
    with open(out_path, "wb") as f:
        while True:
            buf = proc.stdout.read(1 << 16)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            if probe_bytes and got >= probe_bytes:
                break
    proc.stdout.close()
    proc.kill()
    proc.wait()
    return got


def frame_size(vinfo, height):
    h = min(height, vinfo["height"]) if height else vinfo["height"]
    h -= h % 2
    w = int(round(h * vinfo["aspect"]))
    w -= w % 2
    return w, h


def _frame_cmd(path, vinfo, fps, w, h, hw):
    if hw:
        # 显卡解码 + 显卡缩放，只把小图传回内存
        vf = ["yadif_cuda=mode=send_frame:deint=all"] if vinfo.get("interlaced") else []
        vf += [f"scale_cuda={w}:{h}:format=nv12", "hwdownload", "format=nv12", f"fps={fps}"]
        pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    else:
        vf = ["bwdif=mode=send_frame:deint=all"] if vinfo.get("interlaced") else []
        vf += [f"fps={fps}", f"scale={w}:{h}:flags=bicubic"]
        pre = []
    return ["ffmpeg", "-v", "quiet", *pre, "-i", path, "-map", f"0:{vinfo['index']}", "-an", "-sn",
            "-vf", ",".join(vf), "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]


def iter_frames(path, vinfo, fps, height, file_start=0.0, hwaccel=False):
    """按固定帧率产出 (时间秒, BGR 帧)。时间与字幕/音频同一时间轴（相对文件起点）。"""
    _need("ffmpeg")
    w, h = frame_size(vinfo, height)
    offset = max(0.0, vinfo.get("start_time", file_start) - file_start)
    size = w * h * 3
    for hw in ([True, False] if hwaccel else [False]):
        proc = subprocess.Popen(_frame_cmd(path, vinfo, fps, w, h, hw), stdout=subprocess.PIPE, bufsize=size * 4)
        i = 0
        try:
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                yield offset + i / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                i += 1
        finally:
            proc.stdout.close()
            proc.kill()
            proc.wait()
        if i > 0:
            return
        # 一帧都没出来：多半是显卡解码不支持这个格式，退回 CPU
    return


BROWSER_CODECS = {"h264", "vp8", "vp9", "av1"}


def browser_playable(path, info):
    """浏览器 / WebView2 能不能直接播放。不能的话要先转一个预览文件。"""
    v = info.get("video")
    return bool(v) and v.get("codec") in BROWSER_CODECS and str(path).lower().endswith((".mp4", ".m4v", ".webm", ".mov"))


def make_proxy(path, out_path, height=540):
    """转一个 H.264 预览文件给界面播放：先试显卡编码，失败再用 CPU。"""
    _need("ffmpeg")
    base = ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"scale=-2:{height}", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    for venc in (["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]):
        r = subprocess.run(base + venc + [str(out_path)], capture_output=True)
        if r.returncode == 0:
            return True
    raise RuntimeError(f"生成预览视频失败：{r.stderr.decode('utf-8', 'replace')[-300:]}")


def waveform_peaks(wav_path, per_second=50):
    """音频波形峰值（0~255），时间轴用。"""
    import wave

    with wave.open(str(wav_path), "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16)
    step = max(1, rate // per_second)
    usable = len(data) // step * step
    peaks = np.abs(data[:usable].astype(np.int32)).reshape(-1, step).max(axis=1)
    top = max(1, int(np.percentile(peaks, 99.5)) if len(peaks) else 1)
    return {"per_second": rate / step, "peaks": np.clip(peaks * 255 // top, 0, 255).astype(int).tolist()}
