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
import shlex
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


HEVC_LIKE = {"hevc", "h265", "av1", "vp9"}  # 压缩率比 H.264 高的编码


def pick_encoders(encoder, src_codec, bit_depth="auto"):
    """自动：原片是 H.265（或更新的编码）或指定 10-bit 时优先 H.265，否则 H.264；都是先试显卡再用 CPU。"""
    if encoder != "auto":
        return [encoder]
    if bit_depth == "10" or (src_codec or "").lower() in HEVC_LIKE:
        return ["hevc_nvenc", "libx265"]
    return ["h264_nvenc", "libx264"]


def rate_cap(src_bps, src_codec, src_size, out_size, enc):
    """压制码率上限：不超过原片（按画面缩小的比例折算）。画面里的信息不会比原片多，码率再高只是把原片的噪点再存一遍。
    原片是 H.265、压成 H.264 时放宽到 1.5 倍（同样画质 H.264 要多 50% 左右）。原片码率不知道时不限。"""
    if not src_bps or src_bps <= 0:
        return None
    ratio = (out_size[0] * out_size[1]) / max(1, src_size[0] * src_size[1])
    k = 1.5 if enc.startswith(("h264", "libx264")) and (src_codec or "").lower() in HEVC_LIKE else 1.0
    return max(500_000, int(src_bps * min(1.0, ratio) * k))


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


def _ffmpeg(cmd, duration, label, cancel=None, cwd=None, part=None, video=None, vinfo=None):
    """运行 ffmpeg，推 {"type": "progress"} 事件。返回 (返回码, 错误输出末尾, 处理到的秒数)。
    给了 video / vinfo 时按视频帧数也算一份进度（某条音轨中途断了时 ffmpeg 报的时间会卡住）。"""
    from ..sources import media

    def progress(done, total):
        env.progress(min(done, total) if total else done, total, {"mux": "封装", "burn": "压制"}.get(label, label),
                     stage=label, every=0.5)
    fps = media.stream_fps(video, vinfo["index"])[0] if video and vinfo else 0.0
    return media.run_ffmpeg(cmd, duration, progress, cancel, cwd, part, fps=fps)


def _finish(rc, tail, part, out):
    if rc != 0:
        Path(part).unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 出错：{tail or rc}")
    os.replace(part, out)


def _part(out):
    out = Path(out)
    return out.with_name(out.stem + ".part" + out.suffix)


def mux(video, tracks, out, font_files=(), keep_subs=True, vinfo=None, duration=0.0, cancel=None):
    """封装 MKV。tracks：[{"path", "lang", "title", "default"?}]，标了 default 的那条设为默认（都没标就是第一条）。"""
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
    first = next((i for i, t in enumerate(tracks) if t.get("default")), 0)
    for i, t in enumerate(tracks):
        k = len(kept) + i
        cmd += [f"-metadata:s:s:{k}", f"language={lang3(t.get('lang'))}", f"-metadata:s:s:{k}", f"title={t.get('title', '')}",
                f"-disposition:s:{k}", "default" if i == first else "0"]
    for j, f in enumerate(font_files):
        mime = "application/vnd.ms-opentype" if str(f).lower().endswith(".otf") else "application/x-truetype-font"
        cmd += ["-attach", str(f), f"-metadata:s:t:{n_att + j}", f"mimetype={mime}",
                f"-metadata:s:t:{n_att + j}", f"filename={Path(f).name}"]
    cmd += ["-max_interleave_delta", "0", str(part)]
    rc, tail, _ = _ffmpeg(cmd, duration, "mux", cancel, part=part, video=video, vinfo=vinfo)
    _finish(rc, tail, part, out)
    return out


_TRC_RE = re.compile(r"transfer_characteristics\s+\d+\s*=\s*(\d+)")
_CROSS_OVERRIDES = {
    "-b:v": {"-cq", "-crf", "-maxrate", "-bufsize"},
    "-cq": {"-cq", "-crf"},
    "-crf": {"-cq", "-crf"},
    "-c:a": {"-b:a"},
}
_DIRECT_OVERRIDES = {
    "-preset", "-tune", "-rc", "-cq", "-crf", "-b:v", "-maxrate", "-bufsize",
    "-pix_fmt", "-profile:v", "-tag:v", "-c:a", "-b:a", "-bsf:v",
    "-color_range", "-color_primaries", "-color_trc", "-colorspace", "-movflags",
}


def _body_transfer(path, at_sec):
    """采样检测 HEVC 正片码流里的 transfer_characteristics（1=BT.709，16=PQ，18=HLG）。"""
    cmd = ["ffmpeg", "-hide_banner", "-ss", str(max(0, int(at_sec))), "-i", str(path),
           "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "trace_headers", "-frames:v", "90", "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    text = (res.stdout or b"").decode("utf-8", "replace")
    return [int(m.group(1)) for m in _TRC_RE.finditer(text)]


def detect_bt2020_sdr(path, vinfo=None, duration=0.0):
    """判断视频是否为 BT.2020 SDR（需线性光转 BT.709），跳过已是 709 或真 HDR（PQ/HLG）的片源。"""
    st = streams(path)
    vs = None
    if vinfo and vinfo.get("index") is not None:
        vs = next((s for s in st if s.get("index") == vinfo["index"]), None)
    if vs is None:
        vs = next((s for s in st if s.get("codec_type") == "video"), None)
    if not vs:
        return False, "未找到视频流，跳过转 709"
    cp = (vs.get("color_primaries") or "").lower()
    if cp != "bt2020":
        return False, f"色域为 {cp or '未标注'}（非 BT.2020），无需转 709"
    codec = (vs.get("codec_name") or "").lower()
    ct = (vs.get("color_transfer") or "").lower()
    if codec == "hevc" and duration and duration > 0:
        trc = _body_transfer(path, int(duration * 0.1)) + _body_transfer(path, int(duration * 0.5))
        if trc and sum(t in (16, 18) for t in trc) > len(trc) / 2:
            return False, f"正片为 HDR（transfer={sorted(set(trc))}），跳过转 709"
    elif ct in ("smpte2084", "arib-std-b67"):
        return False, f"正片为 HDR（{ct}），跳过转 709"
    return True, "检测到 BT.2020 SDR，启用线性光 BT.2020→BT.709 转换"


def merge_ffmpeg_args(base_args, extra):
    """合并用户自定义 FFmpeg 输出参数，并自动移除与用户选项冲突的默认参数。"""
    if not extra or not str(extra).strip():
        return list(base_args)
    try:
        tokens = shlex.split(str(extra))
    except ValueError as e:
        raise RuntimeError(f"自定义 FFmpeg 参数格式有误：{e}") from e
    if any(t in ("-vf", "-filter:v") or t.startswith(("-vf=", "-filter:v=")) for t in tokens):
        raise RuntimeError("请把自定义视频滤镜填在「附加视频滤镜」里，不要在自定义参数里写 -vf（会覆盖字幕滤镜）")
    user_flags = {t.split("=", 1)[0] for t in tokens if t.startswith("-")}
    drop = set(user_flags & _DIRECT_OVERRIDES)
    for flag, also in _CROSS_OVERRIDES.items():
        if flag in user_flags:
            drop |= also
    filtered = []
    i = 0
    while i < len(base_args):
        tok = base_args[i]
        if tok in drop:
            # base_args 中的默认参数均成对出现，跳过值以免残留孤立参数
            i += 2 if i + 1 < len(base_args) and not base_args[i + 1].startswith("-") else 1
            continue
        filtered.append(tok)
        i += 1
    return filtered + tokens


def _resolve_bit_depth(enc, bit_depth, do_709):
    """按编码器和色深设置确定 (use_10bit, pix_fmt, profile_args, downgraded_from_nvenc_h264)。"""
    is_hevc = enc in ("hevc_nvenc", "libx265")
    want_10 = (bit_depth == "10") or (bit_depth == "auto" and do_709 and is_hevc)
    if want_10 and enc == "h264_nvenc":
        return False, "yuv420p", [], True
    if not want_10:
        return False, "yuv420p", [], False
    if enc == "hevc_nvenc":
        # hevc_nvenc 不接受 yuv420p10le，需用硬件 10-bit 像素格式 p010le
        return True, "p010le", ["-profile:v", "main10"], False
    if enc == "libx265":
        return True, "yuv420p10le", ["-profile:v", "main10"], False
    return True, "yuv420p10le", [], False


def _burn_vf(vinfo, w, h, inter, use_gpu_decode, do_709, use_10, extra_vf):
    """构建压制时的前置输入参数与 -vf 滤镜链。"""
    scaled = (w, h) != (vinfo["width"], vinfo["height"])
    extra = [extra_vf] if extra_vf else []
    subs = ["subtitles=sub.ass:fontsdir=fonts"]
    if do_709:
        pre = ["-hwaccel", "cuda"] if use_gpu_decode else []
        out_fmt = "yuv420p10le" if use_10 else "yuv420p"
        conv = ("zscale=pin=2020:tin=709:min=2020_ncl:rin=limited:p=2020:t=linear:m=gbr:r=full:npl=100,format=gbrpf32le,"
                f"zscale=pin=2020:tin=linear:min=gbr:rin=full:p=709:t=709:m=709:r=limited:dither=error_diffusion,format={out_fmt}")
        tag709 = "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        vf = (["bwdif=mode=send_frame:deint=all"] if inter else []) + ([f"scale={w}:{h}:flags=lanczos"] if scaled else []) + ["setsar=1", conv] + extra + subs + [tag709]
        return pre, vf
    if use_gpu_decode:
        gpu_fmt = "p010le" if use_10 else "yuv420p"
        pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        vf = (["yadif_cuda=mode=send_frame:deint=all"] if inter else []) + [f"scale_cuda={w}:{h}:format={gpu_fmt}", "hwdownload", f"format={gpu_fmt}", "setsar=1"] + extra + subs
        return pre, vf
    pre = []
    vf = (["bwdif=mode=send_frame:deint=all"] if inter else []) + ([f"scale={w}:{h}:flags=lanczos"] if scaled else []) + ["setsar=1"] + extra + subs
    return pre, vf


def burn(video, ass_path, font_files, out, vinfo, encoder="auto", quality=20, height=0, duration=0.0,
         cancel=None, work_dir=None, bit_depth="auto", color_709="off", extra_args="", extra_vf=""):
    """压制硬字幕 MP4。字幕和字体先放进工作目录，ffmpeg 在那里运行（滤镜参数里写 Windows 路径很麻烦）。"""
    from ..sources import media

    if not vinfo:
        raise RuntimeError("纯音频文件不能压制硬字幕")
    if color_709 == "force":
        do_709 = True
        env.log("强制启用 BT.2020 SDR → BT.709 转换")
    elif color_709 == "auto":
        do_709, note = detect_bt2020_sdr(video, vinfo, duration)
        env.log(note)
    else:
        do_709 = False

    work = Path(work_dir)
    shutil.rmtree(work / "fonts", ignore_errors=True)
    (work / "fonts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ass_path, work / "sub.ass")
    for f in font_files:
        shutil.copyfile(f, work / "fonts" / Path(f).name)
    w, h = media.frame_size(vinfo, height)
    inter = vinfo.get("interlaced")
    extra_vf_clean = (extra_vf or "").strip()
    audio = [s.get("codec_name") for s in streams(video) if s.get("codec_type") == "audio"]
    acodec = ["-c:a", "copy"] if all(c in MP4_COPY_AUDIO for c in audio) else ["-c:a", "aac", "-b:a", "192k"]
    part = _part(Path(out).resolve())
    encs = pick_encoders(encoder, vinfo.get("codec"), bit_depth=bit_depth)
    tried = [(e, gpu) for e in encs for gpu in ((True, False) if e.endswith("_nvenc") else (False,))]
    # 原片码率：整个文件的平均码率（含音频，差得不多；很多 TS / M2TS 的视频流不标码率）
    try:
        src_bps = Path(video).stat().st_size * 8 / duration if duration else 0
    except OSError:
        src_bps = 0
    warned_h264_10 = False
    for i, (enc, use_gpu) in enumerate(tried):
        use_10, pix_fmt, prof, downgraded = _resolve_bit_depth(enc, bit_depth, do_709)
        if downgraded and not warned_h264_10:
            env.log("h264_nvenc 不支持 10-bit，已改用 8-bit（如需 10-bit 请选 H.265 或 libx264）")
            warned_h264_10 = True
        pre, vf = _burn_vf(vinfo, w, h, inter, use_gpu, do_709, use_10, extra_vf_clean)
        cap = rate_cap(src_bps, vinfo.get("codec"), (vinfo["width"], vinfo["height"]), (w, h), enc)
        limit = ["-maxrate", str(cap), "-bufsize", str(cap * 2)] if cap else []
        is_hevc = enc in ("hevc_nvenc", "libx265")
        color_flags = (
            ["-color_range", "tv", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
             "-bsf:v", f"{'hevc_metadata' if is_hevc else 'h264_metadata'}=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1"]
            if do_709 else []
        )
        out_args = [*ENCODERS[enc](quality), *prof, *limit, "-pix_fmt", pix_fmt, *color_flags, *acodec, "-movflags", "+faststart"]
        out_args = merge_ffmpeg_args(out_args, extra_args)
        cmd = ["ffmpeg", "-y", "-v", "error", *pre, "-i", str(Path(video).resolve()), "-map", f"0:{vinfo['index']}",
               "-map", "0:a?", "-vf", ",".join(vf), *out_args, str(part)]
        how = f"{enc}{'，显卡解码' if pre else ''}，{'10-bit' if use_10 else '8-bit'}" + ("，BT.709" if do_709 else "")
        env.log(f"压制硬字幕：{how}，画质 {quality}，{w}×{h}" + (f"，码率不超过 {cap / 1e6:.1f} Mbps" if cap else ""))
        rc, tail, done = _ffmpeg(cmd, duration, "burn", cancel, cwd=str(work), part=part, video=video, vinfo=vinfo)
        if rc != 0 and done < 1 and i + 1 < len(tried):  # 一开始就失败（没有 N 卡、显卡不支持这种编码）：换下一种
            env.log(f"{how} 不可用（{tail.splitlines()[-1] if tail else rc}），换下一种")
            continue
        _finish(rc, tail, part, out)
        return out, enc
    raise RuntimeError("没有可用的视频编码器")
