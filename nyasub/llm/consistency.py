"""翻译后统一译法：先找出原文相同 / 相近的字幕（不用大模型），再让大模型逐组看前后文判断该不该统一。

同样的话换了场景可能是另一回事（不同段落里的“（鳴き声）”），所以不按规则一刀切；
大模型判断该统一的才改译文，改过的标上 unified（进待审校），判断不该统一的不动。
"""
from collections import defaultdict

from ..env import Cancelled, log
from ..textutil import fmt_time, full_sim, norm

MIN_LEN = 3      # 太短的（“うん”这类两三个字的应答）不找
MAX_SHOW = 10    # 一组最多给大模型看几处


def _split(text):
    """按“｜”分段、再按换行分行。"""
    return [p for seg in text.split("｜") for p in seg.split("\n")]


def _units(cue, tgt_code):
    """一条字幕拆成几块来比，各带这块的译文（拿得到的话）：整句（译文就是整条译文），
    加上每一段 / 每一行（一句话常拆在两块画面字里，标题卡常是两行；原文和译文的行数对得上才知道这一行的译文）。
    返回 [(规范化后的原文, 这块的译文或 None)]。"""
    o = cue["orig"]["text"]
    t = cue.get("tr", {}).get(tgt_code, {}).get("text", "")
    out = {norm(o.replace("｜", "").replace("\n", "")): t or None}
    ol, tl = _split(o), _split(t)
    for k, line in enumerate(ol):
        out.setdefault(norm(line), tl[k].strip() if len(ol) == len(tl) and len(ol) > 1 else None)
    return [(u, tr) for u, tr in out.items() if len(u) >= MIN_LEN]


def _covers(u, key):
    """这块原文算不算 key 这句：一样、只差一两个字，或者包含它而且多出来的不多
    （“冬を食べるワニ”算“冬を食べる”；提到“一美さん”的一长句不算“一美さん”）。"""
    if u == key:
        return True
    if key in u:
        return len(key) * 2 >= len(u)
    return min(len(u), len(key)) >= 5 and full_sim(u, key) >= 90


def similar_groups(cues, tgt_code):
    """原文相同 / 相近的字幕分成组：[[条目序号...]]。每组围绕一句共同的原文（不会顺着链子把不相干的连成一组），
    只留下这句话的译法确实不一样的组（整条的译文、或者对得上的那一行的译文不一样；
    较长的那条字幕的译文里包含了别处的译法，算一致）。"""
    units = [(i, u, tr) for i, c in enumerate(cues) for u, tr in _units(c, tgt_code)]
    index = defaultdict(set)  # 三个字的片段 → 有这个片段的块，只比有共同片段的，免得两两全比
    for k, (_, u, _) in enumerate(units):
        for j in range(len(u) - 2):
            index[u[j:j + 3]].add(k)
    groups, seen = [], set()
    for key in sorted({u for _, u, _ in units}, key=len):
        cands = set().union(*(index[key[j:j + 3]] for j in range(len(key) - 2)))
        members = {}
        for k in cands:
            i, u, tr = units[k]
            if _covers(u, key):
                exact = u == key or full_sim(u, key) >= 90
                if i not in members or (exact and not members[i][0]):
                    members[i] = (exact, tr if exact else None, cues[i].get("tr", {}).get(tgt_code, {}).get("text", ""))
        if len(members) < 2:
            continue
        ids = tuple(sorted(members))
        if ids in seen:
            continue
        own = {tr for exact, tr, _ in members.values() if exact and tr}  # 这句话本身的各种译法
        if not own:
            continue
        differ = len(own) > 1 or any(not exact and not any(x in full for x in own)
                                     for exact, _, full in members.values())
        if differ:
            seen.add(ids)
            groups.append(list(ids))
    # 一组的条目全在另一组里：只问大的那组，免得同一批字幕被分两次判断、结论还不一样
    return [g for g in groups if not any(set(g) < set(h) for h in groups)]


def _system(src, tgt):
    return (f"你是资深{src.name}→{tgt.name}影视字幕校对，负责全集译法前后一致。"
            "下面每一组是原文相同或很像的几条字幕（带时间、前后各一句和现在的译文）。逐组判断：\n"
            "- 指的是同一回事（同一个标题 / 栏目名 / 标语、同一件事、前后呼应的同一句话）：same 为 true，"
            "选一种最合适的译法，把这组里需要改的条目改成统一的译法；\n"
            "- 场景不同、说的不是同一回事（同样的音效在不同段落里可能是不同的动物，同样的应答在不同语境里语气不同）：same 为 false，什么都不改。\n"
            "改的时候只统一相同的那部分：原文多出来的部分（“冬を食べるワニ”里的“ワニ”）照原来的译法保留，"
            "译文里的“｜”分段保持原样（段数不变）；“ / ”表示换行（两行的标题卡、两个人各说一句），也照样保留“ / ”。"
            "拿不准就 same 为 false。\n"
            '输出 JSON：{"groups": [{"g": 1, "same": true, "reason": "一句话理由", '
            '"items": [{"id": "q12", "tr": "改好的译文"}]}]}，same 为 false 时 items 为空；只列需要改的条目。')


def _group_text(n, g, cues, tgt_code):
    flat = lambda t: t.replace("\n", " / ")
    lines = [f"## 第 {n} 组"]
    for i in g[:MAX_SHOW]:
        c = cues[i]
        prev = flat(cues[i - 1]["orig"]["text"]) if i else "（无）"
        nxt = flat(cues[i + 1]["orig"]["text"]) if i + 1 < len(cues) else "（无）"
        lines.append(f"[{c['id']}] {fmt_time(c['start'])} 原文：{flat(c['orig']['text'])}\n"
                     f"　译文：{flat(c['tr'].get(tgt_code, {}).get('text', ''))}\n"
                     f"　前一句：{prev}　后一句：{nxt}")
    return "\n".join(lines)


def unify(llm, cues, src, tgt, tgt_code, batch=8):
    """找出原文相同 / 相近的组，请大模型逐组判断要不要统一，该统一的改译文、标 unified。
    返回每组的判断 [{"ids", "same", "reason", "changed"}]（日志和对比用）。"""
    from ..core import segments

    groups = similar_groups(cues, tgt_code)
    if not groups:
        return []
    log(f"统一译法：{len(groups)} 组原文相同 / 相近、译法不一样，请大模型逐组判断")
    by_id = {c["id"]: c for c in cues}
    report = []
    for k in range(0, len(groups), batch):
        part = groups[k:k + batch]
        prompt = "\n\n".join(_group_text(n, g, cues, tgt_code) for n, g in enumerate(part, 1))
        try:
            res = llm.chat_json(_system(src, tgt), prompt)
        except Cancelled:
            raise
        except Exception as e:
            log(f"  统一译法失败，保持原样：{e}")
            continue
        got = {int(x.get("g", 0)): x for x in res.get("groups", []) or [] if isinstance(x, dict)}
        for n, g in enumerate(part, 1):
            x = got.get(n) or {}
            ids = [cues[i]["id"] for i in g]
            changed = []
            if x.get("same"):
                for it in x.get("items") or []:
                    # 提示词里换行写成“ / ”，回答里也是：换回换行
                    c, new = by_id.get(str(it.get("id"))), (it.get("tr") or "").strip().replace(" / ", "\n")
                    if not c or c["id"] not in ids or not new:
                        continue
                    old = c["tr"].get(tgt_code, {}).get("text", "")
                    # 分段（“｜”）和换行（两行的标题卡、两个人各说一句）都得保持
                    if new != old and len(segments.pieces(new)) == len(segments.pieces(old)) \
                            and new.count("\n") == old.count("\n"):
                        c["tr"][tgt_code] = {"text": new}
                        if "unified" not in c["flags"]:
                            c["flags"].append("unified")
                        changed.append((c["id"], old, new))
            report.append({"ids": ids, "same": bool(x.get("same")), "reason": x.get("reason", ""), "changed": changed})
    n_same = sum(1 for r in report if r["same"])
    n_changed = sum(len(r["changed"]) for r in report)
    log(f"统一译法：{n_same} 组判断为同一回事，改了 {n_changed} 条（标记待审校）；{len(report) - n_same} 组场景不同，保持原样")
    return report
