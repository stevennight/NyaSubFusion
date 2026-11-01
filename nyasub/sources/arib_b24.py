"""2K 广播字幕（ARIB STD-B24，ffmpeg 里叫 arib_caption）：自己解码，按电视上的样子整理成一屏屏的字幕。

只用在降过级的数据上（degraded）：BS4K 节目录成 .ts 时，录制端把 4K 字幕（ARIB-TTML）转成了 2K 字幕，
转的时候丢了东西，要按 NHK 自己 4K 字幕的样子补回来，而这要看到原始数据。原生的 2K 字幕照旧用 libaribcaption。
降过级的数据：

- 接下一条的箭头 ➡、歌曲 ♬、画外音 ⚟ 这些 2K 字符集里没有的符号，发的是“〓”（显示不了的字）：按位置换回来
  （行尾 → ➡；行首、后面是“〜”或空格 → ♬；行首、后面直接是话 → ⚟）；
- 每行都从最左边写起：NHK 的字幕是每行居中的，输出时居中（上下位置照原样）；
- 振假名是和台词一样大的一行，写在那个词上一行：和下面那行汉字的读音对得上的，当成振假名（小字，放到那个词上面），
  对不上的（真的台词、读法特殊认不出的）保持是普通的一行，不会丢台词。

数据用 ffprobe 逐个包读出来（带时间），解码出每个字的位置、大小、颜色。
"""
import json
import re
import subprocess
import unicodedata

PLANE = (960, 540)  # 字幕平面（2K 广播的标准大小），字的位置都是这个大小下的像素
COLORS = ["#000000", "#FF0000", "#00FF00", "#FFFF00", "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF"]

KANJI, ALNUM, HIRA, KATA, MACRO, ADDSYM, JISKATA = 0x42, 0x4A, 0x30, 0x31, 0x70, 0x3B, 0x49
PROP = {0x36: ALNUM, 0x37: HIRA, 0x38: KATA}  # 比例字体的版本，字一样
TWO_BYTE = {KANJI, ADDSYM, 0x39, 0x3A, 0x3C}
HIRA_EXTRA = "ゝゞー。「」、・"
KATA_EXTRA = "ヽヾー。「」、・"


def _macro(*parts):
    return bytes.fromhex("".join(parts))


# 默认宏 0x60～0x6F（ARIB STD-B24 规定的字符集指定 + 调用）
DEFAULT_MACROS = {
    0x60: _macro("1b2442", "1b2931", "1b2a30", "1b2b2070", "0f1d7d"),
    0x61: _macro("1b2442", "1b2932", "1b2a30", "1b2b2070", "0f1d7d"),
    0x62: _macro("1b2442", "1b292041", "1b2a30", "1b2b2070", "0f1d7d"),
    0x63: _macro("1b2832", "1b2934", "1b2a35", "1b2b2070", "0f1d7d"),
    0x64: _macro("1b2832", "1b2933", "1b2a35", "1b2b2070", "0f1d7d"),
    0x65: _macro("1b2832", "1b292041", "1b2a35", "1b2b2070", "0f1d7d"),
    0x66: _macro("1b282041", "1b292042", "1b2a2043", "1b2b2070", "0f1d7d"),
    0x67: _macro("1b282044", "1b292045", "1b2a2046", "1b2b2070", "0f1d7d"),
    0x68: _macro("1b282047", "1b292048", "1b2a2049", "1b2b2070", "0f1d7d"),
    0x69: _macro("1b28204a", "1b29204b", "1b2a204c", "1b2b2070", "0f1d7d"),
    0x6A: _macro("1b28204d", "1b29204e", "1b2a204f", "1b2b2070", "0f1d7d"),
    0x6B: _macro("1b2442", "1b292042", "1b2a30", "1b2b2070", "0f1d7d"),
    0x6C: _macro("1b2442", "1b292043", "1b2a30", "1b2b2070", "0f1d7d"),
    0x6D: _macro("1b2442", "1b292044", "1b2a30", "1b2b2070", "0f1d7d"),
    0x6E: _macro("1b2831", "1b2930", "1b2a4a", "1b2b2070", "0f1d7d"),
    0x6F: _macro("1b2832", "1b2934", "1b2a35", "1b2b2070", "0f1d7d"),
}


# ---------------- 读包 ----------------
def read_packets(video, sub_index):
    """字幕轨的每个包：[(相对视频开头的秒数, 数据)]。sub_index：第几条字幕轨（ffprobe 的 s:N）。"""
    cmd = ["ffprobe", "-v", "error", "-select_streams", f"s:{sub_index}", "-show_entries", "format=start_time",
           "-show_packets", "-show_data", "-of", "json", video]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    d = json.loads(out.decode("utf-8", "replace"))
    start = float((d.get("format") or {}).get("start_time") or 0)
    pk = []
    for p in d.get("packets", []):
        if "pts_time" not in p or not p.get("data"):
            continue
        pk.append((float(p["pts_time"]) - start, _hexdump(p["data"])))
    return pk


def _hexdump(text):
    """ffprobe -show_data 的十六进制转储 → bytes（每行“偏移: 十六进制  ASCII”）。"""
    out = bytearray()
    for line in text.strip().splitlines():
        out += bytes.fromhex(line[10:49].replace(" ", ""))
    return bytes(out)


# ---------------- 解码 ----------------
class _State:
    def __init__(self):
        self.g = [("g", KANJI), ("g", ALNUM), ("g", HIRA), ("g", MACRO)]
        self.gl, self.gr, self.single = 0, 2, None
        self.sdf, self.sdp = PLANE, (0, 0)
        self.ssm, self.shs, self.svs = (36, 36), 4, 24
        self.size = (1.0, 1.0)
        self.fg = "#FFFFFF"
        self.x = self.y = 0.0  # 当前字格左上角
        self.chars = []

    def cell(self):
        sx, sy = self.size
        return (self.ssm[0] + self.shs) * sx, (self.ssm[1] + self.svs) * sy

    def put(self, ch):
        cw, chh = self.cell()
        sx, sy = self.size
        self.chars.append({"c": ch, "x": self.x, "y": self.y, "w": cw, "h": chh, "fw": self.ssm[0] * sx,
                           "fh": self.ssm[1] * sy, "sp": self.shs * sx, "fg": self.fg})
        self.x += cw
        if self.x >= self.sdp[0] + self.sdf[0] - 0.5:  # 写到头：换行
            self.x = self.sdp[0]
            self.y += chh


def _char(st, cs, b1, b2=None):
    kind, f = cs
    if kind == "drcs":  # 外字：图形，没有对应的字
        return st.put("〓")
    f = PROP.get(f, f)
    if f in TWO_BYTE:
        if b1 >= 0x75:  # 第 85 区以后是 ARIB 追加符号，常用的这里没有对应：当成显示不了的字
            return st.put("〓")
        try:
            return st.put(bytes([b1 | 0x80, b2 | 0x80]).decode("euc_jp"))
        except UnicodeDecodeError:
            return st.put("〓")
    if f == ALNUM:
        return st.put(chr(0xFEE0 + b1))
    if f == HIRA:
        return st.put(chr(0x3041 + b1 - 0x21) if b1 < 0x77 else HIRA_EXTRA[b1 - 0x77])
    if f == KATA:
        return st.put(chr(0x30A1 + b1 - 0x21) if b1 < 0x77 else KATA_EXTRA[b1 - 0x77])
    if f == JISKATA:
        return st.put(chr(0xFF61 + b1 - 0x21))
    return st.put("〓")


def _csi(st, params, final):
    nums = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else []
    if final == 0x56 and len(nums) >= 2:    # SDF 显示区大小
        st.sdf = (nums[0], nums[1])
    elif final == 0x5F and len(nums) >= 2:  # SDP 显示区位置
        st.sdp = (nums[0], nums[1])
    elif final == 0x57 and len(nums) >= 2:  # SSM 字的大小
        st.ssm = (nums[0], nums[1])
    elif final == 0x58 and nums:            # SHS 字间距
        st.shs = nums[0]
    elif final == 0x59 and nums:            # SVS 行间距
        st.svs = nums[0]
    elif final == 0x61 and len(nums) >= 2:  # ACPS 按像素定位（字格左下角）
        st.x, st.y = nums[0], nums[1] - st.cell()[1]


def _run(st, data):
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        i += 1
        if b == 0x1B:  # ESC：调用 / 指定字符集
            c = data[i]
            if c in (0x6E, 0x6F):
                st.gl = c - 0x6C
                i += 1
            elif c in (0x7E, 0x7D, 0x7C):
                st.gr = 0x7F - c
                i += 1
            elif c in (0x28, 0x29, 0x2A, 0x2B):
                g = c - 0x28
                if data[i + 1] == 0x20:
                    st.g[g] = ("drcs", data[i + 2])
                    i += 3
                else:
                    st.g[g] = ("g", data[i + 1])
                    i += 2
            elif c == 0x24:
                d = data[i + 1]
                if d in (0x28, 0x29, 0x2A, 0x2B):
                    g = d - 0x28
                    if data[i + 2] == 0x20:
                        st.g[g] = ("drcs", data[i + 3])
                        i += 4
                    else:
                        st.g[g] = ("g", data[i + 2])
                        i += 3
                else:
                    st.g[0] = ("g", d)
                    i += 2
            else:
                i += 1
            continue
        if b == 0x0F:
            st.gl = 0
        elif b == 0x0E:
            st.gl = 1
        elif b in (0x19, 0x1D):
            st.single = 2 if b == 0x19 else 3
        elif b == 0x0C:  # CS 清屏
            st.chars = []
        elif b == 0x1C:  # APS 按行列定位
            cw, ch = st.cell()
            st.y = st.sdp[1] + (data[i] - 0x40) * ch
            st.x = st.sdp[0] + (data[i + 1] - 0x40) * cw
            i += 2
        elif b == 0x0D:  # APR 回到行首、下一行
            st.x = st.sdp[0]
            st.y += st.cell()[1]
        elif b == 0x09:
            st.x += st.cell()[0]
        elif b == 0x08:
            st.x -= st.cell()[0]
        elif b == 0x0A:
            st.y += st.cell()[1]
        elif b == 0x0B:
            st.y -= st.cell()[1]
        elif b == 0x16:  # PAPF 前进几格
            st.x += (data[i] - 0x40) * st.cell()[0]
            i += 1
        elif b == 0x20:
            st.put("　")
        elif 0x80 <= b <= 0x87:
            st.fg = COLORS[b - 0x80]
        elif b == 0x88:  # SSZ 小号
            st.size = (0.5, 0.5)
        elif b == 0x89:  # MSZ 中号（半宽）
            st.size = (0.5, 1.0)
        elif b == 0x8A:  # NSZ 标准
            st.size = (1.0, 1.0)
        elif b == 0x90:  # COL 调色板
            if data[i] == 0x20:
                i += 2
            else:
                if (data[i] & 0xF0) == 0x40:
                    st.fg = COLORS[data[i] & 0x07]
                i += 1
        elif b in (0x8B, 0x91, 0x93, 0x94, 0x97, 0x98):
            i += 1
        elif b == 0x92:
            i += 2 if data[i] == 0x20 else 1
        elif b == 0x95:  # 宏定义：跳到结束
            while i < n - 1 and not (data[i] == 0x95 and data[i + 1] == 0x4F):
                i += 1
            i += 2
        elif b == 0x9B:  # CSI：参数直到 “空格 + 结束字节”
            j = i
            while j < n and not (0x40 <= data[j] <= 0x6F and data[j - 1] == 0x20):
                j += 1
            if j < n:
                _csi(st, data[i:j - 1].decode("ascii", "replace").strip(), data[j])
            i = j + 1
        elif b == 0x9D:  # TIME
            i += 2 if data[i] == 0x20 else 1
        elif 0x21 <= b <= 0x7E or 0xA1 <= b <= 0xFE:
            g = st.single if st.single is not None else (st.gl if b < 0x80 else st.gr)
            st.single = None
            cs, b1 = st.g[g], b & 0x7F
            if cs == ("g", MACRO):
                if b1 in DEFAULT_MACROS:
                    _run(st, DEFAULT_MACROS[b1])
                continue
            if (cs[0] == "g" and cs[1] in TWO_BYTE) or cs == ("drcs", 0x40):
                _char(st, cs, b1, data[i] & 0x7F)
                i += 1
            else:
                _char(st, cs, b1)


def decode(packets):
    """[(时间, 包)] → 每次画面变化：[{"t", "chars": [{"c", "x", "y", "w", "h", "fw", "fh", "sp", "fg"}]}]。"""
    st = _State()
    out = []
    for t, raw in packets:
        if len(raw) < 3 or raw[0] != 0x80:
            continue
        d = raw[3 + (raw[2] & 0x0F):]
        if len(d) < 5 or (d[0] >> 2) in (0x00, 0x20):  # 字幕管理数据（不是正文）
            continue
        body = d[5:5 + ((d[3] << 8) | d[4])]
        k = 1 + (5 if (body[0] >> 6) in (1, 2) else 0)
        end = k + 3 + ((body[k] << 16) | (body[k + 1] << 8) | body[k + 2])
        k += 3
        changed = False
        while k + 5 <= end and body[k] == 0x1F:
            ln = (body[k + 2] << 16) | (body[k + 3] << 8) | body[k + 4]
            if body[k + 1] == 0x20:  # 正文
                _run(st, body[k + 5:k + 5 + ln])
                changed = True
            k += 5 + ln
        if changed:
            out.append({"t": round(t, 3), "chars": [dict(c) for c in st.chars]})
    return out


def degraded(statements):
    """是不是降过级的数据（从 4K 字幕转出来的那种）：每行都从最左边写起，也没有小号的字（振假名）。
    原生的 2K 字幕每行有自己的位置、振假名是小字，libaribcaption 转得很好（还带背景、颜色、外字），不用这里补。"""
    starts, small = [], False
    for s in statements:
        rows = {}
        for c in s["chars"]:
            rows.setdefault(round(c["y"] + c["h"], 1), c["x"])
            small = small or c["fh"] < 30
        starts += rows.values()
    return bool(starts) and not small and sum(x < 1 for x in starts) >= 0.95 * len(starts)


# ---------------- 整理成一屏屏 ----------------
KANA_ROW = re.compile(r"^[぀-ヿ　]+$")


def fix_geta(text):
    """“〓”换回 NHK 用的符号：行尾 → ➡（接下一条），行首、后面是“〜”或空格 → ♬（歌），行首、后面直接是话 → ⚟（画外音）。"""
    if text.endswith("〓"):
        text = text[:-1] + "➡"
    if text.startswith("〓"):
        text = ("♬" if text[1:2] in ("〜", "　", "") else "⚟") + text[1:]
    return text


def _rows(chars):
    """同一行（字格底边一样）的字连起来：[{"y": 底边, "x", "x1", "text", "color", "fw", "fh", "sp", "h"}]，从上往下。"""
    by = {}
    for c in chars:
        by.setdefault(round(c["y"] + c["h"], 1), []).append(c)
    out = []
    for yb, cs in sorted(by.items()):
        cs.sort(key=lambda c: c["x"])
        text = "".join(c["c"] for c in cs).rstrip("　")
        if text.strip("　"):
            c0 = cs[0]
            out.append({"y": yb, "x": c0["x"], "x1": cs[-1]["x"] + cs[-1]["w"], "text": text, "color": c0["fg"],
                        "fw": c0["fw"], "fh": c0["fh"], "sp": c0["sp"], "h": c0["h"]})
    return out


def _plain(s):
    """平假名、去掉浊点（连浊：鮨 すし / ずし）。"""
    from ..lang import ruby

    return "".join(unicodedata.normalize("NFD", ch)[0] for ch in ruby.to_hira(s))


def _kanji_segments(pack, line):
    """这行里每段汉字的 (开始, 结束, 读音)（分词给的）。"""
    from ..lang import ruby

    out, pos = [], 0
    for w in pack.annotate(line):
        for seg in w["segs"]:
            n = len(seg[0])
            if len(seg) > 1 and any(ruby.is_kanji(c) for c in seg[0]):
                out.append((pos, pos + n, seg[1]))
            pos += n
    return out


def match_ruby(pack, row, line):
    """读音行里用空格隔开的每组读音，在下面那行里注的是哪几个字：[(读音, (开始, 结束))]；有一组对不上就返回 None。"""
    try:
        segs = _kanji_segments(pack, line)
    except ImportError:  # 没装分词：认不出振假名，都当普通的行
        return None
    spans = []
    for g in [g for g in row.split("　") if g]:
        want, hit = _plain(g), None
        for a in range(len(segs)):  # 连着的几段汉字合起来正好是这个读音
            acc = ""
            for b in range(a, len(segs)):
                if b > a and segs[b][0] != segs[b - 1][1]:
                    break
                acc += segs[b][2]
                if _plain(acc) == want:
                    hit = (segs[a][0], segs[b][1])
                    break
                if len(_plain(acc)) > len(want):
                    break
            if hit:
                break
        if not hit:  # 一个词开头 / 结尾几个字的读音（日光街道 にっこう、足立区 あだち）：按读音长短分字
            for a, b, r in segs:
                pr = _plain(r)
                if len(pr) > len(want) and (pr.startswith(want) or pr.endswith(want)):
                    k = max(1, int(len(want) / len(pr) * (b - a) + 0.4))
                    hit = (a, a + k) if pr.startswith(want) else (b - k, b)
                    break
        if not hit:
            return None
        spans.append((g, hit))
    return spans or None


def screens(statements, end_time):
    """每次画面变化 → 一屏：{"start", "end", "lines": [行], "rubies": [{"y", "text", "span", "line": 下面那行的序号}]}。
    行：_rows 的格式，文字里的“〓”已经换回符号。没有字的（清屏）不算一屏；同样的画面重发的并成一屏。"""
    from .. import lang

    pack = lang.get("ja")
    out = []
    for k, s in enumerate(statements):
        rows = _rows(s["chars"])
        if not rows:
            continue
        t1 = statements[k + 1]["t"] if k + 1 < len(statements) else max(end_time, s["t"] + 3)
        lines, rubies = [], []
        for i, r in enumerate(rows):
            # 读音行注的是哪一行：一连几行读音（一行有好几个词要注）下面紧挨着的第一行字
            j = i
            while j + 1 < len(rows) and abs(rows[j + 1]["y"] - rows[j]["y"] - r["h"]) < 1 and KANA_ROW.match(rows[j + 1]["text"]):
                j += 1
            base = rows[j + 1] if j + 1 < len(rows) and abs(rows[j + 1]["y"] - rows[j]["y"] - r["h"]) < 1 else None
            spans = match_ruby(pack, r["text"], base["text"]) if KANA_ROW.match(r["text"]) and base else None
            if spans:
                rubies += [{"y": base["y"], "text": g, "span": sp} for g, sp in spans]
            else:
                lines.append(dict(r, text=fix_geta(r["text"])))
        for rb in rubies:
            rb["line"] = next((n for n, ln in enumerate(lines) if ln["y"] == rb["y"]), None)
        cur = {"start": s["t"], "end": t1, "lines": lines, "rubies": [rb for rb in rubies if rb["line"] is not None]}
        prev = out[-1] if out else None
        if prev and prev["lines"] == cur["lines"] and prev["rubies"] == cur["rubies"] and cur["start"] - prev["end"] < 0.3:
            prev["end"] = cur["end"]
        else:
            out.append(cur)
    return out
