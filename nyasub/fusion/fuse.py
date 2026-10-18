"""三路融合（规则版，不需要大模型）。

- 字幕 subs：文字最可信。
- 画面字 telop / info：和字幕重复的去掉；能和语音对上的算台词（telop），否则算说明文字（info）。
  字幕和画面字同时存在时两者都保留——这正是“半句字幕、半句画面字”的情况。
- 语音 asr：能被字幕 / 画面字解释的丢掉（只用来校准时间）；没有任何字幕覆盖的单独成条；
  时间重叠但文字对不上的（多半是听错了），作为“语音参考”附到那条上。
产出的 items 交给大模型做最终的合并 / 纠错 / 翻译；cues 是不经过大模型的原文合并字幕。
"""
import re

from ..textutil import overlap, sim

# 字幕里不是“说话”的部分：（笑い声）（競りの声）这类括号说明、♪♬〜 音乐记号、➡ 接续符号
_NOT_SPEECH = re.compile(r"[（(\[［【][^）)\]］】]*[）)\]］】]|[♪♬〜～➡→]")


def spoken(text):
    """字幕里真正说出来的话（去掉说话人标注、音效 / 音乐说明）。"""
    return _NOT_SPEECH.sub("", text or "").strip()


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


def prefilter(ocr_events, duration, pack):
    """标记不需要的画面字：台标 / 水印、杂讯、视觉大模型判定的实物上的字（招牌、地图、包装…）。"""
    logos = _logo_ids(ocr_events, duration or max((e["end"] for e in ocr_events), default=0))
    for e in ocr_events:
        e.pop("dropped", None)
        if e["id"] in logos:
            e["dropped"] = "logo"
        elif _is_junk(e, pack):
            e["dropped"] = "junk"
        elif e.get("kind") == "scene":
            e["dropped"] = "scene"
        e["prefiltered"] = e.get("dropped") in ("logo", "junk")  # 台标和杂讯不用送去视觉复核


def fuse(subs, ocr_events, asr, m: dict, pack, duration=0.0):
    """m 是 FusionCfg 的 dict，pack 是源语言的语言包。"""
    dup_th, asr_th = m["dup_similarity"], m["asr_match_similarity"]

    prefilter(ocr_events, duration, pack)
    ocr_all = ocr_events
    ocr_events = [e for e in ocr_events if not e.get("dropped")]

    asr = [a for a in asr if not any(h in a["text"] for h in pack.hallucinations)
           and not (a.get("no_speech", 0) > 0.6 and a.get("logprob", 0) < -1.0)]

    # 1. 画面字分类
    ocr_items = []
    for e in ocr_events:
        e.pop("dup_of", None)
        hit, sub_text = _concat(subs, e["start"], e["end"], slack=1.0)
        best = max((sim(e["text"], c["text"]) for c in hit), default=0)
        if hit and (best >= dup_th or sim(e["text"], sub_text) >= dup_th):
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
    sub_items = [dict(c, src="subs") for c in subs]
    text_items = sub_items + [o for o in ocr_items if o["src"] == "telop"]
    intervals = [(x["start"], x["end"]) for x in text_items]
    asr_items, n_hint = [], 0
    for a in asr:
        hit, txt = _concat(text_items, a["start"], a["end"], slack=0.5)
        s = sim(a["text"], txt) if txt else 0
        if s >= asr_th:
            continue
        cov = _coverage(intervals, a["start"], a["end"])
        if cov < m["asr_uncovered_ratio"]:
            asr_items.append({"id": a["id"], "src": "asr", "start": a["start"], "end": a["end"], "text": a["text"],
                              "uncovered": True, **({"second_pass": True} if a.get("second_pass") else {})})
            continue
        target = max(hit, key=lambda x: overlap(x["start"], x["end"], a["start"], a["end"]))
        target["asr_hint"] = (target.get("asr_hint", "") + " " + a["text"]).strip()
        n_hint += 1

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
        "subs": len(subs), "ocr_total": len(ocr_all),
        "ocr_logo": sum(1 for e in ocr_all if e.get("dropped") == "logo"),
        "ocr_junk": sum(1 for e in ocr_all if e.get("dropped") == "junk"),
        "ocr_scene": sum(1 for e in ocr_all if e.get("dropped") == "scene"),
        "ocr_dup": sum(1 for e in ocr_events if e.get("dup_of")),
        "telop": sum(1 for o in ocr_items if o["src"] == "telop"),
        "info": sum(1 for o in ocr_items if o["src"] == "info"),
        "asr_total": len(asr),
        "asr_uncovered": len(asr_items),
        "asr_hint": n_hint,
    }
    return {"items": items, "cues": cues, "stats": stats}
