"""手动修改：存在项目目录的 edits.json，注音和输出前套用；上游重跑也不会丢。

    {"cues": {"q3": {"orig": "…", "tr": {"zh-Hans": "…"}, "start": 1.2, "end": 3.4, "type": "…",
                     "review": "ok", "delete": true}},
     "ops":  [{"op": "split", "id": "q3", "at": 12.5, "orig": ["前半", "后半"], "tr": {"zh-Hans": ["…", "…"]}},
              {"op": "merge", "ids": ["q5", "q6"]},
              {"op": "insert", "id": "u1", "start": 20.0, "end": 22.0, "type": "dialogue", "orig": "", "tr": {}}]}

套用顺序：字段修改 → 结构操作（按顺序）→ 再套一遍字段修改（拆分 / 插入出来的新条目也能改字）。
字段修改都是“设成某个值”，套两遍结果不变。
旧格式（id 直接在顶层）也支持。

每次修改前把旧内容压进 edits_history.json，用来撤销 / 重做（最多 100 步）。
"""
import copy

from .. import lang
from .store import load_json, save_json

HISTORY_MAX = 100


def normalize(edits):
    edits = edits or {}
    if "cues" in edits or "ops" in edits:
        return {"cues": dict(edits.get("cues") or {}), "ops": list(edits.get("ops") or [])}
    return {"cues": {k: v for k, v in edits.items() if not k.startswith("_")}, "ops": []}


def _fields(cues, fields):
    out = []
    for c in cues:
        e = fields.get(c["id"])
        if not e:
            out.append(c)
            continue
        if e.get("delete"):
            continue
        c = dict(c, tr={k: dict(v) for k, v in c.get("tr", {}).items()}, orig=dict(c["orig"]))
        for k in ("start", "end", "type", "review"):
            if k in e:
                c[k] = e[k]
        if "orig" in e:
            c["orig"]["text"] = e["orig"]
        for code, text in (e.get("tr") or {}).items():
            c["tr"][code] = {"text": text}
        out.append(c)
    return out


def _split(cues, op):
    out = []
    for c in cues:
        if c["id"] != op["id"] or not (c["start"] < op["at"] < c["end"]):
            out.append(c)
            continue
        a, b = copy.deepcopy(c), copy.deepcopy(c)
        a["id"], b["id"] = c["id"] + "a", c["id"] + "b"
        a["end"] = b["start"] = op["at"]
        oa, ob = (op.get("orig") or [c["orig"]["text"], ""])[:2]
        a["orig"]["text"], b["orig"]["text"] = oa, ob
        for code, halves in (op.get("tr") or {}).items():
            a["tr"][code], b["tr"][code] = {"text": halves[0]}, {"text": halves[1]}
        a["review"] = b["review"] = "todo"
        out += [a, b]
    return out


def _merge(cues, op):
    ids = op["ids"]
    group = [c for c in cues if c["id"] in ids]
    if len(group) < 2:
        return cues
    group.sort(key=lambda c: c["start"])
    head = copy.deepcopy(group[0])
    head["start"] = min(c["start"] for c in group)
    head["end"] = max(c["end"] for c in group)
    head["orig"]["text"] = "\n".join(c["orig"]["text"] for c in group if c["orig"]["text"])
    codes = {k for c in group for k in c.get("tr", {})}
    head["tr"] = {k: {"text": "\n".join(c["tr"][k]["text"] for c in group if c.get("tr", {}).get(k, {}).get("text"))}
                  for k in codes}
    head["sources"] = [s for c in group for s in c.get("sources", [])]
    head["flags"] = sorted({f for c in group for f in c.get("flags", [])})
    head["onscreen"] = any(c.get("onscreen") for c in group)
    head["review"] = "todo"
    rest = {c["id"] for c in group[1:]}
    return [head if c["id"] == group[0]["id"] else c for c in cues if c["id"] not in rest]


def _insert(cues, op, src_lang):
    cue = {"id": op["id"], "start": op["start"], "end": op["end"], "type": op.get("type", "dialogue"),
           "sources": [], "onscreen": False, "orig": {"lang": src_lang, "text": op.get("orig", "")},
           "tr": {k: {"text": v} for k, v in (op.get("tr") or {}).items()}, "flags": ["manual"], "review": "todo"}
    return cues + [cue]


def apply(cues, edits, src_lang=None):
    e = normalize(edits)
    if not e["cues"] and not e["ops"]:
        return cues
    out = _fields(cues, e["cues"])
    for op in e["ops"]:
        if op.get("op") == "split":
            out = _split(out, op)
        elif op.get("op") == "merge":
            out = _merge(out, op)
        elif op.get("op") == "insert":
            out = _insert(out, op, src_lang or (cues[0]["orig"]["lang"] if cues else ""))
    out = _fields(out, e["cues"])
    return sorted(out, key=lambda c: (c["start"], c["end"]))


# ---------------- 生成操作 ----------------


def split_op(cue, at):
    """在 at 秒处拆开，文字按时间比例切，切点吸附到空格 / 标点。"""
    r = (at - cue["start"]) / max(1e-6, cue["end"] - cue["start"])

    def halves(text, code):
        pack = lang.get(code)
        a, b = pack.split(text.replace("｜", ""), [r, 1 - r])
        return [a, b]
    return {"op": "split", "id": cue["id"], "at": round(at, 3),
            "orig": halves(cue["orig"]["text"], cue["orig"]["lang"]),
            "tr": {code: halves(v["text"], code) for code, v in cue.get("tr", {}).items()}}


def piece_split_ops(cue, ks):
    """在“｜”分段的第 k 个分界处（k 从 1 起）拆成单独的字幕，文字按段切、时间用每段的时间。
    ks 可以有几个（全部拆开）：从后往前拆，每次拆的是上一次拆出的前半条。返回 (操作列表, 拆完后第一条的 id)。"""
    from . import segments

    ps = segments.pieces(cue["orig"]["text"])
    trs = {code: segments.pieces(v["text"]) for code, v in cue.get("tr", {}).items() if v.get("text")}
    parts = cue.get("parts") or segments.proportional(cue)
    ops, cid, end = [], cue["id"], len(ps)
    for k in sorted(set(ks), reverse=True):
        if not 0 < k < len(ps):
            continue
        op = {"op": "split", "id": cid, "at": parts[k][0],
              "orig": ["｜".join(ps[:k]), "｜".join(ps[k:end])],
              "tr": {code: ["｜".join(p[:k]), "｜".join(p[k:end])] for code, p in trs.items() if len(p) == len(ps)}}
        ops.append(op)
        cid, end = cid + "a", k
    return ops, cid


def piece_join_fields(cue, k):
    """把第 k 个分界前后两段合成一段（去掉那个“｜”）。返回字段修改。"""
    from . import segments

    def join(text, code):
        ps = segments.pieces(text)
        if not 0 < k < len(ps):
            return text
        return "｜".join(ps[:k - 1] + [segments.joiner(code).join([ps[k - 1].strip(), ps[k].strip()])] + ps[k + 1:])
    return {"orig": join(cue["orig"]["text"], cue["orig"]["lang"]),
            "tr": {code: join(v["text"], code) for code, v in cue.get("tr", {}).items() if v.get("text")}}


def next_insert_id(edits):
    n = sum(1 for op in normalize(edits)["ops"] if op.get("op") == "insert")
    return f"u{n + 1}"


# ---------------- 读写与历史 ----------------


class Store:
    def __init__(self, proj):
        self.path = proj / "edits.json"
        self.hist = proj / "edits_history.json"

    def load(self):
        return normalize(load_json(self.path, {}))

    def save(self, new):
        """保存并把旧内容压进撤销栈。"""
        old = self.load()
        if old == new:
            return new
        h = load_json(self.hist, {"past": [], "future": []})
        h["past"] = (h["past"] + [old])[-HISTORY_MAX:]
        h["future"] = []
        save_json(self.hist, h)
        save_json(self.path, new)
        return new

    def _move(self, src, dst):
        h = load_json(self.hist, {"past": [], "future": []})
        if not h[src]:
            return None
        cur = self.load()
        prev = h[src].pop()
        h[dst].append(cur)
        save_json(self.hist, h)
        save_json(self.path, prev)
        return prev

    def undo(self):
        return self._move("past", "future")

    def redo(self):
        return self._move("future", "past")

    def depth(self):
        h = load_json(self.hist, {"past": [], "future": []})
        return {"undo": len(h["past"]), "redo": len(h["future"])}

    # 便捷操作
    def set_fields(self, cid, body):
        e = self.load()
        cur = dict(e["cues"].get(cid, {}))
        for k, v in body.items():
            if k == "tr":
                cur["tr"] = {**cur.get("tr", {}), **v}
            else:
                cur[k] = v
        e["cues"][cid] = cur
        return self.save(e)

    def add_op(self, op):
        e = self.load()
        e["ops"].append(op)
        return self.save(e)
