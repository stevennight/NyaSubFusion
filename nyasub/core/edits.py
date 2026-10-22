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
    """统一成 {"cues", "ops", ...}。另外几项（见 rebase.py）：anchors（被修改条目的锚点）、
    base（记锚点时翻译结果的指纹）、orphans（重新翻译后对不上、没有套用的修改）。"""
    edits = edits or {}
    if "cues" in edits or "ops" in edits:
        out = {"cues": dict(edits.get("cues") or {}), "ops": list(edits.get("ops") or [])}
        for k in ("anchors", "base", "orphans"):
            if edits.get(k):
                out[k] = edits[k]
        return out
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
        for k in ("start", "end", "type", "review", "parts", "rect", "show_orig", "pos") + tuple(SEG_KEYS):
            if k in e:
                c[k] = e[k]
        if "orig" in e:
            c["orig"]["text"] = e["orig"]
        for code, text in (e.get("tr") or {}).items():
            c["tr"][code] = {"text": text}
        out.append(c)
    return out


# 每段单独的设置（列表，一段一个，None = 跟随整条）→ 只剩一段时对应的整条设置
SEG_KEYS = {"parts_type": "type", "parts_show": "show_orig"}


def _seg_lists(c, a, b, n_a):
    """拆成两条时，每段的设置跟着各自的段走；拆出来只剩一段的，变成那一条的整条设置。"""
    from . import segments

    n = len(segments.pieces(c["orig"]["text"]))
    for key, whole in SEG_KEYS.items():
        lst = c.get(key)
        for part, sub in ((a, (lst or [])[:n_a]), (b, (lst or [])[n_a:])):
            part.pop(key, None)
            if not lst or len(lst) != n:
                continue
            if len(sub) == 1:
                if sub[0] is not None:
                    part[whole] = sub[0]
            elif any(x is not None for x in sub):
                part[key] = sub


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
        from . import segments

        _seg_lists(c, a, b, len(segments.pieces(oa)) or 1)
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
    out = {"orig": join(cue["orig"]["text"], cue["orig"]["lang"]),
           "tr": {code: join(v["text"], code) for code, v in cue.get("tr", {}).items() if v.get("text")}}
    parts = cue.get("parts")
    if parts and 0 < k < len(parts):
        out["parts"] = parts[:k - 1] + [[parts[k - 1][0], parts[k][1]]] + parts[k + 1:]
    for key in SEG_KEYS:  # 合成的那段沿用前一段的设置（前一段没设就用后一段的）
        lst = cue.get(key)
        if lst and 0 < k < len(lst):
            out[key] = lst[:k - 1] + [lst[k - 1] if lst[k - 1] is not None else lst[k]] + lst[k + 1:]
    return out


def piece_cut_fields(cue, t):
    """在 t 秒处把所在的那一段一分为二（同一条字幕里多一屏），文字按时间比例切。返回字段修改；t 不在某段中间返回 None。"""
    from . import segments

    parts = cue.get("parts") or [[cue["start"], cue["end"]]]
    k = next((i for i, (a, b) in enumerate(parts) if a + 0.2 < t < b - 0.2), None)
    if k is None:
        return None
    a, b = parts[k]
    r = (t - a) / (b - a)

    def cut(text, code):
        ps = segments.pieces(text) if len(parts) > 1 else [text]
        if len(ps) != len(parts):
            return None
        x, y = lang.get(code).split(ps[k].strip(), [r, 1 - r])
        if not x.strip() or not y.strip():
            return None
        return "｜".join(ps[:k] + [x.strip(), y.strip()] + ps[k + 1:])

    orig = cut(cue["orig"]["text"], cue["orig"]["lang"])
    if orig is None:
        return None
    tr = {code: cut(v["text"], code) for code, v in cue.get("tr", {}).items() if v.get("text")}
    out = {"orig": orig, "tr": {k2: v for k2, v in tr.items() if v is not None},
           "parts": parts[:k] + [[a, round(t, 3)], [round(t, 3), b]] + parts[k + 1:]}
    for key, whole in SEG_KEYS.items():  # 分出来的两段沿用原来那段的设置
        lst = cue.get(key) if len(parts) > 1 else [cue.get(whole) if key == "parts_show" else None]
        if lst and len(lst) == len(parts) and any(x is not None for x in lst):
            out[key] = lst[:k] + [lst[k], lst[k]] + lst[k + 1:]
    return out


def piece_swap_fields(cue, i, j):
    """第 i 段和第 j 段（从 0 起）互换文字（原文、各译文，以及各段单独的类型 / 原文显示）。
    每段的时间位置不动：第 i 屏还是原来的时间，只是换成另一段的内容。返回字段修改；段号不对返回 None。"""
    from . import segments

    n = len(segments.pieces(cue["orig"]["text"]))
    if not (0 <= i < n and 0 <= j < n) or i == j:
        return None

    def swap(text):
        ps = segments.pieces(text)
        if len(ps) != n:
            return None
        ps[i], ps[j] = ps[j], ps[i]
        return "｜".join(p.strip() for p in ps)

    out = {"orig": swap(cue["orig"]["text"]),
           "tr": {code: t for code, v in cue.get("tr", {}).items() if v.get("text") and (t := swap(v["text"])) is not None}}
    for key in SEG_KEYS:
        lst = cue.get(key)
        if lst and len(lst) == n:
            lst = list(lst)
            lst[i], lst[j] = lst[j], lst[i]
            out[key] = lst
    return out


def piece_isolate_ops(cue, seg):
    """把第 seg 段（从 0 起）单独拆成一条：返回 (操作列表, 那一条的 id)。"""
    from . import segments

    n = len(segments.pieces(cue["orig"]["text"]))
    ks = [k for k in (seg, seg + 1) if 0 < k < n]
    ops, _ = piece_split_ops(cue, ks)
    cid = cue["id"]
    if len(ks) == 2:
        return ops, cid + "ab"
    if ks == [seg]:      # 最后一段：拆出的后半条
        return ops, cid + "b"
    if ks == [seg + 1]:  # 第一段：拆出的前半条
        return ops, cid + "a"
    return ops, cid


FIELD_LBL = {"orig": "原文", "tr": "译文", "start": "时间", "end": "时间", "parts": "分段时间", "type": "类型",
             "review": "审校状态", "delete": "删除", "rect": "位置", "show_orig": "原文显示",
             "parts_type": "分段类型", "parts_show": "分段原文显示"}
OP_LBL = {"split": "拆分", "merge": "合并", "insert": "插入"}


def describe_change(before, after, undone):
    """撤销 / 重做前后的修改记录比一比：改的是哪一条、改了什么，界面跳过去并提示。
    undone = True 表示这是撤销（after 是撤销后的状态）。返回 {"id": 跳到哪一条（可能没有）, "at": 时间（没有 id 时用）, "what": 说明}。"""
    b, a = normalize(before), normalize(after)
    ops_b, ops_a = b["ops"], a["ops"]
    short, long_ = (ops_a, ops_b) if len(ops_a) < len(ops_b) else (ops_b, ops_a)
    if len(short) != len(long_) and long_[:len(short)] == short:
        op = long_[len(short)]                      # 撤销 / 重做的是一个结构操作（拆分 / 合并 / 插入）
        gone = undone                              # 撤销 = 这个操作没了；重做 = 这个操作回来了
        kind = op.get("op")
        if kind == "split":
            cid = op["id"] if gone else op["id"] + "a"
        elif kind == "merge":
            cid = op["ids"][0]
        else:
            cid = None if gone else op.get("id")
        n = len(long_) - len(short)
        what = OP_LBL.get(kind, kind) + (f"等 {n} 步" if n > 1 else "")
        return {"id": cid, "at": op.get("at") or op.get("start"), "what": what}
    changed = [cid for cid in set(b["cues"]) | set(a["cues"]) if b["cues"].get(cid) != a["cues"].get(cid)]
    if not changed:
        return {"id": None, "at": None, "what": "修改"}
    cid = changed[0]
    old, new = b["cues"].get(cid, {}), a["cues"].get(cid, {})
    keys = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
    names = []
    for k in keys:
        lbl = FIELD_LBL.get(k, k)
        if lbl not in names:
            names.append(lbl)
    what = "、".join(names) or "修改"
    if len(changed) > 1:
        what += f"（共 {len(changed)} 条）"
    deleted = new.get("delete") and not old.get("delete")
    return {"id": None if deleted else cid, "at": new.get("start", old.get("start")), "what": what}


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

    def write_quiet(self, new):
        """不进撤销栈地保存（记锚点、重新翻译后对编号这类程序自己做的整理）。"""
        save_json(self.path, new)

    def map_history(self, fn):
        """撤销 / 重做栈里的每一个状态都用 fn 整理一遍（重新翻译后编号变了，撤销回去也要是新的编号）。"""
        h = load_json(self.hist, {"past": [], "future": []})
        if h["past"] or h["future"]:
            save_json(self.hist, {k: [fn(normalize(x)) for x in h[k]] for k in ("past", "future")})

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
