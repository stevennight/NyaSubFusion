"""大模型阶段：整理（术语表 → 分段把几路来源合并成一条条字幕，只出原文）→ 翻译（只翻一次，翻的时候可以把相邻几条合成一条）
→ 分段、统一 → 其余目标语言。

两种跑法：
- api：直接调 OpenAI 兼容接口（见 client.py）。
- export：把任务写成文件，交给 Claude Code 之类的助手完成，再导回（见 jobs.py）。
"""
import re
from difflib import SequenceMatcher

from .. import lang
from ..env import Cancelled, log, progress
from ..textutil import covered, fmt_time, full_sim, norm, sim
from . import prompts

SPEECH, SCREEN = ("subs", "asr"), ("telop", "info")


def chunk_items(items, seconds, max_items):
    chunks, cur, cur_start, cur_end = [], [], None, 0.0
    for it in items:
        if cur and it["start"] >= cur_end and (it["start"] - cur_start > seconds or len(cur) >= max_items):
            chunks.append(cur)
            cur = []
        if not cur:
            cur_start = it["start"]
        cur.append(it)
        cur_end = max(cur_end, it["end"])
    if cur:
        chunks.append(cur)
    return chunks


def fix_type(typ, srcs):
    """说明 / 人名条 / 标题是画面上打出来的字：来源里没有画面字（只有字幕、语音）的，是旁白念出来的，不能当说明（否则会显示到画面顶部）。"""
    if typ in ("info", "name", "title") and srcs and not any(s["src"] in SCREEN for s in srcs):
        return "narration"
    return typ


def _timing(srcs):
    """定时间用的来源：没说话的字幕（♬〜、（笑い声））常常一挂十几秒，和别的来源并在一起时不算，免得把整条拉长
    （「♬〜」和片名并成一条，片名没出来译文就先出来了）。"""
    from ..fusion.fuse import spoken

    said = [s for s in srcs if s["src"] not in SPEECH or spoken(s["text"])]
    return said or srcs


def make_cue(srcs, orig, tr, tgt, typ, src_lang, fallback=False):
    typ = fix_type(typ, srcs)
    ts = _timing(srcs)
    cue = {"start": min(s["start"] for s in ts), "end": max(s["end"] for s in ts),
           "type": typ or "dialogue", "sources": [s["id"] for s in srcs],
           "onscreen": any(s["src"] in ("telop", "info") for s in srcs),
           "orig": {"lang": src_lang, "text": orig}, "tr": {tgt: {"text": tr}} if tr else {}}
    if cue["type"] in ("info", "name", "title"):
        geo = info_geometry(srcs)
        if geo:
            cue["rect"] = geo["rect"]
            if all(s["src"] in SCREEN for s in ts):  # 只有画面字时，时间也只看主要的那几条
                cue["start"], cue["end"] = geo["start"], geo["end"]
    flags = []
    if all(s["src"] == "asr" for s in srcs):
        flags.append("asr_only")
    if any(s.get("ocr_score", 1) < 0.85 for s in srcs if s["src"] in ("telop", "info")):
        flags.append("ocr_low_conf")
    if any(s.get("asr_hint") for s in srcs):
        flags.append("conflict")
    if fallback:
        flags.append("llm_fallback")
    cue["flags"] = flags
    return cue


def info_geometry(srcs):
    """说明类字幕的位置和时间：以最主要的那条画面字（置信度 × 面积最大）为准，
    只并上挨着它的（标题分成两行识别的情况）；离得远的（画面另一处被误认成同样文字的）不算，
    否则位置会取到两处之间、时间也被拉长。返回 {"rect", "start", "end"}，没有位置返回 None。"""
    rs = [s for s in srcs if s.get("rect")]
    if not rs:
        return None
    area = lambda r: max(1e-6, (r[2] - r[0]) * (r[3] - r[1]))
    best = max(rs, key=lambda s: s.get("ocr_score", 1) * area(s["rect"]))

    def near(r, b):
        gap = max(b[3] - b[1], r[3] - r[1], 0.02) * 1.5  # 相隔不超过一行半
        same_row = min(r[3], b[3]) - max(r[1], b[1]) >= 0.5 * min(r[3] - r[1], b[3] - b[1])  # 同一行左右两段（“制作・著作　NHK”）
        return same_row or (r[0] < b[2] + gap and b[0] < r[2] + gap and r[1] < b[3] + gap and b[1] < r[3] + gap)

    group, rest = [best], [s for s in rs if s is not best]
    while True:  # 挨着组里任何一条的都并进来（竖着叠了三段的台标：上段挨中段、中段挨下段）
        add = [s for s in rest if any(near(s["rect"], g["rect"]) for g in group)]
        if not add:
            break
        group += add
        rest = [s for s in rest if s not in add]
    return {"rect": [min(s["rect"][0] for s in group), min(s["rect"][1] for s in group),
                     max(s["rect"][2] for s in group), max(s["rect"][3] for s in group)],
            "start": min(s["start"] for s in group), "end": max(s["end"] for s in group)}


def shown(cues):
    """去掉标成 drop 的条目（旧版本翻译结果里，模型想丢掉的条目被当成了字幕）。"""
    return [c for c in cues if c.get("type") != "drop"]


def apply_result(chunk, result, tgt, src_lang):
    """校验大模型结果，返回 (cues, 漏掉的 id)。时间由 src 计算。"""
    by_id = {it["id"]: it for it in chunk}
    seen, cues = set(), []
    for c in result.get("cues", []) or []:
        src = [s for s in (c.get("src") or []) if s in by_id and s not in seen]
        if not src:
            continue
        seen.update(src)
        if c.get("type") == "drop":  # 模型有时把要丢掉的条目写成 type 为 drop 的字幕，而不是放进 drop 列表
            continue
        srcs = [by_id[s] for s in src]
        orig = (c.get("orig") or c.get("ja") or "").strip().replace(" / ", "\n") or "\n".join(s["text"] for s in srcs)  # “ / ”是提示词里的换行
        orig = keep_subs_wording(srcs, orig)
        tr = (c.get("tr") or c.get("zh") or "").strip()
        srcs, orig, tr, split = split_unspoken(srcs, orig, tr)
        srcs, loose = split_orphans(srcs, orig)
        cues.append(make_cue(srcs, orig, tr, tgt, c.get("type"), src_lang))
        cues += [make_cue([it], o, t, tgt, "info", src_lang) for it, o, t in split]
        # 内容没用进这条字幕的画面字：单独成一条说明（译文之后补翻），不再把台词的时间拉长
        cues += [make_cue([it], it["text"], "", tgt, "info", src_lang) for it in loose]
    for d in result.get("drop", []) or []:
        if d in by_id:
            seen.add(d)
    return cues, [i for i in by_id if i not in seen]


def split_unspoken(srcs, orig, tr):
    """大模型把没人说出来的画面字（菜名、标题）和台词合成了一条时，按“｜”分段拆出来。
    只拆规则融合标了 over_speech（同一时间字幕在说别的话）的画面字。

    合在一起的话，这几个字会跟着台词提前出现、用台词的样式显示（明明没人说），还会和下一句台词时间重叠。
    原文和译文的段数一致、能找到和这条画面字整体对得上的连续几段才拆；拆不了就保持原样。
    返回 (留下的来源, 原文, 译文, [(拆出的画面字, 原文段, 译文段)])。"""
    speech = [s for s in srcs if s["src"] in SPEECH]
    screen = [s for s in srcs if s["src"] in SCREEN]
    if not speech or not screen:
        return srcs, orig, tr, []
    said = "".join(s["text"] + s.get("asr_hint", "") for s in speech)
    # 只拆“同一时间字幕在说别的话”的画面字。半句字幕、半句画面字是同一句话：画面字出现时字幕通常不重复显示，不会有这个标记
    loose = [s for s in screen if s.get("over_speech") and sim(s["text"], said) < 80]
    o_parts = orig.split("｜")
    t_parts = tr.split("｜") if tr else [""] * len(o_parts)  # 整理只出原文：没有译文，只按原文拆
    if not loose or len(o_parts) < 2 or len(o_parts) != len(t_parts):
        return srcs, orig, tr, []
    split = []
    for s in loose:
        # 找和这条画面字整体对得上的连续几段（“五十猛｜三次”这种两行的标签会被分成两段）；
        # 用整体相似度而不是部分包含：台词里提到同一个词（“昔は「虫踏み」と呼ばれていました”）不算
        best, span = 0.0, None
        for i in range(len(o_parts)):
            for j in range(i + 1, len(o_parts) + 1):
                if j - i >= len(o_parts):  # 至少留一段给台词
                    continue
                part = "".join(o_parts[i:j])
                score = full_sim(s["text"], part)
                # 大模型只保留了画面字的一部分（“冬を食べる／坂網鴨”只留“坂網鴨”）：这段是画面字的一部分、台词里又没有，也算
                if score < 70 and sim(part, s["text"]) >= 90 and sim(part, said) < 60 and len(part.strip()) >= 2:
                    score = 70
                if score > best:
                    best, span = score, (i, j)
        if not span or best < 70:
            continue
        i, j = span
        split.append((s, "｜".join(p.strip() for p in o_parts[i:j]), "｜".join(p.strip() for p in t_parts[i:j]) if tr else ""))
        del o_parts[i:j], t_parts[i:j]
    if not split:
        return srcs, orig, tr, []
    gone = {id(s) for s, _, _ in split}
    return [s for s in srcs if id(s) not in gone], "｜".join(o_parts), "｜".join(t_parts) if tr else "", split


def keep_subs_wording(srcs, orig):
    """字幕（subs）是官方文字，最可信：大模型整理原文时改掉的字（比如把人名“キミイ”换成了另一个人“一美”）改回字幕的写法。
    只改“替换”的小片段（前后都对得上、两边都不超过 6 个字）；大模型删掉的、加进来的（画面字的半句）不动；“｜”位置保持。"""
    subs = sorted((s for s in srcs if s["src"] == "subs"), key=lambda s: s["start"])
    if not subs:
        return orig
    ref = "".join(s["text"].replace("➡", "").replace(" / ", "").replace("\n", "") for s in subs)
    flat, bars = "", []
    for ch in orig:
        if ch == "｜":
            bars.append(len(flat))
        else:
            flat += ch
    out, moved = "", {}  # moved：原来第 i 个字 → 新文字里的位置（放回“｜”用）
    ops = SequenceMatcher(None, flat, ref, autojunk=False).get_opcodes()
    for k, (op, i1, i2, j1, j2) in enumerate(ops):
        if op == "insert":
            continue
        start = len(out)
        # 只改“夹在两段相同文字中间、长短差不多”的替换（字被换掉了）；只是全角半角、空格不同的不改
        swap = (op == "replace" and max(i2 - i1, j2 - j1) <= 6 and abs((i2 - i1) - (j2 - j1)) <= 2
                and 0 < k < len(ops) - 1 and ops[k - 1][0] == "equal" and ops[k + 1][0] == "equal"
                and "\n" not in flat[i1:i2] and norm(flat[i1:i2]) != norm(ref[j1:j2]))
        if swap:
            for i in range(i1, i2):
                moved[i] = start if i == i1 else start + (j2 - j1)
            out += ref[j1:j2]
        else:
            for i in range(i1, i2):
                moved[i] = start + (i - i1)
            out += flat[i1:i2]
    moved[len(flat)] = len(out)
    for p in sorted({moved[b] for b in bars}, reverse=True):
        out = out[:p] + "｜" + out[p:]
    return out


def split_orphans(srcs, orig):
    """大模型把“同一时间字幕在说别的话”的画面字并进了台词，但原文里根本没用它的内容（只是时间挨着），
    比如采访时画面上打出的问题概括、标签、注释：拆出来单独显示。返回 (留下的来源, 拆出的画面字)。"""
    if not any(s["src"] in SPEECH for s in srcs):
        return srcs, []
    text = orig.replace("｜", "")
    loose = [s for s in srcs if s["src"] in SCREEN and s.get("over_speech") and covered(s["text"], text) < 0.8]
    if not loose:
        return srcs, []
    return [s for s in srcs if s not in loose], loose


def fallback(chunk, missing, tgt, src_lang):
    """模型漏掉的条目：字幕和画面台词保留原文（不译），其余丢弃。"""
    return [make_cue([it], it["text"], "", tgt, "dialogue", src_lang, fallback=True)
            for it in chunk if it["id"] in missing and it["src"] in ("subs", "telop")]


def merge_dup_info(cues, keep=()):
    """同一块画面字被识别成了两条（两行的人名条被跟踪成上下两条、文字却一样）：合成一条，不再显示两遍。
    条件：都是说明类、文字几乎一样、时间重叠一半以上。keep 里的（手动改过的）不动。"""
    info = sorted((c for c in cues if c.get("type") in ("info", "name", "title") and c.get("id") not in keep),
                  key=lambda c: c["start"])
    gone = set()
    for i, a in enumerate(info):
        if id(a) in gone:
            continue
        for b in info[i + 1:]:
            if b["start"] > a["end"]:
                break
            if id(b) in gone:
                continue
            ov = min(a["end"], b["end"]) - max(a["start"], b["start"])
            same = full_sim(a["orig"]["text"].replace("｜", ""), b["orig"]["text"].replace("｜", "")) >= 85
            if same and ov > 0.5 * min(a["end"] - a["start"], b["end"] - b["start"]):
                a["sources"] = a.get("sources", []) + [s for s in b.get("sources", []) if s not in a.get("sources", [])]
                a["start"], a["end"] = min(a["start"], b["start"]), max(a["end"], b["end"])
                a["tr"] = {**(b.get("tr") or {}), **(a.get("tr") or {})}
                gone.add(id(b))
    return [c for c in cues if id(c) not in gone]


def finalize(cues):
    """去掉重复的说明、排序、编号。"""
    cues[:] = merge_dup_info(cues)
    cues.sort(key=lambda c: (c["start"], c["end"]))
    for i, c in enumerate(cues, 1):
        c["id"] = f"q{i}"
    return cues


def merge_terms(known, new):
    """作品术语表在前（人工确认过的优先），新术语去重后追加。"""
    out, seen = [], set()
    for t in list(known or []) + list(new or []):
        k = (t.get("src") or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def recall(chunk, done, tgt_code, per_item=2, limit=15):
    """译文记忆：这一段里和前面已经翻好的字幕原文相同 / 很像的条目，找出前面的译法和前后各一句。
    只是给大模型参考，沿用还是另译由它按前后文判断（同样的话换了场景可能是另一回事）。
    太短的（“うん”这类两三个字的应答）不找，免得噪音。前面有几种译法就各给一条（最早那条），每条最多给 per_item 种。
    返回 [(条目 id, 前面那条的开始时间, 前面的原文, 前面的译文, 前一句原文, 后一句原文)]。"""
    # 前面每条字幕整句，加上它的每一段（一句话常被拆在两块画面字里，这里的条目只是其中一块）
    keys = [{norm(c["orig"]["text"].replace("｜", ""))} | {norm(p) for p in c["orig"]["text"].split("｜")} for c in done]
    out = []
    for it in chunk:
        a = norm(it["text"].replace("➡", ""))
        if len(a) < 3:
            continue
        seen, picked = set(), []
        for i, bs in enumerate(keys):
            if not any(b and (a == b or (min(len(a), len(b)) >= 5 and full_sim(a, b) >= 90)) for b in bs):
                continue
            tr = done[i].get("tr", {}).get(tgt_code, {}).get("text", "")
            if tr and tr not in seen:
                seen.add(tr)
                picked.append(i)
        for i in picked[:per_item]:
            ctx = lambda j: done[j]["orig"]["text"] if 0 <= j < len(done) else ""
            out.append((it["id"], done[i]["start"], done[i]["orig"]["text"], done[i]["tr"][tgt_code]["text"], ctx(i - 1), ctx(i + 1)))
    return out[:limit]


def organize_api(items, cfg, cache_dir, src_code, known_terms):
    """整理（只出原文，不翻译）：术语表 / 概要 → 分段把几路来源合并成一条条字幕（多来源合并、纠正错字、分类、丢杂字）。
    要看译文才能定的合并、分屏放在翻译那一步。返回 (cues, glossary)。"""
    from .client import LLM

    llm = LLM(cfg, cache_dir=cache_dir)
    src = lang.get(src_code)
    tgt_code = lang.canonical(cfg.project.targets[0])
    tgt = lang.get(tgt_code)

    log("生成术语表 / 本集概要")
    gl = llm.chat_json(prompts.glossary_system(src, tgt), prompts.glossary_input(items, known_terms))
    terms = merge_terms(known_terms, gl.get("terms"))
    glossary = {"summary": gl.get("summary", ""), "terms": terms, "new": len(terms) - len(known_terms or [])}
    gl_text = prompts.glossary_text(terms)

    system = prompts.organize_system(src, cfg.project.context)
    chunks = chunk_items(items, cfg.llm.chunk_seconds, cfg.llm.chunk_max_items)
    all_cues = []
    for i, chunk in enumerate(chunks, 1):
        progress(i - 1, len(chunks), f"整理 {i}/{len(chunks)} 段", every=0)
        log(f"整理第 {i}/{len(chunks)} 段（{fmt_time(chunk[0]['start'])} 起，{len(chunk)} 条）")
        prompt = prompts.organize_prompt(chunk, gl_text, glossary["summary"], all_cues[-6:])
        cues, missing = apply_result(chunk, llm.chat_json(system, prompt), tgt_code, src.code)
        if missing:
            log(f"  模型漏了 {len(missing)} 条，补问一次")
            retry = llm.chat_json(system, prompt + f"\n\n上次输出漏掉了这些 id：{', '.join(missing)}。请重新完整输出。")
            cues2, missing2 = apply_result(chunk, retry, tgt_code, src.code)
            if len(missing2) < len(missing):
                cues, missing = cues2, missing2
            cues += fallback(chunk, missing, tgt_code, src.code)
        all_cues += sorted(cues, key=lambda x: x["start"])
    for c in all_cues:
        c["tr"] = {}  # 整理不翻译：模型顺手写了译文也不要
    return finalize(all_cues), glossary


MERGE_MAX, MERGE_GAP = 4, 2.0


def can_merge(group):
    """翻译时合成一条的几条：都是对话或都是旁白、相邻两条间隔不超过 MERGE_GAP 秒、最多 MERGE_MAX 条。"""
    if len(group) > MERGE_MAX or len({c.get("type") for c in group}) != 1 or group[0].get("type") not in ("dialogue", "narration"):
        return False
    return all(b["start"] - a["end"] <= MERGE_GAP for a, b in zip(group, group[1:]))


def merge_cues(group, tr, tgt_code):
    """几条整理好的字幕合成一条：原文按顺序用“｜”接起来（每条原来是一屏），时间、来源合起来。"""
    c = dict(group[0])
    c["flags"] = list(c.get("flags", []))
    if len(group) > 1:
        srcs, flags = [], []
        for g in group:
            srcs += [s for s in g.get("sources", []) if s not in srcs]
            flags += [f for f in g.get("flags", []) if f not in flags]
        c.update(start=min(g["start"] for g in group), end=max(g["end"] for g in group), sources=srcs, flags=flags,
                 onscreen=any(g.get("onscreen") for g in group),
                 orig=dict(group[0]["orig"], text="｜".join(g["orig"]["text"] for g in group)))
    c["tr"] = {**(c.get("tr") or {}), tgt_code: {"text": tr}}
    return c


def apply_translation(chunk, result, tgt_code):
    """校验翻译结果：每项 ids 是一条或连续几条（合成一条，见 can_merge）。返回 (翻好的字幕, 没翻到的条目)。
    合并不合规矩、译文是空的，这几条都当没翻到。"""
    pos = {c["id"]: i for i, c in enumerate(chunk)}
    seen, out = set(), []
    for x in (result or {}).get("items") or []:
        if not isinstance(x, dict):
            continue
        ids = x.get("ids") if isinstance(x.get("ids"), list) else ([x["id"]] if x.get("id") else [])
        ids = [str(i) for i in ids]
        if not ids or len(set(ids)) != len(ids) or any(i not in pos or i in seen for i in ids):
            continue
        ix = sorted(pos[i] for i in ids)
        group = [chunk[i] for i in ix]
        tr = (x.get("tr") or "").strip().replace(" / ", "\n")
        if not tr or ix != list(range(ix[0], ix[-1] + 1)) or (len(group) > 1 and not can_merge(group)):
            continue
        seen.update(ids)
        out.append(merge_cues(group, tr, tgt_code))
    return out, [c for c in chunk if c["id"] not in seen]


def chunk_images(chunk, frames):
    """这几条字幕的画面：每条取第一张有截图的来源。"""
    out = []
    for c in chunk:
        p = next((frames[s] for s in c.get("sources") or [] if frames.get(s)), None)
        if p:
            out.append({"path": str(p), "label": f"图 {len(out) + 1} · {c['id']}", "detail": "low"})
    return out


class Asker:
    """带画面问；接口不收图片（模型不支持看图）就这一集剩下的都不带图。"""

    def __init__(self, llm, frames):
        self.llm, self.frames, self.use = llm, frames or {}, bool(frames)

    def __call__(self, system, prompt, chunk, limit=None):
        from .client import FatalLLMError

        imgs = chunk_images(chunk, self.frames)[:limit] if self.use else []
        if imgs:
            try:
                return self.llm.chat_json(system, prompt + prompts.FRAMES_NOTE, imgs)
            except FatalLLMError as e:
                if e.status in (401, 403):
                    raise
                self.use = False
                log(f"  翻译接口不接受图片（{str(e)[:120]}），这一集改为不带画面截图翻译")
        return self.llm.chat_json(system, prompt)


def learn_terms(glossary, res):
    """这一段新定下的译名（术语表里没有的人名等）记下来，后面各段沿用，前后统一。"""
    learned = [dict(t, type=t.get("type") or "人名", note=t.get("note") or "翻译时补充")
               for t in (res or {}).get("new_terms") or [] if isinstance(t, dict) and t.get("src") and t.get("tr")]
    if learned:
        glossary["terms"] = merge_terms(glossary["terms"], learned)


def translate_chunks(llm, cues, glossary, cfg, src, tgt, tgt_code, frames=None):
    """普通翻译：整理好的字幕按时间分段，一段一段翻（带前文译文、译文记忆、画面），可以把相邻几条合成一条翻。
    这一集新定下的译名记进 glossary["terms"]。返回翻好的字幕（没翻到的留着空译文，之后补翻）。"""
    system = prompts.translate_system(src, tgt, cfg.project.context, cfg.render.lang_options.get(tgt_code, {}))
    ask = Asker(llm, frames)
    chunks = chunk_items(cues, cfg.llm.chunk_seconds, cfg.llm.chunk_max_items)
    done = []
    for i, chunk in enumerate(chunks, 1):
        progress(i - 1, len(chunks), f"翻译 {i}/{len(chunks)} 段", every=0)
        log(f"翻译第 {i}/{len(chunks)} 段（{fmt_time(chunk[0]['start'])} 起，{len(chunk)} 条）")
        gl_text = prompts.glossary_text(glossary["terms"])
        memory = recall([{"id": c["id"], "text": c["orig"]["text"]} for c in chunk], done, tgt_code)
        prompt = prompts.translate_prompt(chunk, gl_text, glossary.get("summary"), done[-6:], tgt_code, memory)
        res = ask(system, prompt, chunk)
        out, missing = apply_translation(chunk, res, tgt_code)
        if missing:
            log(f"  模型漏了 {len(missing)} 条，补问一次")
            res2 = ask(system, prompt + f"\n\n上次输出漏掉了这些 id：{', '.join(c['id'] for c in missing)}。请重新完整输出。", chunk)
            out2, missing2 = apply_translation(chunk, res2, tgt_code)
            if len(missing2) < len(missing):
                res, out, missing = res2, out2, missing2
        learn_terms(glossary, res)
        done += sorted(out + [dict(c, flags=list(c.get("flags", []))) for c in missing], key=lambda c: (c["start"], c["end"]))
    return done


def translate_api(cues, glossary, items, cfg, cache_dir, src_code, known_terms, frames=None):
    """翻译（只翻一次，思考程度用在这一次）：整理好的字幕 → 普通翻译或分组按顺序翻（都可以把相邻几条合成一条翻）
    → 补翻漏掉的 → 按来源重新分段、拆太长的 → 统一术语、统一译法 → 其余目标语言。
    items：融合后的来源条目（分段时看来源的时间）；frames：{来源条目 id: 画面截图路径}。返回 (cues, glossary)。"""
    from .client import LLM
    from .consistency import unify

    llm = LLM(cfg, cache_dir=cache_dir)
    deep = LLM(cfg, cache_dir=cache_dir, effort=cfg.llm.reasoning_effort)
    src = lang.get(src_code)
    targets = [lang.canonical(t) for t in cfg.project.targets]
    tgt_code = targets[0]
    tgt = lang.get(tgt_code)
    n_before = len(cues)
    if cfg.llm.grouped:
        from . import fine

        cues = fine.run(deep, llm, cues, cfg, src, tgt, tgt_code, glossary, known_terms, frames)
    else:
        if deep.effort:
            log(f"翻译：思考程度「{deep.effort}」")
        cues = translate_chunks(deep, cues, glossary, cfg, src, tgt, tgt_code, frames)
    if len(cues) < n_before:
        log(f"翻译时合并了 {n_before - len(cues)} 处（一句话被拆在几屏里、按原来的分法拆开译不通顺）")
    cues = finalize(cues)
    glossary["new"] = len(glossary["terms"]) - len(known_terms or [])
    terms = glossary["terms"]
    gl_text = prompts.glossary_text(terms)
    translate_missing(llm, cues, src, tgt_code, gl_text, cfg)
    by_id = {it["id"]: it for it in items}
    resegment(llm, cues, by_id, src, tgt_code, cfg)
    split_long(llm, cues, by_id, src, tgt_code, cfg)
    restore_labels(cues, terms, tgt_code)
    enforce_terms(llm, cues, terms, src, tgt_code, cfg)
    unify(llm, cues, src, tgt, tgt_code)

    for extra in targets[1:]:
        translate_extra(llm, cues, src, extra, tgt_code, gl_text, cfg)
    return cues, glossary


_KATA = "ァ-ヺー"
_HIRA = "ぁ-ゖー"  # 平假名名字（“みーちゃん”）


def _mentions(text, name):
    """原文里有没有提到这个名字。片假名名字要前后都不接着片假名（“イナ”不算“イナゴ”）；
    平假名名字前面不能接着长音 / 片假名（“みー”不算“まーみー”里的那半截）；前面接助词（“これはみーちゃん”）、后面接“ちゃん”“の”照样算。"""
    if re.fullmatch(f"[{_KATA}]+", name):
        return re.search(f"(?<![{_KATA}]){re.escape(name)}(?![{_KATA}])", text) is not None
    if re.fullmatch(f"[{_HIRA}]+", name):
        return re.search(f"(?<![{_KATA}]){re.escape(name)}", text) is not None
    return name in text


def term_misses(cues, terms, tgt_code):
    """译文里没按术语表翻的名字 / 术语：[(条目序号, [(原文, 译名)])]。术语表里的每一条都查（人名、店名、食物、器物都一样，
    术语表就是用来保证全集统一的）。“ミーちゃん”去掉敬称是“ミー”：长音也算字，两个字的名字不能当成太短跳过。"""
    want = []
    for t in terms:
        s = re.sub(r"(さん|様|君|ちゃん|くん)$", "", (t.get("src") or "").strip())
        tr = re.split(r"[（(]", (t.get("tr") or "").strip())[0].strip()  # “常用名（当地叫法）”只要求前半
        if len(re.sub(r"\s", "", s)) >= 2 and norm(s) and tr and (s, tr) not in want:
            want.append((s, tr))
    out = []
    for i, c in enumerate(cues):
        tr = c.get("tr", {}).get(tgt_code, {}).get("text", "")
        if not tr:
            continue
        miss = [(s, t) for s, t in want if _mentions(c["orig"]["text"], s) and t not in tr]
        if miss:
            out.append((i, miss))
    return out


_LABEL = re.compile(r"^[（(]([^）)\n]{1,12})[）)]")


def restore_labels(cues, terms, tgt_code):
    """原文开头有说话人标签（「（中村）これがね…」），译文却把它丢了的：按术语表的译名补回去（「（中村）这个啊……」）。
    只补术语表里有的名字（「（妻は）」这种不是名字的不补，交给模型）。"""
    names = {}
    for t in terms:
        src, tr = (t.get("src") or "").strip(), re.split(r"[（(]", (t.get("tr") or "").strip())[0].strip()
        if src and tr:
            names.setdefault(src, tr)
            names.setdefault(re.sub(r"(さん|様|君|ちゃん|くん)$", "", src), tr)
    n = 0
    for c in cues:
        m = _LABEL.match(c["orig"]["text"])
        tr = c.get("tr", {}).get(tgt_code, {}).get("text", "")
        if not m or not tr or _LABEL.match(tr) or m.group(1) not in names:
            continue
        c["tr"][tgt_code] = {**c["tr"][tgt_code], "text": f"（{names[m.group(1)]}）{tr}"}
        n += 1
    if n:
        log(f"补回说话人标签：{n} 条译文把原文开头的（说话人）丢了")


def enforce_terms(llm, cues, terms, src, tgt_code, cfg, batch=30):
    """最后统一一遍术语表：译文里没用术语表译名的（前面几段还没定下译名时随手音译的、把两个人名弄混的、
    猫的名字一会儿“牛奶”一会儿“咪酱”的、食物换了个叫法的），只改这些名字 / 叫法。"""
    from ..core import segments

    todo = term_misses(cues, terms, tgt_code)
    if not todo:
        return
    log(f"统一译名：{len(todo)} 条译文里的名字 / 叫法和术语表不一致，改成术语表的译法")
    tgt = lang.get(tgt_code)
    system = (f"你是{src.name}→{tgt.name}字幕校对。下面每条字幕的译文里，有名字 / 叫法没有按术语表的指定译法翻译。"
              "请只把这些名字 / 叫法改成指定的译法（指代的人 / 地方 / 东西要和原文一致），语句其他部分尽量不动；"
              '译文里的“｜”分段保持原样（段数不变）。输出 JSON：{"items": [{"id": "q12", "tr": "改好的译文"}]}')
    for k in range(0, len(todo), batch):
        group = todo[k:k + batch]
        lines = []
        for i, miss in group:
            c = cues[i]
            names = "、".join(f"{s} → {t}" for s, t in miss)
            lines.append(f"[{c['id']}] 原文：{c['orig']['text'].replace(chr(10), ' / ')}\n"
                         f"　译文：{c['tr'][tgt_code]['text']}\n　指定译名：{names}")
        try:
            res = llm.chat_json(system, "\n\n".join(lines))
        except Cancelled:  # 停止按钮：不能当成一次普通失败吞掉
            raise
        except Exception as e:
            log(f"  统一译名失败，保持原样：{e}")
            continue
        got = {str(x.get("id")): (x.get("tr") or "").strip() for x in res.get("items", []) or []}
        for i, _ in group:
            c, new = cues[i], got.get(cues[i]["id"])
            old = c["tr"][tgt_code]["text"]
            if new and len(segments.pieces(new)) == len(segments.pieces(old)):
                c["tr"][tgt_code] = {"text": new}


def resegment(llm, cues, items, src, tgt_code, cfg, batch=15):
    """原文和译文的“｜”分段段数对不上的（几条字幕合成一句时常见），或者“｜”落在一条字幕中间的（大模型把换行当成了分屏，
    译文常跟着错开一段）：请大模型按来源字幕的换屏和停顿重新分段，原文和译文分成同样多段。
    原文只许动“｜”的位置；对不上的保持原样（排版时按译文的分段换屏；落在一条字幕中间的整理时合回去）。"""
    from ..core import segments

    def inside(c, orig=None):
        """“｜”落在同一条字幕 / 画面字中间：大模型把里面的换行当成了分屏（段数对得上也算，译文常错开一段）。"""
        if c.get("type") not in ("dialogue", "narration"):
            return False
        return bool(segments.card_bars(c if orig is None else dict(c, orig=dict(c["orig"], text=orig)), items))

    todo, wrong = [], set()
    for i, c in enumerate(cues):
        o, t = c["orig"]["text"], c.get("tr", {}).get(tgt_code, {}).get("text", "")
        if not t or "｜" not in o + t:
            continue
        if inside(c):
            wrong.add(i)
        if i in wrong or (not segments.bars_ok(c) and not segments.reconcile(o, {tgt_code: t})):
            todo.append(i)
    if not todo:
        return
    log(f"重新分段：{len(todo)} 条字幕的原文和译文分段对不上，或者分屏落在一条字幕中间")
    tgt = lang.get(tgt_code)
    system = (f"你是{src.name}→{tgt.name}字幕校对。下面每条字幕由几条原来的字幕合成，要用“｜”分成几屏依次显示，"
              "但原文和译文的分段对不上，或者“｜”放在了一条来源字幕的中间。请重新分段：\n"
              "- 每条来源字幕原来就是一屏：“｜”只放在两条来源的交界处；来源里的“ / ”是同一屏里的换行，不是分屏，那里不能放“｜”；\n"
              "- 分段对应来源里原来的换屏和说话的停顿（看来源的时间），每屏一眼读得完、不超过 2 行，不要断在半句话中间；\n"
              "- 原文和译文分成同样多段、一一对应（每段译文就是那段原文的意思，语序可以在段之间微调）；\n"
              "- 原文只能调整“｜”的位置，文字一个字都不能改；译文尽量保持原来的措辞。\n"
              '输出 JSON：{"items": [{"id": "q12", "orig": "分好段的原文", "tr": "分好同样多段的译文"}]}')
    for k in range(0, len(todo), batch):
        group = todo[k:k + batch]
        blocks = []
        for i in group:
            c = cues[i]
            srcs = sorted((items[s] for s in c.get("sources", []) if s in items), key=lambda s: s["start"])
            src_lines = "\n".join(f"　[{prompts.SRC_LABEL.get(s['src'], s['src'])} {fmt_time(s['start'])}-{fmt_time(s['end'])}] "
                                  f"{prompts.lines_text(s)}" for s in srcs)
            blocks.append(f"[{c['id']}]\n来源：\n{src_lines}\n原文：{c['orig']['text']}\n译文：{c['tr'][tgt_code]['text']}")
        try:
            res = llm.chat_json(system, "\n\n".join(blocks))
        except Cancelled:  # 停止按钮：不能当成一次普通失败吞掉
            raise
        except Exception as e:
            log(f"  重新分段失败，保持原样：{e}")
            continue
        got = {str(x.get("id")): x for x in res.get("items", []) or [] if isinstance(x, dict)}
        for i in group:
            c, g = cues[i], got.get(cues[i]["id"]) or {}
            o, t = (g.get("orig") or "").strip(), (g.get("tr") or "").strip()
            same_text = norm(o.replace("｜", "")) == norm(c["orig"]["text"].replace("｜", ""))
            n = len(segments.pieces(o))
            # 只有一条来源的，合成一屏（不分段）也对；分屏还落在一条字幕中间的不要
            if (o and t and same_text and n == len(segments.pieces(t)) and (n >= 2 or i in wrong)
                    and not inside(c, o)):
                c["orig"]["text"], c["tr"][tgt_code] = o, {"text": t}


def split_long(llm, cues, items, src, tgt_code, cfg, batch=15):
    """一屏（两行）放不下的长句：请大模型在意群边界把原文和译文一起分成几段，每段单独一屏、各有时间（编辑器里能看到、能调）。
    不在排版时按字数硬切，免得一个词被拆开、原文和译文对不上。原文的字不能动，只加“｜”；分不好的保持原样。"""
    from ..core import segments

    cap = 2 * segments.line_chars(tgt_code, cfg)  # 一屏两行能放几个字

    def too_long(c):
        t = c.get("tr", {}).get(tgt_code, {}).get("text", "")
        ps = segments.pieces(t) if segments.bars_ok(c) else [segments.unbar(t, tgt_code)]
        return any(segments.screen_lines(p, tgt_code, cfg) > 2 for p in ps)

    todo = [i for i, c in enumerate(cues) if c.get("type") in ("dialogue", "narration") and too_long(c)]
    if not todo:
        return
    log(f"长句分段：{len(todo)} 条字幕一屏放不下，请大模型按意思分段")
    tgt = lang.get(tgt_code)
    system = (f"你是{src.name}→{tgt.name}字幕校对。下面每条字幕太长，一屏（两行）放不下，要用“｜”分成几段依次显示：\n"
              f"- 每段译文不超过 {cap} 个字，一眼读得完；段数尽量少；\n"
              "- 只在意群 / 句子的边界分开，不要把一个词、一个短语拆开；分段尽量对应说话的停顿（看来源的时间）；\n"
              "- 原文和译文分成同样多段、一一对应（每段译文就是那段原文的意思，语序可以在段之间微调）；\n"
              "- 原文只能加“｜”，文字一个字都不能改；已有的“｜”可以保留或移动；译文尽量保持原来的措辞。\n"
              '输出 JSON：{"items": [{"id": "q12", "orig": "分好段的原文", "tr": "分好同样多段的译文"}]}')
    done = 0
    for k in range(0, len(todo), batch):
        group = todo[k:k + batch]
        blocks = []
        for i in group:
            c = cues[i]
            srcs = sorted((items[s] for s in c.get("sources", []) if s in items), key=lambda s: s["start"])
            src_lines = "\n".join(f"　[{prompts.SRC_LABEL.get(s['src'], s['src'])} {fmt_time(s['start'])}-{fmt_time(s['end'])}] "
                                  f"{prompts.lines_text(s)}" for s in srcs)
            blocks.append(f"[{c['id']}] {fmt_time(c['start'])}-{fmt_time(c['end'])}\n来源：\n{src_lines}\n"
                          f"原文：{c['orig']['text']}\n译文：{c['tr'][tgt_code]['text']}")
        try:
            res = llm.chat_json(system, "\n\n".join(blocks))
        except Cancelled:
            raise
        except Exception as e:
            log(f"  长句分段失败，保持原样（输出时按字数分屏）：{e}")
            continue
        got = {str(x.get("id")): x for x in res.get("items", []) or [] if isinstance(x, dict)}
        for i in group:
            c, g = cues[i], got.get(cues[i]["id"]) or {}
            o, t = (g.get("orig") or "").strip(), (g.get("tr") or "").strip()
            same_text = norm(o.replace("｜", "")) == norm(c["orig"]["text"].replace("｜", ""))
            if o and t and same_text and len(segments.pieces(o)) == len(segments.pieces(t)) >= 2:
                c["orig"]["text"], c["tr"][tgt_code] = o, {"text": t}
                done += 1
    log(f"长句分段：{done} 条分好了段")


def translate_missing(llm, cues, src, tgt_code, gl_text, cfg, batch=30):
    """还没有译文的条目（拆出来的画面字、模型漏掉的台词）补翻一次，带前后文。"""
    tgt = lang.get(tgt_code)
    todo = [i for i, c in enumerate(cues) if c["orig"]["text"].strip() and not c.get("tr", {}).get(tgt_code, {}).get("text")]
    if not todo:
        return
    log(f"补翻 {len(todo)} 条没有译文的字幕 / 画面字")
    system = prompts.missing_system(src, tgt, cfg.project.context, cfg.render.lang_options.get(tgt_code, {}))
    for k in range(0, len(todo), batch):
        group = todo[k:k + batch]
        try:
            res = llm.chat_json(system, prompts.missing_prompt(cues, group, gl_text, tgt_code))
        except Cancelled:  # 停止按钮：不能当成一次普通失败吞掉
            raise
        except Exception as e:
            log(f"  补翻失败，保留原文：{e}")
            continue
        got = {str(x.get("id")): (x.get("tr") or "").strip() for x in res.get("items", []) or []}
        for i in group:
            c = cues[i]
            if got.get(c["id"]):
                c.setdefault("tr", {})[tgt_code] = {"text": got[c["id"]]}
                if "llm_fallback" in c["flags"]:
                    c["flags"].remove("llm_fallback")
            elif "llm_fallback" not in c["flags"]:
                c["flags"].append("llm_fallback")


def translate_extra(llm, cues, src, tgt_code, ref_code, gl_text, cfg, batch=40):
    """在第一个目标语言的基础上，把整理好的原文再翻成另一种语言。"""
    tgt = lang.get(tgt_code)
    system = prompts.extra_target_system(src, tgt, lang.get(ref_code), cfg.project.context,
                                         cfg.render.lang_options.get(tgt_code, {}))
    todo = [c for c in cues if c["orig"]["text"]]
    for i in range(0, len(todo), batch):
        group = todo[i:i + batch]
        progress(i, len(todo), f"翻译成{tgt.name}", every=0)
        log(f"翻译成{tgt.name}：{i + 1}~{i + len(group)} / {len(todo)}")
        res = llm.chat_json(system, prompts.extra_target_prompt(group, gl_text, src.code, ref_code))
        got = {str(x.get("id")): (x.get("tr") or "").strip() for x in res.get("items", []) or []}
        for c in group:
            if got.get(c["id"]):
                c["tr"][tgt_code] = {"text": got[c["id"]]}
            elif "llm_fallback" not in c["flags"]:
                c["flags"].append("llm_fallback")


def add_quality_flags(cues):
    """语速检查：译文每秒字数超过语言包上限。"""
    for c in cues:
        dur = max(0.1, c["end"] - c["start"])
        for code, t in c.get("tr", {}).items():
            pack = lang.get(code)
            if pack.length(t["text"].replace("｜", "")) / dur > pack.cps and "cps_high" not in c["flags"]:
                c["flags"].append("cps_high")
        c["review"] = "todo" if c["flags"] else "ok"
    return cues


def retranslate(cfg, cache_dir, cue, context, src_code, tgt_code, terms, instruction="", sources=(), orig_edited=False):
    """整句重译：给出 3 个候选 [{"orig": 分好段的原文, "tr": 分好段的译文}]。
    分段可以保持，也可以按来源字幕的时间、停顿和每屏长度重新分（原文文字不变，只动“｜”的位置）。
    context 是前后几条字幕（含当前条）；sources 是这条字幕的来源条目（带时间）。
    orig_edited：用户手动改过原文——按改后的原文翻译，来源只用来看时间和语境，原文里删掉的不能从来源里加回来。"""
    from ..core import segments
    from .client import LLM

    llm = LLM(cfg, cache_dir=cache_dir)
    src, tgt = lang.get(src_code), lang.get(tgt_code)
    cur_orig = cue["orig"]["text"]
    cur_tr = cue.get("tr", {}).get(tgt_code, {}).get("text", "")
    system = (f"你是资深{src.name}→{tgt.name}影视字幕译者兼校对。节目信息：{cfg.project.context or '（未提供）'}\n"
              f"给你一条字幕的原文、当前分段和译文、它的来源（原来的字幕 / 画面字 / 语音，带时间）和前后文，"
              f"请给出 3 个不同的{tgt.name}译文候选，按推荐程度排序。\n"
              "分段：一条字幕可以用“｜”分成几屏依次显示。当前分段合理就保持；不合理就重新分。"
              "分段要对应来源里原来的换屏和说话的停顿（看来源的时间），每屏一眼读得完、不超过 2 行；"
              "一屏放得下又没有明显停顿的不要分。原文和译文必须分成同样多段、一一对应，"
              "原文只能调整“｜”的位置，文字一个字都不能改。不同候选的分段可以不同。\n"
              + ("这条字幕的原文是用户手动改过的：严格按“本条当前分段”里的原文翻译。来源是改之前识别到的内容，"
                 "只用来参考时间、停顿和语境；原文里没有的内容（被删掉的说话人标注、语气词、半句话等）不要从来源里加回来。\n"
                 if orig_edited else "")
              + f"{tgt.prompt_target_rules(cfg.render.lang_options.get(tgt_code, {}))}\n"
              '输出 JSON：{"alts": [{"orig": "用｜分好段的原文", "tr": "用｜分好同样多段的译文"}, ...]}')
    parts = cue.get("parts") or [[cue["start"], cue["end"]]]
    o_ps = segments.pieces(cur_orig) if len(parts) > 1 else [cur_orig]
    t_ps = segments.pieces(cur_tr) if len(parts) > 1 else [cur_tr]
    seg_lines = [f"第{i + 1}段 {fmt_time(a)}-{fmt_time(b)}：{o_ps[i] if i < len(o_ps) else ''}　〔{t_ps[i] if i < len(t_ps) else ''}〕"
                 for i, (a, b) in enumerate(parts)]
    src_lines = [f"[{prompts.SRC_LABEL.get(s['src'], s['src'])} {fmt_time(s['start'])}-{fmt_time(s['end'])}] "
                 f"{prompts.lines_text(s)}" for s in sorted(sources, key=lambda s: s["start"])]
    ctx = []
    for c in context:
        mark = "→ 本条" if c["id"] == cue["id"] else "　 "
        ctx.append(f"{mark} {c['orig']['text'].replace(chr(10), ' / ')}　〔当前译文：{c.get('tr', {}).get(tgt_code, {}).get('text', '')}〕")
    user = (f"## 术语表\n{prompts.glossary_text(terms)}\n\n## 本条当前分段\n" + "\n".join(seg_lines)
            + ((("\n\n## 本条改之前的来源（只看时间和语境，内容以原文为准）\n" if orig_edited else "\n\n## 本条的来源\n")
                + "\n".join(src_lines)) if src_lines else "")
            + "\n\n## 前后文\n" + "\n".join(ctx))
    if instruction:
        user += f"\n\n## 额外要求\n{instruction}"
    res = llm.chat_json(system, user)
    out = []
    for a in res.get("alts", []) or []:
        if isinstance(a, str):
            a = {"tr": a}
        if not isinstance(a, dict) or not (a.get("tr") or "").strip():
            continue
        o, t = (a.get("orig") or "").strip(), a["tr"].strip()
        if norm(o.replace("｜", "")) != norm(cur_orig.replace("｜", "")):
            o = cur_orig  # 原文的字被改了：不采用它的原文，保持当前原文
        if len(segments.pieces(o)) != len(segments.pieces(t)):
            if len(segments.pieces(t)) == len(segments.pieces(cur_orig)):
                o = cur_orig
            else:  # 段数怎么也对不上：都不分段
                o, t = segments.unbar(o, src_code), segments.unbar(t, tgt_code)
        out.append({"orig": o, "tr": t})
    return out[:3]
