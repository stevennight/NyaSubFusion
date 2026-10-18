"""外挂字幕读取：srt / ass / vtt，以及从 ARIB 转出的 ass。统一成 [{start,end,text}]。"""
import os
import re
import statistics

import pysubs2

SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")
_TAG = re.compile(r"\{[^}]*\}")
_FS = re.compile(r"\\fs(\d+(?:\.\d+)?)")
_POS = re.compile(r"\\pos\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)")


OWN_MARK = "由 NyaSubFusion 生成"  # 本工具写出的 ASS 文件头里有这一行（render/ass.py header）


def _own_ass(path):
    try:
        with open(path, "rb") as f:
            return OWN_MARK.encode("utf-8") in f.read(4096)
    except OSError:
        return False


def is_own_output(path, legacy_srt=True):
    """是不是本工具自己写出的字幕（不能当成“自带字幕”再读进来，否则译文会被当成原文）。
    ASS 看文件头的标记；SRT 没地方写标记：旧项目（还没有输出记录）里，旁边有同名的、我们写的 ASS 就算。"""
    low = path.lower()
    if "-merged." in low:
        return True
    if low.endswith((".ass", ".ssa")):
        return _own_ass(path)
    if legacy_srt and low.endswith(".srt"):
        base = path[: -len(".srt")]
        return any(os.path.exists(base + e) and _own_ass(base + e) for e in (".ass", ".ASS"))
    return False


def find_external(video, skip_names=(), legacy_srt=True):
    """找与视频同名的字幕：xxx.srt / xxx.ja.srt / xxx.jpn.ass ...
    skip_names：本工具写出过的文件名（现在的配置会写的 + 以前写过的）；另外按内容认出本工具的输出，一起跳过。"""
    folder = os.path.dirname(os.path.abspath(video))
    stem = os.path.splitext(os.path.basename(video))[0]
    skip = {n.lower() for n in skip_names}
    hits = []
    for name in os.listdir(folder):
        low = name.lower()
        if not low.endswith(SUB_EXTS) or low in skip:
            continue
        # 文件名要么就是“视频名.扩展名”，要么是“视频名.xxx.扩展名”：ep1 不能匹配到 ep10.srt
        if not (os.path.splitext(name)[0] == stem or name.startswith(stem + ".")):
            continue
        path = os.path.join(folder, name)
        if not is_own_output(path, legacy_srt):
            hits.append(path)
    # 文件名越短越像“原始”那份
    hits.sort(key=len)
    return hits[0] if hits else None


def _load(path):
    last = None
    for enc in ("utf-8-sig", "utf-16", "cp932", "euc-jp", "gb18030"):
        try:
            return pysubs2.load(path, encoding=enc)
        except (UnicodeDecodeError, UnicodeError) as e:
            last = e
        except Exception as e:  # 格式问题不再换编码重试
            raise RuntimeError(f"读取字幕失败 {path}: {e}")
    raise RuntimeError(f"无法识别字幕编码 {path}: {last}")


_TTML_DOC = re.compile(rb"<\?xml[^>]*\?>\s*(<tt[\s>].*?</tt>)", re.S)
_NS = {"tt": "http://www.w3.org/ns/ttml", "tts": "http://www.w3.org/ns/ttml#styling"}


def _ttml_time(s):
    s = s.strip()
    if s.endswith("s") and ":" not in s:
        return float(s[:-1])
    h, m, sec = s.split(":")[:3]
    return int(h) * 3600 + int(m) * 60 + float(sec)


def is_arib_ttml(data):
    return b"arib-ttml" in data or (b"<tt" in data and b"http://www.w3.org/ns/ttml" in data)


def load_arib_ttml(path, ruby_ratio=0.7):
    """解析 4K/8K 广播（ARIB-TTML）的字幕数据流：一条字幕 = 一个 TTML 文档。

    时间是相对节目起点的，和视频时间轴之间通常差 1 秒左右，后面融合时会和语音自动对时。"""
    import xml.etree.ElementTree as ET

    raw = open(path, "rb").read()
    cues = []
    for m in _TTML_DOC.finditer(raw):
        try:
            root = ET.fromstring(m.group(1))
        except ET.ParseError:
            continue
        sizes = {}
        for st in root.iter(f"{{{_NS['tt']}}}style"):
            fs = st.get(f"{{{_NS['tts']}}}fontSize")
            if fs:
                sizes[st.get("{http://www.w3.org/XML/1998/namespace}id")] = float(fs.split()[-1].rstrip("px") or 0)
        regions = {}
        for rg in root.iter(f"{{{_NS['tt']}}}region"):
            org = (rg.get(f"{{{_NS['tts']}}}origin") or "0px 0px").split()
            try:
                regions[rg.get("{http://www.w3.org/XML/1998/namespace}id")] = (float(org[1].rstrip("px")), float(org[0].rstrip("px")))
            except (ValueError, IndexError):
                pass
        for div in root.iter(f"{{{_NS['tt']}}}div"):
            if not div.get("begin"):
                continue
            begin = _ttml_time(div.get("begin"))
            end = _ttml_time(div.get("end")) if div.get("end") else begin + 3.0
            paras = []
            for p in div.iter(f"{{{_NS['tt']}}}p"):
                spans = []
                for sp in p.iter(f"{{{_NS['tt']}}}span"):
                    size = max((sizes.get(k, 0) for k in (sp.get("style") or "").split()), default=0)
                    spans.append((size, "".join(sp.itertext())))
                if not spans:
                    spans = [(0, "".join(p.itertext()))]
                paras.append((regions.get(p.get("region"), (0, 0)), spans))
            # 振假名是小一号的字，常单独占一个段落：按整条字幕的正文字号过滤
            body = max((s for _, spans in paras for s, _ in spans), default=0)
            lines = []
            for pos, spans in paras:
                text = "".join(t for s, t in spans if not body or not s or s >= body * ruby_ratio).strip()
                if text:
                    lines.append((pos, text))
            lines.sort(key=lambda x: x[0])
            text = "\n".join(t for _, t in lines)
            if text and end > begin:
                cues.append({"start": round(begin, 3), "end": round(end, 3), "text": text})
    cues.sort(key=lambda c: c["start"])
    out = []
    for c in cues:  # 同一条字幕可能重复发送
        if out and out[-1]["text"] == c["text"] and c["start"] - out[-1]["end"] < 0.3:
            out[-1]["end"] = max(out[-1]["end"], c["end"])
            continue
        out.append(c)
    for i, c in enumerate(out):
        c["id"] = f"c{i + 1}"
    return out


def load_cues(path, ruby_ratio=0.7):
    subs = _load(path)
    raw = []
    for ev in subs.events:
        if ev.is_comment or ev.is_drawing:
            continue
        text = ev.text
        if "\\p1" in text or "\\p2" in text:
            continue
        style = subs.styles.get(ev.style)
        size = float(style.fontsize) if style else 0.0
        m = _FS.findall(text)
        if m:
            size = float(m[-1])
        pos = _POS.search(text)
        y = float(pos.group(2)) if pos else (ev.marginv or 0)
        plain = _TAG.sub("", text).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        plain = "\n".join(l.strip() for l in plain.split("\n") if l.strip())
        if not plain:
            continue
        raw.append({"start": ev.start / 1000, "end": ev.end / 1000, "text": plain, "size": size, "y": y})
    if not raw:
        return []
    # ARIB 转出来的 ass 常把每行/振假名拆成单独事件：小字号视为振假名丢弃
    sizes = [r["size"] for r in raw if r["size"] > 0]
    if sizes:
        body = statistics.median(sizes)
        raw = [r for r in raw if r["size"] <= 0 or r["size"] >= body * ruby_ratio]
    # 同一时间段的多行合并成一条
    raw.sort(key=lambda r: (round(r["start"], 2), round(r["end"], 2), r["y"]))
    cues = []
    for r in raw:
        if cues and abs(cues[-1]["start"] - r["start"]) < 0.05 and abs(cues[-1]["end"] - r["end"]) < 0.05:
            if r["text"] not in cues[-1]["text"]:
                cues[-1]["text"] += "\n" + r["text"]
            continue
        # 连续重复（ARIB 常见：同一句被刷新多次）
        if cues and cues[-1]["text"] == r["text"] and r["start"] - cues[-1]["end"] < 0.3:
            cues[-1]["end"] = max(cues[-1]["end"], r["end"])
            continue
        cues.append({"start": r["start"], "end": r["end"], "text": r["text"]})
    cues = [c for c in cues if c["end"] > c["start"]]
    for i, c in enumerate(cues):
        c["id"] = f"c{i + 1}"
    return cues
