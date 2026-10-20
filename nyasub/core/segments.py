"""一条字幕里用“｜”分出的几段：什么时候算数、每段各自什么时候显示。

大模型把相邻的几条字幕合成一条（一句话被拆在两条字幕里、半句在画面字里）时，用“｜”按原来的字幕条分段，
输出时每段单独一屏。每段的时间按这段原文在来源字幕里的位置算：落在两条来源字幕交界附近的，直接用交界的时间，
这样和原来的字幕 / 语音对得上；对不上的地方才按字数比例。
"""
from difflib import SequenceMatcher

from .. import lang
from ..textutil import norm

SPEECH = ("subs", "asr")
MIN_PART = 0.5  # 每段至少显示这么久，短于这个就不按来源分时间


def pieces(text):
    return [p for p in (text or "").split("｜") if p.strip()]


def bars_ok(cue):
    """原文和每个译文的段数一样（至少两段）才按“｜”分段。
    对不上（大模型拿“｜”当换行用，只在译文里写了）就当成一整句，排版时按长度自己断。"""
    n = len(pieces(cue["orig"]["text"]))
    if n < 2:
        return False
    trs = [v.get("text", "") for v in (cue.get("tr") or {}).values() if v.get("text")]
    return all(len(pieces(t)) == n for t in trs)


def valid_parts(parts, cue):
    """手动调过的每段时间还能不能用：段数对得上、首尾就是这条字幕的起止、一段接一段、每段至少 0.2 秒。"""
    if not parts or len(parts) != len(pieces(cue["orig"]["text"])):
        return False
    if abs(parts[0][0] - cue["start"]) > 0.01 or abs(parts[-1][1] - cue["end"]) > 0.01:
        return False
    return all(b - a >= 0.2 for a, b in parts) and all(abs(p[1] - q[0]) < 0.01 for p, q in zip(parts, parts[1:]))


def joiner(code):
    """去掉“｜”时两段之间放什么：按词分隔的语言和中文（中文字幕句中停顿用空格）放空格，日文直接接上。"""
    pack = lang.get(code)
    return " " if pack.word_based or pack.code.startswith("zh") else ""


def unbar(text, code):
    return joiner(code).join(p.strip() for p in (text or "").split("｜") if p.strip())


def sources(cue, items):
    """这条字幕的来源（按时间排，去掉同一段画面字识别出的重复条目）。"""
    out = []
    for s in sorted((items[i] for i in cue.get("sources", []) if i in items), key=lambda s: s["start"]):
        if any(norm(o["text"]) == norm(s["text"]) and s["start"] < o["end"] for o in out):
            continue
        out.append(s)
    return out


def single_event(cue, items):
    """来源只有一条（或几条同时出现的），也就是原来就是一屏：这种“｜”只是大模型拿来换行的，不分段。"""
    srcs = sources(cue, items)
    return bool(srcs) and all(abs(s["start"] - srcs[0]["start"]) < 0.3 for s in srcs)


def _timeline(srcs):
    """来源字幕按时间接起来：(规范化后的文字, 每个字的时间, 每条来源开头在文字里的位置 → 开始时间)。"""
    text, times, edges = "", [], {}
    for s in srcs:
        t = norm(s["text"])
        if not t:
            continue
        edges[len(text)] = s["start"]
        n = len(t)
        times += [s["start"] + (s["end"] - s["start"]) * i / n for i in range(n)]
        text += t
    edges[len(text)] = srcs[-1]["end"] if srcs else 0.0
    return text, times, edges


def proportional(cue):
    """按字数比例分时间（来源对不上时用）：用第一个译文的字数，没有译文用原文。"""
    tr = next((v["text"] for v in (cue.get("tr") or {}).values() if v.get("text")), "")
    ps = pieces(tr) if len(pieces(tr)) == len(pieces(cue["orig"]["text"])) else pieces(cue["orig"]["text"])
    w = [max(1, len(norm(p))) for p in ps]
    t, out = cue["start"], []
    for x in w:
        d = (cue["end"] - cue["start"]) * x / sum(w)
        out.append([round(t, 3), round(t + d, 3)])
        t += d
    return out


def part_times(cue, items):
    """每段的 [开始, 结束]；算不出来（来源对不上、某段太短）返回 None，排版时按字数比例分。"""
    ps = pieces(cue["orig"]["text"])
    if len(ps) < 2:
        return None
    t0, t1 = cue["start"], cue["end"]
    srcs = sources(cue, items)
    # 半句字幕、半句画面字都算；“同一时间字幕在说别的话”的画面字（另一种说法的概括）不算
    main = [s for s in srcs if s["src"] in SPEECH or not s.get("over_speech")]
    srcs = [s for s in (main or srcs) if s["end"] > t0 and s["start"] < t1]
    if not srcs:
        return None
    text, times, edges = _timeline(srcs)
    if not text:
        return None
    normed = [norm(p) for p in ps]
    orig = "".join(normed)
    sm = SequenceMatcher(None, orig, text, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if sum(b.size for b in blocks) < 0.5 * len(orig):
        return None

    def to_src(i):
        """原文第 i 个字 → 来源文字里的位置。"""
        for b in blocks:
            if b.a <= i < b.a + b.size:
                return b.b + (i - b.a)
        before = [b for b in blocks if b.a + b.size <= i]
        after = [b for b in blocks if b.a > i]
        if before and after:  # 夹在两块之间：按比例插
            p, q = before[-1], after[0]
            pa, pb, qa, qb = p.a + p.size, p.b + p.size, q.a, q.b
            return pb + (qb - pb) * (i - pa) / max(1, qa - pa)
        if before:
            p = before[-1]
            return min(len(text), p.b + p.size + (i - p.a - p.size))
        return max(0, after[0].b - (after[0].a - i))

    def at(pos):
        """来源文字位置 → 时间；离某条来源的开头不到 3 个字，就吸到那条来源的开始时间。"""
        near = min(edges, key=lambda e: abs(e - pos))
        if abs(near - pos) <= 3:
            return edges[near]
        k = min(len(times) - 1, max(0, int(round(pos))))
        return times[k]

    bounds, i = [], 0
    for p in normed[:-1]:
        i += len(p)
        bounds.append(min(t1, max(t0, at(to_src(i)))))
    cuts = [t0] + bounds + [t1]
    if any(b - a < MIN_PART for a, b in zip(cuts, cuts[1:])):
        return None
    return [[round(a, 3), round(b, 3)] for a, b in zip(cuts, cuts[1:])]
