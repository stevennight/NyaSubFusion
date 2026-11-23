"""广播字幕原样：把 ARIB 字幕本来的样子（位置、字号、颜色、背景、振假名）转成一份原文 ASS，
可以单独用，也可以封装进 MKV、压制成硬字幕。

- 4K / 8K 广播的 ARIB-TTML：自己转。每个段落是画面上的一块字（一行），位置、大小都是原样的像素
  （画布 3840×2160 或 1920×1080），全角 / 半角字宽、字间距、文字颜色、半透明底色照搬；振假名本来就是单独的小字块，
  放在它自己的位置上。原字体是电视机里的丸ゴシック，电脑上一般没有，换成等宽的日文字体（字宽和原来一样，排版对得上）。
- 2K 广播的 ARIB 字幕（arib_caption）：用 ffmpeg 的 libaribcaption 转成带样式的 ASS（带振假名）。

时间和融合用的字幕一样：按字幕对时算出的整体偏移挪过（广播字幕的时间和视频常差一秒左右）。
"""
import re
import xml.etree.ElementTree as ET

from ..sources.subtitle import OWN_MARK, _NS, _TTML_DOC, _ttml_time
from . import fonts
from .ass import ts

# 电视机上的字幕字体是丸ゴシック；电脑上按这个顺序找（圆体优先，其次等宽的哥特体：ARIB 每个字占固定宽度）
FONTS = ("Kosugi Maru", "M PLUS Rounded 1c", "Zen Maru Gothic", "Hiragino Maru Gothic ProN", "BIZ UDGothic",
         "MS Gothic", "Noto Sans JP", "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo")
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
_ARIB = "{http://www.arib.or.jp/ns/arib-ttml/v1_0}"
SUFFIX = "-broadcast.ass"   # 文件名：视频名.语言-broadcast.ass


def pick_font(prefer=""):
    """用哪个字体：指定了而且装了就用指定的，否则按 FONTS 的顺序找第一个装了的。"""
    for f in ((prefer,) if prefer else ()) + FONTS:
        if fonts.find(f):
            return f
    return prefer or FONTS[-1]


def _px(v, k=0):
    try:
        return float((v or "").split()[k].rstrip("px"))
    except (ValueError, IndexError):
        return None


def _rgba(v):
    """#RRGGBB[AA] → (ASS 颜色 &HBBGGRR&, ASS 透明度 0~255)；没有返回 None。"""
    h = (v or "").lstrip("#")
    if len(h) not in (6, 8):
        return None
    a = int(h[6:8], 16) if len(h) == 8 else 255
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper(), 255 - a


def parse_ttml(path):
    """读出每条字幕在画面上的样子：[{"start", "end", "boxes": [{"x", "y", "w", "h", "bg", "spans": [...]}]}]，
    span：{"text", "fw", "fh", "sp", "color"}（字宽、字高、字间距、文字颜色）。另外返回画布大小 (宽, 高)。"""
    raw = open(path, "rb").read()
    docs, far = [], 0.0
    for m in _TTML_DOC.finditer(raw):
        try:
            root = ET.fromstring(m.group(1))
        except ET.ParseError:
            continue
        styles = {}
        for st in root.iter(f"{{{_NS['tt']}}}style"):
            a = {k.split("}")[-1]: v for k, v in st.attrib.items()}
            styles[st.get(_XML_ID)] = a
        regions = {}
        for rg in root.iter(f"{{{_NS['tt']}}}region"):
            org, ext = rg.get(f"{{{_NS['tts']}}}origin"), rg.get(f"{{{_NS['tts']}}}extent")
            x, y, w, h = _px(org, 0), _px(org, 1), _px(ext, 0), _px(ext, 1)
            if None not in (x, y, w, h):
                regions[rg.get(_XML_ID)] = (x, y, w, h)
                far = max(far, x + w, (y + h) * 16 / 9)
        for div in root.iter(f"{{{_NS['tt']}}}div"):
            if not div.get("begin"):
                continue
            begin = _ttml_time(div.get("begin"))
            end = _ttml_time(div.get("end")) if div.get("end") else begin + 3.0
            boxes = []
            for p in div.iter(f"{{{_NS['tt']}}}p"):
                box = regions.get(p.get("region"))
                if not box:
                    continue
                spans, bg = [], None
                for sp in p.iter(f"{{{_NS['tt']}}}span"):
                    a = {}
                    for k in (sp.get("style") or "").split():
                        a.update(styles.get(k, {}))
                    text = "".join(sp.itertext())
                    if not text:
                        continue
                    fw, fh = _px(a.get("fontSize"), 0), _px(a.get("fontSize"), 1) or _px(a.get("fontSize"), 0)
                    spans.append({"text": text, "fw": fw or fh or box[3] * 0.6, "fh": fh or box[3] * 0.6,
                                  "sp": _px(a.get("letter-spacing")) or 0.0, "color": a.get("color")})
                    bg = bg or a.get("backgroundColor")
                if spans:
                    boxes.append({"x": box[0], "y": box[1], "w": box[2], "h": box[3], "bg": bg, "spans": spans})
            if boxes and end > begin:
                docs.append({"start": begin, "end": end, "boxes": boxes})
    docs.sort(key=lambda d: d["start"])
    out = []
    for d in docs:  # 同一条字幕常重复发送：接着上一条、样子一样的并成一条
        if out and out[-1]["boxes"] == d["boxes"] and d["start"] - out[-1]["end"] < 0.3:
            out[-1]["end"] = max(out[-1]["end"], d["end"])
            continue
        out.append(d)
    canvas = (3840, 2160) if far > 1920 else (1920, 1080)
    return out, canvas


def _box_events(b, start, end, font, em_scale):
    bg = _rgba(b["bg"])
    lines = []
    t0, t1 = ts(start), ts(end)
    x, y, w, h = (round(v) for v in (b["x"], b["y"], b["w"], b["h"]))
    if bg and bg[1] < 255:  # 底色：这一块的区域画一个半透明的框
        lines.append(f"Dialogue: 0,{t0},{t1},Broadcast,,0,0,0,,{{\\an7\\pos({x},{y})\\p1\\bord0\\shad0"
                     f"\\1c{bg[0]}\\1a&H{bg[1]:02X}&}}m 0 0 l {w} 0 {w} {h} 0 {h}")
    # 字：这一行竖直方向居中；ASS 的字号是字体整体高度，按字体的比例换算，使字宽（全角一个字）和原来一样
    edge = "\\bord0\\shad0" if bg else f"\\bord{max(2, round(b['spans'][0]['fh'] * 0.05))}\\shad0\\3c&H101010&"
    text, cur = "", None
    for s in b["spans"]:
        c = _rgba(s["color"]) or ("&HFFFFFF&", 0)
        fs = s["fh"] / em_scale
        tag = f"\\fs{fs:.1f}\\fscx{100 * s['fw'] / s['fh']:.0f}\\fsp{s['sp']:g}\\1c{c[0]}\\1a&H{c[1]:02X}&"
        if tag != cur:
            text += "{" + tag + "}"
            cur = tag
        text += s["text"].replace("{", "｛").replace("}", "｝").replace("\n", "")
    lines.append(f"Dialogue: 1,{t0},{t1},Broadcast,,0,0,0,,{{\\an4\\pos({x},{y + h / 2:g}){edge}}}{text}")
    return lines


def _header(canvas, font, size):
    return "\n".join([
        "[Script Info]", f"; {OWN_MARK}（广播字幕原样）", "ScriptType: v4.00+", f"PlayResX: {canvas[0]}",
        f"PlayResY: {canvas[1]}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Broadcast,{font},{size:g},&H00FFFFFF,&H000000FF,&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"])


def ttml_to_ass(path, out, offset=0.0, font=""):
    """ARIB-TTML → 原样的 ASS。offset：字幕对时算出的偏移（秒，加到每条的时间上）。返回写了几条字幕。"""
    docs, canvas = parse_ttml(path)
    font = pick_font(font)
    em_scale = fonts.metrics(font).em_scale
    body = []
    for d in docs:
        a, b = d["start"] + offset, d["end"] + offset
        if b <= 0:
            continue
        for box in d["boxes"]:
            body += _box_events(box, max(0.0, a), b, font, em_scale)
    size = round(canvas[1] / 15 / em_scale)
    with open(out, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(_header(canvas, font, size) + "\n" + "\n".join(body) + "\n")
    return len(docs)


_ASS_TIME = re.compile(r"^(Dialogue:\s*\d+,)(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d),")


def _parse_ts(s):
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


BOX_PAD = 7  # 底框比字四周各大出多少（字幕画布的像素；libaribcaption 的画布是 960x540）


def _box_style(line, fmt):
    """libaribcaption 的样式：BorderStyle 4 + 阴影 4 + 不透明黑。广播数据里是半透明黑底框（ARIB 的半透明色，和 TTML 一样 50%），
    但 BorderStyle 4 各播放器认法不一：libass（mpv、压制）画成实心黑框，VSFilter（MPC、PotPlayer）不认，画成往右下偏的黑影。
    改成都认的 BorderStyle 3（每行一个底框，颜色取描边色）：半透明黑，四周大出 BOX_PAD，不要阴影。"""
    name, _, vals = line.partition(":")
    vals = [v.strip() for v in vals.split(",")]
    if len(vals) != len(fmt):
        return line
    for k, v in (("BorderStyle", "3"), ("Outline", str(BOX_PAD)), ("Shadow", "0"),
                 ("OutlineColour", "&H7F000000"), ("BackColour", "&H7F000000")):
        if k in fmt:
            vals[fmt.index(k)] = v
    return f"{name}: " + ",".join(vals)


NEAR = 2 * BOX_PAD + 12  # 上下两行字之间空得比这少，就当成同一块字幕：底框拼成一整块（广播里是连着的）
_POS = re.compile(r"\\pos\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)")
_FS = re.compile(r"\\fs([\d.]+)")


def _tile_boxes(lines, fmt):
    """同一屏的几行（正文、上面的注音、下一行）各有一个底框，挨得近的上下叠在一起，叠的地方颜色深一道。
    按上下相邻两行字之间的中线分开：底框伸到中线（\\ybord），超出的用 \\clip 裁掉，正好拼上、不叠也不留缝。
    离得远的照旧四周大出 BOX_PAD。只处理左上角定位（\\an7 + \\pos）的行（libaribcaption 都是这样写的）。"""
    width, size = 1920, 36.0
    for ln in lines:
        if ln.startswith("PlayResX:"):
            width = int(float(ln.split(":")[1]))
        elif ln.startswith("Style:") and "Fontsize" in fmt:
            try:
                size = float(ln.partition(":")[2].split(",")[fmt.index("Fontsize")])
            except (ValueError, IndexError):
                pass
    rows = {}  # (开始, 结束) → [(行号, 上沿, 字高)]
    for i, ln in enumerate(lines):
        m = _ASS_TIME.match(ln)
        if not m:
            continue
        body = ln[m.end():].split(",", 6)[-1]
        p = _POS.search(body)
        if not p or "\\an7" not in body or not re.sub(r"\{[^}]*\}", "", body).strip():
            continue
        f = _FS.search(body)
        rows.setdefault((m.group(2), m.group(3)), []).append((i, float(p.group(2)), float(f.group(1)) if f else size))
    for group in rows.values():
        ys = sorted({(y, h) for _, y, h in group})
        for i, y, h in group:
            top, bottom = y - BOX_PAD, y + h + BOX_PAD
            # 上下挨着的行（两行字之间空不到 NEAR）：上面那行的下沿、下面那行的上沿
            above = [y2 + h2 for y2, h2 in ys if y2 < y and y - (y2 + h2) < NEAR]
            below = [y2 for y2, h2 in ys if y2 > y and y2 - (y + h) < NEAR]
            if above:
                top = (max(above) + y) / 2
            if below:
                bottom = (y + h + min(below)) / 2
            if not above and not below:
                continue
            bord = max(y - top, bottom - y - h, 0)
            lines[i] = lines[i].replace("\\an7", f"\\an7\\ybord{bord:g}\\clip(0,{top:g},{width},{bottom:g})", 1)


def shift_ass(src, out, offset=0.0, note="广播字幕原样"):
    """别的工具转好的带样式 ASS（libaribcaption 的输出）：时间挪 offset 秒，文件头记上是本工具输出的（不会再被当成外挂字幕读进来）；
    底框改成各播放器都画得一样的半透明黑框（_box_style）。返回字幕条数。"""
    lines, n, fmt = [], 0, []
    for line in open(src, encoding="utf-8-sig").read().splitlines():
        m = _ASS_TIME.match(line)
        if m:
            a, b = _parse_ts(m.group(2)) + offset, _parse_ts(m.group(3)) + offset
            if b <= 0:
                continue
            # 每条都带的 \3a&Hff&（描边全透明）在 BorderStyle 3 下会把底框也弄没
            line = f"{m.group(1)}{ts(a)},{ts(b)}," + re.sub(r"\\3a&H[Ff]{2}&", "", line[m.end():]).replace("{}", "")
            n += 1
        elif line.startswith("Format:") and not fmt and "Fontname" in line:
            fmt = [v.strip() for v in line.partition(":")[2].split(",")]
        elif line.startswith("Style:") and fmt:
            line = _box_style(line, fmt)
        elif line.startswith("ScaledBorderAndShadow:"):
            continue
        lines.append(line)
        if line.strip() == "[Script Info]":
            lines.append(f"; {OWN_MARK}（{note}）")
            lines.append("ScaledBorderAndShadow: yes")  # 底框大小按字幕画布算（不然 4K 画面上只有几个像素）
    _tile_boxes(lines, fmt)
    with open(out, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return n
