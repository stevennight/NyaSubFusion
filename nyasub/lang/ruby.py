"""注音通用工具：假名转换、罗马字、送假名对齐、拼音声调、把排好的行对回注音词。

“注音词”（word）是语言包 annotate() 的输出，形如：
    {"t": "引き上げる", "segs": [["引", "ひ"], ["き"], ["上", "あ"], ["げる"]],
     "pos": "verb", "p": False, "num": False, "src": "auto", "guess": False}
segs 里每段是 [底字] 或 [底字, 读音, ...]（中文是 [字, 带调拼音, 注音符号]）。
"""
import re
import unicodedata

# ---------------- 假名 ----------------


def to_hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s or "")


def to_kata(s):
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s or "")


def is_kanji(c):
    return "㐀" <= c <= "鿿" or c in "々〆ヶ"


def is_kana(c):
    return "぀" <= c <= "ヿ"


_KANA = {}
for _p in ("あa いi うu えe おo かka きki くku けke こko さsa しshi すsu せse そso たta ちchi つtsu てte とto "
           "なna にni ぬnu ねne のno はha ひhi ふfu へhe ほho まma みmi むmu めme もmo やya ゆyu よyo "
           "らra りri るru れre ろro わwa ゐi ゑe をo んn がga ぎgi ぐgu げge ごgo ざza じji ずzu ぜze ぞzo "
           "だda ぢji づzu でde どdo ばba びbi ぶbu べbe ぼbo ぱpa ぴpi ぷpu ぺpe ぽpo "
           "ぁa ぃi ぅu ぇe ぉo ゔvu ゎwa").split():
    _KANA[_p[0]] = _p[1:]
_SMALL_Y = {"ゃ": "a", "ゅ": "u", "ょ": "o"}
_SMALL_V = {"ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o"}
_PUNCT = {"、": ",", "。": ".", "・": " ", "　": " ", "（": "(", "）": ")", "？": "?", "！": "!", "「": '"', "」": '"',
          "『": '"', "』": '"', "…": "...", "～": "~", "〜": "~", "：": ":"}


SOKUON = "ʔ"  # 词尾的促音，等下一个词来决定双写哪个辅音


def romaji(kana, keep_sokuon=False):
    """平假名 / 片假名 → 罗马字（Hepburn，长音不加长音符号）。
    keep_sokuon：词尾的「っ」保留成 SOKUON，由调用方接到下一个词上（食べなかっ|た → tabenakatta）。"""
    s = to_hira(unicodedata.normalize("NFKC", kana))
    out = ""
    i = 0
    while i < len(s):
        c, n = s[i], s[i + 1] if i + 1 < len(s) else ""
        if c == "っ":
            if not n and keep_sokuon:
                out += SOKUON
                i += 1
                continue
            nx = _KANA.get(n, "")
            out += "t" if nx.startswith("ch") else (nx[:1] if nx[:1].isalpha() and nx[:1] not in "aiueon" else "")
            i += 1
            continue
        if c == "ー":
            if out and out[-1] in "aiueo":
                out += out[-1]
            i += 1
            continue
        r = _KANA.get(c)
        if r is None:
            out += _PUNCT.get(c, c)
            i += 1
            continue
        if n in _SMALL_Y and r.endswith("i") and len(r) > 1:
            r = (r[:-1] if r in ("shi", "chi", "ji") else r[:-1] + "y") + _SMALL_Y[n]
            i += 1
        elif n in _SMALL_V and r in ("fu", "vu", "te", "de", "u"):  # ファ、ヴィ、ティ…
            r = {"fu": "f", "vu": "v", "te": "t", "de": "d", "u": "w"}[r] + _SMALL_V[n]
            i += 1
        elif r == "n" and n and (_KANA.get(n, "x")[:1] in "aiueoy"):
            r = "n'"
        out += r
        i += 1
    return out


# ---------------- 送假名对齐 ----------------


def align_okurigana(surface, reading):
    """把整词读音分给词里的各段汉字：「引き上げる」+「ひきあげる」→ [引|ひ] き [上|あ] げる。
    对不上（熟字训「今朝」、读音与写法不一致）时整词注音。没有汉字时不注音。"""
    reading = to_hira(reading)
    runs = []
    for c in surface:
        k = is_kanji(c)
        if runs and runs[-1][1] == k:
            runs[-1][0] += c
        else:
            runs.append([c, k])
    if not any(k for _, k in runs):
        return [[surface]]
    if len(runs) == 1:
        return [[surface, reading]]
    pattern = "".join("(.+?)" if k else "(" + re.escape(to_hira(t)) + ")" for t, k in runs)
    m = re.fullmatch(pattern, reading)
    if not m:
        return [[surface, reading]]
    return [[t, g] if k else [t] for (t, k), g in zip(runs, m.groups())]


# ---------------- 拼音 ----------------

_TONE = {"ā": ("a", 1), "á": ("a", 2), "ǎ": ("a", 3), "à": ("a", 4), "ē": ("e", 1), "é": ("e", 2), "ě": ("e", 3),
         "è": ("e", 4), "ī": ("i", 1), "í": ("i", 2), "ǐ": ("i", 3), "ì": ("i", 4), "ō": ("o", 1), "ó": ("o", 2),
         "ǒ": ("o", 3), "ò": ("o", 4), "ū": ("u", 1), "ú": ("u", 2), "ǔ": ("u", 3), "ù": ("u", 4), "ǖ": ("ü", 1),
         "ǘ": ("ü", 2), "ǚ": ("ü", 3), "ǜ": ("ü", 4), "ń": ("n", 2), "ň": ("n", 3), "ǹ": ("n", 4), "ḿ": ("m", 2)}


def tone_style(syl, style):
    """带调拼音 → mark（原样）/ number（pin1）/ none（pin）。"""
    if style == "mark":
        return syl
    out, n = "", 0
    for ch in syl:
        if ch in _TONE:
            base, n = _TONE[ch]
            out += base
        else:
            out += ch
    return out + (str(n) if n else "") if style == "number" else out


# ---------------- 把排好的行对回注音词 ----------------


def _stream(words):
    out = []
    for wi, w in enumerate(words):
        for si, seg in enumerate(w["segs"]):
            for ch in seg[0]:
                out.append((ch, (wi, si)))
    return out


def _same(a, b):
    return a == b or unicodedata.normalize("NFKC", a) == unicodedata.normalize("NFKC", b)


def chunkify(lines, words):
    """lines 是排版后的若干行文字（标点可能被替换、删掉，可能插入了空格）。
    返回每行的块列表：[{"text": "引", "key": (词序号, 段序号) 或 None}]，key 相同的连续字合成一块。"""
    stream = _stream(words)
    p = 0
    out = []
    for line in lines:
        chunks = []
        for ch in line:
            key = None
            for q in range(p, min(len(stream), p + 8)):
                if _same(stream[q][0], ch):
                    key, p = stream[q][1], q + 1
                    break
            if chunks and chunks[-1]["key"] == key:
                chunks[-1]["text"] += ch
            else:
                chunks.append({"text": ch, "key": key})
        out.append(chunks)
    return out
