"""视频输出：把字幕封装进 MKV（附带用到的字体），或者压制成硬字幕 MP4。

- 字体：从 ASS 里找出每种字体（含粗体）实际用到的字，默认只嵌入这些字的子集（几百 KB），
  播放端不用装字体；字体许可写明“不允许嵌入”的跳过，“不允许取子集”的嵌入完整字体。
- 封装：视频 / 音频原样复制，不重新编码，几秒到几十秒；字幕轨标上语言和名字，第一条设为默认。
- 压制：用 libass 把 ASS 画到画面上再编码；先试显卡编码（NVENC），不行再用 CPU（x264）。
  隔行视频先反交错，非方形像素先缩放成方形像素（不然字会被压扁）。
- 进度用 ffmpeg -progress 读出来，推 {"type": "progress"} 事件；取消时直接结束 ffmpeg。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .. import env
from ..sources.media import Stopped  # noqa: F401（取消时抛出，pipeline 里按 video.Stopped 捕获）
from . import fonts

MUX_SUFFIX = ".sub.mkv"
BURN_SUFFIX = ".hardsub.mp4"
OUTPUT_SUFFIXES = (MUX_SUFFIX, BURN_SUFFIX)

LANG3 = {"ja": "jpn", "zh": "chi", "en": "eng", "ko": "kor", "fr": "fre", "de": "ger", "es": "spa", "ru": "rus",
         "it": "ita", "pt": "por", "th": "tha", "vi": "vie", "id": "ind"}
MKV_COPY_SUBS = {"subrip", "ass", "ssa", "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "webvtt"}
MKV_CONVERT_SUBS = {"mov_text": "srt", "text": "srt"}
MP4_COPY_AUDIO = {"aac", "mp3", "ac3", "eac3"}
MKV_CONVERT_AUDIO = {"aac_latm"}  # 日本广播的 LATM 封装 AAC：放进 MKV 很多播放器不认，转成普通 AAC
ENCODERS = {
    "h264_nvenc": lambda q: ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0"],
    "hevc_nvenc": lambda q: ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
                             "-tag:v", "hvc1"],
    "libx264": lambda q: ["-c:v", "libx264", "-preset", "medium", "-crf", str(q)],
    "libx265": lambda q: ["-c:v", "libx265", "-preset", "medium", "-crf", str(q), "-tag:v", "hvc1"],
}


def lang3(code):
    return LANG3.get((code or "").split("-")[0].lower(), "und")


def is_output(path):
    """是不是自己输出的视频（加文件夹进队列时跳过）。"""
    return str(path).lower().endswith(OUTPUT_SUFFIXES)


# ---------------- 字体 ----------------
_TAG = re.compile(r"\{([^}]*)\}")


def ass_fonts(path):
    """ASS 里用到的字体：{(字体名, 是否粗体): 用到的字}。按样式和行内 \\fn \\b \\r 计算。"""
    styles, fmt, out = {}, None, {}
    section = ""
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("["):
            section, fmt = line.lower(), None
            continue
        key, _, rest = line.partition(":")
        if key == "Format":
            fmt = [x.strip().lower() for x in rest.split(",")]
        elif key == "Style" and fmt and "style" in section:
            v = [x.strip() for x in rest.split(",", len(fmt) - 1)]
            d = dict(zip(fmt, v))
            styles[d.get("name", "")] = (d.get("fontname", ""), d.get("bold", "0") not in ("0", ""))
        elif key == "Dialogue" and fmt and "events" in section:
            v = rest.split(",", len(fmt) - 1)
            d = dict(zip(fmt, [x.strip() for x in v[:-1]] + v[-1:]))
            base = styles.get(d.get("style", ""), next(iter(styles.values()), ("", False)))
            font, bold = base
            pos = 0
            text = d.get("text", "")
            for m in list(_TAG.finditer(text)) + [None]:
                seg = text[pos: m.start() if m else len(text)]
                seg = seg.replace("\\N", "").replace("\\n", "").replace("\\h", " ")
                if seg and font:
                    out.setdefault((font, bold), set()).update(seg)
                if not m:
                    break
                pos = m.end()
                for tag in m.group(1).split("\\")[1:]:
                    if tag.startswith("fn"):
                        font = tag[2:].strip() or base[0]
                    elif tag.startswith("r"):
                        font, bold = styles.get(tag[1:].strip(), base)
                    elif re.fullmatch(r"b\d+", tag):
                        n = int(tag[1:])
                        bold = n == 1 or n >= 600
    return out


def _safe(name):
    return re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_") or "font"


def collect_fonts(ass_paths, out_dir, mode="subset"):
    """把 ASS 用到的字体放进 out_dir。mode：subset 只含用到的字 / full 完整字体。
    返回 (字体文件列表, 提示列表)。"""
    if mode == "none":
        return [], []
    want = {}
    for p in ass_paths:
        for k, chars in ass_fonts(p).items():
            want.setdefault(k, set()).update(chars)
    out_dir = Path(out_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    files, notes, done = [], [], {}
    for (family, bold), chars in sorted(want.items()):
        hit = fonts.find(family, bold)
        if not hit:
            notes.append(f"找不到字体「{family}」，没有嵌入（播放端会换成别的字体）")
            continue
        key = (hit[0], hit[1])
        if key in done:  # 粗体找不到单独的字形时会落到常规体上：合并用到的字
            done[key][1].update(chars)
            continue
        done[key] = [family, set(chars), hit[2]]
    import logging

    from fontTools.ttLib import TTFont

    logging.getLogger("fontTools.subset").setLevel(logging.ERROR)  # “某某表不能取子集，已丢弃”之类的提示
    for (path, index), (family, chars, is_bold) in done.items():
        try:
            face = TTFont(path, fontNumber=index)
            fs = face["OS/2"].fsType if "OS/2" in face else 0
            if fs & 0x000F == 0x0002:
                notes.append(f"字体「{family}」的许可不允许嵌入，已跳过")
                continue
            ext = ".otf" if ("CFF " in face or "CFF2" in face) else ".ttf"
            target = out_dir / f"{_safe(family)}{'-Bold' if is_bold else ''}{ext}"
            if mode == "subset" and not fs & 0x0100:
                from fontTools import subset

                opt = subset.Options()
                opt.name_IDs, opt.name_languages, opt.name_legacy = ["*"], ["*"], True
                opt.layout_features, opt.notdef_outline, opt.hinting = ["*"], True, False
                opt.glyph_names, opt.legacy_kern, opt.ignore_missing_unicodes = False, True, True
                sub = subset.Subsetter(opt)
                sub.populate(unicodes={ord(c) for c in chars} | {0x20, 0x3000})
                sub.subset(face)
                face.save(target)
            elif not path.lower().endswith((".ttc", ".otc")):
                shutil.copyfile(path, target)
            else:
                face.save(target)  # 字体集合里只取这一个字形
            files.append(target)
        except Exception as e:
            notes.append(f"字体「{family}」处理失败（{e}），没有嵌入")
    return files, notes


# ---------------- ffmpeg ----------------
def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, check=True).stdout
    return json.loads(out.decode("utf-8", "replace")).get("streams", [])


def _ffmpeg(cmd, duration, label, cancel=None, cwd=None, part=None):
    """运行 ffmpeg，推 {"type": "progress"} 事件。返回 (返回码, 错误输出末尾, 处理到的秒数)。"""
    from ..sources import media

    def progress(done, total):
        env.progress(done, total, {"mux": "封装", "burn": "压制"}.get(label, label), stage=label, every=0.5)
    return media.run_ffmpeg(cmd, duration, progress, cancel, cwd, part)


def _finish(rc, tail, part, out):
    if rc != 0:
        Path(part).unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 出错：{tail or rc}")
    os.replace(part, out)


def _part(out):
    out = Path(out)
    return out.with_name(out.stem + ".part" + out.suffix)


def mux(video, tracks, out, font_files=(), keep_subs=True, vinfo=None, duration=0.0, cancel=None):
    """封装 MKV。tracks：[{"path", "lang", "title"}]，第一条设为默认。"""
    src = streams(video)
    part = _part(out)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    for t in tracks:
        cmd += ["-i", str(t["path"])]
    if vinfo:
        cmd += ["-map", f"0:{vinfo['index']}"]
    cmd += ["-map", "0:a?"]
    kept, codecs = [], []
    if keep_subs:
        for s in src:
            c = s.get("codec_name", "")
            if s.get("codec_type") == "subtitle" and (c in MKV_COPY_SUBS or c in MKV_CONVERT_SUBS):
                cmd += ["-map", f"0:{s['index']}"]
                kept.append(s)
                codecs.append(MKV_CONVERT_SUBS.get(c, "copy"))
        n_att = sum(1 for s in src if s.get("codec_type") == "attachment")
        if n_att:
            cmd += ["-map", "0:t"]
    else:
        n_att = 0
    for i in range(len(tracks)):
        cmd += ["-map", f"{i + 1}:0"]
    cmd += ["-c", "copy"]
    for k, s in enumerate(x for x in src if x.get("codec_type") == "audio"):
        if s.get("codec_name") in MKV_CONVERT_AUDIO:
            cmd += [f"-c:a:{k}", "aac", f"-b:a:{k}", "192k"]
    for k, c in enumerate(codecs):
        if c != "copy":
            cmd += [f"-c:s:{k}", c]
    for k in range(len(kept)):
        cmd += [f"-disposition:s:{k}", "0"]
    for i, t in enumerate(tracks):
        k = len(kept) + i
        cmd += [f"-metadata:s:s:{k}", f"language={lang3(t.get('lang'))}", f"-metadata:s:s:{k}", f"title={t.get('title', '')}",
                f"-disposition:s:{k}", "default" if i == 0 else "0"]
    for j, f in enumerate(font_files):
        mime = "application/vnd.ms-opentype" if str(f).lower().endswith(".otf") else "application/x-truetype-font"
        cmd += ["-attach", str(f), f"-metadata:s:t:{n_att + j}", f"mimetype={mime}",
                f"-metadata:s:t:{n_att + j}", f"filename={Path(f).name}"]
    cmd += ["-max_interleave_delta", "0", str(part)]
    rc, tail, _ = _ffmpeg(cmd, duration, "mux", cancel, part=part)
    _finish(rc, tail, part, out)
    return out


def burn(video, ass_path, font_files, out, vinfo, encoder="auto", quality=20, height=0, duration=0.0,
         cancel=None, work_dir=None):
    """压制硬字幕 MP4。字幕和字体先放进工作目录，ffmpeg 在那里运行（滤镜参数里写 Windows 路径很麻烦）。"""
    from ..sources import media

    if not vinfo:
        raise RuntimeError("纯音频文件不能压制硬字幕")
    work = Path(work_dir)
    shutil.rmtree(work / "fonts", ignore_errors=True)
    (work / "fonts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ass_path, work / "sub.ass")
    for f in font_files:
        shutil.copyfile(f, work / "fonts" / Path(f).name)
    w, h = media.frame_size(vinfo, height)
    inter = vinfo.get("interlaced")
    subs = ["setsar=1", "subtitles=sub.ass:fontsdir=fonts"]
    # 显卡解码：反交错、缩放、转 8 位都在显卡上做，只把成品帧传回内存画字幕（4K HEVC 用 CPU 解码很慢）
    gpu = (["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"],
           (["yadif_cuda=mode=send_frame:deint=all"] if inter else []) + [f"scale_cuda={w}:{h}:format=yuv420p", "hwdownload",
                                                                          "format=yuv420p"] + subs)
    cpu = ([], (["bwdif=mode=send_frame:deint=all"] if inter else [])
           + ([f"scale={w}:{h}:flags=lanczos"] if (w, h) != (vinfo["width"], vinfo["height"]) else []) + subs)
    audio = [s.get("codec_name") for s in streams(video) if s.get("codec_type") == "audio"]
    acodec = ["-c:a", "copy"] if all(c in MP4_COPY_AUDIO for c in audio) else ["-c:a", "aac", "-b:a", "192k"]
    part = _part(Path(out).resolve())
    encs = ["h264_nvenc", "libx264"] if encoder == "auto" else [encoder]
    tried = [(e, d) for e in encs for d in ((gpu, cpu) if e.endswith("_nvenc") else (cpu,))]
    for i, (enc, (pre, vf)) in enumerate(tried):
        how = f"{enc}{'，显卡解码' if pre else ''}"
        cmd = ["ffmpeg", "-y", "-v", "error", *pre, "-i", str(Path(video).resolve()), "-map", f"0:{vinfo['index']}",
               "-map", "0:a?", "-vf", ",".join(vf), *ENCODERS[enc](quality), "-pix_fmt", "yuv420p", *acodec,
               "-movflags", "+faststart", str(part)]
        env.log(f"压制硬字幕：{how}，画质 {quality}，{w}×{h}")
        rc, tail, done = _ffmpeg(cmd, duration, "burn", cancel, cwd=str(work), part=part)
        if rc != 0 and done < 1 and i + 1 < len(tried):  # 一开始就失败（没有 N 卡、显卡不支持这种编码）：换下一种
            env.log(f"{how} 不可用（{tail.splitlines()[-1] if tail else rc}），换下一种")
            continue
        _finish(rc, tail, part, out)
        return out, enc
    raise RuntimeError("没有可用的视频编码器")
