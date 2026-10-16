"""大模型阶段：术语表 → 分段“合并 + 纠错 + 翻译” → 其余目标语言。

两种跑法：
- api：直接调 OpenAI 兼容接口（见 client.py）。
- export：把任务写成文件，交给 Claude Code 之类的助手完成，再导回（见 jobs.py）。
"""
from .. import lang
from ..env import log
from ..textutil import fmt_time
from . import prompts


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
    if cue["type"] in ("info", "name", "title") and rects:
        cue["rect"] = rects[0]
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
        cues.append(make_cue(srcs, orig, tr, tgt, c.get("type"), src_lang))
    for d in result.get("drop", []) or []:
        if d in by_id:
            seen.add(d)
    return cues, [i for i in by_id if i not in seen]


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

    for extra in targets[1:]:
        translate_extra(llm, cues, src, extra, tgt_code, gl_text, cfg)
    return cues, glossary


def translate_extra(llm, cues, src, tgt_code, ref_code, gl_text, cfg, batch=40):
    """在第一个目标语言的基础上，把整理好的原文再翻成另一种语言。"""
    tgt = lang.get(tgt_code)
    system = prompts.extra_target_system(src, tgt, lang.get(ref_code), cfg.project.context,
                                         cfg.render.lang_options.get(tgt_code, {}))
    todo = [c for c in cues if c["orig"]["text"]]
    for i in range(0, len(todo), batch):
        group = todo[i:i + batch]
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
