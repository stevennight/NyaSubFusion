"""三路融合（规则版，不需要大模型）。

- 字幕 subs：文字最可信。
- 画面字 telop / info：和字幕重复的去掉；能和语音对上的算台词（telop），否则算说明文字（info）。
  字幕和画面字同时存在时两者都保留——这正是“半句字幕、半句画面字”的情况。
- 语音 asr：能被字幕 / 画面字解释的丢掉（只用来校准时间）；没有任何字幕覆盖的单独成条；
  时间重叠但文字对不上的（多半是听错了），作为“语音参考”附到那条上；画面字只是从这句话里摘的几个字、
  或者只有画面字而且一个词都对不上的（标题卡下面的另一句话），语音单独成条。
产出的 items 交给大模型做最终的合并 / 纠错 / 翻译；cues 是不经过大模型的原文合并字幕。
"""
import re
from difflib import SequenceMatcher

from ..textutil import covered, full_sim, norm, overlap, sim

# 字幕里不是“说话”的部分：（笑い声）（競りの声）这类括号说明、♪♬〜 音乐记号、➡ 接续符号
_NOT_SPEECH = re.compile(r"[（(\[［【][^）)\]］】]*[）)\]］】]|[♪♬〜～➡→]")


def spoken(text):
    """字幕里真正说出来的话（去掉说话人标注、音效 / 音乐说明）。"""
    return _NOT_SPEECH.sub("", text or "").strip()


def music_only(text):
    """只有音乐记号的字幕（「♬〜」）：只是告诉观众在放音乐，一挂十几秒，不翻译、不显示；后面带歌词的不算。"""
    return bool(re.search(r"[♪♬]", text or "")) and not re.sub(r"[♪♬〜～\s]", "", text)


def _concat(items, t0, t1, slack=0.0):
    hit = [x for x in items if overlap(x["start"] - slack, x["end"] + slack, t0, t1) > 0]
    return hit, "".join(x["text"] for x in hit)


def _coverage(intervals, t0, t1):
    segs = sorted((max(a, t0), min(b, t1)) for a, b in intervals if b > t0 and a < t1)
    total, cur0, cur1 = 0.0, None, None
    for a, b in segs:
        if cur1 is None or a > cur1:
            if cur1 is not None:
                total += cur1 - cur0
            cur0, cur1 = a, b
        else:
            cur1 = max(cur1, b)
    if cur1 is not None:
        total += cur1 - cur0
    return total / max(1e-6, t1 - t0)


def _logo_ids(events, duration):
    """台标 / 水印：顶部同一小块位置反复出现、累计时间很长。OCR 每次认成不同的字，所以只按位置判断。"""
    cand = [e for e in events if e["rect"][3] < 0.2]
    limit = max(30.0, duration * 0.02)
    out = set()
    for e in cand:
        x0, y0, x1, y1 = e["rect"]
        total = 0.0
        for f in cand:
            a0, b0, a1, b1 = f["rect"]
            if abs((a0 + a1) - (x0 + x1)) < 0.04 and abs((b0 + b1) - (y0 + y1)) < 0.04 \
                    and abs((a1 - a0) - (x1 - x0)) < 0.05 and abs((b1 - b0) - (y1 - y0)) < 0.03:
                total += f["end"] - f["start"]
        if total > limit:
            out.add(e["id"])
    return out


def _bottom_center(e):
    x0, y0, x1, y1 = e["rect"]
    return e["zone"] == "bottom" and abs((x0 + x1) / 2 - 0.5) < 0.12


def _is_junk(e, pack):
    n = e["text"].replace("\n", "")
    dur = e["end"] - e["start"]
    if not pack.meaningful.search(n) and len(n) <= 8:
        return True  # 很短、不像这门语言的文字：招牌、标签、OCR 噪声
    if dur < 1.0 and not _bottom_center(e):
        return True  # 一闪而过的背景文字
    return False


def _rescued_titles(ocr_events):
    """视觉复核偶尔把标题卡（打在木纹、水面这类有纹理的背景上）判成实物上的字。同一集里同样的字、差不多的位置
    别处判成了叠加字幕的，这条也算叠加字幕；和它同时出现、挨着的（标题卡的另一行“ナマコ”）一起算。返回这些 id。"""
    caps = [e for e in ocr_events if e.get("kind") == "caption"]
    scene = [e for e in ocr_events if e.get("kind") == "scene"]
    mid = lambda r: ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)

    def same_spot(a, b):
        (ax, ay), (bx, by) = mid(a["rect"]), mid(b["rect"])
        return abs(ax - bx) < 0.06 and abs(ay - by) < 0.06

    def touching(a, b):
        pad = 0.5 * min(a["rect"][3] - a["rect"][1], b["rect"][3] - b["rect"][1])
        ra, rb = a["rect"], b["rect"]
        return ra[0] < rb[2] + pad and rb[0] < ra[2] + pad and ra[1] < rb[3] + pad and rb[1] < ra[3] + pad

    saved = [e for e in scene if any(full_sim(e["text"], c["text"]) >= 90 and same_spot(e, c) for c in caps)]
    saved += [e for e in scene if e not in saved
              and any(abs(e["start"] - s["start"]) < 1.5 and touching(e, s) for s in saved)]
    return {e["id"] for e in saved}


def prefilter(ocr_events, duration, pack):
    """标记不需要的画面字：台标 / 水印、杂讯、视觉大模型判定的实物上的字（招牌、地图、包装…）。"""
    logos = _logo_ids(ocr_events, duration or max((e["end"] for e in ocr_events), default=0))
    titles = _rescued_titles(ocr_events)
    for e in ocr_events:
        e.pop("dropped", None)
        if e["id"] in logos:
            e["dropped"] = "logo"
        elif e.get("ruby_of"):  # 振假名：已经并到下面那条上（sources/ocr.attach_ruby）
            e["dropped"] = "ruby"
        elif _is_junk(e, pack):
            e["dropped"] = "junk"
        elif e.get("kind") == "scene" and e["id"] not in titles:
            e["dropped"] = "scene"
        e["prefiltered"] = e.get("dropped") in ("logo", "junk", "ruby")  # 台标和杂讯不用送去视觉复核


def _caption_lines(ocr_events, asr, pack, asr_th):
    """识别时把背景上的字（地图、报纸、海报）和下面的硬字幕连成了一个框，整框被判成实物上的字丢掉，
    硬字幕那一行也跟着没了。丢掉的框里逐行看：底部居中、和同一时间的语音对得上的那几行留下来当画面字
    （位置换成那几行的，时间收到对上的那段语音），别的行照旧丢掉。返回留下来的个数。"""
    n = 0
    for e in ocr_events:
        pieces = e.get("pieces") or []
        if e.get("dropped") != "scene" or e.get("vertical") or len(pieces) < 2:
            continue
        a_hit, a_text = _concat(asr, e["start"], e["end"])
        if not a_text:
            continue
        said_line = [p for p in pieces if p[1] >= 0.7 and len(norm(p[4])) >= 3 and pack.meaningful.search(p[4])
                     and sim(p[4], a_text) >= asr_th]
        lines = [p for p in said_line if abs((p[0] + p[2]) / 2 - 0.5) < 0.12]
        # 两行字幕左对齐时，短的那行不居中（「(ワニが)ほんと好き」在「お正月くらいしか食べられないから」上面）：
        # 和留下的某行左边对齐、上下紧挨着的也是这条字幕
        grew = True
        while lines and grew:
            grew = False
            for p in said_line:
                if p not in lines and any(abs(p[0] - q[0]) <= 0.03 and max(p[1], q[1]) - min(p[3], q[3]) <= (q[3] - q[1])
                                          for q in lines):
                    lines.append(p)
                    grew = True
        if not lines or len(lines) == len(pieces):
            continue
        lines.sort(key=lambda p: p[1])
        text = "\n".join(p[4] for p in lines)
        said = [a for a in a_hit if sim(a["text"], text) >= asr_th] or a_hit
        rect = [round(min(p[0] for p in lines), 4), round(min(p[1] for p in lines), 4),
                round(max(p[2] for p in lines), 4), round(max(p[3] for p in lines), 4)]
        e["caption"] = {"text": text, "rect": rect, "from": e["text"]}
        e.update(text=text, rect=rect, lines=len(lines), zone="bottom",
                 start=round(max(e["start"], min(a["start"] for a in said) - 0.3), 3),
                 end=round(min(e["end"], max(a["end"] for a in said) + 0.5), 3))
        e.pop("dropped")
        _make_room(e, ocr_events)
        n += 1
    return n


def _make_room(e, ocr_events):
    """救回来的字幕行时间是整框（背景上的字）出现的时间、只按语音收了一下，常比这一行真正出现得早 / 晚：
    同一位置同时还有另一条硬字幕（「自分の性別がわからないとか」之后才是框里的「決めたくないとか」）时，
    这一行不可能同时在那儿，时间让开那一条，免得排到它前面。让开后剩不到 0.5 秒的不动。"""
    r = e["rect"]
    for o in ocr_events:
        if o is e or o.get("dropped") or o.get("vertical"):
            continue
        q = o["rect"]
        if min(r[2], q[2]) - max(r[0], q[0]) <= 0 or min(r[3], q[3]) - max(r[1], q[1]) <= 0:
            continue
        if overlap(e["start"], e["end"], o["start"], o["end"]) <= 0:
            continue
        if o["start"] <= e["start"] + 0.3:
            if e["end"] - o["end"] >= 0.5:
                e["start"] = round(o["end"], 3)
        elif o["start"] - e["start"] >= 0.5:
            e["end"] = round(o["start"], 3)


def _mostly_said(text, said):
    """画面字的内容字幕基本都说到了：大部分（≥ 60%）出现在字幕里，或者没说到的不超过 3 个字（“1kg”和“１キロ”这种写法不同）。
    只有一小段对得上（字幕只有“仮設。”，画面字是“おれ仮設におった”，是另一个人说的另一句话）不算重复。"""
    if covered(text, said) >= 0.6:
        return True
    a, b = norm(text), norm(said)
    hit = {i for x in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if x.size >= 2
           for i in range(x.a, x.a + x.size)}
    return sum(1 for i, ch in enumerate(a) if i not in hit and ch.isalnum()) <= 3  # “＝”这种符号不算


_INTERJ = re.compile(r"^[ぁ-ゖー]{1,3}$")


def _content(speech):
    """语音里有内容的部分：去掉标点隔开的“ね。そう、あ、…うん”这种三个字以内的纯平假名语气词、应答。"""
    return "".join(p for p in re.split(r"[、。，,．.！？!?…\s]+", speech or "") if p and not _INTERJ.match(p))


def _share(speech, texts):
    """speech 有多少比例的字出现在这几条字里（每条分别对，按至少两个字连续相同的片段算；
    几条按时间排的先后不一定是说的先后，不能拼起来对）。"""
    a = norm(speech)
    if not a:
        return 1.0
    hit = set()
    for t in texts:
        for x in SequenceMatcher(None, a, norm(t), autojunk=False).get_matching_blocks():
            if x.size >= 2:
                hit.update(range(x.a, x.a + x.size))
    return len(hit) / len(a)


def _latin(text):
    """大半是拉丁字母（英文）。"""
    t = [ch for ch in norm(text) if ch.isalnum()]
    return bool(t) and sum(ch.isascii() and ch.isalpha() for ch in t) * 2 >= len(t)


def _unrelated(speech, hit):
    """同时只有画面字（没有广播字幕，也没有「♬〜」）、语音和画面字一个词都对不上（连两个字连续相同的都没有）、
    去掉语气词还有至少 4 个字：是画面字之外的另一句话（片尾标题卡挂着十几秒，旁白说“ぜひご覧ください”），
    附到画面字上当参考就丢了，单独成条。
    有广播字幕的地方，字幕没打的话是电视台有意不打的（音乐下的闲聊），照旧；英文的话配日文画面字
    （“Good night”和「グッドナイト」）是翻译关系、写法不同对不上，也照旧。"""
    c = _content(speech)
    texts = [x["text"] for x in hit]
    return (not any(x["src"] == "subs" for x in hit) and len(norm(c)) >= 4 and _share(c, texts) == 0
            and not _latin(speech) and not any(_latin(t) for t in texts))


def _quoted(text, speech):
    """画面字只是这句话里的几个字：几乎整个出现在语音里，而且不到语音（去掉语气词）的一半长（「刀剣」之于
    “刀剣の波紋や、切子の繊細な手仕事など…”）。和语音差不多长的（方言硬字幕、听错了几个字，
    「踊り食いね」之于“ね。そう、あ、踊り食いね、うん。”）不算。"""
    t = norm(text)
    return len(t) >= 2 and len(t) * 2 <= len(norm(_content(speech))) and covered(text, speech) >= 0.8


def _drop_vlm_dups(ocr_events, gap=1.0):
    """视觉复核“补出本地漏掉的字”时，模型给的框偶尔不对（框到了旁边没字的地方），本地明明认出了同一句，
    却当成漏掉的补了一条：位置不对，时间按那个框找也早了（「命日にお経さ読んで…」早出来 1.4 秒，盖住前一句）。
    和本地认出的某条字一样、时间重叠或相隔不到 1 秒的，丢掉补出来的这条，以本地的时间和位置为准。"""
    local = [e for e in ocr_events if e.get("found_by") != "vlm" and not e.get("dropped")]
    for e in ocr_events:
        t = norm(e["text"])
        if e.get("found_by") != "vlm" or e.get("dropped") or len(t) < 4:
            continue
        if any(full_sim(o["text"], e["text"]) >= 90 and o["start"] - gap <= e["end"] and e["start"] <= o["end"] + gap for o in local):
            e["dropped"] = "dup_vlm"


def _same_place(a, b, th=0.6):
    """两个框大半重合（交集占小的那个的 th 以上）。"""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return small > 0 and ix * iy >= th * small


def _join_split(ocr_events, gap=0.5):
    """同一行画面字被识别切成前后两条（「国連が昆虫食は世界の飢えを救う」31:16.9–20.2 和 20.2–21.3，背景一变跟踪就断了）：
    同一个位置、字一样、前后接着的，合成一条（时间接上），后面那条按重复去掉，否则同一句会显示、翻译两遍。
    逐字出现的字幕先认出前半截（「昆虫が」→「昆虫が 国とか世界を救っちゃう」、「今では」→「今では 似たような…」）：
    前半截并进后面完整的那条（开始时间提前），否则前半截单独译一遍，“昆虫｜昆虫能拯救…”。"""
    kept = []
    for e in sorted(ocr_events, key=lambda e: e["start"]):
        near = [p for p in reversed(kept) if -0.05 <= e["start"] - p["end"] <= gap and _same_place(p["rect"], e["rect"])]
        prev = next((p for p in near if len(norm(e["text"])) >= 2 and full_sim(p["text"], e["text"]) >= 90), None)
        if prev:
            prev["end"] = max(prev["end"], e["end"])
            e["dup_of"] = [prev["id"]]
            continue
        a = norm(e["text"])
        head = next((p for p in near if 2 <= len(norm(p["text"])) < len(a) and a.startswith(norm(p["text"]))), None)
        if head:
            kept.remove(head)
            e["start"] = min(e["start"], head["start"])
            head["dup_of"] = [e["id"]]
        kept.append(e)
    return kept


def _caption_block(pieces):
    """一框里的字幕那几行：底部居中的行，加上和它左边对齐、上下紧挨着的行（两行左对齐时短的那行不居中）。"""
    lines = [p for p in pieces if p[1] >= 0.8 and abs((p[0] + p[2]) / 2 - 0.5) < 0.12]
    grew = True
    while lines and grew:
        grew = False
        for p in pieces:
            if p not in lines and p[1] >= 0.7 and any(abs(p[0] - q[0]) <= 0.03 and max(p[1], q[1]) - min(p[3], q[3]) <= (q[3] - q[1])
                                                      for q in lines):
                lines.append(p)
                grew = True
    return lines


def _split_supers(ocr_events, pack):
    """识别时把人名条（「「ふすべ漬け」づくりの名人 吉田和子さん」，在字幕右上方）和下面的字幕连成了一框：
    整框会被当成一句台词翻译（“雪菜烫腌名手 吉田和子｜以前哪儿都是…”）。字幕那几行和上面不居中的另一块字分成两条，
    上面那块另起一条（编号加 n）。上面那块得像人名条（「…さん」「（82）」结尾）；字是模型改过的（和各块的字对不上）就不拆。"""
    out = []
    for e in ocr_events:
        out.append(e)
        pieces = e.get("pieces") or []
        if e.get("vertical") or len(pieces) < 2 or norm(e["text"]) != norm("".join(p[4] for p in pieces)):
            continue
        cap = _caption_block(pieces)
        rest = [p for p in pieces if p not in cap]
        if not cap or not rest:
            continue
        top = min(p[1] for p in cap)
        if any(p[3] > top + 0.01 or abs((p[0] + p[2]) / 2 - 0.5) < 0.12 for p in rest):
            continue  # 另一块不全在字幕上面，或者也居中（两三行居中的字幕、标题）：是一整块
        if not (pack.name_super and pack.name_super.search("\n".join(p[4] for p in rest))):
            continue  # 上面那块不像人名条：多半是另一个人的话（两人对话各占一行、错开放），还是一整块
        box = lambda ps: [round(min(p[0] for p in ps), 4), round(min(p[1] for p in ps), 4),
                          round(max(p[2] for p in ps), 4), round(max(p[3] for p in ps), 4)]
        cap.sort(key=lambda p: p[1])
        rest.sort(key=lambda p: (p[1], p[0]))
        from ..sources.ocr import _zone
        sup = dict(e, id=e["id"] + "n", text="\n".join(p[4] for p in rest), rect=box(rest), pieces=rest, lines=len(rest))
        sup["zone"] = _zone(sup["rect"], False)
        e.update(text="\n".join(p[4] for p in cap), rect=box(cap), pieces=cap, lines=len(cap), zone="bottom")
        out.append(sup)
    return out


def _drop_misread(ocr_events, pack, gap=0.5):
    """本地只认出几个乱码（「ER」）的地方，模型读出了一整句、和同一位置紧挨着的另一条差不多（字幕淡出那一刻，
    模型把正在消失的「NHKニュース 穴水町で…」编成了「NHKニュース 九州で…」）：是那一条的重复，去掉。"""
    for e in ocr_events:
        loc = e.get("text_local")
        if e.get("dropped") or e.get("found_by") == "vlm" or loc is None or len(norm(e["text"])) < 6 \
                or (pack.meaningful.search(loc) and len(norm(loc)) > 3):
            continue
        if any(o is not e and not o.get("dropped") and o["start"] - gap <= e["end"] and e["start"] <= o["end"] + gap
               and _same_place(o["rect"], e["rect"]) and sim(o["text"], e["text"]) >= 70 for o in ocr_events):
            e["dropped"] = "dup_vlm"


def fuse(subs, ocr_events, asr, m: dict, pack, duration=0.0):
    """m 是 FusionCfg 的 dict，pack 是源语言的语言包。"""
    dup_th, asr_th = m["dup_similarity"], m["asr_match_similarity"]

    prefilter(ocr_events, duration, pack)
    n_caption = _caption_lines(ocr_events, asr, pack, asr_th)
    ocr_all = ocr_events
    _drop_vlm_dups(ocr_events)
    _drop_misread(ocr_events, pack)
    ocr_events = _join_split(_split_supers([e for e in ocr_events if not e.get("dropped")], pack))

    asr = [a for a in asr if not any(h in a["text"] for h in pack.hallucinations)
           and not (a.get("no_speech", 0) > 0.6 and a.get("logprob", 0) < -1.0)]

    # 1. 画面字分类
    reviewed = any("text_local" in e for e in ocr_all)  # 做过视觉复核（没复核过的条目才值得怀疑）
    ocr_items = []
    for e in ocr_events:
        e.pop("dup_of", None)
        hit, _ = _concat(subs, e["start"], e["end"], slack=1.0)
        # 对得上的字幕得和画面字同时在屏幕上：前后差一点、只是靠时间余量带进来的（画面字消失后才说的
        # “失敗しない清志さんも…”）不算，否则“私 失敗しないので”会因为共用一个短语被当成重复去掉
        near = [c for c in hit if overlap(e["start"], e["end"], c["start"], c["end"]) >= 0.3]
        best = max((sim(e["text"], c["text"]) for c in near), default=0)
        said = "\n".join(c["text"] for c in near)
        if near and (best >= dup_th or sim(e["text"], said) >= dup_th) and _mostly_said(e["text"], said):
            e["dup_of"] = [c["id"] for c in hit]
            continue
        a_hit, a_text = _concat(asr, e["start"], e["end"])
        speech_ratio = _coverage([(a["start"], a["end"]) for a in a_hit], e["start"], e["end"])
        s = sim(e["text"], a_text) if a_text else 0
        # 同时段字幕在说别的话（画面字和字幕、语音都对不上）：多半是没人念出来的标题 / 菜名 / 说明，
        # 也可能是旁白同时另一个人说的话。这里不下结论（交给大模型按内容判断），只记下事实，
        # 大模型要是把它和台词合成一条，翻译阶段会按这个标记拆开（见 llm/translate.py split_unspoken）。
        talk = [c for c in hit if len(spoken(c["text"])) >= 2]
        sub_cov = _coverage([(c["start"], c["end"]) for c in talk], e["start"], e["end"]) if talk else 0.0
        other_talk = sub_cov >= 0.6 and s < asr_th
        long_line = len(e["text"].replace("\n", "")) >= 6
        cx = (e["rect"][0] + e["rect"][2]) / 2
        # 人名条：语言包的人名条格式，或靠左下的单行
        name_super = bool(pack.name_super and pack.name_super.search(e["text"])) or (e["lines"] == 1 and cx < 0.35)
        # 底部居中的基本都是台词；语音识别常漏掉方言，不能只靠它判断
        if s >= asr_th or (_bottom_center(e) and not name_super and len(e["text"]) >= 3) or \
                (e["zone"] == "bottom" and long_line and speech_ratio >= 0.3 and e["lines"] <= 3 and not name_super):
            kind = "telop"
        else:
            kind = "info"
        # 有证据才用：复核时两次读法对不上、本地也没把握的（unverified），或者从没复核过、本地也没把握的台词，
        # 语音又对不上：没有任何一方作证，多半是背景上的字读成了乱码（海报小字「加熱用のわに」读成「加他用のbに」），不要
        if s < asr_th and (e.get("unverified") or (kind == "telop" and reviewed and "text_local" not in e
                                                   and e.get("score", 1.0) < 0.8)):
            e["dropped"] = "unverified"
            continue
        item = {"id": e["id"], "src": kind, "start": e["start"], "end": e["end"], "text": e["text"],
                "zone": e["zone"], "rect": e["rect"], "ocr_score": e["score"]}
        if e.get("crop"):
            item["crop"] = e["crop"]
        if other_talk:
            item["over_speech"] = True
        if kind == "telop" and a_hit:  # 起点吸附到语音开始
            first = min(a_hit, key=lambda a: abs(a["start"] - e["start"]))
            if abs(first["start"] - e["start"]) <= m["snap_seconds"]:
                item["start"] = round(min(item["start"], first["start"]), 3)
        ocr_items.append(item)

    # 2. 语音：没被覆盖的单独成条；重叠但对不上的作为参考附到那条上（否则会被当成另一句话翻译，叠在一起显示）
    sub_items = [dict(c, src="subs") for c in subs if not music_only(c["text"])]
    # 只有音乐记号的字幕不要了，但还算“这段有字幕”：放音乐时语音识别出的歌声、杂音不单独成条（和以前一样）
    music = [dict(c, src="subs") for c in subs if music_only(c["text"])]
    text_items = sub_items + music + [o for o in ocr_items if o["src"] == "telop"]
    asr_items, n_hint, quoted_ids = [], 0, {}
    for a in asr:
        hit, txt = _concat(text_items, a["start"], a["end"], slack=0.5)
        # 画面上只是从这句话里摘出来的几个字（旁白“日本の伝統工芸をはじめ、各地の食文化、建築まで”时的「伝統工芸」「建築」），
        # 同时的画面字合起来也没说到这句话的一半：不算“画面字已经有了”，这句话单独成条，否则整句被丢掉 / 附到那两个字上，
        # 翻译出来只剩“传统工艺”。一句长话配好几行采访硬字幕的，每行也是这句话的一段，但合起来说到了大半，不算
        # 底部居中的一整句是这句话的字幕（采访硬字幕常把话缩短：「豆を煮た弁当も何も 雪の中に迷い込んでしまった」之于
        # “…舞い込んでしまって、どこに置いたか忘れてしまって、見えないくなってしまって”），不是摘出的几个字，照旧用它；
        # 底部只打出一个词的（“ぜいたくな食べ物だ”时的「ぜいたく」）还是摘出的字
        sentence = lambda x: _bottom_center(x) and len(norm(x["text"])) >= 6
        quotes = [x for x in hit if x["src"] == "telop" and not sentence(x) and _quoted(x["text"], a["text"])]
        if quotes and not any(x["src"] == "subs" for x in hit) and _share(_content(a["text"]), [x["text"] for x in hit]) < 0.5:
            asr_items.append({"id": a["id"], "src": "asr", "start": a["start"], "end": a["end"], "text": a["text"],
                              "uncovered": True, **({"second_pass": True} if a.get("second_pass") else {})})
            for x in quotes:  # 这几个字语音里念了，和有字幕时一样按重复去掉
                quoted_ids.setdefault(x["id"], a["id"])
            continue
        s = sim(a["text"], txt) if txt else 0
        if s >= asr_th:
            continue
        cov = _coverage([(x["start"], x["end"]) for x in hit], a["start"], a["end"])
        if cov < m["asr_uncovered_ratio"] or _unrelated(a["text"], hit):
            asr_items.append({"id": a["id"], "src": "asr", "start": a["start"], "end": a["end"], "text": a["text"],
                              "uncovered": True, **({"second_pass": True} if a.get("second_pass") else {})})
            continue
        target = max(hit, key=lambda x: overlap(x["start"], x["end"], a["start"], a["end"]))
        target["asr_hint"] = (target.get("asr_hint", "") + " " + a["text"]).strip()
        n_hint += 1

    quoted_ids = {k: v for k, v in quoted_ids.items()  # 另一句语音附在它上面的，它还是那句话的字幕，留着
                  if not any(o["id"] == k and o.get("asr_hint") for o in ocr_items)}
    if quoted_ids:
        ocr_items = [o for o in ocr_items if o["id"] not in quoted_ids]
        for e in ocr_events:
            if e["id"] in quoted_ids:
                e["dup_of"] = [quoted_ids[e["id"]]]
    items = sub_items + ocr_items + asr_items
    items.sort(key=lambda x: (x["start"], x["end"]))

    # 不经过大模型的原文合并字幕
    cues = []
    for it in items:
        cue = {"id": "m" + it["id"], "start": it["start"], "end": it["end"], "kind": it["src"],
               "type": "info" if it["src"] == "info" else "dialogue", "sources": [it["id"]],
               "onscreen": it["src"] in ("telop", "info"), "orig": {"lang": pack.code, "text": it["text"]}}
        if it.get("rect"):
            cue["rect"] = it["rect"]
        cues.append(cue)
    stats = {
        "subs": len(subs), "subs_music": len(music), "ocr_total": len(ocr_all),
        "ocr_logo": sum(1 for e in ocr_all if e.get("dropped") == "logo"),
        "ocr_junk": sum(1 for e in ocr_all if e.get("dropped") == "junk"),
        "ocr_scene": sum(1 for e in ocr_all if e.get("dropped") == "scene"),
        "ocr_unverified": sum(1 for e in ocr_all if e.get("dropped") == "unverified"),
        "ocr_ruby": sum(1 for e in ocr_all if e.get("dropped") == "ruby"),
        "ocr_dup": sum(1 for e in ocr_events if e.get("dup_of")),
        "ocr_caption": n_caption,
        "telop": sum(1 for o in ocr_items if o["src"] == "telop"),
        "info": sum(1 for o in ocr_items if o["src"] == "info"),
        "asr_total": len(asr),
        "asr_uncovered": len(asr_items),
        "asr_hint": n_hint,
    }
    return {"items": items, "cues": cues, "stats": stats}
