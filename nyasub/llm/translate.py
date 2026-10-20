"""大模型阶段：术语表 → 分段“合并 + 纠错 + 翻译” → 其余目标语言。

两种跑法：
- api：直接调 OpenAI 兼容接口（见 client.py）。
- export：把任务写成文件，交给 Claude Code 之类的助手完成，再导回（见 jobs.py）。
"""
from difflib import SequenceMatcher

from .. import lang
from ..env import log, progress
from ..textutil import fmt_time, full_sim, norm, sim
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


def make_cue(srcs, orig, tr, tgt, typ, src_lang, fallback=False):
    cue = {"start": min(s["start"] for s in srcs), "end": max(s["end"] for s in srcs),
           "type": typ or "dialogue", "sources": [s["id"] for s in srcs],
           "onscreen": any(s["src"] in ("telop", "info") for s in srcs),
           "orig": {"lang": src_lang, "text": orig}, "tr": {tgt: {"text": tr}} if tr else {}}
    rects = [s["rect"] for s in srcs if s.get("rect")]
    if cue["type"] in ("info", "name", "title") and rects:  # 几个画面字合成一条时（标题分成两行识别），取并集
        cue["rect"] = [min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)]
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


def apply_result(chunk, result, tgt, src_lang):
    """校验大模型结果，返回 (cues, 漏掉的 id)。时间由 src 计算。"""
    by_id = {it["id"]: it for it in chunk}
    seen, cues = set(), []
    for c in result.get("cues", []) or []:
        src = [s for s in (c.get("src") or []) if s in by_id and s not in seen]
        if not src:
            continue
        seen.update(src)
        srcs = [by_id[s] for s in src]
        orig = (c.get("orig") or c.get("ja") or "").strip() or "\n".join(s["text"] for s in srcs)
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
    o_parts, t_parts = orig.split("｜"), tr.split("｜")
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
        split.append((s, "｜".join(p.strip() for p in o_parts[i:j]), "｜".join(p.strip() for p in t_parts[i:j])))
        del o_parts[i:j], t_parts[i:j]
    if not split:
        return srcs, orig, tr, []
    gone = {id(s) for s, _, _ in split}
    return [s for s in srcs if id(s) not in gone], "｜".join(o_parts), "｜".join(t_parts), split


def _covered(text, by):
    """text 有多少比例的字出现在 by 里（按至少两个字连续相同的片段算）。"""
    a, b = norm(text), norm(by)
    if not a:
        return 1.0
    blocks = SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(x.size for x in blocks if x.size >= 2) / len(a)


def split_orphans(srcs, orig):
    """大模型把“同一时间字幕在说别的话”的画面字并进了台词，但原文里根本没用它的内容（只是时间挨着），
    比如采访时画面上打出的问题概括、标签、注释：拆出来单独显示。返回 (留下的来源, 拆出的画面字)。"""
    if not any(s["src"] in SPEECH for s in srcs):
        return srcs, []
    text = orig.replace("｜", "")
    loose = [s for s in srcs if s["src"] in SCREEN and s.get("over_speech") and _covered(s["text"], text) < 0.8]
    if not loose:
        return srcs, []
    return [s for s in srcs if s not in loose], loose


def fallback(chunk, missing, tgt, src_lang):
    """模型漏掉的条目：字幕和画面台词保留原文（不译），其余丢弃。"""
    return [make_cue([it], it["text"], "", tgt, "dialogue", src_lang, fallback=True)
            for it in chunk if it["id"] in missing and it["src"] in ("subs", "telop")]


def finalize(cues):
    """排序、编号。"""
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


def run_api(items, cfg, cache_dir, src_code, known_terms):
    """返回 (cues, glossary)。"""
    from .client import LLM

    llm = LLM(cfg, cache_dir=cache_dir)
    src = lang.get(src_code)
    targets = [lang.canonical(t) for t in cfg.project.targets]
    tgt_code = targets[0]
    tgt = lang.get(tgt_code)
    opts = cfg.render.lang_options

    log("生成术语表 / 本集概要")
    gl = llm.chat_json(prompts.glossary_system(src, tgt), prompts.glossary_input(items, known_terms))
    terms = merge_terms(known_terms, gl.get("terms"))
    glossary = {"summary": gl.get("summary", ""), "terms": terms, "new": len(terms) - len(known_terms or [])}
    gl_text = prompts.glossary_text(terms)

    system = prompts.chunk_system(src, tgt, cfg.project.context, opts.get(tgt_code, {}))
    chunks = chunk_items(items, cfg.llm.chunk_seconds, cfg.llm.chunk_max_items)
    all_cues = []
    for i, chunk in enumerate(chunks, 1):
        progress(i - 1, len(chunks), f"翻译 {i}/{len(chunks)} 段", every=0)
        log(f"融合翻译第 {i}/{len(chunks)} 段（{fmt_time(chunk[0]['start'])} 起，{len(chunk)} 条）")
        prompt = prompts.chunk_prompt(chunk, gl_text, glossary["summary"], all_cues[-6:], tgt_code)
        cues, missing = apply_result(chunk, llm.chat_json(system, prompt), tgt_code, src.code)
        if missing:
            log(f"  模型漏了 {len(missing)} 条，补问一次")
            retry = llm.chat_json(system, prompt + f"\n\n上次输出漏掉了这些 id：{', '.join(missing)}。请重新完整输出。")
            cues2, missing2 = apply_result(chunk, retry, tgt_code, src.code)
            if len(missing2) < len(missing):
                cues, missing = cues2, missing2
            cues += fallback(chunk, missing, tgt_code, src.code)
        all_cues += sorted(cues, key=lambda x: x["start"])
    cues = finalize(all_cues)
    translate_missing(llm, cues, src, tgt_code, gl_text, cfg)

    for extra in targets[1:]:
        translate_extra(llm, cues, src, extra, tgt_code, gl_text, cfg)
    return cues, glossary


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


def retranslate(cfg, cache_dir, cue, context, src_code, tgt_code, terms, instruction=""):
    """单条重译：给出 3 个候选。context 是前后几条字幕（含当前条）。"""
    from .client import LLM

    llm = LLM(cfg, cache_dir=cache_dir)
    src, tgt = lang.get(src_code), lang.get(tgt_code)
    system = (f"你是资深{src.name}→{tgt.name}影视字幕译者。节目信息：{cfg.project.context or '（未提供）'}\n"
              f"给你一条字幕的原文、当前译文和前后文，请给出 3 个不同的{tgt.name}译文候选，按推荐程度排序。"
              f"{tgt.prompt_target_rules(cfg.render.lang_options.get(tgt_code, {}))}\n"
              '输出 JSON：{"alts": ["候选1", "候选2", "候选3"]}')
    lines = []
    for c in context:
        mark = "→ 本条" if c["id"] == cue["id"] else "　 "
        lines.append(f"{mark} {c['orig']['text'].replace(chr(10), ' / ')} ｜ 当前译文：{c.get('tr', {}).get(tgt_code, {}).get('text', '')}")
    user = f"## 术语表\n{prompts.glossary_text(terms)}\n\n## 前后文\n" + "\n".join(lines)
    if instruction:
        user += f"\n\n## 额外要求\n{instruction}"
    res = llm.chat_json(system, user)
    return [a.strip() for a in res.get("alts", []) if isinstance(a, str) and a.strip()][:3]
