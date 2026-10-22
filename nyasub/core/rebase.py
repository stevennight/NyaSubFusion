"""重新翻译后，把手动修改对到新的字幕条目上。

修改记录（edits.json）按条目编号（q12）记。重新翻译会重新编号，编号对应的已经是另一句了；
直接套用会把修改套到别的句子上（删掉别的句子、把别的句子的文字改掉）。所以：

- 每次注音（每次修改后都会做）时，给被修改的条目记一个“锚点”：它的来源（字幕 / 画面字 / 语音的 id）、时间、原文；
  再记下这一版翻译结果的指纹（base）。
- 下次注音时，翻译结果的指纹变了，就按锚点找新结果里对应的条目：来源重合最多的那一条（来源会跟着重新识别变，
  再看时间重叠和原文相似度）。
- 以前没有锚点的修改（旧版本留下的）：按修改的内容找——改过原文的找原文最像的一条；改过时间 / 位置的找时间重叠的一条；
  只改了译文的找译文最像的一条。
- 对不上的修改不再套用，放进 orphans，审校页列出来让用户自己重做。
"""
import re

from ..textutil import full_sim, norm
from .store import digest

_BASE = re.compile(r"^(q\d+)([ab]*)$")
INFO = ("info", "name", "title")


def base_of(cues):
    """这一版翻译结果的指纹：编号 + 来源。"""
    return digest([[c["id"], sorted(c.get("sources", []))] for c in cues])


def split_id(cid):
    """q12ab → ("q12", "ab")；插入的 u3 等不是翻译结果里的条目，返回 (None, None)。"""
    m = _BASE.match(cid or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def anchor(cue):
    return {"src": sorted(cue.get("sources", [])), "start": round(cue["start"], 3), "end": round(cue["end"], 3),
            "orig": cue["orig"]["text"]}


def referenced(e):
    """修改记录里用到的翻译结果条目编号（不带拆分后缀）。"""
    ids = set()
    for cid in e.get("cues", {}):
        b, _ = split_id(cid)
        if b:
            ids.add(b)
    for op in e.get("ops", []):
        for cid in [op.get("id")] + list(op.get("ids") or []):
            b, _ = split_id(cid)
            if b:
                ids.add(b)
    return ids


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _flat(t):
    return norm((t or "").replace("｜", ""))


def _by_anchor(a, cues):
    """有锚点：来源重合最多的；来源一条都不重合时按时间重叠 + 原文相似。"""
    src = set(a.get("src") or [])
    best, score = None, 0.0
    for c in cues:
        cs = set(c.get("sources", []))
        jac = len(src & cs) / len(src | cs) if src and cs else 0.0
        ov = _overlap(a["start"], a["end"], c["start"], c["end"]) / max(0.1, a["end"] - a["start"])
        sim = full_sim(_flat(a.get("orig")), _flat(c["orig"]["text"])) / 100
        s = jac * 2 + ov + sim if jac >= 0.5 else (ov + sim if ov >= 0.5 and sim >= 0.6 else 0.0)
        if s > score:
            best, score = c, s
    return best


def _by_content(f, cues):
    """没有锚点（旧版本的修改）：按改了什么来找。找不到可信的返回 None。"""
    def unique_best(scores, need):
        scores = sorted(((s, c) for s, c in scores if s >= need), key=lambda x: -x[0])
        if not scores or (len(scores) > 1 and scores[1][0] >= scores[0][0] - 2):
            return None  # 没有，或者有好几条一样像：宁可不套
        return scores[0][1]

    if f.get("orig"):
        want = _flat(f["orig"])
        return unique_best([(full_sim(want, _flat(c["orig"]["text"])), c) for c in cues], 80)
    if f.get("start") is not None and f.get("end") is not None:
        s0, s1 = f["start"], f["end"]
        # 只在同一类里找：改的是说明（有位置 / 类型是说明）就只看说明，否则只看台词旁白（同一时间常常两类都有）
        info = bool(f.get("rect")) or f.get("type") in INFO
        same = [c for c in cues if (c.get("type") in INFO) == info]
        return unique_best([(100 * _overlap(s0, s1, c["start"], c["end"]) / max(0.1, max(s1 - s0, c["end"] - c["start"])), c)
                            for c in same], 50)
    trs = [v for v in (f.get("tr") or {}).values() if v]
    if trs:
        want = _flat(trs[0])
        return unique_best([(max((full_sim(want, _flat(v.get("text"))) for v in c.get("tr", {}).values()), default=0), c)
                            for c in cues], 75)
    return None


def rebase(e, cues):
    """把修改记录 e（normalize 过的）对到新的翻译结果 cues 上。返回 (新的修改记录, 对上了几处, 新增的对不上的)。"""
    e = {**e, "cues": dict(e.get("cues") or {}), "ops": list(e.get("ops") or []), "orphans": list(e.get("orphans") or [])}
    anchors = e.get("anchors") or {}
    mapping, taken = {}, set()
    for old in sorted(referenced(e), key=lambda x: int(x[1:])):
        a = anchors.get(old)
        c = _by_anchor(a, cues) if a else _by_content(e["cues"].get(old, {}), cues)
        if c and c["id"] not in taken:
            mapping[old] = c["id"]
            taken.add(c["id"])

    def move(cid):
        b, suffix = split_id(cid)
        if not b:
            return cid  # 插入的条目（u1…）：和翻译结果无关，原样保留
        return mapping[b] + suffix if b in mapping else None

    cues_new, lost = {}, []
    for cid, f in e["cues"].items():
        nid = move(cid)
        if nid is None:
            lost.append({"id": cid, "fields": f, "anchor": anchors.get(split_id(cid)[0])})
        else:
            cues_new[nid] = f
    ops_new = []
    for op in e["ops"]:
        ids = [op.get("id")] if op.get("id") else list(op.get("ids") or [])
        moved = [move(x) for x in ids]
        if any(x is None for x in moved):
            lost.append({"id": ids[0], "op": op})
            continue
        op = dict(op)
        if op.get("id"):
            op["id"] = moved[0]
        else:
            op["ids"] = moved
        ops_new.append(op)
    e["cues"], e["ops"] = cues_new, ops_new
    e["orphans"] += lost
    e["anchors"] = {}
    return e, len(mapping), lost


def describe(orphan):
    """给审校页看的一句话：这处修改原来改的是哪句、改了什么。"""
    from .edits import FIELD_LBL

    f = orphan.get("fields") or {}
    a = orphan.get("anchor") or {}
    where = (a.get("orig") or f.get("orig") or next(iter((f.get("tr") or {}).values()), "") or "").replace("｜", " ")
    if orphan.get("op"):
        what = {"split": "拆分", "merge": "合并", "insert": "插入"}.get(orphan["op"].get("op"), "结构修改")
    else:
        names = []
        for k in f:
            lbl = FIELD_LBL.get(k, k)
            if lbl not in names:
                names.append(lbl)
        what = "、".join(names)
    return {"where": where[:40], "what": what, "fields": f, "at": a.get("start", f.get("start"))}
